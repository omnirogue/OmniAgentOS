#!/usr/bin/env bash
# Auto-dispatch a verifier the moment a lane finishes. Never batch, never wait for a human.
#
# Fable's diagnosis, implemented: "verdicts should be auto-dispatched on lane completion,
# not batch-triggered — that is what let 41 accumulate."
#
# It was right about the cause. The verdict queue did not grow because verification was
# slow; it grew because NOTHING TRIGGERED IT. Lanes finished, went quiet, and waited for a
# human to notice — so the operator was the trigger, which is the same structural fault as
# the operator being the scheduler. A queue with no pump is a queue that only drains when
# someone remembers it exists.
#
# Detection is mechanical: a lane is "finished" when it has changed files, no live agent
# process, and no verdict recorded. That is derived by executing git/pgrep, never by asking
# an agent whether it is done — a self-report would reintroduce the exact failure the
# verdict gate exists to catch.
#
# Concurrency is bounded. Over-dispatching is not free: at high load three lanes died at
# exit 144 and 26 of 32 verifiers produced nothing, so an unbounded pump would convert a
# backlog into a graveyard faster than it drains it.
#
# ==============================================================================
# S04 — BOUNDED IN CONCURRENCY, UNBOUNDED IN ATTEMPTS
# ==============================================================================
# PUMP_MAX bounds how many verifiers run AT ONCE. Nothing bounded how many times ONE LANE
# could be dispatched: this pump READ the fleet ledger (`fleet-ledger.py scan/query`) and
# wrote NO attempt row, so the shipped 2-retry cap in omniagentos/swarm/scheduler.py
# governed it not at all. "26 of 32 verifiers produced nothing" is what an unbounded
# attempt count looks like from the other end — each of those 26 lanes stayed
# `awaiting_verdict` and was re-pumped on the very next cycle, forever.
#
# Every dispatch now writes a swarm_attempts row through the existing SwarmDal API, and the
# SAME gate the scheduler's cap uses decides the next one. The fingerprint hashed here is
# the dispatch payload (this pump reacts to no verdict — it CREATES the demand for one), so
# two consecutive identical dispatches for a lane are by construction a lane that is not
# moving, and it is quarantined instead of re-pumped.
#
# A brief-less lane is quarantined too: a verifier with no contract cannot verify, and 25
# of 70 clones had no var/task.md.
#
# ARMING: REWORK_DRY_RUN=1 (one switch for all four pumps) prints the decision and payload
# without spawning a verifier.
#
# `set -e` is deliberately NOT enabled: this loop must survive a non-zero grep.
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO"
PY="$REPO/.venv/bin/python"
MAX_CONCURRENT="${PUMP_MAX:-40}"
INTERVAL="${PUMP_INTERVAL:-90}"
CYCLES="${PUMP_CYCLES:-0}"
LOG="${PUMP_LOG:-$REPO/var/swarm/verdict-pump.log}"
CLONES="${PUMP_CLONES:-$REPO/var/swarm/clones}"
QUARANTINE="${PUMP_QUARANTINE:-$REPO/var/swarm/quarantine}"
PAYLOAD_DIR="${PUMP_PAYLOAD_DIR:-$REPO/var/swarm/verdict-payloads}"
DRY_RUN="${REWORK_DRY_RUN:-0}"
RETRY_CAP="${PUMP_RETRY_CAP:-2}"
# Outer cap MUST stay > dispatch-verifier.sh:70's 2400s inner watchdog or its
# failure record becomes unreachable. 2700s leaves 300s (12.5%) headroom; the observed
# real verdict run is 1507s.
DISPATCH_TIMEOUT_S="${VERDICT_DISPATCH_TIMEOUT_S:-2700}"
# GNU coreutils timeout: Homebrew/macOS installs it as `gtimeout`, Linux as
# `timeout`. Both accept the same -k/duration syntax; resolve whichever exists.
TIMEOUT_BIN="$(command -v gtimeout || command -v timeout || true)"
BRIEF_CANDIDATES="${REWORK_BRIEF_CANDIDATES:-var/task.md LANE-BRIEF.md var/LANE-BRIEF.md TASK.md}"
QUEUE_CMD="${PUMP_QUEUE_CMD:-\"$PY\" scripts/fleet-ledger.py query awaiting_verdict}"
LEDGER_SOURCE="real"; [ "$DRY_RUN" = "1" ] && LEDGER_SOURCE="test"

ledger() { "$PY" -W ignore::RuntimeWarning -m omniagentos.swarm.dal "$@"; }

preflight() {
  local fatal=0
  [ -x "$PY" ] || { echo "FATAL(preflight): no python at $PY" >&2; fatal=1; }
  [ -d "$CLONES" ] || { echo "FATAL(preflight): clones root missing: $CLONES" >&2; fatal=1; }
  mkdir -p "$QUARANTINE" "$PAYLOAD_DIR" "$(dirname "$LOG")" 2>/dev/null
  [ -d "$QUARANTINE" ] || { echo "FATAL(preflight): cannot create $QUARANTINE" >&2; fatal=1; }
  if [ "$fatal" -eq 0 ] && ! ledger pump-hash --verdict-hash preflight >/dev/null 2>&1; then
    echo "FATAL(preflight): swarm ledger CLI unusable (omniagentos.swarm.dal)" >&2; fatal=1
  fi
  [ -n "$TIMEOUT_BIN" ] || { echo "FATAL(preflight): no GNU timeout (gtimeout or timeout) on PATH" >&2; fatal=1; }
  if [ "$DRY_RUN" != "1" ] && [ ! -x "$REPO/scripts/dispatch-verifier.sh" ]; then
    echo "FATAL(preflight): scripts/dispatch-verifier.sh missing or not executable" >&2; fatal=1
  fi
  [ "$fatal" -eq 0 ] || exit 1
}

resolve_brief() {  # lane -> first non-empty candidate path
  local D="$CLONES/$1" c
  for c in $BRIEF_CANDIDATES; do
    [ -s "$D/$c" ] && { printf '%s\n' "$D/$c"; return 0; }
  done
  return 1
}

is_quarantined() { [ -f "$QUARANTINE/$1/reason" ]; }

# WHAT THIS LANE CURRENTLY IS, as evidence rather than as a remembered verdict.
#
# Quarantine used to be terminal: `is_quarantined` short-circuits before the gate, so a
# lane parked for a SETUP fault stayed parked after the fault was fixed. A verifier
# refused for an unborn branch was quarantined `verdict-repeat` on the next cycle, and
# then repairing the branch exactly as instructed changed nothing — the pump never looked
# again. A fail-closed guard with no correction path is its own outage.
#
# The fingerprint is everything a dispatch depends on: which branch, which commit, which
# contract. If it MOVED, the lane is not the thing that was parked, and it goes back to
# the gate on evidence rather than on anyone's say-so.
lane_fingerprint() {  # lane -> branch|head-sha|contract-sha
  local D="$CLONES/$1" BR SHA BSHA BRIEF
  BR=$(git -C "$D" rev-parse --abbrev-ref HEAD 2>/dev/null) || BR=""
  SHA=$(git -C "$D" rev-parse HEAD 2>/dev/null) || SHA=""
  BSHA=""
  BRIEF=$(resolve_brief "$1") && BSHA=$(shasum -a 256 "$BRIEF" 2>/dev/null | awk '{print $1}')
  printf '%s|%s|%s\n' "${BR:-none}" "${SHA:-none}" "${BSHA:-none}"
}

# Returns 0 (skip this lane) or 1 (it moved; it was released and may proceed).
quarantine_holds() {  # lane
  local LANE="$1" Q="$QUARANTINE/$1" WAS NOW
  WAS=$(cat "$Q/fingerprint" 2>/dev/null) || WAS=""
  NOW=$(lane_fingerprint "$LANE")
  # A quarantine written before fingerprints existed cannot be compared. Record the
  # current state and keep holding — releasing on an unknown would release everything
  # at once, which is the favourable-absence reading this whole file argues against.
  if [ -z "$WAS" ]; then
    printf '%s\n' "$NOW" > "$Q/fingerprint" 2>/dev/null || true
    return 0
  fi
  [ "$WAS" = "$NOW" ] && return 0
  # A release that could not actually remove the marker must KEEP HOLDING. Otherwise the
  # stale fingerprint on disk re-triggers a release every single cycle — an unbounded
  # loop that dispatches the lane over and over on a filesystem fault.
  rm -rf "$Q"
  if [ -e "$Q/reason" ]; then
    echo "  $LANE: cannot clear quarantine at $Q — still holding" | tee -a "$LOG"
    return 0
  fi
  echo "RELEASE lane=$LANE reason=lane-changed"
  # Logged, NOT recorded as an attempt row. A release is not a dispatch, and every
  # attempt row spends the retry cap that exists to bound real dispatches — a release
  # that consumed that budget would hand the lane back and immediately re-block it.
  echo "  $LANE: released from quarantine — lane changed ($WAS -> $NOW)" | tee -a "$LOG"
  return 1
}

quarantine() {  # lane reason — durable on disk, announced exactly once
  local LANE="$1" REASON="$2" Q="$QUARANTINE/$1"
  if is_quarantined "$LANE"; then
    [ "$DRY_RUN" = "1" ] && echo "SKIP lane=$LANE reason=quarantined"
    return 0
  fi
  mkdir -p "$Q" || { echo "  $LANE: FAILED to quarantine" | tee -a "$LOG"; return 1; }
  printf '%s\n' "$REASON" > "$Q/reason"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$Q/first_seen"
  printf '%s\n' "$CLONES/$LANE" > "$Q/lane_dir"
  # The state this decision was made ABOUT. When it changes, the decision expires.
  lane_fingerprint "$LANE" > "$Q/fingerprint"
  echo "QUARANTINE lane=$LANE reason=$REASON"
  echo "  $LANE: QUARANTINED ($REASON)" >> "$LOG"
  ledger pump-attempt --lane "$LANE" --pump verdict --provider pump --model quarantine \
    --verdict-hash "quarantine:$REASON" --end-reason blocked \
    --detail "quarantine:$REASON" --source "$LEDGER_SOURCE" >/dev/null 2>>"$LOG" || true
  return 0
}

# The dispatch payload, written to a file so the bytes that are hashed, printed and
# executed are the same bytes. The brief's sha256 is part of it: a lane whose contract
# CHANGED produces a different payload and is therefore allowed a fresh attempt, while a
# lane whose contract is unchanged and whose last verifier produced nothing is not.
build_payload() {  # lane brief_path out_path
  local LANE="$1" BRIEF="$2" OUTF="$3" BSHA BR SHA
  BSHA=$(shasum -a 256 "$BRIEF" 2>/dev/null | awk '{print $1}')
  # WHICH BRANCH, AT WHICH COMMIT. A payload of contract-only made two dispatches
  # identical whenever the contract was unchanged — including when the lane had been
  # REPAIRED between them. So a lane refused for a broken branch, then fixed exactly as
  # the refusal instructed, produced a byte-identical payload and was blocked
  # `verdict-repeat` and quarantined. A verification is OF a commit on a branch; the
  # fingerprint has to say which, or "same dispatch" means "same contract" and a
  # corrected lane can never be told apart from a stalled one.
  BR=$(git -C "$CLONES/$LANE" rev-parse --abbrev-ref HEAD 2>/dev/null) || BR=""
  SHA=$(git -C "$CLONES/$LANE" rev-parse HEAD 2>/dev/null) || SHA=""
  {
    printf 'verifier-dispatch lane=%s\n' "$LANE"
    printf 'command=./scripts/dispatch-verifier.sh %s\n' "$LANE"
    printf 'branch=%s\n' "${BR:-none}"
    printf 'head_sha=%s\n' "${SHA:-none}"
    printf 'contract_sha256=%s\n' "$BSHA"
    printf -- '--- contract ---\n'
    head -800 "$BRIEF"
  } > "$OUTF"
}

running_verifiers() { pgrep -f 'dispatch-verifier.sh' 2>/dev/null | wc -l | tr -d ' '; }

preflight

cycle=0
while :; do
  cycle=$((cycle+1))
  "$PY" scripts/fleet-ledger.py scan >/dev/null 2>&1
  BUSY=$(running_verifiers)
  FREE=$(( MAX_CONCURRENT - BUSY ))
  [ "$FREE" -lt 0 ] && FREE=0

  # Lanes that are finished-and-unjudged. The ledger derives this from evidence:
  # changed files present, no agent alive, no valid verdict on file.
  # macOS ships bash 3.2 — no `mapfile`. A temp file is portable and keeps the
  # queue a single mechanical read rather than a pipeline whose failure would be
  # invisible.
  QFILE=$(mktemp)
  # The queue SOURCE is configurable (PUMP_QUEUE_CMD); the default is the fleet ledger.
  # One named seam beats a second hidden code path — the preflight suite drives the pump
  # through exactly the production branch instead of a test-only shortcut around it.
  eval "$QUEUE_CMD" 2>/dev/null \
    | grep -vE '^\(' | awk '{print $1}' | grep -v '^$' > "$QFILE" || true
  QN=$(wc -l < "$QFILE" | tr -d ' ')
  echo "[$(date -u +%H:%M:%S)] cycle=$cycle queued=$QN verifiers_busy=$BUSY free=$FREE" | tee -a "$LOG"

  n=0
  while IFS= read -r LANE; do
    [ "$n" -ge "$FREE" ] && break
    [ -z "$LANE" ] && continue
    if is_quarantined "$LANE" && quarantine_holds "$LANE"; then
      [ "$DRY_RUN" = "1" ] && echo "SKIP lane=$LANE reason=quarantined"
      continue
    fi
    # Never double-dispatch a lane that already has a verifier attached.
    pgrep -f "dispatch-verifier.sh $LANE\$" >/dev/null 2>&1 && continue

    [ -d "$CLONES/$LANE/.git" ] || { quarantine "$LANE" missing-clone; continue; }

    # HARD PRECONDITION: a verifier with no contract can only guess. No path to dispatch.
    BRIEF=$(resolve_brief "$LANE") || BRIEF=""
    [ -s "$BRIEF" ] || { quarantine "$LANE" missing-brief; continue; }

    PAYLOADF="$PAYLOAD_DIR/$LANE.payload"
    build_payload "$LANE" "$BRIEF" "$PAYLOADF"

    GATE=$(ledger pump-gate --lane "$LANE" --verdict-body-file "$PAYLOADF" \
             --retry-cap "$RETRY_CAP" 2>>"$LOG")
    case "$GATE" in
      *"decision=blocked"*)
        REASON=$(printf '%s' "$GATE" | sed -n 's/.*reason=\([a-z-]*\).*/\1/p')
        quarantine "$LANE" "${REASON:-gate-blocked}"; continue ;;
      *"decision=allow"*) : ;;
      *) echo "  $LANE: gate unreadable — refusing to dispatch" | tee -a "$LOG"; continue ;;
    esac

    if [ "$DRY_RUN" = "1" ]; then
      echo "DISPATCH lane=$LANE"
      echo "--- DISPATCH-PAYLOAD BEGIN lane=$LANE ---"
      cat "$PAYLOADF"
      echo "--- DISPATCH-PAYLOAD END lane=$LANE ---"
      ledger pump-attempt --lane "$LANE" --pump verdict --provider verifier \
        --model dispatch-verifier --verdict-body-file "$PAYLOADF" --end-reason blocked \
        --detail "dry-run: verifier payload built, nothing spawned" \
        --source test >/dev/null 2>>"$LOG" || true
      n=$((n+1)); continue
    fi

    ATT=$(ledger pump-attempt --lane "$LANE" --pump verdict --provider verifier \
            --model dispatch-verifier --verdict-body-file "$PAYLOADF" --keep-open \
            --source real 2>>"$LOG" | sed -n 's/^ATTEMPT id=\([^ ]*\).*/\1/p')
    if [ -z "$ATT" ]; then
      echo "  $LANE: ledger refused the attempt — NOT dispatching" | tee -a "$LOG"; continue
    fi
    echo "DISPATCH lane=$LANE"
    # The attempt row is settled by the verifier's exit, not left live: a pump killed
    # mid-flight would otherwise wedge the lane until the next open reaps it.
    # dispatch-verifier returns 3 for its inner watchdog; GNU timeout returns 137 when
    # -k escalates a TERM-ignoring child to SIGKILL. Both are timeout outcomes.
    #
    # DETACH WITHOUT nohup: under a caller with no controlling tty — the merge-gate
    # CI runner AND the gate-loop launchd daemon — BSD `nohup` aborts with
    # "can't detach from console: Inappropriate ioctl for device" and NEVER execs
    # the command, so the verifier silently never runs (measured on the estate
    # gate runner 2026-08-14; a plausible contributor to the "26 of 32 verifiers
    # vanished" class). The POSIX-portable equivalent needs no tty: a subshell that
    # ignores SIGHUP with stdin closed. stdout/stderr already go to $LOG.
    (
      trap "" HUP
      "$TIMEOUT_BIN" -k 30 "$DISPATCH_TIMEOUT_S" ./scripts/dispatch-verifier.sh "$LANE" >> "$LOG" 2>&1
      RC=$?
      if [ $RC -eq 0 ]; then R=completed; elif [ $RC -eq 3 ] || [ $RC -eq 124 ] || [ $RC -eq 137 ]; then R=timeout; else R=crashed; fi
      "$PY" -W ignore::RuntimeWarning -m omniagentos.swarm.dal pump-close \
        --attempt-id "$ATT" --end-reason "$R" --detail "dispatch-verifier rc=$RC" \
        >/dev/null 2>&1
    ) </dev/null >> "$LOG" 2>&1 &
    echo "  pumped: $LANE" | tee -a "$LOG"
    n=$((n+1))
    sleep 0.3
  done < "$QFILE"
  rm -f "$QFILE"
  [ "$n" = "0" ] && echo "  nothing to pump (queue empty or verifiers saturated)" | tee -a "$LOG"

  [ "$CYCLES" != "0" ] && [ "$cycle" -ge "$CYCLES" ] && break
  if [ "$DRY_RUN" = "1" ]; then sleep "${REWORK_DRY_INTERVAL:-0}"; else sleep "$INTERVAL"; fi
done
