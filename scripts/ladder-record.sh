#!/usr/bin/env bash
# Run the ladder ONCE, bind the result to a commit SHA, so verifiers stop re-running it.
#
# Measured: one aggregate verdict spent 1507s (25 minutes) re-running `pytest tests/` while a
# scoped run of the same surfaces takes ~23s. Verdict latency is the fleet's binding
# constraint, and most of it is a reviewer repeating work the coordinator already did.
#
# WHY THIS DOES NOT VIOLATE THE FAR-SIDE RULE
# Handing a reviewer "the tests passed, trust me" would be exactly the unwitnessed claim this
# repo exists to refuse. So the record is BOUND TO A SHA and cheap to falsify:
#
#   - it records `git rev-parse HEAD` at run time, plus the tree state
#   - a verifier checks the SHA matches what it is reviewing — one command, instant
#   - if the SHA differs, the record is INVALID and the verifier must re-run
#   - the record stores the verbatim pytest tail, not a summary, so "passed" is checkable
#
# The reviewer is not trusting the claim; it is checking a binding it can break in one step.
# That is the same move as the merge receipt: make the claim cheap to verify rather than
# asking anyone to believe it.
set -uo pipefail
REPO="${OMNIAGENTOS_REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
REPO="$(cd "$REPO" && pwd)"
cd "$REPO"
PY="$REPO/.venv/bin/python"
OUT="${OMNIAGENTOS_VAR_DIR:-$REPO/var}/integration/ladder"
mkdir -p "$OUT"

SHA=$(git rev-parse HEAD)
BRANCH=$(git rev-parse --abbrev-ref HEAD)
# A dirty tree means the record describes something that is not the commit. Say so.
DIRTY=$(git status --porcelain | grep -vE '^\?\?' | wc -l | tr -d ' ')
F="$OUT/${SHA:0:12}.ladder"

TARGETS="${LADDER_TARGETS:-tests/memlife/ tests/scheduler/ tests/swarm/ tests/routing/ tests/acceptance/}"

echo "ladder: $BRANCH @ ${SHA:0:12} (dirty=$DIRTY)"
{
  echo "sha: $SHA"
  echo "branch: $BRANCH"
  echo "tree: $REPO"
  echo "dirty_tracked_files: $DIRTY"
  echo "ran_at: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "targets: $TARGETS"
  echo "---"
} > "$F"

START=$(date +%s)
# shellcheck disable=SC2086
RESULT=$("$PY" -m pytest -q $TARGETS 2>&1 | tail -25)
RC=$?
END=$(date +%s)

{
  printf '%s\n' "$RESULT"
  echo "---"
  echo "exit_code: $RC"
  echo "wall_seconds: $((END-START))"
} >> "$F"

SUMMARY=$(printf '%s' "$RESULT" | grep -E '^[0-9]+ (passed|failed)|passed,|failed,' | tail -1)
echo "  $SUMMARY ($((END-START))s)"
echo "  record: ${F#"$REPO"/}"
if [ "$DIRTY" != "0" ]; then
  echo "  WARNING: $DIRTY tracked file(s) dirty — this record does NOT describe commit ${SHA:0:12} alone"
fi
[ "$RC" -ne 0 ] && echo "  LADDER RED — do not hand this to a verifier as a pass"
exit $RC
