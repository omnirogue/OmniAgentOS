#!/usr/bin/env bash
# The cheap, fast overseer. Answers ONE question the mechanical rules cannot:
#
#     "What needs doing that no process owns?"
#
# WHERE THIS SITS
# ---------------
# `bulletin.sh` handles everything that can be decided by a rule: dead pumps get revived,
# orphaned rejections get adopted, capacity gets counted twice and reported as `unknown`
# when the probes disagree. That is the right layer for anything mechanically decidable, and
# it needs no model at all.
#
# But every gap found tonight was invisible to every rule that existed at the time, because
# a rule only covers a case someone already thought of:
#
#   - Anthropic rejections landed in a directory no pump watched
#   - `FAIL` was not recognised as a refusal because the picker matched only `REJECT`
#   - the swarm executor committed onto whatever branch the coordinator was standing on
#   - a lane that COMMITTED its work was classified as a lane that had done nothing
#
# None of those were empty slots. Each was work stranded somewhere nothing was looking. That
# is a judgement call, it is cheap, and it wants a fast model rather than a smart one.
#
# ITS OUTPUT IS ADVISORY AND SAYS SO
# A model's claim is not a witness. The overseer therefore writes to its OWN section, clearly
# marked, and can only ever raise a question — it never edits the mechanical sections, never
# dispatches, and never marks anything approved. If it is wrong, it has wasted a glance; it
# cannot put anything into main.
#
#   overseer.sh once
#   overseer.sh loop     # every $OVERSEER_INTERVAL seconds (default 300)
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO" || exit 1
PY="$REPO/.venv/bin/python"
OUT="$REPO/var/swarm/OVERSEER.md"
LOG="$REPO/var/swarm/overseer.log"
INTERVAL="${OVERSEER_INTERVAL:-300}"
mkdir -p "$REPO/var/swarm"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >> "$LOG"; }

once() {
  local LEDGER PUMPS CAP RECENT PROMPT

  LEDGER=$("$PY" "$REPO/scripts/fleet-ledger.py" summary 2>/dev/null)
  PUMPS=""
  for p in review-pump rework-pump sim-pump verdict-pump fleet-supervisor plan-consolidator bulletin; do
    if pgrep -f "$p.sh" >/dev/null 2>&1; then PUMPS="$PUMPS
  up   $p"; else PUMPS="$PUMPS
  DOWN $p"; fi
  done
  CAP=""
  for b in grok codex gemini claude; do
    CAP="$CAP
  $b: $(pgrep -x "$b" 2>/dev/null | wc -l | tr -d ' ') running"
  done
  RECENT=$(tail -25 "$REPO/var/swarm/bulletin.log" 2>/dev/null)

  PROMPT="You are the fleet OVERSEER for a multi-agent build system. You are fast and cheap
on purpose. Answer ONE question and nothing else:

    What needs doing that no process currently owns?

You are NOT asked whether slots are empty — a script already counts that, and an idle fleet
with a genuinely empty queue is a legitimate state, not a problem to invent work for. You
are asked to spot work that is STRANDED: finished, refused, or blocked somewhere nothing is
watching.

Real examples from this system, each invisible to every rule that existed at the time:
  - lanes rejected by a final reviewer landed in a directory no pump read
  - a picker matched the word REJECT but not FAIL, so refused lanes sat unowned
  - a lane that committed its work was counted as a lane that had produced nothing
  - a simulation committed onto whatever branch the coordinator was standing on

LANE LEDGER (mechanically derived, trust it):
$LEDGER

LOOPS:$PUMPS

AGENTS RUNNING:$CAP

RECENT COORDINATOR ACTIONS:
$RECENT

Reply with AT MOST 5 bullets, each one line, in this exact form:
  - <what is stranded> -> <the single command or action that would unblock it>

If nothing is stranded, reply with exactly: NOTHING STRANDED
Do not explain. Do not summarise the state back. Do not praise anything."

  local ANS RC
  # gemini-flash: fast, and this is advisory coordination rather than a security or
  # verification surface, where a stronger model is required.
  ANS=$(printf '%s' "$PROMPT" | gemini -m gemini-3.6-flash 2>/dev/null)
  RC=$?

  {
    echo "# OVERSEER — advisory only"
    echo
    echo "Written by \`scripts/overseer.sh\` at $(date -u +%Y-%m-%dT%H:%M:%SZ) using gemini-3.6-flash."
    echo
    echo "**This section is a QUESTION, not a finding.** A model's claim is not a witness."
    echo "Nothing here has been verified, and nothing here dispatches, approves or merges."
    echo "The mechanical sections of \`BULLETIN.md\` are the authority; this only points."
    echo
    if [ "$RC" -ne 0 ] || [ -z "$ANS" ]; then
      # An overseer that could not run reports that it could not run. Silence from a
      # failed probe must never read as "nothing stranded".
      echo "## UNAVAILABLE"
      echo
      echo "The overseer could not run (rc=$RC). This is NOT a report that nothing is stranded —"
      echo "it is the absence of a report. Treat it as unknown."
      log "UNAVAILABLE rc=$RC"
    else
      printf '%s\n' "$ANS"
      log "reported $(printf '%s' "$ANS" | grep -c '^\s*-') item(s)"
    fi
  } > "$OUT.tmp" && mv "$OUT.tmp" "$OUT"

  echo "overseer: ${OUT#"$REPO"/}"
  grep -E '^\s*-|^NOTHING STRANDED|^## UNAVAILABLE' "$OUT" | head -6
}

case "${1:-once}" in
  once) once ;;
  loop) log "overseer loop up (${INTERVAL}s)"; while :; do once >/dev/null 2>&1; sleep "$INTERVAL"; done ;;
  show) cat "$OUT" 2>/dev/null || echo "no overseer report yet" ;;
  *)    echo "usage: overseer.sh [once|loop|show]" >&2; exit 2 ;;
esac
