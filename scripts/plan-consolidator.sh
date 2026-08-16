#!/usr/bin/env bash
# Standing role: review every queued plan against the CURRENT codebase, every 30 minutes.
#
# Why this is a standing role rather than a one-off review: the codebase changes faster than
# the plans describing it. In a single night this repo gained memlife, a merge gate, a fleet
# ledger, a verdict pump, and four sweep merges — while plans written hours earlier still
# proposed building things that now exist. A plan is a claim about the code, and a claim
# about code decays the moment the code moves.
#
# Concretely observed tonight: a failure record asserted the path-security registry already
# existed on main (it was unmerged in a lane), and a gap analysis proposed a git-backed
# memory store while `omniagentos/memlife/` was being merged. An analyst working from either
# document would have built something we already had.
#
# Runs on a SEPARATE Anthropic account so it does not consume this session's rate limit.
# Provider capacity shards — that is the same insight that raised the fleet ceiling from
# "cores" to "sum of per-provider headroom", applied to our own account pool.
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO"
ACCT="${PLAN_ACCT:-$HOME/.claude-account-6}"
INTERVAL="${PLAN_INTERVAL:-1800}"          # 30 minutes
CYCLES="${PLAN_CYCLES:-0}"
OUT="$REPO/var/swarm/plan-reviews"
mkdir -p "$OUT"

cycle=0
while :; do
  cycle=$((cycle+1))
  TS=$(date -u +%Y%m%d-%H%M%S)
  PLANS=$(ls devtasks/*.md 2>/dev/null | tr '\n' ' ')
  HEAD_SHA=$(git rev-parse --short HEAD)
  # What moved since the last review — the reason a plan may now be stale.
  SINCE=$(cat "$OUT/LAST_SHA" 2>/dev/null || echo "")
  CHANGED=""
  [ -n "$SINCE" ] && CHANGED=$(git diff --name-only "$SINCE..HEAD" 2>/dev/null | head -40 | tr '\n' ' ')

  echo "[$TS] cycle=$cycle head=$HEAD_SHA plans=$(echo "$PLANS" | wc -w | tr -d ' ')"

  CLAUDE_CONFIG_DIR="$ACCT" claude --effort high -p "You are the PLAN CONSOLIDATOR, a standing role. Review every queued plan against the CURRENT codebase and report what has gone stale, what should merge with what, and what should be cut.

Repo: $REPO (HEAD $HEAD_SHA). Plans: $PLANS

Files changed since your last review: ${CHANGED:-<first review — no delta available>}

## Why you exist
The codebase moves faster than the plans describing it. A plan is a CLAIM ABOUT THE CODE, and such a claim decays the moment the code moves. Tonight a failure record asserted a registry existed on main when it was unmerged in a lane, and a gap analysis proposed a git-backed memory store while omniagentos/memlife/ was being merged. Either would have caused someone to build what we already had.

## Your job, in order
1. **STALE CLAIMS.** For each plan, find assertions about the repo that are no longer true. Cite the plan line AND the file:line that contradicts it. This is your highest-value output — a wrong plan is worse than no plan because it is actioned.
2. **CONSOLIDATE.** Which plans now overlap enough to merge into one? Name the pair and what survives. Multiple plans proposing the same thing is how work gets done twice.
3. **ALREADY BUILT.** What does a plan still propose that now EXISTS? Cite the file. Say it plainly.
4. **REPRIORITISE.** Given the current bottleneck, what should move UP and what should move DOWN? Do not just restate priorities — justify changes from evidence.
5. **CUT.** What should be deleted outright, one line each.

## Rules
- Verify against the CODE, never against another plan. Plans citing plans is how a stale claim propagates.
- Cite file:line. State what you could NOT verify.
- If nothing has gone stale since the last review, say so plainly and stop — a role that always finds work manufactures busywork.
- No padding. No restating plans back at me.

Write to $OUT/review-$TS.md" \
    --model claude-fable-5 --permission-mode acceptEdits \
    --allowedTools "Bash(git:*)" "Bash(grep:*)" "Bash(ls:*)" "Bash(rg:*)" "Bash(find:*)" "Bash(head:*)" "Bash(wc:*)" "Bash(cat:*)" "Bash(sed:*)" "Read" "Grep" "Glob" "Write" \
    > "$OUT/log-$TS.txt" 2>&1
  RC=$?

  # A review that produced nothing is recorded as FAILED, not left as silence — the same
  # policy as the verifier wrapper, for the same reason.
  if [ "$RC" -ne 0 ] || [ ! -s "$OUT/review-$TS.md" ]; then
    printf '# Plan review %s\n\nFAILED: consolidator exited rc=%s and wrote %s bytes.\nAn absent review is not a clean review; re-dispatch.\n' \
      "$TS" "$RC" "$(wc -c < "$OUT/review-$TS.md" 2>/dev/null || echo 0)" > "$OUT/review-$TS.md"
    echo "  FAILED recorded (rc=$RC)"
  else
    echo "  review written: $(wc -c < "$OUT/review-$TS.md") bytes"
    echo "$HEAD_SHA" > "$OUT/LAST_SHA"
  fi
  ln -sfn "review-$TS.md" "$OUT/LATEST.md"

  [ "$CYCLES" != "0" ] && [ "$cycle" -ge "$CYCLES" ] && break
  sleep "$INTERVAL"
done
