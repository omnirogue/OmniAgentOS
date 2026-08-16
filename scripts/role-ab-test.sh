#!/usr/bin/env bash
# A/B/C test of role FORMATIONS, on identical evidence, every cycle.
#
#   A  one fable-low answering BOTH questions
#   B  two fable-low agents, one question each (no shared context)
#   C  one fable-xhigh answering BOTH
#
# The question being tested is not "which model is smarter" but "which FORMATION
# produces the best immediately-actionable advice per unit of cost". A single agent
# holding both questions can see that the answers interact — "we have idle slots"
# and "verification is the constraint" combine into "spend the slots on verifiers,
# not coders". Two isolated agents cannot see that, but each has full attention on
# its own question. That trade is exactly what this measures.
#
# Fairness: all arms get the SAME snapshot, generated once per cycle, so they are
# answering about the same fleet. Anything else would compare states, not models.
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO"
PY="$REPO/.venv/bin/python"
OUT="$REPO/var/swarm/role-ab"
INTERVAL="${ROLE_INTERVAL:-120}"
CYCLES="${ROLE_CYCLES:-0}"
mkdir -p "$OUT"

Q1='**Q1. Are we still running maximum parallel?** If not, say exactly what to dispatch and how many. If we are at capacity, say so and say why more would not help.'
Q2='**Q2. Where is the bottleneck, and what do we need to eliminate it?** Naming the constraint is half the job — say what action removes it. If the constraint is structural rather than a shortage of agents, say that; it is more useful than "add more agents".'
RULES='
Rules: ground every claim in a number from the snapshot; do not invent state. Prefer ONE
decisive action over a list of five. If the honest answer is "hold, do nothing", say it — a
role that always finds work manufactures busywork. Max 250 words. Terse beats thorough.'

snapshot() {
  "$PY" scripts/fleet-ledger.py scan >/dev/null 2>&1
  {
    echo "# Fleet snapshot"
    echo '```'
    "$PY" scripts/fleet-ledger.py summary 2>/dev/null
    echo '```'
    echo "- our live agent processes: $(pgrep -f 'codex exec|claude -p|grok -p|qwen -p|kimi -p' 2>/dev/null | wc -l | tr -d ' ')"
    echo "- machine: $(sysctl -n hw.ncpu) cores, load $(uptime | sed 's/.*averages*: *//')"
    echo "- NOTE: another session may be running its own test; load is not all ours."
    echo
    echo "## Lanes awaiting a verdict"
    echo '```'
    "$PY" scripts/fleet-ledger.py query awaiting_verdict 2>/dev/null | head -10
    echo '```'
    cat <<'CTX'
## Context
- Coders are non-Anthropic; verifiers are opus. Nothing merges without a recorded
  Anthropic verdict (scripts/merge-gate.sh enforces it).
- Agents CANNOT commit — the sandbox denies writes under .git. The coordinator lands work
  via scripts/land-lane.sh --commit. Policy, not a bug to route around.
- A verifier that produces nothing now records an explicit FAILED verdict
  (scripts/dispatch-verifier.sh), so dead runs are re-dispatchable rather than invisible.
- Recurring defect class, in product AND tooling: a non-result presented as a favourable
  result — "the absence of the claimed effect was interpreted as evidence for the effect".
CTX
  }
}

ask() {  # model, effort-flag, prompt, outfile
  local model="$1" eff="$2" prompt="$3" out="$4"
  # `--effort` is a SESSION flag and must precede `-p`. Placed after, the CLI rejects it
  # with "unknown option" — which silently broke arm C twice, so the low-vs-xhigh
  # comparison never actually ran. A broken arm reads as a model that underperformed.
  # shellcheck disable=SC2086
  claude $eff -p "$prompt" --model "$model" --permission-mode acceptEdits \
    --allowedTools "Read" > "$out" 2>&1 || true
}

cycle=0
while :; do
  cycle=$((cycle+1))
  TS=$(date -u +%H%M%S)
  SNAP="$OUT/snapshot-$TS.md"
  snapshot > "$SNAP"
  echo "=== cycle $cycle ($TS) ==="

  # Arm A — one fable-low, both questions (can see the interaction between them)
  ask claude-fable-5 "" "$(cat "$SNAP")

$Q1

$Q2
$RULES" "$OUT/A-both-low-$TS.md" &
  PA=$!

  # Arm B — two fable-low, one question each (full attention, no shared context)
  ask claude-fable-5 "" "$(cat "$SNAP")

$Q1
$RULES" "$OUT/B-q1-low-$TS.md" &
  PB1=$!
  ask claude-fable-5 "" "$(cat "$SNAP")

$Q2
$RULES" "$OUT/B-q2-low-$TS.md" &
  PB2=$!

  # Arm C — one fable-xhigh, both questions
  ask claude-fable-5 "--effort xhigh" "$(cat "$SNAP")

$Q1

$Q2
$RULES" "$OUT/C-both-xhigh-$TS.md" &
  PC=$!

  wait $PA $PB1 $PB2 $PC 2>/dev/null
  for f in "$OUT"/A-both-low-$TS.md "$OUT"/B-q1-low-$TS.md "$OUT"/B-q2-low-$TS.md "$OUT"/C-both-xhigh-$TS.md; do
    printf '  %-28s %s bytes\n' "$(basename "$f")" "$(wc -c < "$f" 2>/dev/null || echo 0)"
  done
  echo "  latest cycle: $TS"
  echo "$TS" > "$OUT/LATEST"

  [ "$CYCLES" != "0" ] && [ "$cycle" -ge "$CYCLES" ] && break
  sleep "$INTERVAL"
done
