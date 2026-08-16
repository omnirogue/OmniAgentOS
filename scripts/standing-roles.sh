#!/usr/bin/env bash
# Standing role-agents — each owns ONE question, fires on a timer, produces an ACTION.
#
# the operator's diagnosis, 2026-07-29: "notice how I have to keep prodding to keep everyone working,
# just like with my employees."
#
# He was right, and the reason is structural: a supervisor that MEASURES has no drive. It
# writes a status file and stops. Every time the fleet went idle, the drive came from the
# operator asking "are we still running maximum parallel?" — so the operator was the
# scheduler, which does not scale and does not sleep.
#
# A standing role is a cheaper unit of ownership than a task: it never completes, so it never
# needs re-dispatching, and because it holds ONE question its context stays small no matter
# how large the fleet grows.
#
# Every role reads `scripts/fleet-ledger.py`, which derives state by EXECUTING things
# (git status, mtimes, pgrep, parsing verdict files) rather than asking agents how they are
# doing. A role built on self-reports would inherit exactly the failure it exists to catch.
#
# Roles PROPOSE. They do not dispatch, merge, or kill — an autonomous actor that acts on a
# heuristic is how real work gets lost. Proposals land in var/swarm/role-log.jsonl for the
# coordinator, and are timestamped so formations can be compared after the fact.
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO"
PY="$REPO/.venv/bin/python"
LOG="$REPO/var/swarm/role-log.jsonl"
INTERVAL="${ROLE_INTERVAL:-120}"
CYCLES="${ROLE_CYCLES:-0}"          # 0 = until killed
FORMATION="${FORMATION:-flat-fanout}"  # recorded so formations can be compared
CORES=$(sysctl -n hw.ncpu 2>/dev/null || echo 8)

# CAPACITY — corrected twice, and the second correction is the important one.
#
# First error: a "measured knee of ~1x cores" was CONFOUNDED by another session running
# its own parallelism test, so shared load was attributed to our fleet.
#
# Second error, worse: cores were never the constraint at all. MEASURED with 24 agents
# live — total CPU 0.0%, total RSS 0.6GB (~25MB each). Agent processes are I/O-bound,
# blocked on remote APIs essentially all of the time. Sizing a fleet of network-waiters
# by CPU cores is measuring the wrong resource, and it produced a ceiling ~4x too low.
#
# The real constraint is PER-PROVIDER API capacity, which SHARDS: anthropic, openai,
# xai, google, moonshot and openrouter each have independent limits, and an anthropic
# session limit does not slow a grok agent. So capacity is the SUM of per-provider
# headroom, not one machine-wide number, and the way to raise it is to spread across
# providers rather than to add processes on one.
#
# Local resources bound the total far higher than any provider does: at ~25MB/agent,
# 100 agents is ~2.5GB — trivial on this box.
PROV_CAP="${PROV_CAP:-20}"        # concurrent agents per provider before that provider throttles
count_provider() { pgrep -f "$1" 2>/dev/null | wc -l | tr -d ' '; }
N_ANTHROPIC=$(count_provider 'claude5 -p')
N_OPENAI=$(count_provider 'codex exec')
N_XAI=$(count_provider 'grok -p')
N_MOONSHOT=$(count_provider 'kimi -p')
N_GOOGLE=$(count_provider 'gemini -p')
OUR_PROCS=$(( N_ANTHROPIC + N_OPENAI + N_XAI + N_MOONSHOT + N_GOOGLE ))
SAFE_MAX=$(( PROV_CAP * 5 ))
EXTERNAL=0

emit() {  # role, question, finding, action
  "$PY" - "$1" "$2" "$3" "$4" "$FORMATION" <<'PY' >> "$LOG"
import json, sys, time
role, question, finding, action, formation = sys.argv[1:6]
print(json.dumps({"ts": int(time.time()), "formation": formation, "role": role,
                  "question": question, "finding": finding, "action": action}))
PY
  printf '[%s] %s\n    finding: %s\n    ACTION : %s\n' "$1" "$2" "$3" "$4"
}

cycle=0
while :; do
  cycle=$((cycle+1))
  "$PY" scripts/fleet-ledger.py scan >/dev/null 2>&1
  SUM=$("$PY" scripts/fleet-ledger.py summary 2>/dev/null)
  live=$(pgrep -f 'codex exec|claude5 -p|grok -p|qwen -p|kimi -p' 2>/dev/null | wc -l | tr -d ' ')
  load=$(uptime | sed 's/.*averages*: *//' | awk '{print int($1)}')
  get() { echo "$SUM" | awk -v k="$1" '$1==k{print $2}'; }
  working=$(get working); awaiting=$(get awaiting_verdict); mergeable=$(get mergeable)
  silent=$(get silent); empty=$(get empty)
  : "${working:=0}" "${awaiting:=0}" "${mergeable:=0}" "${silent:=0}" "${empty:=0}"

  echo "=== cycle $cycle | formation=$FORMATION live=$live load=$load ==="

  # ---- ROLE 1: "Are we still running maximum parallel?" ----------------------
  # Maximum is not a wish, it is a measured knee. Over-dispatching past it kills
  # processes and lowers throughput, which reads as "more parallelism" while
  # producing less.
  if [ "$EXTERNAL" -gt 4 ] && [ "$live" -lt "$SAFE_MAX" ]; then
    emit "parallel-improver" "Are we still running maximum parallel?" \
      "load=$load but only $live are OURS — ~$EXTERNAL is EXTERNAL (another session/process)" \
      "Our fleet is NOT the constraint. Do not shrink it. To eliminate: find the other producer and decide whether it or we should yield."
  elif [ "$load" -gt "$(( SAFE_MAX + 4 ))" ]; then
    emit "parallel-improver" "Are we still running maximum parallel?" \
      "live=$live load=$load exceeds headroom (~$SAFE_MAX = $CORES cores - $EXTERNAL external)" \
      "DO NOT dispatch. Over-subscription killed 3 lanes (exit 144) and cost 26 of 32 verifier outputs. Let load fall first."
  elif [ "$live" -lt "$(( SAFE_MAX - 4 ))" ]; then
    SLOTS=$(( SAFE_MAX - live ))
    if [ "$awaiting" -gt 0 ]; then
      emit "parallel-improver" "Are we still running maximum parallel?" \
        "$SLOTS idle slots; $awaiting lane(s) finished and blocked on a verdict" \
        "Dispatch $(( SLOTS < awaiting ? SLOTS : awaiting )) opus verifier(s) — verified work is worth more than new work."
    elif [ "$empty" -gt 3 ]; then
      emit "parallel-improver" "Are we still running maximum parallel?" \
        "$SLOTS idle slots; no work waiting; $empty reclaimable lanes" \
        "Dispatch new coder lanes, and reclaim $empty empty lanes to free disk."
    else
      emit "parallel-improver" "Are we still running maximum parallel?" \
        "$SLOTS idle slots and no queued work" \
        "Backlog is dry — generate work: next queue item, or a sweep of an unswept surface."
    fi
  else
    emit "parallel-improver" "Are we still running maximum parallel?" \
      "live=$live vs capacity ~$SAFE_MAX (anthropic=$N_ANTHROPIC openai=$N_OPENAI xai=$N_XAI moonshot=$N_MOONSHOT google=$N_GOOGLE, cap $PROV_CAP each)" \
      "At capacity on the saturated provider(s) only. To go wider, SHARD: dispatch to a provider below its cap rather than adding processes to one that is at it."
  fi

  # ---- ROLE 2: "Where is the bottleneck and what do we need to eliminate it?" ------------------------------------
  # The constraint moves. It was coding, then it was verdicts. Naming it each
  # cycle is the whole job — optimising anything else is waste.
  # LATENCY, not just depth. The role reported "VERIFICATION: N awaiting" for hours while
  # the actual defect was that each verdict spent 1507s re-running tests. Counting a queue
  # cannot see the cost of servicing it — measure the service time too.
  SLOWEST=$(grep -hoE "in [0-9]+\.[0-9]+s" "$REPO"/var/swarm/verdicts/*.md 2>/dev/null \
            | grep -oE "[0-9]+" | sort -rn | head -1)
  : "${SLOWEST:=0}"
  # And WATCH THE WATCHERS. Three loops died today and nothing noticed, including the
  # supervisor whose job was noticing. A dead monitor reports nothing, which reads as calm.
  DEAD=""
  for lp in review-pump rework-pump sim-pump verdict-pump fleet-supervisor plan-consolidator; do
    pgrep -f "$lp.sh" >/dev/null 2>&1 || DEAD="$DEAD $lp"
  done

  if [ -n "$DEAD" ]; then
    emit "bottleneck-finder" "Where is the bottleneck and what do we need to eliminate it?" \
      "DEAD LOOPS:$DEAD — a stopped loop emits nothing, and silence reads as health" \
      "Restart them. A monitor that dies is worse than none: its absence looks like calm."
  elif [ "$SLOWEST" -gt 300 ]; then
    emit "bottleneck-finder" "Where is the bottleneck and what do we need to eliminate it?" \
      "VERDICT LATENCY: a recorded verdict took ${SLOWEST}s; a scoped ladder takes ~25s" \
      "The reviewer is re-running work already done. Use scripts/ladder-record.sh and have the verifier CHECK THE SHA BINDING instead of re-running."
  elif [ "$silent" -gt 0 ]; then
    emit "bottleneck-finder" "Where is the bottleneck and what do we need to eliminate it?" \
      "$silent lane(s) ALIVE but writing nothing — a stall looks identical to work" \
      "Inspect those lanes for a permission prompt or a retry loop. Two leads sat blocked for HOURS tonight looking healthy."
  elif [ "$awaiting" -gt "$(( working + 2 ))" ]; then
    emit "bottleneck-finder" "Where is the bottleneck and what do we need to eliminate it?" \
      "VERIFICATION: $awaiting awaiting verdict vs $working coding" \
      "Shift capacity from coders to opus verifiers. Finished-but-unverified work is inventory, not progress."
  elif [ "$mergeable" -gt 0 ]; then
    emit "bottleneck-finder" "Where is the bottleneck and what do we need to eliminate it?" \
      "INTEGRATION: $mergeable lane(s) verified and ready" \
      "Merge them now. Verified work sitting unmerged is the most wasteful state — it is finished AND idle."
  elif [ "$working" -gt 0 ] && [ "$awaiting" = "0" ]; then
    emit "bottleneck-finder" "Where is the bottleneck and what do we need to eliminate it?" \
      "THROUGHPUT: $working coding, nothing queued behind them" \
      "Coders are the constraint. Add lanes if load allows, else wait."
  else
    emit "bottleneck-finder" "Where is the bottleneck and what do we need to eliminate it?" \
      "no lanes in flight" "The constraint is WORK SELECTION — decide what matters next."
  fi

  [ "$CYCLES" != "0" ] && [ "$cycle" -ge "$CYCLES" ] && break
  sleep "$INTERVAL"
done
