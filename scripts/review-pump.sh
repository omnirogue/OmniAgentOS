#!/usr/bin/env bash
# TWO-TIER REVIEW. gpt-5.6 first-pass at scale; opus once, on the aggregate.
#
# The bottleneck was Opus. 40 lanes sat blocked because every lane needed an Anthropic
# verdict and Anthropic is our scarcest provider. the operator's fix inverts it: put the plentiful
# provider on the per-lane pass and the scarce one on the aggregate.
#
#   grok-4.5 (xai)   codes a lane
#   gpt-5.6  (openai) reviews that lane          <- 40 concurrent, cheap, abundant
#   gpt-5.6  merges approved lanes -> integration/reviewed
#   opus     (anthropic) reviews the INTEGRATION  <- once, not 40 times
#
# Opus workload drops ~40x, and the lineage chain improves: three families, no same-lineage
# pair anywhere. That is stronger than the previous xai->anthropic pairing, not weaker.
#
# Reviewing the aggregate is also the CORRECT granularity, independently of cost: the two
# worst defects tonight were SEAM defects, invisible in any single lane and only visible
# where lanes meet. A per-lane reviewer structurally cannot see them.
#
# What does NOT change: the merge gate still demands an Anthropic verdict before anything
# reaches main. It now demands one verdict on the integration rather than forty on the
# parts, which is the same guarantee at a defensible cost.
#
# ==============================================================================
# S04 — SAME BRIEF BUG, SAME LEDGER GAP
# ==============================================================================
# This pump's prompt said "Read var/task.md for the contract" — the identical
# filename assumption that cost rework-pump 726 dead dispatches, because 22 of 70 clones
# use LANE-BRIEF.md and 3 have no brief at all. The contract is now INLINED: a reviewer
# reading this prompt never has to find a file to know what it is judging against, so the
# naming convention is no longer load-bearing, and a lane with NO resolvable brief is
# quarantined instead of dispatched (a reviewer with no contract cannot review; it can
# only guess, and a guessed verdict is worse than no verdict).
#
# LEDGER: this pump READ the fleet ledger (`fleet-ledger.py scan/query`) and wrote NO
# attempt row, so the shipped 2-retry cap governed nothing here either. Every dispatch now
# writes a swarm_attempts row through the existing SwarmDal API, and the SAME gate function
# the scheduler's retry cap uses decides whether the next one is allowed.
#
# NOTE ON `codex exec`: it takes -s/--sandbox and its -p means --profile. It has NO
# --permission-mode. That flag was added to rework-pump.sh's `grok -p` and must NOT be
# copied here.
#
# ARMING: REWORK_DRY_RUN=1 (one switch for all four pumps) evaluates every gate and prints
# the decision plus the full dispatch payload without invoking a paid reviewer.
#
# `set -e` is deliberately NOT enabled: this loop must survive a non-zero grep.
set -uo pipefail
# Shared verdict-line grammar. Resolved before `cd` and fail-CLOSED: an unset
# VERDICT_LINE_RE is an empty pattern, and an empty pattern matches everything.
# It is EXPORTED by the library because review_exec runs inside a `bash -c` that
# inherits the environment, not this shell's variables.
_VERDICT_GRAMMAR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/verdict-grammar.sh"
# shellcheck source=scripts/lib/verdict-grammar.sh
# Loaded AND non-empty: a truncated library would leave the patterns empty, and an
# empty grep pattern matches every line — the most fail-open state available here.
. "$_VERDICT_GRAMMAR" 2>/dev/null && [ -n "${VERDICT_LINE_RE:-}" ] && [ -n "${VERDICT_APPROVE_RE:-}" ] \
  && command -v verdict_decision >/dev/null 2>&1 || {
  echo "FATAL: verdict grammar unusable ($_VERDICT_GRAMMAR)" >&2; exit 1; }
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO"
PY="$REPO/.venv/bin/python"
MAX="${REVIEW_MAX:-400}"
INTERVAL="${REVIEW_INTERVAL:-6}"
CYCLES="${REVIEW_CYCLES:-0}"
LOG="${REVIEW_LOG:-$REPO/var/swarm/review-pump.log}"
STAGE="${REVIEW_STAGE:-$REPO/var/swarm/sol-verdicts}"
CLONES="${REVIEW_CLONES:-$REPO/var/swarm/clones}"
QUARANTINE="${REVIEW_QUARANTINE:-$REPO/var/swarm/quarantine}"
DRY_RUN="${REWORK_DRY_RUN:-0}"
RETRY_CAP="${REVIEW_RETRY_CAP:-2}"
DISPATCH_TIMEOUT_S="${REVIEW_DISPATCH_TIMEOUT_S:-1800}"
# GNU coreutils timeout: Homebrew/macOS installs it as `gtimeout`, Linux as
# `timeout`. Both accept the same -k/duration syntax; resolve whichever exists.
TIMEOUT_BIN="$(command -v gtimeout || command -v timeout || true)"
BRIEF_CANDIDATES="${REWORK_BRIEF_CANDIDATES:-var/task.md LANE-BRIEF.md var/LANE-BRIEF.md TASK.md}"
QUEUE_CMD="${REVIEW_QUEUE_CMD:-\"$PY\" scripts/fleet-ledger.py query awaiting_verdict}"
LEDGER_SOURCE="real"; [ "$DRY_RUN" = "1" ] && LEDGER_SOURCE="test"
mkdir -p "$STAGE"

ledger() { "$PY" -W ignore::RuntimeWarning -m omniagentos.swarm.dal "$@"; }

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
  if [ "$DRY_RUN" != "1" ] && ! command -v codex >/dev/null 2>&1; then
    echo "FATAL(preflight): codex CLI not on PATH and REWORK_DRY_RUN is not 1" >&2; fatal=1
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
  echo "QUARANTINE lane=$LANE reason=$REASON"
  echo "  $LANE: QUARANTINED ($REASON)" >> "$LOG"
  ledger pump-attempt --lane "$LANE" --pump review --provider pump --model quarantine \
    --verdict-hash "quarantine:$REASON" --end-reason blocked \
    --detail "quarantine:$REASON" --source "$LEDGER_SOURCE" >/dev/null 2>>"$LOG" || true
  return 0
}

# The review prompt, with the lane's contract INLINED. Written to a file so the exact
# bytes that are dispatched are also the bytes that are hashed and (in dry-run) printed —
# a prompt built twice is a prompt that can differ twice.
build_prompt() {  # lane brief_path out_path
  local LANE="$1" BRIEF="$2" OUTF="$3" BSHA
  BSHA=$(shasum -a 256 "$BRIEF" 2>/dev/null | awk '{print $1}')
  cat > "$OUTF" <<PROMPT
# FIRST-PASS REVIEWER (gpt-5.6-sol, openai). The coder was grok-4.5 (xai) — different lineage.

Do NOT re-implement. The contract you are judging against is reproduced IN FULL below;
do not go looking for it on disk.

## The lane's contract, verbatim (sha256 $BSHA)

$(head -800 "$BRIEF")

## How to verify

Use ./.venv/bin/pytest and ./.venv/bin/ruff. NEVER \`uv run\`/\`npm\` — they write outside the
sandbox and fail. Local task; no web search.

Report REAL OUTPUT for each:
1. Run the lane's tests. Exact pass/fail line.
2. For EVERY claimed fix: revert it while KEEPING its test, and confirm the test FAILS.
   **Print the changed line to prove the mutation applied BEFORE trusting the result.**
   A regex that matches nothing yields a green run against UNMODIFIED code — that has
   produced four false conclusions in this repo. If a revert does not move the tests, the
   fix is unproven; say so.
3. Restore byte-for-byte; confirm green.
4. OVER-CORRECTION: if a two-valued return became three-valued (None added), enumerate
   every caller and confirm each handles None EXPLICITLY. Bare truthiness on a three-valued
   return is a NEW instance of the same defect — None is falsy, so unknown silently takes
   the false branch.
5. Scope: only the contracted files. Report out-of-scope edits.
6. If the lane claims the surface was CLEAN, check that is honest rather than lazy: name
   what it checked and spot-check two plausible defect sites yourself.

Governing filter: unknown, absent and unparseable must never render as good.

Write your verdict to var/sol-verdict.md (inside this working directory). It MUST begin a line with 'VERDICT:' followed by APPROVE,
APPROVE-WITH-NOTES, or REJECT, name you (gpt-5.6-sol), and carry the evidence.

A claimed fix with no failing-on-revert test is a REJECT regardless of how many tests pass.
Do NOT commit.
PROMPT
}

review_exec() {  # lane attempt_id prompt_file
  local LANE="$1" ATTEMPT="$2" PROMPTF="$3" D="$CLONES/$1"
  # codex exec --cd "$D" sandboxes ALL writes to $D. Telling it to write into
  # var/swarm/sol-verdicts (outside the lane) produced rc=0 with no file — 39 of 39
  # "FAILED", which was my error, not the reviewer's. Sol writes INSIDE the lane; the
  # unsandboxed pump copies it out.
  local INNER="$D/var/sol-verdict.md"
  local OUT="$STAGE/$LANE.md"
  rm -f "$INNER"
  [ -e "$D/.venv" ] || ln -sfn "$REPO/.venv" "$D/.venv"

  # Hard cap provider hangs; this backstops the telemetry reaper's flag-never-kill policy.
  "$TIMEOUT_BIN" -k 30 "$DISPATCH_TIMEOUT_S" codex exec --cd "$D" -m gpt-5.6-sol -s workspace-write - > "$D/var/sol-review.log" 2>&1 < "$PROMPTF"
  local RC=$?
  [ -s "$INNER" ] && cp "$INNER" "$OUT"
  # Same policy as the Anthropic wrapper: a review that produced nothing is FAILED, never
  # silence. An unwritten verdict is not a pending verdict.
  if [ "$RC" -ne 0 ] || [ "$(verdict_decision "$OUT")" = "NONE" ]; then
    printf '# %s\n\nVERDICT: FAILED (first-pass reviewer produced no usable verdict, rc=%s)\n\nReviewer: gpt-5.6-sol. An absent verdict is not a clean one; re-dispatch.\n' \
      "$LANE" "$RC" > "$OUT"
    echo "  $LANE: FAILED (rc=$RC)" | tee -a "$LOG"
    local END_REASON=crashed
    # gtimeout returns 137 when -k escalates a TERM-ignoring child to SIGKILL.
    case "$RC" in 124|137) END_REASON=timeout ;; esac
    ledger pump-close --attempt-id "$ATTEMPT" --end-reason "$END_REASON" \
      --detail "reviewer produced no usable verdict rc=$RC" >/dev/null 2>>"$LOG" || true
  else
    echo "  $LANE: $(verdict_line "$OUT" | cut -c1-46)" | tee -a "$LOG"
    ledger pump-close --attempt-id "$ATTEMPT" --end-reason completed \
      --detail "verdict written" >/dev/null 2>>"$LOG" || true
  fi
}
export -f review_exec 2>/dev/null || true

busy() { pgrep -f 'sol-review-lane' 2>/dev/null | wc -l | tr -d ' '; }

preflight

cycle=0
while :; do
  cycle=$((cycle+1))
  "$PY" scripts/fleet-ledger.py scan >/dev/null 2>&1
  B=$(busy); FREE=$(( MAX - B )); [ "$FREE" -lt 0 ] && FREE=0

  QFILE=$(mktemp)
  # The queue SOURCE is configurable (REVIEW_QUEUE_CMD); the default is the fleet ledger.
  # One named seam beats a second hidden code path — the preflight suite drives the pump
  # through exactly the production branch instead of a test-only shortcut around it.
  eval "$QUEUE_CMD" 2>/dev/null \
    | grep -vE '^\(' | awk '{print $1}' | grep -v '^$' > "$QFILE" || true
  echo "[$(date -u +%H:%M:%S)] cycle=$cycle queued=$(wc -l < "$QFILE" | tr -d ' ') sol_busy=$B free=$FREE" | tee -a "$LOG"

  n=0
  while IFS= read -r LANE; do
    [ "$n" -ge "$FREE" ] && break
    [ -z "$LANE" ] && continue
    if is_quarantined "$LANE"; then
      [ "$DRY_RUN" = "1" ] && echo "SKIP lane=$LANE reason=quarantined"
      continue
    fi
    [ -s "$STAGE/$LANE.md" ] && continue          # already first-pass reviewed
    pgrep -f "sol-review-lane $LANE" >/dev/null 2>&1 && continue

    D="$CLONES/$LANE"
    [ -d "$D/.git" ] || { quarantine "$LANE" missing-clone; continue; }

    # HARD PRECONDITION: no contract -> no review. No path from here to a dispatch.
    BRIEF=$(resolve_brief "$LANE") || BRIEF=""
    [ -s "$BRIEF" ] || { quarantine "$LANE" missing-brief; continue; }

    mkdir -p "$D/var" 2>/dev/null
    PROMPTF="$D/var/sol-review-prompt.md"
    build_prompt "$LANE" "$BRIEF" "$PROMPTF"

    GATE=$(ledger pump-gate --lane "$LANE" --verdict-body-file "$PROMPTF" \
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
      cat "$PROMPTF"
      echo "--- DISPATCH-PAYLOAD END lane=$LANE ---"
      ledger pump-attempt --lane "$LANE" --pump review --provider codex --model gpt-5.6-sol \
        --verdict-body-file "$PROMPTF" --end-reason blocked \
        --detail "dry-run: review payload built, no reviewer invoked" \
        --source test >/dev/null 2>>"$LOG" || true
      n=$((n+1)); continue
    fi

    ATT=$(ledger pump-attempt --lane "$LANE" --pump review --provider codex \
            --model gpt-5.6-sol --verdict-body-file "$PROMPTF" --keep-open --source real \
            2>>"$LOG" | sed -n 's/^ATTEMPT id=\([^ ]*\).*/\1/p')
    if [ -z "$ATT" ]; then
      echo "  $LANE: ledger refused the attempt — NOT dispatching" | tee -a "$LOG"; continue
    fi
    echo "DISPATCH lane=$LANE"
    ( exec -a "sol-review-lane $LANE" bash -c "$(declare -f review_exec ledger); \
        REPO='$REPO' PY='$PY' CLONES='$CLONES' STAGE='$STAGE' LOG='$LOG' DISPATCH_TIMEOUT_S='$DISPATCH_TIMEOUT_S' TIMEOUT_BIN='$TIMEOUT_BIN'; \
        review_exec '$LANE' '$ATT' '$PROMPTF'" ) &
    n=$((n+1)); sleep 0.2
  done < "$QFILE"
  rm -f "$QFILE"
  [ "$n" = "0" ] && echo "  nothing to review" | tee -a "$LOG" || echo "  dispatched $n sol reviewers" | tee -a "$LOG"

  [ "$CYCLES" != "0" ] && [ "$cycle" -ge "$CYCLES" ] && break
  if [ "$DRY_RUN" = "1" ]; then sleep "${REWORK_DRY_INTERVAL:-0}"; else sleep "$INTERVAL"; fi
done
