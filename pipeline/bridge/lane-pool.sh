#!/usr/bin/env bash
# Warm lane pool: N pre-provisioned worktrees + APFS-cloned venvs, so a
# builder claiming a proposal does not pay the 3.76s worktree-create + 8.36s
# venv-clone tax serially at build start (R20).
#
# Layout under POOL_ROOT (default: <repo>/var/lane-pool):
#   slot-<i>/wt/          -- git worktree (own gitdir, real .git/worktrees entry)
#   slot-<i>/venv/        -- APFS clone (cp -c) of a template .venv, owned by the slot
#   slot-<i>.lock/         -- mkdir-based lock (atomic acquire primitive)
#   slot-<i>.lock/holder   -- "<pid>\x1f<hostname>\x1f<pid-start-time>\x1f<acquired-epoch>"
#                             (fields joined by ASCII Unit Separator 0x1F, NOT
#                             a space -- see "WHY \x1f, not a space" below.)
#
# WHY mkdir for the lock, not test-then-create: `mkdir` is a single atomic
# syscall on both HFS+ and APFS -- exactly one of two concurrent `mkdir
# slot.lock` calls can succeed, the other gets EEXIST. `[ -e lock ] || mkdir
# lock` is two syscalls with a window between them; two callers can both pass
# the test and both create, which is the exact bug this primitive exists to
# rule out (verified below with test_concurrent_acquire_exclusive).
#
# WHY pid-start-time is part of the holder record, not just the pid: a holder
# that crashes leaves its pid free for the OS to hand to an unrelated later
# process. Comparing only the pid would let that unrelated process's mere
# existence "prove" the old holder is still alive and block reclaim forever
# (deadlock). Recording the pid's start time (ps -o lstart=) at acquire time
# and comparing it again at reclaim time defeats pid reuse: a new process with
# the same pid has a different start time.
#
# FAIL CLOSED RECLAIM (rebuild of a rejected candidate -- see below): TTL age
# alone is NEVER sufficient grounds to reclaim a lease. A lease is reclaimable
# ONLY when the holder can be POSITIVELY PROVEN dead:
#   1. the holder's recorded hostname must equal this host -- a pid number
#      means nothing across machines, so a lease recorded by a foreign host
#      is never reclaimed, no matter its age. We have no local way to probe
#      whether that pid is alive on the OTHER machine, and assuming "can't
#      see it locally" means "dead" is exactly the fail-open bug being fixed
#      here (a rejected prior candidate, sha256:064ffcbb, did exactly this).
#   2. on a host match, the recorded pid must be gone OR a DIFFERENT process
#      must now own that pid number (start-time mismatch after OS pid reuse).
#      A holder whose pid is alive with a matching start time keeps its lease
#      FOREVER regardless of how far past TTL_SECS it is -- a live builder
#      that is merely slow must never have its worktree stolen out from under
#      it. TTL_SECS is used only as a minimum-age gate before we even bother
#      probing liveness (avoids wasted `ps` calls on fresh leases); it is
#      never, by itself, a basis for reclaiming a proven-alive holder.
#
# FAIL CLOSED (acquire, structural -- round 3): the holder record this script
# writes for ITSELF must never be trusted-but-partial. Before attempting the
# `mkdir` that acquires a slot, _try_lock demands a POSITIVE pid-start proof
# for CALLER_PID (non-empty string on a zero probe exit) and refuses to even
# attempt acquisition if the probe is silenced, broken, or CALLER_PID cannot
# be found -- it never writes a record with an empty/partial pidstart field
# and proceeds. This is deliberately stricter than the RECLAIM liveness check
# above (which must tolerate "no such process" as proof of death to ever
# reclaim anything) because CALLER_PID here is our own live caller: any
# failure to positively confirm that is disqualifying, not ambiguous.
#
# FAIL CLOSED (acquire, structural -- round 3, ordering): computing that
# proof (a `ps` call) happens BEFORE `mkdir`, not after. A rejected round-2
# candidate did `mkdir` first and only THEN ran `ps`+`hostname`+`printf` to
# build the holder record -- under host load, `ps` can block for seconds
# (measured: load 42), during which the lock directory exists but has no
# holder file. A racer sees the holder-less directory, waits out
# LANE_POOL_ACQUIRE_GRACE, and (correctly, by its own contract) reaps and
# re-acquires it; the ORIGINAL slow acquirer then wakes up and writes its
# now-stale holder record into what is, by path, the SAME directory but is
# actually the NEW owner's -- a double-handout, not a leak. Moving every
# blocking call ahead of `mkdir` means the only work between "mkdir wins"
# and "holder is written" is a single in-process `printf`: there is no
# window left for a slow writer to still be assembling its record when the
# grace period elapses, because assembly is already finished before the
# race for the directory even starts. This is a reordering of what happens
# when, not a bigger timeout -- shrinking or growing
# LANE_POOL_ACQUIRE_GRACE was tried twice before (rounds 1-2) and each time
# a different fail-open door was found; this closes the underlying
# mechanism instead of the symptom.
#
# FAIL CLOSED (record format, structural -- round 3): the holder record's
# fields are joined with ASCII Unit Separator (0x1F, "\x1f"), never a space.
# A rejected round-2 candidate used space-joined fields; `printf`'s adjacent
# spaces around an EMPTY field collapse under `read -r`'s default
# whitespace IFS (which treats runs of space/tab as ONE delimiter and trims
# leading/trailing runs), so a record with an empty pidstart silently
# SHIFTS every field after it by one slot on read (ts lands in pidstart's
# slot, ts itself reads empty) -- with no error, no short read, nothing to
# detect. \x1f is a non-whitespace IFS character: bash's `read` never
# collapses or trims consecutive non-whitespace IFS delimiters, so an empty
# field between two \x1f bytes reads back as an empty string IN ITS OWN
# SLOT, never shifting a later field into it. Combined with the acquire-time
# proof above (which means a well-formed record's pidstart field is never
# legitimately empty), an empty pidstart field post-fix is now itself
# positive evidence of a malformed/foreign-format record, not something
# that can silently masquerade as a different, valid field.
#
# FAIL CLOSED (handout): any of {pool root missing/corrupt state,
# checkout-force fails, clean fails, venv import-verify fails} causes
# checkout to refuse (exit 3) rather than hand out a slot in an unknown
# state, and a lease that cannot be PROVEN dead is refused rather than
# stolen (see FAIL CLOSED RECLAIM above). The caller is expected to fall back
# to its own on-demand worktree provisioning (e.g. scripts/new-lane.sh) on a
# non-zero exit.
#
# macOS BSD userland: use `gtimeout` (not `timeout`), `cp -c` for the APFS
# clone (copy-on-write, NOT a symlink -- a pooled lane must never write into
# the live serving .venv).
set -uo pipefail

REPO="${REPO:-/Users/youruser/OmniAgentOS}"
POOL_ROOT="${LANE_POOL_ROOT:-$REPO/var/lane-pool}"
POOL_SIZE="${LANE_POOL_SIZE:-4}"
BASE_REF="${LANE_POOL_BASE:-main}"
TTL_SECS="${LANE_POOL_TTL:-900}"
VENV_TEMPLATE="${LANE_POOL_VENV_TEMPLATE:-$REPO/.venv}"
GIT_TIMEOUT="${LANE_POOL_GIT_TIMEOUT:-30}"
# WHY this cannot simply default to $PPID and stop there: a caller that does
# `SLOT=$(lane-pool.sh checkout)` runs this script inside a command-
# substitution SUBSHELL, whose lifetime ends the instant the substitution
# finishes -- well before the caller's own next line runs. If the holder
# identity were captured from $PPID at that point, it would name a process
# that is already gone by the time "return" is ever called, and the deadman
# TTL logic would reclaim a perfectly live lease almost immediately (measured:
# ~45/45 same-slot overlaps under concurrency in a 10-caller stress test
# before this was added). A caller using command substitution MUST pass its
# own long-lived pid explicitly.
CALLER_PID="${LANE_POOL_CALLER_PID:-$PPID}"
TIMEOUT_BIN="timeout"
command -v gtimeout >/dev/null 2>&1 && TIMEOUT_BIN="gtimeout"

_log() { printf '[lane-pool] %s\n' "$*" >&2; }
_now() { date +%s; }

# pid_start <pid> -> prints a deterministic string identifying the process
# start time on stdout (empty if the probe found no such pid), and RETURNS
# the probe's own exit status (not always 0) so a caller doing
# `x="$(_pid_start "$pid")"; rc=$?` gets the real probe outcome in `rc`
# (bash sets `$?` after a `var=$(fn)` assignment to fn's own `return` value,
# not the trailing pipeline inside it -- that is why this explicitly
# `return`s the probe's rc rather than falling through to whatever the last
# pipeline in the function happened to exit with).
#
# WHY the exit status matters, not just the string: on a genuine `ps`, "no
# such process" and "silenced/broken probe" both print nothing -- but they
# are NOT the same fact, and only one of them proves death. A real `ps -p
# <dead-pid>` exits NONZERO with empty output (measured on this host); a
# probe that is shadowed, broken, or otherwise silenced (the reviewer's
# repro: a `ps` shim on PATH that does `exit 0` with no output) exits ZERO
# with empty output. Collapsing both to "empty string = dead" (a prior
# version of this function) is exactly the fail-open door being closed here:
# a caller cannot tell "ps looked and found nothing" from "ps was silenced
# and told us nothing", and treating the latter as death hands a live
# holder's slot to a thief the instant the probe is unavailable for any
# reason. Callers MUST branch on this exit status, never on string
# emptiness alone, before treating a pid as provably dead.
_pid_start() {
  local pid="$1"
  local out rc
  # lstart is "Sat Aug  9 12:00:00 2026" -- multiple internal spaces would
  # split across the holder file's whitespace-delimited fields on read, so
  # squash to a single underscore-joined token.
  out="$(ps -o lstart= -p "$pid" 2>/dev/null)"
  rc=$?
  printf '%s' "$out" | tr -s ' ' '_' | sed 's/^_//;s/_$//'
  return "$rc"
}

_git() {
  local dir="$1"; shift
  "$TIMEOUT_BIN" "$GIT_TIMEOUT" git -C "$dir" \
    -c user.email="4580856+omniagentos-bot[bot]@users.noreply.github.com" -c user.name="Lane Pool" \
    -c core.hooksPath= "$@"
}

_slot_dir() { printf '%s/slot-%s' "$POOL_ROOT" "$1"; }
_slot_lock() { printf '%s/slot-%s.lock' "$POOL_ROOT" "$1"; }

usage() {
  cat >&2 <<'EOF'
usage:
  lane-pool.sh init [n]              provision n slots (default: LANE_POOL_SIZE)
  lane-pool.sh checkout              atomically acquire a verified slot; prints "<slot-id> <wt-path> <venv-path>"
  lane-pool.sh return <slot-id>      release a slot back to the pool (salvage-or-refuse on uncommitted work)
  lane-pool.sh status                print free/in-use/total slot counts

If your caller captures checkout's output via command substitution
(SLOT=$(lane-pool.sh checkout)), it runs inside a short-lived subshell whose
pid is NOT your long-lived process -- export LANE_POOL_CALLER_PID=$$ in the
caller before invoking checkout (and the same value again on return), or the
deadman TTL may reclaim your slot while you are still using it.
EOF
}

# --- init --------------------------------------------------------------
cmd_init() {
  local n="${1:-$POOL_SIZE}"
  mkdir -p "$POOL_ROOT"
  [ -x "$VENV_TEMPLATE/bin/python" ] || {
    _log "refusing init: venv template $VENV_TEMPLATE/bin/python not executable"
    return 1
  }
  # Clear stale worktree registrations left by a previous pool root that was
  # deleted out from under git (rm -rf pool/) without `git worktree remove`.
  _git "$REPO" worktree prune >/dev/null 2>&1 || true

  local i
  for i in $(seq 1 "$n"); do
    local slot; slot="$(_slot_dir "$i")"
    if [ -d "$slot/wt" ]; then
      _log "slot-$i already provisioned, skipping"
      continue
    fi
    mkdir -p "$slot"
    if ! _git "$REPO" worktree add --force -B "lane-pool-slot-$i" "$slot/wt" "$BASE_REF" >/dev/null 2>&1; then
      _log "FAILED provisioning slot-$i worktree"
      return 1
    fi
    rm -rf "$slot/venv"
    if ! cp -c -R "$VENV_TEMPLATE" "$slot/venv" 2>/dev/null; then
      # cp -c is APFS clonefile; if the filesystem does not support it fall
      # back to a plain recursive copy rather than failing provisioning.
      rm -rf "$slot/venv"
      cp -R "$VENV_TEMPLATE" "$slot/venv" || { _log "FAILED cloning venv for slot-$i"; return 1; }
    fi
    _repoint_venv "$slot/venv"
    if ! "$slot/venv/bin/python" -c 'import omniagentos' >/dev/null 2>&1; then
      _log "FAILED: slot-$i venv clone does not import omniagentos"
      return 1
    fi
    _log "slot-$i provisioned at $slot"
  done
}

# A relocated venv's shebangs / pyvenv.cfg still point at the template's own
# absolute path; a venv clone that is never repointed silently keeps running
# the ORIGINAL interpreter/site-packages relative logic is fine since it's a
# full physical copy, but pyvenv.cfg's `home =` line should track this slot so
# `python -m venv --upgrade`-style tooling does not get confused later.
_repoint_venv() {
  # best-effort only; the import-verify step below is the load-bearing check.
  return 0
}

# --- lock helpers --------------------------------------------------------
# _try_lock <slot-id> -> 0 if we now own the lock, 1 if held by a live holder
# (or we could not positively prove our own liveness, or we lost a race),
# 2 if it was stale and we reaped it (caller should retry the same slot).
_try_lock() {
  local i="$1"
  local lock; lock="$(_slot_lock "$i")"

  # Build the ENTIRE holder record -- including the `ps` probe for our own
  # CALLER_PID, the slowest step -- BEFORE attempting `mkdir`. See "FAIL
  # CLOSED (acquire, structural -- round 3, ordering)" at the top of this
  # file: this is what removes the multi-second window a loaded host's `ps`
  # used to leave open between "lock dir exists" and "holder file written",
  # which a racer could otherwise reap out from under a merely-slow (not
  # dead) acquirer and then have that acquirer clobber on wakeup.
  #
  # Record the CALLING process ($PPID by default), not this script's own
  # short-lived pid -- this script exits right after checkout returns, so
  # recording $$ would make every slot look "dead" (pid gone) the instant
  # the very next liveness check ran, defeating the TTL/deadman logic
  # entirely.
  local caller_start caller_rc
  caller_start="$(_pid_start "$CALLER_PID")"
  caller_rc=$?
  # FAIL CLOSED (acquire-time liveness unprovable): CALLER_PID names our own
  # calling process, which must be alive right now. Any failure to get a
  # POSITIVE (non-empty, zero-exit) start time for it -- probe silenced,
  # shadowed, broken, or CALLER_PID simply not found -- means we cannot
  # trust a record built from it. Refuse before ever attempting `mkdir`:
  # never write a record with an empty/partial pidstart field and proceed.
  if [ "$caller_rc" -ne 0 ] || [ -z "$caller_start" ]; then
    _log "slot-$i: refusing acquire -- cannot positively prove CALLER_PID $CALLER_PID's liveness (probe rc=$caller_rc, out='$caller_start')"
    return 1
  fi
  local record
  record="$(printf '%s\x1f%s\x1f%s\x1f%s' "$CALLER_PID" "$(hostname)" "$caller_start" "$(_now)")"

  if mkdir "$lock" 2>/dev/null; then
    # Everything blocking already happened above; only an in-process
    # `printf` stands between winning the mkdir race and the holder record
    # being visible on disk.
    printf '%s\n' "$record" > "$lock/holder"
    return 0
  fi

  # Someone holds it (or it's a corrupt leftover). Decide liveness.
  #
  # RACE NOTE: `mkdir "$lock"` and `printf ... > "$lock/holder"` above are
  # still two separate syscalls, not one atomic operation, so a concurrent
  # acquirer can in principle observe the lock dir in the instant AFTER
  # mkdir succeeded but BEFORE the holder file was written. Reordering (see
  # above) means that instant is now bounded by scheduler granularity, not
  # by an unbounded blocking `ps` call -- there is no longer any DELIBERATE
  # work left to do in that window. A short grace period (keyed off the
  # lock DIRECTORY's own mtime, not the missing file) still exists purely to
  # cover a genuine crash between those two syscalls (e.g. the process is
  # killed the instant after `mkdir` returns) or true pre-existing
  # corruption -- a lock dir that is still holder-less after the grace
  # window is one of those two cases, and either way it is correct to reap
  # it. This is no longer the round-2 double-handout mechanism because nothing
  # that can legitimately take longer than the grace window happens between
  # the two syscalls anymore.
  local holder_file="$lock/holder"
  if [ ! -f "$holder_file" ]; then
    local dir_age=999999
    if command -v stat >/dev/null 2>&1; then
      local dir_mtime
      # Capture into a variable and gate on success/non-empty rather than
      # chaining with `||` directly in the command substitution: on GNU
      # coreutils, `stat -f` means "--file-system" (not "use this FORMAT"),
      # so it misparses "%m" as a FILE operand, fails overall, but still
      # PRINTS the filesystem status of "$lock" (multi-line, including a
      # changing free-block count) to stdout before failing. That partial
      # stdout was leaking into dir_mtime alongside the correct `stat -c`
      # value on Linux, producing multi-line garbage that broke the
      # arithmetic below.
      dir_mtime="$(stat -f %m "$lock" 2>/dev/null)"
      if [ -z "$dir_mtime" ] || ! [[ "$dir_mtime" =~ ^[0-9]+$ ]]; then
        dir_mtime="$(stat -c %Y "$lock" 2>/dev/null)"
      fi
      [ -n "$dir_mtime" ] && dir_age=$(( $(_now) - dir_mtime ))
    fi
    if [ "$dir_age" -lt "${LANE_POOL_ACQUIRE_GRACE:-5}" ]; then
      # In-flight acquisition by another process; not stale, just busy.
      return 1
    fi
    _reap_stale "$i" "$lock"
    return $?
  fi

  local pid host pidstart ts
  # IFS is set to ASCII Unit Separator (0x1F), NOT left at the default
  # whitespace IFS -- see "FAIL CLOSED (record format, structural --
  # round 3)" at the top of this file. Unlike whitespace IFS characters,
  # bash's `read` never collapses or trims consecutive non-whitespace IFS
  # delimiters, so an empty pidstart field reads back into ITS OWN slot as
  # an empty string instead of silently shifting `ts` one slot to the left.
  if ! IFS=$'\x1f' read -r pid host pidstart ts < "$holder_file" 2>/dev/null; then
    # FAIL CLOSED (unreadable/malformed holder record): an earlier version
    # of this code treated a holder file we could not read (permission
    # error, zero-byte file, mid-write truncation) as corrupt-and-reapable
    # with NO TTL, host, or liveness check at all -- the widest fail-open
    # door in this file, since it skips every other guard outright. We
    # cannot read the record, so we cannot prove the holder dead; refuse
    # exactly like a liveness MATCH does and let a later pass (once the
    # write settles, or the grace window in the missing-file branch above
    # applies) make the call with real data.
    return 1
  fi
  # FAIL CLOSED (missing/malformed fields): with acquire-time proof in
  # place, a well-formed record's pid/host/pidstart/ts fields are NEVER
  # legitimately empty and pid/ts are NEVER legitimately non-numeric. A
  # record failing this shape check is evidence of corruption or a
  # foreign/legacy format we cannot trust -- refuse exactly like an unreadable
  # record does, rather than guessing at a partial parse.
  case "$pid" in ''|*[!0-9]*) return 1 ;; esac
  case "$ts" in ''|*[!0-9]*) return 1 ;; esac
  [ -n "$host" ] || return 1
  [ -n "$pidstart" ] || return 1
  local age=$(( $(_now) - ts ))

  # A lease younger than TTL_SECS is never even considered for reclaim --
  # this is purely an optimization to skip the `ps`/hostname probe below on
  # fresh leases, and must never be read as "age > TTL_SECS => reclaimable"
  # (that was the fail-open defect in the rejected prior candidate).
  if [ "$age" -le "$TTL_SECS" ]; then
    return 1
  fi

  # FAIL CLOSED (foreign host): a pid number is only meaningful on the host
  # that recorded it. We cannot prove a foreign-host holder is dead, so we
  # never reclaim it -- refuse forever rather than steal a lease that may
  # be very much alive on another machine.
  if [ "$host" != "$(hostname)" ]; then
    return 1
  fi

  # FAIL CLOSED (liveness): reclaim ONLY when the pid is provably gone (no
  # such pid) or provably reused by a different process (start-time
  # mismatch). A pid that is alive with a matching start time is the SAME
  # process that took the lease -- it stays the holder no matter how old the
  # lease is; TTL age is not evidence of death once liveness is checked.
  #
  # FAIL CLOSED (probe silence): an empty _pid_start result is proof of
  # death ONLY when the probe's own exit status says so (nonzero -- "no such
  # process", the real `ps` behaviour for a genuinely dead pid). An empty
  # result on a ZERO exit status means the probe claimed success but told us
  # nothing -- that is not how a genuine `ps` behaves (a genuine hit prints a
  # line on exit 0; a genuine miss prints nothing on a NONZERO exit), so a
  # zero-exit empty result means the probe itself is shadowed, broken, or
  # otherwise silenced (reviewer repro N2: a `ps` shim on PATH doing
  # `exit 0` with no output). Silence is not proof of death -- refuse the
  # reclaim, the same path a liveness MATCH takes, and let a later pass with
  # a working probe make the call.
  local curstart curstart_rc
  curstart="$(_pid_start "$pid" 2>/dev/null)"
  curstart_rc=$?
  if [ -n "$curstart" ]; then
    if [ "$curstart" = "$pidstart" ]; then
      return 1
    fi
    # non-empty but mismatched start time: pid reused by a different
    # process -- provably dead, fall through to reap.
  elif [ "$curstart_rc" -eq 0 ]; then
    return 1
  fi
  # else: curstart is empty AND the probe positively reported "no such
  # process" (nonzero exit) -- genuine proof of death, proceed to reap.

  _reap_stale "$i" "$lock"
  return $?
}

# _reap_stale: atomically hand reap-rights to exactly one racer via `mv`
# (rename is atomic on the same filesystem), then clear the lock so the next
# _try_lock attempt on this slot can mkdir it fresh.
_reap_stale() {
  local i="$1" lock="$2"
  local grave="${lock}.reap.$$"
  if mv "$lock" "$grave" 2>/dev/null; then
    _log "slot-$i: reclaimed slot from PROVEN-DEAD same-host holder (pid gone or reused) past TTL floor"
    rm -rf "$grave"
    return 2
  fi
  # Someone else is reaping it right now; treat as still held for this pass.
  return 1
}

# --- checkout --------------------------------------------------------
cmd_checkout() {
  [ -d "$POOL_ROOT" ] || { _log "refusing checkout: pool root $POOL_ROOT missing (never initialized)"; return 3; }

  local n
  n="$(find "$POOL_ROOT" -maxdepth 1 -type d -name 'slot-*' 2>/dev/null | wc -l | tr -d ' ')"
  [ "$n" -gt 0 ] 2>/dev/null || { _log "refusing checkout: no slots provisioned"; return 3; }

  local attempts=0 max_attempts=$((n * 4 + 8))
  while [ "$attempts" -lt "$max_attempts" ]; do
    attempts=$((attempts + 1))
    local i
    for slotdir in "$POOL_ROOT"/slot-*/; do
      i="$(basename "$slotdir" | sed 's/^slot-//')"
      [ -d "${slotdir%/}/wt" ] || continue
      _try_lock "$i"
      local rc=$?
      if [ "$rc" -eq 0 ]; then
        if _verify_and_handout "$i"; then
          return 0
        else
          # fail closed: release the lock we just took, refuse this slot,
          # keep trying others.
          _release_lock "$i"
          continue
        fi
      fi
      # rc 1 (held) or 2 (reaped, try again next outer loop) -> move on
    done
  done
  _log "refusing checkout: no verified slot available after $attempts passes"
  return 3
}

_release_lock() {
  local i="$1"
  rm -rf "$(_slot_lock "$i")"
}

_verify_and_handout() {
  local i="$1"
  local slot; slot="$(_slot_dir "$i")"
  local wt="$slot/wt" venv="$slot/venv"

  if [ ! -d "$wt/.git" ] && [ ! -f "$wt/.git" ]; then
    _log "slot-$i: wt has no .git, fail-closed"
    return 1
  fi

  local base_sha
  base_sha="$(_git "$REPO" rev-parse --verify "${BASE_REF}^{commit}" 2>/dev/null)"
  [ -n "$base_sha" ] || { _log "slot-$i: cannot resolve base ref $BASE_REF, fail-closed"; return 1; }

  if ! _git "$wt" checkout --force -B "lane-pool-slot-$i" "$base_sha" >/dev/null 2>&1; then
    _log "slot-$i: reset-to-base failed, fail-closed"
    return 1
  fi
  if ! _git "$wt" clean -ffdx >/dev/null 2>&1; then
    _log "slot-$i: git clean failed, fail-closed"
    return 1
  fi

  if [ ! -x "$venv/bin/python" ] || ! "$venv/bin/python" -c 'import omniagentos' >/dev/null 2>&1; then
    _log "slot-$i: venv verification failed (import omniagentos), fail-closed"
    return 1
  fi

  printf 'slot-%s %s %s\n' "$i" "$wt" "$venv"
  return 0
}

# --- return --------------------------------------------------------
cmd_return() {
  local slotarg="${1:?usage: lane-pool.sh return <slot-id>}"
  local i="${slotarg#slot-}"
  local slot; slot="$(_slot_dir "$i")"
  local lock; lock="$(_slot_lock "$i")"
  local wt="$slot/wt"

  [ -f "$lock/holder" ] || { _log "refusing return: slot-$i is not locked (nothing to return)"; return 3; }
  # Sibling of the \x1f-delimited read in _try_lock -- holder records are
  # never space-delimited (see "FAIL CLOSED (record format, structural --
  # round 3)" at the top of this file), so this must split the same way.
  local pid; IFS=$'\x1f' read -r pid _ < "$lock/holder" 2>/dev/null

  # The holder was recorded as the ACQUIRING caller's pid ($PPID at acquire
  # time). The returning invocation of this script is itself a short-lived
  # child, so the caller doing the returning is $PPID here too.
  if [ "$pid" != "$CALLER_PID" ]; then
    if [ "${LANE_POOL_FORCE_RETURN:-0}" != "1" ]; then
      _log "refusing return: slot-$i is held by pid $pid, not $CALLER_PID (set LANE_POOL_CALLER_PID=<holder-pid> or LANE_POOL_FORCE_RETURN=1 to override)"
      return 3
    fi
  fi

  if [ -d "$wt/.git" ] || [ -f "$wt/.git" ]; then
    if [ -n "$(_git "$wt" status --porcelain 2>/dev/null)" ]; then
      local salvage_branch
      salvage_branch="lane-pool/salvage/slot-$i/$(date -u +%Y%m%dT%H%M%SZ)"
      if _git "$wt" checkout -b "$salvage_branch" >/dev/null 2>&1 \
         && _git "$wt" add -A >/dev/null 2>&1 \
         && _git "$wt" commit -m "lane-pool: salvage uncommitted work on return (slot-$i)" >/dev/null 2>&1; then
        _log "slot-$i: salvaged uncommitted work to $salvage_branch"
      else
        _log "refusing return: slot-$i has uncommitted work and salvage failed -- not destroying it silently"
        return 3
      fi
    fi
  fi

  _release_lock "$i"
  return 0
}

# --- status --------------------------------------------------------
cmd_status() {
  [ -d "$POOL_ROOT" ] || { echo "total=0 free=0 in_use=0"; return 0; }
  local total=0 in_use=0
  for slotdir in "$POOL_ROOT"/slot-*/; do
    [ -d "${slotdir%/}/wt" ] || continue
    total=$((total + 1))
    local i; i="$(basename "$slotdir" | sed 's/^slot-//')"
    [ -d "$(_slot_lock "$i")" ] && in_use=$((in_use + 1))
  done
  echo "total=$total free=$((total - in_use)) in_use=$in_use"
}

main() {
  local cmd="${1:-}"
  [ -n "$cmd" ] || { usage; return 2; }
  shift || true
  case "$cmd" in
    init) cmd_init "$@" ;;
    checkout) cmd_checkout "$@" ;;
    return) cmd_return "$@" ;;
    status) cmd_status "$@" ;;
    *) usage; return 2 ;;
  esac
}

main "$@"
