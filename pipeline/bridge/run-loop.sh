#!/usr/bin/env bash
# Run a loop prompt continuously inside tmux, with a durable log.
#
#   ./run-loop.sh planning claude5
#   ./run-loop.sh reviewer claude6
#
# The first arg (planning|reviewer|implementer) selects PROMPT-${ROLE}-loop.md
# — a prompt-FILE selector, not the schema role enum. "planning" is correct
# and required here even though schema/*.schema.json's producer.role enum is
# planner|reviewer|implementer|external: the file is PROMPT-planning-loop.md,
# and renaming this arg to "planner" would document a broken invocation.
#
# Each iteration is a FRESH claude process. That is deliberate, not a
# limitation: `claude -p` does one turn and exits, and this system's state
# lives in files rather than in a context window — so a new session each iteration
# loses nothing and gains a clean instruction set. It is the same property that
# lets an account rotate mid-run.
#
# tmux gives you detach/reattach and survives logout. It does NOT survive a
# reboot — for that, use a LaunchAgent (see the janitor/governor plists).
set -uo pipefail

ROLE="${1:?usage: run-loop.sh <planning|reviewer|implementer> <claudeN> [fallback...]}"
LAUNCHER="${2:?usage: run-loop.sh <role> <claudeN> [fallback...]}"
shift 2 || true
# Remaining args are the failover ladder. ROUTING.md says a Claude session limit
# is a routing event, not an outage — but that instruction lives in the PROMPT,
# and a rate-limited `claude -p` dies BEFORE the model can read it. Failover has
# to live out here or it does not exist. Default ladder: the other accounts.
FALLBACKS=("$@")
if [ ${#FALLBACKS[@]} -eq 0 ]; then
  for c in claude1 claude2 claude3 claude6 claude7; do
    [ "$c" != "$LAUNCHER" ] && command -v "$c" >/dev/null 2>&1 && FALLBACKS+=("$c")
  done
fi
SEATS_CSV="$LAUNCHER$([ ${#FALLBACKS[@]} -gt 0 ] && printf ',%s' "${FALLBACKS[@]}")"

TL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROMPT="$TL/prompts/PROMPT-${ROLE}-loop.md"
WORKDIR="${LOOP_WORKDIR:-$HOME/OmniAgentOS}"   # the repo the loop WORKS ON
LOGDIR="$WORKDIR/var/loopqueue/logs"
LOG="$LOGDIR/${ROLE}-loop.log"
SESSION="loop-${ROLE}"
SLEEP_BETWEEN="${LOOP_SLEEP:-60}"

# The backoff has to key on something the system EMITS. `governor.py --check`
# samples live, names the binding limit, and exits 3 when one binds — see
# governor.check() for the phantom this replaces. Resolved to an absolute
# interpreter here because the loop body runs under `bash -lc` inside tmux and
# must not depend on PATH.
GOV_PY="${LOOP_GOV_PY:-$WORKDIR/.venv/bin/python}"
[ -x "$GOV_PY" ] || GOV_PY="$(command -v python3 || echo /usr/bin/python3)"
GOV_CHECK="$GOV_PY $TL/bridge/governor.py --loops-root $WORKDIR/var/loopqueue --check"
PARK_CLI="$GOV_PY $TL/bridge/loop_park.py"
SEAT_CLI="$GOV_PY $TL/bridge/loop_seat.py"
ITER_CAP="$LOGDIR/.${ROLE}-iter.out"   # this iteration's captured output (weekly-limit detection)

# ── restart-churn fix (2026-08-12) ──────────────────────────────────────────
# The ~8h 0-merge stall on 2026-08-11→12 was NOT ladder exhaustion (the claudeN
# seats are distinct accounts with independent weekly quotas; claude1/2/3 worked
# throughout). It was a control-loop churn: every fresh process — an operator
# restart OR a loop-watchdog relaunch after a hang-recycle — re-entered ACTIVE at
# the LAUNCHER (claude4), which was weekly-limited, burned two iterations failing
# on it, then rotated; and because the rotated seat lived only in process memory,
# the next relaunch threw that progress away and started over at claude4.
#
# loop_seat.py keeps durable rotation state so a restart (1) DEMOTES a
# weekly-limited seat to the back of the ladder (still tried last, never LED
# with), and (2) RESUMES at the seat the loop was actually using. It returns a
# PERMUTATION of the seats or nothing; we re-check the permutation property and
# fall back to the operator's ORIGINAL order on anything unexpected, so this can
# only ever change seat PRIORITY, never which seats exist. See loop_seat.py.
mkdir -p "$LOGDIR" 2>/dev/null || true   # so a loop_seat.py stderr diagnostic below has somewhere to land
_seats_all=("$LAUNCHER")
[ ${#FALLBACKS[@]} -gt 0 ] && _seats_all+=("${FALLBACKS[@]}")
_seats_csv_in="$(IFS=,; printf '%s' "${_seats_all[*]}")"
_ordered="$($SEAT_CLI order --root "$WORKDIR/var/loopqueue" --role "$ROLE" --seats "$_seats_csv_in" 2>>"$LOG" || true)"
if [ -n "$_ordered" ]; then
  read -r -a _ord <<< "$_ordered"
  # Permutation guard: identical count AND identical multiset as the input.
  if [ "${#_ord[@]}" -eq "${#_seats_all[@]}" ] \
     && [ "$(printf '%s\n' "${_ord[@]}" | sort)" = "$(printf '%s\n' "${_seats_all[@]}" | sort)" ]; then
    [ "${_ord[0]}" != "$LAUNCHER" ] && \
      echo "seat-rotation: leading with ${_ord[0]} (operator launcher was $LAUNCHER) — order: ${_ord[*]}"
    LAUNCHER="${_ord[0]}"
    FALLBACKS=("${_ord[@]:1}")
  fi
fi
# Rebuild the seats CSV so the park record reflects the EFFECTIVE order.
SEATS_CSV="$LAUNCHER$([ ${#FALLBACKS[@]} -gt 0 ] && printf ',%s' "${FALLBACKS[@]}")"

[ -f "$PROMPT" ] || { echo "no prompt at $PROMPT"; exit 1; }
command -v "$LAUNCHER" >/dev/null || { echo "no launcher '$LAUNCHER' on PATH"; exit 1; }
mkdir -p "$LOGDIR"

# The planner runs with a READ-ONLY GitHub badge: GH_TOKEN overrides the keyring
# login for that process only, so a planner session physically cannot push or
# merge anything. The token is read from the 600-perm env file AT RUNTIME inside
# the loop shell — never placed in the tmux command line, where any local
# process could read it from the argument list. Reviewer is NOT downgraded here:
# its job includes pushing PR branches, and its isolation lives at the account
# level (a separate GitHub identity), not in this launcher. The implementer
# keeps the full keyring credential on purpose — it is the one role allowed to
# merge (branch protection on main restricts merge to that identity).
# OMNI_NTFY_URL wires every loop's ALERTS.md write to a phone push (ntfy).
# Read at RUNTIME from the 600-perm env file, the same pattern as GH_TOKEN
# below — never placed on the tmux command line where a local process could
# read it. ALL THREE ROLES inherit it, so an alert from any loop reaches the
# operator without a live session. If it is unset the push is simply a no-op
# (bridge/notify.py fails soft); the ALERTS.md line is written regardless.
ROLE_ENV='_n="$(grep "^OMNI_NTFY_URL=" "$HOME/.config/omni/connections.env" 2>/dev/null | tail -1 | cut -d= -f2-)"; if [ -n "$_n" ]; then export OMNI_NTFY_URL="$_n"; fi; unset _n'
if [ "$ROLE" = "planning" ]; then
  ROLE_ENV="$ROLE_ENV; "'export GIT_TERMINAL_PROMPT=0; _t="$(grep "^GH_TOKEN_READONLY=" "$HOME/.config/omni/connections.env" 2>/dev/null | tail -1 | cut -d= -f2-)"; if [ -n "$_t" ]; then export GH_TOKEN="$_t"; else echo "WARN: GH_TOKEN_READONLY missing - planner is running with the FULL-POWER keyring credential"; fi; unset _t'
fi

if [ "${RUN_LOOP_BUILD_ONLY:-0}" != "1" ] && tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "session '$SESSION' already running. Attach with:  tmux attach -t $SESSION"
  exit 0
fi

# The loop body. Logged to a FILE, not just tmux scrollback — scrollback is
# capped and dies with the server, and the log is what you read when something
# went wrong at 3am.
read -r -d '' BODY <<LOOPEOF || true
$ROLE_ENV
# Remote gate dispatch is E2E-proven (2026-08-08: pin by direct push, 10-check
# preflight, evidence sync-back — all live-verified). integration.py reads this
# env as the default for --allow-remote-gate.
export THREELOOPS_ALLOW_REMOTE_GATE=1
ACTIVE="$LAUNCHER"
LADDER=(${FALLBACKS[*]})
fails=0
idle=0
while true; do
  echo "───── \$(date -u +%Y-%m-%dT%H:%M:%SZ) ${ROLE} iteration start" | tee -a "$LOG"
  : > "$ITER_CAP" 2>/dev/null || true   # truncate: capture only THIS iteration for weekly-limit detection
# HEADLESS PERMISSIONS. Without this the loop could BUILD but never COMMIT: a
# a headless claude in default permission mode blocks tools needing approval, and
  # NOTE: this body is an UNQUOTED heredoc -- backticks here are COMMAND
  # SUBSTITUTION and execute at expansion time. A backticked word in this comment
  # silently ran claude with no prompt and hung every iteration (2026-08-08).
# headless mode nobody can approve, so every candidate died as an uncommitted
# worktree edit — 45 proposals, 0 candidates (2026-08-08). This does NOT disable
# the estate's guardrails: PreToolUse hooks (git-guard.py, estate-host-guard.py)
# fire regardless of permission mode, so the Git Isolation Doctrine still binds.
  # --strict-mcp-config with NO --mcp-config = zero MCP servers. The loops are
  # headless queue-workers (Bash/Read/Write/Edit/git/gh/codex/grok/kimi); none use
  # an mcp__ tool. Without this, every turn spawned ~12 MCP servers (playwright,
  # slack, tavily, brave, duckduckgo, markitdown, filesystem, memory, git, sqlite,
  # fetch, sequential-thinking) as a startup handshake — ~6 min to spawn, a hang
  # risk when one blocks (measured: turns stalling at "iteration start" for 25-46
  # min, 2026-08-09), and they LEAKED on recycle (51 orphans estate-wide). A loop
  # that needs git/sqlite uses them via Bash, not MCP.
  case "\$ACTIVE" in
    kimi*)
      kimi -p "\$(cat '$PROMPT')" -m fireworks/kimi-k3-fast --output-format text 2>&1 | tee -a "$LOG" "$ITER_CAP"
      ;;
    codex*|sol*|gpt-*)
      codex exec -m gpt-5.6-sol -c model_reasoning_effort='"high"' \
        -c 'mcp_servers={}' -s workspace-write \
        -c sandbox_workspace_write.network_access=true \
        "\$(cat '$PROMPT')" 2>&1 | tee -a "$LOG" "$ITER_CAP"
      ;;
    *)
      \$ACTIVE --model claude-opus-5 --dangerously-skip-permissions --strict-mcp-config -p "\$(cat '$PROMPT')" 2>&1 | tee -a "$LOG" "$ITER_CAP"
      ;;
  esac
  rc=\${PIPESTATUS[0]}
  # Weekly-limit demotion signal. Keyed on the provider STRING, not on rc — a
  # generic rc=1 (transient error) must not demote a healthy seat for hours. A
  # future restart reads this and leads with a non-limited seat instead of \$ACTIVE.
  if grep -qF "hit your weekly limit" "$ITER_CAP" 2>/dev/null; then
    _rl="\$(grep -F 'hit your weekly limit' "$ITER_CAP" | tail -1)"
    $SEAT_CLI record-limit --root "$WORKDIR/var/loopqueue" --role "$ROLE" --seat "\$ACTIVE" --text "\$_rl" >/dev/null 2>>"$LOG" || true
  fi
  if [ "\$rc" != "0" ] && [ "\$rc" != "2" ]; then
    fails=\$((fails + 1))
    # Rotate the ACCOUNT before giving up — a quota problem is not a capability
    # problem. Only after every seat has failed is this a real outage.
    if [ \$fails -ge 2 ] && [ \${#LADDER[@]} -gt 0 ]; then
      next="\${LADDER[0]}"; LADDER=("\${LADDER[@]:1}")
      echo "───── rc=\$rc twice on \$ACTIVE — rotating to \$next" | tee -a "$LOG"
      ACTIVE="\$next"; fails=0
      $SEAT_CLI record-active --root "$WORKDIR/var/loopqueue" --role "$ROLE" --seat "\$ACTIVE" >/dev/null 2>>"$LOG" || true   # persist: a restart resumes here, not at the launcher
    elif [ \$fails -ge 2 ]; then
      echo "───── every seat failed — PARKING, alerting once" | tee -a "$LOG"
      $PARK_CLI park --root "$WORKDIR/var/loopqueue" --role "$ROLE" \
        --reason "every seat failed" --seats "$SEATS_CSV" \
        --alerts-file "$WORKDIR/var/loopqueue/ALERTS.md" --log-tail-file "$LOG" \
        2>&1 | tee -a "$LOG"
      exit 2
    fi
  else
    fails=0
    if [ "\$rc" = "0" ]; then
      $PARK_CLI clear --root "$WORKDIR/var/loopqueue" --role "$ROLE" >/dev/null 2>&1 || true
      # This seat just did real work: record it as the resume point and clear any
      # stale weekly-limit marker on it (it has demonstrably recovered).
      $SEAT_CLI record-active --root "$WORKDIR/var/loopqueue" --role "$ROLE" --seat "\$ACTIVE" >/dev/null 2>>"$LOG" || true
      $SEAT_CLI clear-limit --root "$WORKDIR/var/loopqueue" --role "$ROLE" --seat "\$ACTIVE" >/dev/null 2>>"$LOG" || true
    fi
  fi
  echo "───── \$(date -u +%Y-%m-%dT%H:%M:%SZ) ${ROLE} iteration end rc=\$rc" | tee -a "$LOG"
  # rc 2 means could-not-run (Ruling #4): the iteration's instrument/gate could
  # not evaluate this input. Sleeping longer avoids hammering a condition that
  # will not change until the mechanics are fixed.
  #
  # The backoff below used to be decided by grepping this log for
  #   "governor|blocked|disk|load|wip|cap|UNKNOWN, so stop"
  # which matches the MODEL'S OWN PROSE. Any iteration in which the model wrote
  # the word "load" or "cap" — nearly all of them — scored as a governor block.
  # Measured on the live logs 2026-08-08: 59 of 61 implementer iterations, 7 of
  # 14 planning, 4 of 5 reviewer, every one of them announcing "backing off 0s"
  # and then respawning immediately. It announced a block that had not happened
  # and a backoff it did not take, and cost an operator an afternoon chasing a
  # governor fault that did not exist.
  #
  # It now asks the governor. Exit 3 = a limit binds, and the reason is printed.
  if [ "\$rc" = "2" ]; then
    sleep 900                       # could-not-run: back off until mechanics change
  else
    gov_out="\$($GOV_CHECK 2>&1)"; gov_rc=\$?
    if [ "\$gov_rc" = "3" ]; then
      idle=\$((idle + 1))
      back=\$(( $SLEEP_BETWEEN * (idle < 5 ? 1 << idle : 16) ))   # 60s -> 16m, capped. MUST be \$-expanded at heredoc build: the child shell has no SLEEP_BETWEEN, and a bare name reads as 0 — measured as "backing off 0s" on all three loops 2026-08-08.
      # Belt and braces: that bug was silent, and a 0s backoff is
      # indistinguishable from no backoff at all in the log.
      [ "\$back" -gt 0 ] 2>/dev/null || back=$SLEEP_BETWEEN
      echo "───── governor limit binding; backing off \${back}s — \$gov_out" | tee -a "$LOG"
      sleep \$back
    else
      # gov_rc 0 = clear. Anything else means --check itself failed; that is an
      # instrument fault, not a governor block, and it must not masquerade as one.
      [ "\$gov_rc" = "0" ] || echo "───── governor --check unusable (rc=\$gov_rc): \$gov_out" | tee -a "$LOG"
      idle=0
      sleep $SLEEP_BETWEEN
    fi
  fi
done
LOOPEOF

# -c sets the working directory, and it must be the repo the loop WORKS ON —
# that is where var/loopqueue, ARCHI.md and the code live. Pointing it at the
# prompt package instead looks fine (no warning) but is wrong for a subtler
# reason: the warning only disappears because that directory has no settings
# file to ignore. Without -c at all, tmux inherits the invoker's cwd and a loop
# started from $HOME silently ignores every permissions.allow entry — 175 of
# them, measured.
# BUILD-ONLY self-test hook (no production effect; unset by default). Validates
# that the assembled loop body is syntactically valid and prints the EFFECTIVE
# seat order, WITHOUT launching tmux or touching any live loop. Drives the
# dry-trace own-gate and permanently guards against a heredoc syntax regression.
if [ "${RUN_LOOP_BUILD_ONLY:-0}" = "1" ]; then
  if bash -n <<<"$BODY"; then
    echo "BUILD_ONLY ok: role=$ROLE active=$LAUNCHER ladder=(${FALLBACKS[*]:-})"
    exit 0
  fi
  echo "BUILD_ONLY: assembled loop body has a syntax error" >&2
  exit 1
fi

tmux new-session -d -s "$SESSION" -n "$ROLE" -c "$WORKDIR" "bash -lc $(printf '%q' "$BODY")"
tmux set-option -t "$SESSION" history-limit 100000 2>/dev/null   # generous scrollback

echo ""
echo "  ✅ RUNNING NOW — '$SESSION' is live on $LAUNCHER (failover: ${FALLBACKS[*]:-none})"
echo ""
echo "  watch   : tmux attach -t $SESSION      (detach with Ctrl-b then d)"
echo "  tail    : tail -f $LOG"
echo "  stop    : tmux kill-session -t $SESSION"
