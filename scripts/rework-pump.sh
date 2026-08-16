#!/usr/bin/env bash
# REWORK PUMP — send rejected lanes back to a coder WITH the reviewer's specific reason.
#
# The bottleneck moved again: coding -> verification -> rework. 32 lanes carry a REJECT
# from gpt-5.6-sol and nothing sends them back. Without this the queue simply stops, and
# every rejected lane's work is stranded — finished, judged, and abandoned.
#
# Measured rejection causes across those 32 verdicts:
#   33  out-of-scope edits        touched files the contract did not own
#   26  over-correction           a two-valued return made three-valued without fixing callers
#   19  unproven                  a claimed fix with no test that fails when it is reverted
#   16  no failing-on-revert      same thing, named explicitly by the reviewer
#   12  bare truthiness           None is falsy, so unknown silently takes the false branch
#
# None of these require a rewrite. They are all "you did the work, now prove it binds" —
# which is exactly the discipline this repo runs on, so the rework brief is short.
#
# The coder receives the reviewer's ACTUAL verdict text, not a summary of it. A paraphrase
# would lose the file:line the reviewer cited, and the lane would guess.
#
# ==============================================================================
# S04 — THE DISPATCH RUNAWAY, AND THE FOUR THINGS THAT NOW STOP IT
# ==============================================================================
# MEASURED: this loop is `while :;` with CYCLES=0 meaning infinite and REWORK_MAX a
# CONCURRENCY cap, not an attempt cap — there was no per-lane attempt counter anywhere
# in the file. The prompt hardcoded "Your original contract is in var/task.md". On disk
# 45 clones use var/task.md, 22 use LANE-BRIEF.md, 3 have neither: the 7/29 sw-* cohort
# wrote var/task.md, the 7/30 p1-/p2- cohort wrote LANE-BRIEF.md, and the pump only knew
# the first convention. For 25 of 70 clones every dispatch told the agent to read a file
# that does not exist; the agent ran `sed var/task.md`, died in 11-12s, and the pump
# immediately re-dispatched. Result: 5,702 REWORK sessions flat at 29-30/hour/lane for
# 24 hours, 726/726 p1-counterfeit dispatches quoting the same
# `sed: var/task.md: No such file or directory`, ZERO commits, ~22.2h of session time.
#
# 1. READ THE BRIEF, DO NOT NAME IT. The prompt now carries the brief's CONTENTS. Merely
#    resolving the path and interpolating the path would fix the symptom and leave the bug
#    class intact — the next cohort invents a third filename and it breaks again. Inlining
#    makes the dispatched prompt self-contained and the filename non-load-bearing.
#
# 2. HARD PRECONDITION, NOT A HINT. `[ -s "$BRIEF" ]` is checked BEFORE the dispatch call
#    and a failure quarantines the lane. THERE IS NO CODE PATH FROM 'no brief' TO
#    'dispatch'. Quarantine is a directory on disk, not a shell variable, so it survives a
#    pump restart — an in-memory skip list would have re-dispatched all 726 after a crash.
#
# 3. VERDICT-TEXT HASH, NOT end_reason. end_reason records how the PROCESS ended; all 726
#    of those dispatches would have carried the same one. The BODY is what proves
#    non-progress: two consecutive identical reviewer verdicts for a lane mean the next
#    dispatch is guaranteed to reproduce the last, so the lane is quarantined instead.
#
# 4. EVERY DISPATCH WRITES A LEDGER ROW. Before this, rework-pump read no ledger and wrote
#    no attempt row, so the shipped 2-retry cap in omniagentos/swarm/scheduler.py governed
#    nothing here. Now each dispatch opens a swarm_attempts row through the existing
#    SwarmDal API (open_attempt -> record_attempt_usage -> close_attempt) carrying the
#    verdict hash, and the SAME cap function the scheduler uses gates the next dispatch.
#
# ARMING: REWORK_DRY_RUN=1 evaluates every gate and prints the machine-readable decision
# plus the full dispatch payload WITHOUT invoking a paid agent. That is how this file is
# tested (tests/acceptance/s04_pump_preflight.sh); the concurrency cap REWORK_MAX is
# unchanged and is not a substitute for any of the above.
#
# `set -e` is deliberately NOT enabled: a long-running pump that exits on the first
# non-zero grep is a new outage, and every failure path below is handled explicitly.
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO"
PY="$REPO/.venv/bin/python"
STAGE="${REWORK_STAGE:-$REPO/var/swarm/sol-verdicts}"
CLONES="${REWORK_CLONES:-$REPO/var/swarm/clones}"
QUARANTINE="${REWORK_QUARANTINE:-$REPO/var/swarm/quarantine}"
MAX="${REWORK_MAX:-25}"
INTERVAL="${REWORK_INTERVAL:-120}"
CYCLES="${REWORK_CYCLES:-0}"
DRY_RUN="${REWORK_DRY_RUN:-0}"
RETRY_CAP="${REWORK_RETRY_CAP:-2}"
LOG="${REWORK_LOG:-$REPO/var/swarm/rework-pump.log}"
DISPATCH_TIMEOUT_S="${REWORK_DISPATCH_TIMEOUT_S:-1800}"
# GNU coreutils timeout: Homebrew/macOS installs it as `gtimeout`, Linux as
# `timeout`. Both accept the same -k/duration syntax; resolve whichever exists.
TIMEOUT_BIN="$(command -v gtimeout || command -v timeout || true)"

# The brief is resolved by CONTENT, not by name: the first candidate that exists and is
# non-empty wins. The list is the union of every convention this fleet has actually used
# (7/29 cohort, 7/30 cohort, and the var/ spelling), and is overridable so a fourth
# convention costs a variable rather than a repeat of the 726-dispatch outage. Note the
# prompt embeds the brief's BODY, so this list can never again be load-bearing for what
# the agent is told — only for whether the lane is dispatchable at all.
BRIEF_CANDIDATES="${REWORK_BRIEF_CANDIDATES:-var/task.md LANE-BRIEF.md var/LANE-BRIEF.md TASK.md}"

LEDGER_SOURCE="real"; [ "$DRY_RUN" = "1" ] && LEDGER_SOURCE="test"

ledger() { "$PY" -W ignore::RuntimeWarning -m omniagentos.swarm.dal "$@"; }

# Deterministic preflight. Fails fast and loudly: a pump that starts against a broken
# ledger would dispatch with no attempt accounting at all, which is the original defect.
preflight() {
  local fatal=0
  [ -x "$PY" ] || { echo "FATAL(preflight): no python at $PY" >&2; fatal=1; }
  [ -d "$CLONES" ] || { echo "FATAL(preflight): clones root missing: $CLONES" >&2; fatal=1; }
  mkdir -p "$STAGE" "$QUARANTINE" "$(dirname "$LOG")" 2>/dev/null
  [ -d "$QUARANTINE" ] || { echo "FATAL(preflight): cannot create $QUARANTINE" >&2; fatal=1; }
  if [ "$fatal" -eq 0 ] && ! ledger pump-hash --verdict-hash preflight >/dev/null 2>&1; then
    echo "FATAL(preflight): swarm ledger CLI unusable (omniagentos.swarm.dal)" >&2; fatal=1
  fi
  [ -n "$TIMEOUT_BIN" ] || { echo "FATAL(preflight): no GNU timeout (gtimeout or timeout) on PATH" >&2; fatal=1; }
  if [ "$DRY_RUN" != "1" ] && ! command -v grok >/dev/null 2>&1; then
    echo "FATAL(preflight): grok CLI not on PATH and REWORK_DRY_RUN is not 1" >&2; fatal=1
  fi
  [ "$fatal" -eq 0 ] || exit 1
}

busy() { pgrep -f 'rework-lane' 2>/dev/null | wc -l | tr -d ' '; }

# --- brief resolution ---------------------------------------------------------------
resolve_brief() {  # lane -> prints absolute path of the first non-empty candidate
  local D="$CLONES/$1" c
  for c in $BRIEF_CANDIDATES; do
    [ -s "$D/$c" ] && { printf '%s\n' "$D/$c"; return 0; }
  done
  return 1
}

# --- quarantine (durable, idempotent) -------------------------------------------------
# A directory, not a variable. `reason` is written once, so re-entering quarantine is
# silent — that is what makes "exactly one QUARANTINE line per lane" true across cycles
# AND across restarts, and it is the observable difference between a pump that remembers
# and a pump that re-decides.
is_quarantined() { [ -f "$QUARANTINE/$1/reason" ]; }

quarantine() {  # lane reason
  local LANE="$1" REASON="$2" Q="$QUARANTINE/$1"
  if is_quarantined "$LANE"; then
    [ "$DRY_RUN" = "1" ] && echo "SKIP lane=$LANE reason=quarantined"
    return 0
  fi
  mkdir -p "$Q" || { echo "  $LANE: FAILED to quarantine (mkdir $Q)" | tee -a "$LOG"; return 1; }
  printf '%s\n' "$REASON" > "$Q/reason"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$Q/first_seen"
  printf '%s\n' "$CLONES/$LANE" > "$Q/lane_dir"
  echo "QUARANTINE lane=$LANE reason=$REASON"
  echo "  $LANE: QUARANTINED ($REASON)" >> "$LOG"
  # A quarantine IS an attempt outcome, so it is recorded like one: end_reason 'blocked'
  # is literally true (the attempt did not proceed) and it counts against the cap.
  ledger pump-attempt --lane "$LANE" --pump rework --provider pump --model quarantine \
    --verdict-body-file "$STAGE/$LANE.md" --end-reason blocked \
    --detail "quarantine:$REASON" --source "$LEDGER_SOURCE" >/dev/null 2>>"$LOG" || true
  return 0
}

# --- prompt construction --------------------------------------------------------------
# PURE FUNCTION OF THE BRIEF'S CONTENTS. The body is inlined; the sha256 of the FULL body
# is printed in the header so that even a brief whose only difference falls past the
# inline cap still produces a different dispatched prompt. The filename appears nowhere
# as an instruction — an agent reading this prompt never needs to find a file on disk to
# know its contract, which is what makes the naming convention permanently non-load-bearing.
build_prompt() {  # lane brief_path verdict_path
  local LANE="$1" BRIEF="$2" V="$3" D="$CLONES/$1"
  local BSHA
  BSHA=$(shasum -a 256 "$BRIEF" 2>/dev/null | awk '{print $1}')
  mkdir -p "$D/var" 2>/dev/null
  cat > "$D/var/rework.md" <<REWORK
# REWORK — your lane was REJECTED by a cross-lineage reviewer. Fix exactly what it named.

You are the same lane, corrected. Your original contract is reproduced IN FULL below —
it still holds. Do not go looking for it on disk; everything binding is in this prompt.

## Your contract, verbatim (sha256 $BSHA)

$(head -800 "$BRIEF")

## The reviewer's verdict, verbatim

$(head -120 "$V" 2>/dev/null)

## What to do

Fix precisely what the reviewer named. Do not expand scope, do not rewrite what it approved.
The common causes across this fleet, in case yours is one of them:

- **out-of-scope edits** — you touched files your contract does not own. REVERT those files
  to their original state. Owning less is not a lesser fix.
- **over-correction** — you made a two-valued return three-valued (added None) without
  fixing its callers. Every caller must handle None EXPLICITLY. A caller using bare
  truthiness on a three-valued return is a NEW instance of the very defect you were fixing:
  None is falsy, so unknown silently takes the false branch.
- **unproven / no failing-on-revert test** — your test does not bind. Revert your production
  change, run the test, and confirm it FAILS. If it still passes, the test is decorative;
  rewrite it against the REQUIREMENT, not against your code. **Print the changed line to
  prove the mutation applied** — a regex that matches nothing yields a green run against
  unmodified code, which has produced false conclusions here repeatedly.
- **clean-but-lazy** — if you reported the surface clean, name what you checked and
  spot-check two plausible defect sites.

## Environment
\`./.venv/bin/pytest\`, \`./.venv/bin/ruff\`. NEVER \`uv run\`/\`npm\` — they write outside your
sandbox and fail. Local task; no web search.

## Constraints
Work only in this directory. Never \`git stash\`; do not push or rebase. You CANNOT commit
(the sandbox denies writes under \`.git\`) — leave the work uncommitted and REPORT it. Do not
edit the repo-root WORKBOOK.md. Never pipe pytest through \`tail\`. If an operation is
refused, report it and STOP.

Report the test output in BOTH directions — fix applied and fix reverted — with real output.

## Far-side proof (MANDATORY — without this the rejection stands)
Write \`var/rework-proof.md\` containing:
1. The reviewer defect restated in one line
2. A shell command in a bash fenced block that demonstrates the defect is GONE (must exit 0)
3. The verbatim command output

The rework pump will RUN that command. Exit non-zero means the rejection stands.
REWORK
}

# --- the agent invocation (background) --------------------------------------------------
rework_exec() {  # lane attempt_id
  local LANE="$1" ATTEMPT="$2" D="$CLONES/$1" V="$STAGE/$1.md"
  local MODEL="grok-4.5"
  # Critical surfaces get grok-4.5; everything else gets the faster model. The standing
  # constraint outranks speed: no gemini on security/isolation/verification surfaces —
  # it once rewrote inode containment as a string prefix with 43 tests passing.
  case "$LANE" in
    *polic*|*scope*|*sessions*|*lease*|*csi*|*gate*|*audit*|*accounts*|*api*) MODEL="grok-4.5" ;;
    *) MODEL="grok-4.5" ;;   # keep grok until a gemini lane is proven on this shape
  esac
  [ -e "$D/.venv" ] || ln -sfn "$REPO/.venv" "$D/.venv"

  # --permission-mode acceptEdits: this was the only `grok -p` in the repo dispatched with
  # --sandbox but NO permission mode. `codex exec` has no such flag (its surface is
  # -s/--sandbox and its -p means --profile) and gemini has only -s/-y, so this belongs
  # here and nowhere else — review-pump.sh's `codex exec` must NOT gain it.
  "$TIMEOUT_BIN" -k 30 "$DISPATCH_TIMEOUT_S" grok -p "$(cat "$D/var/rework.md")" --output-format json --sandbox workspace \
       --permission-mode acceptEdits \
       --cwd "$D" --reasoning-effort high --model "$MODEL" > "$D/var/rework.log" 2>&1
  local RC=$?
  # A rework that produced nothing is recorded, not left silent — same policy as everywhere.
  if [ "$RC" -ne 0 ]; then
    echo "  $LANE: rework FAILED rc=$RC" | tee -a "$LOG"
    local END_REASON=crashed
    # gtimeout returns 137 when -k escalates a TERM-ignoring child to SIGKILL.
    case "$RC" in 124|137) END_REASON=timeout ;; esac
    ledger pump-close --attempt-id "$ATTEMPT" --end-reason "$END_REASON" \
      --detail "rework agent exit rc=$RC" >/dev/null 2>>"$LOG" || true
    return 0
  fi
  # BLOCKING rework-loop fix: exit-0 is a near-side claim. Retire the rejection
  # only when a far-side proof command in var/rework-proof.md runs clean.
  local PROOF="$D/var/rework-proof.md"
  if [ ! -f "$PROOF" ]; then
    echo "  $LANE: rework REJECTED — missing var/rework-proof.md (exit-0 alone is not evidence)" | tee -a "$LOG"
    ledger pump-close --attempt-id "$ATTEMPT" --end-reason review_denied \
      --detail "no var/rework-proof.md" >/dev/null 2>>"$LOG" || true
    return 0
  fi
  local CMD
  CMD=$(
    "$PY" - "$PROOF" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"```(?:bash|sh|shell)?\n(.*?)```", text, re.S)
if m:
    print(m.group(1).strip())
    raise SystemExit(0)
for line in text.splitlines():
    s = line.strip()
    if s.startswith("$ "):
        print(s[2:])
        raise SystemExit(0)
raise SystemExit(1)
PY
  ) || CMD=""
  if [ -z "$CMD" ]; then
    echo "  $LANE: rework REJECTED — rework-proof.md has no runnable command" | tee -a "$LOG"
    ledger pump-close --attempt-id "$ATTEMPT" --end-reason review_denied \
      --detail "rework-proof.md has no runnable command" >/dev/null 2>>"$LOG" || true
    return 0
  fi
  echo "  $LANE: running rework proof" | tee -a "$LOG"
  if ( cd "$D" && bash -lc "$CMD" ) >"$D/var/rework-proof.out" 2>&1; then
    mv "$V" "$STAGE/.reworked-$LANE.md" 2>/dev/null
    echo "  $LANE: reworked, proof GREEN, verdict cleared for re-review" | tee -a "$LOG"
    ledger pump-close --attempt-id "$ATTEMPT" --end-reason completed \
      --detail "far-side proof green" >/dev/null 2>>"$LOG" || true
  else
    echo "  $LANE: rework REJECTED — proof command failed (verdict stands)" | tee -a "$LOG"
    ledger pump-close --attempt-id "$ATTEMPT" --end-reason review_denied \
      --detail "far-side proof command failed" >/dev/null 2>>"$LOG" || true
  fi
}
export -f rework_exec 2>/dev/null || true

# --- main loop --------------------------------------------------------------------------
preflight

cycle=0
while :; do
  cycle=$((cycle+1))
  B=$(busy); FREE=$(( MAX - B )); [ "$FREE" -lt 0 ] && FREE=0
  QFILE=$(mktemp)
  for f in "$STAGE"/*.md; do
    [ -f "$f" ] || continue
    # FAIL is a refusal too. This recognised only REJECT, so three reviewed-and-refused
    # lanes (connectors-sweep, sw-audit, sw-execution) were owned by no pump at all and sat
    # untouched — one spelling of "no" was handled and the other read as silence.
    #
    # REWORK is a THIRD spelling of "no", and it is the one the estate's own reviewers are
    # told to use: opus-critic.md and codex-critic.md both declare
    # `approve | approve-with-changes | rework | unreproducible-findings | inconclusive`.
    # A lane refused as REWORK matched nothing here and was owned by no pump — the same
    # wound as above, reopened by vocabulary rather than by spelling.
    #
    # DELIBERATELY TOLERANT, and not the shared grammar. This decides who to QUEUE, never
    # what to merge, and the two error directions are not symmetric: a false positive costs
    # one wasted rework dispatch, a false negative abandons a refused lane. The shared
    # grammar is stricter, so adopting it here would silently DROP lanes this pattern picks
    # up. Converting this carrier is its own lane, with its own before/after over the live
    # corpus. scripts/bulletin.sh:152 carries the same pattern for the same reason and the
    # two must be edited together — see the note there.
    grep -qiE '^\s*\**\s*#*\s*VERDICT[^A-Za-z]*(REJECT|FAIL|REWORK)' "$f" && basename "$f" .md >> "$QFILE"
  done
  N=$(wc -l < "$QFILE" 2>/dev/null | tr -d ' '); : "${N:=0}"
  echo "[$(date -u +%H:%M:%S)] cycle=$cycle rejected=$N busy=$B free=$FREE" | tee -a "$LOG"

  n=0
  while IFS= read -r LANE; do
    [ "$n" -ge "$FREE" ] && break
    [ -z "$LANE" ] && continue

    # (a) DURABLE QUARANTINE FIRST — read from disk, before any dispatch decision, so a
    #     restarted pump remembers instead of re-deciding.
    if is_quarantined "$LANE"; then
      [ "$DRY_RUN" = "1" ] && echo "SKIP lane=$LANE reason=quarantined"
      continue
    fi
    pgrep -f "rework-lane $LANE" >/dev/null 2>&1 && continue

    D="$CLONES/$LANE"
    # (b) a lane with no clone can never be reworked; it is quarantined, not skipped,
    #     because a silent skip is how a lane ends up owned by no pump at all.
    [ -d "$D/.git" ] || { quarantine "$LANE" missing-clone; continue; }

    # (c) HARD PRECONDITION: no brief -> quarantine. The `continue` IS the guarantee —
    #     there is no path from this branch to a dispatch.
    BRIEF=$(resolve_brief "$LANE") || BRIEF=""
    [ -s "$BRIEF" ] || { quarantine "$LANE" missing-brief; continue; }

    # (d) the gate the scheduler's own retry cap reads: an identical verdict body twice in
    #     a row, or attempts already spent, terminate the loop instead of feeding it.
    GATE=$(ledger pump-gate --lane "$LANE" --verdict-body-file "$STAGE/$LANE.md" \
             --retry-cap "$RETRY_CAP" 2>>"$LOG")
    case "$GATE" in
      *"decision=blocked"*)
        REASON=$(printf '%s' "$GATE" | sed -n 's/.*reason=\([a-z-]*\).*/\1/p')
        quarantine "$LANE" "${REASON:-gate-blocked}"; continue ;;
      *"decision=allow"*) : ;;
      *)
        # An unreadable gate is a REFUSAL, never an implicit allow. The original defect was
        # exactly this shape: absence of a signal read as permission.
        echo "  $LANE: gate unreadable — refusing to dispatch" | tee -a "$LOG"; continue ;;
    esac

    build_prompt "$LANE" "$BRIEF" "$STAGE/$LANE.md"

    if [ "$DRY_RUN" = "1" ]; then
      echo "DISPATCH lane=$LANE"
      echo "--- DISPATCH-PAYLOAD BEGIN lane=$LANE ---"
      cat "$D/var/rework.md"
      echo "--- DISPATCH-PAYLOAD END lane=$LANE ---"
      ledger pump-attempt --lane "$LANE" --pump rework --provider grok --model grok-4.5 \
        --verdict-body-file "$STAGE/$LANE.md" --end-reason blocked \
        --detail "dry-run: dispatch payload built, no agent invoked" \
        --source test >/dev/null 2>>"$LOG" || true
      n=$((n+1)); continue
    fi

    ATT=$(ledger pump-attempt --lane "$LANE" --pump rework --provider grok --model grok-4.5 \
            --verdict-body-file "$STAGE/$LANE.md" --keep-open --source real 2>>"$LOG" \
          | sed -n 's/^ATTEMPT id=\([^ ]*\).*/\1/p')
    if [ -z "$ATT" ]; then
      # No ledger row means no attempt accounting, which is the exact condition that
      # produced 726 dispatches. Refuse rather than dispatch unaccounted.
      echo "  $LANE: ledger refused the attempt — NOT dispatching" | tee -a "$LOG"; continue
    fi
    echo "DISPATCH lane=$LANE"
    ( exec -a "rework-lane $LANE" bash -c "$(declare -f rework_exec ledger); \
        REPO='$REPO' PY='$PY' CLONES='$CLONES' STAGE='$STAGE' LOG='$LOG' DISPATCH_TIMEOUT_S='$DISPATCH_TIMEOUT_S' TIMEOUT_BIN='$TIMEOUT_BIN'; \
        rework_exec '$LANE' '$ATT'" ) &
    n=$((n+1)); sleep 0.2
  done < "$QFILE"
  rm -f "$QFILE"
  echo "  dispatched $n rework coder(s)" | tee -a "$LOG"

  [ "$CYCLES" != "0" ] && [ "$cycle" -ge "$CYCLES" ] && break
  # Dry-run still PACES itself (REWORK_DRY_INTERVAL, default 0): a preflight that spins
  # hot would make "kill it and restart it" untestable and would bury the decision lines.
  if [ "$DRY_RUN" = "1" ]; then sleep "${REWORK_DRY_INTERVAL:-0}"; else sleep "$INTERVAL"; fi
done
