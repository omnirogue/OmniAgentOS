#!/usr/bin/env bash
# Assemble a rework brief that carries EVERY prior verdict verbatim.
#
# Why this is a script and not a habit: in the 2026-07-29 batch only 1 of 5 rework
# briefs told the implementer to read the reviewer's verdict — the rest carried the
# coordinator's paraphrase, which at least once propagated the coordinator's own
# misdiagnosis. And var/sol-verdict.md was overwritten every round, so round 6's
# implementer could not see what rounds 1-5 were rejected for.
#
# Each `grok -p` / `gemini -p` is a fresh process with no memory. The brief is its only
# continuity. Paraphrase may be ADDED; it may not REPLACE.
#
# Usage: rework-brief.sh <lane-dir> [notes-file]
#   <lane-dir>   lane worktree (must contain var/)
#   [notes-file] optional coordinator notes, appended AFTER the verbatim verdicts
#
# Writes <lane-dir>/var/REWORK-r<N>.md and prints its path.
set -euo pipefail

LANE_DIR="${1:?usage: rework-brief.sh <lane-dir> [notes-file]}"
NOTES="${2:-}"
VAR="$LANE_DIR/var"
ARCHIVE="$VAR/verdicts"
[ -d "$VAR" ] || { echo "no var/ in $LANE_DIR" >&2; exit 2; }
mkdir -p "$ARCHIVE"

# Archive the current verdict before it can be overwritten by the next review.
if [ -f "$VAR/sol-verdict.md" ]; then
  SHA=$(grep -m1 '^Candidate-Sha:' "$VAR/sol-verdict.md" | awk '{print $2}' || true)
  if ! grep -rqlF "${SHA:-__none__}" "$ARCHIVE" 2>/dev/null; then
    N=$(( $(find "$ARCHIVE" -name 'sol-verdict-r*.md' | wc -l | tr -d ' ') + 1 ))
    cp "$VAR/sol-verdict.md" "$ARCHIVE/sol-verdict-r${N}.md"
  fi
fi

ROUND=$(( $(find "$ARCHIVE" -name 'sol-verdict-r*.md' | wc -l | tr -d ' ') + 1 ))
OUT="$VAR/REWORK-r${ROUND}.md"

{
  echo "# REWORK ROUND ${ROUND} — $(basename "$LANE_DIR")"
  echo
  echo "You are a fresh process with no memory of earlier rounds. Everything you need is"
  echo "in this file, in var/BRIEF.md (the original contract), and in var/REPORT.md (your"
  echo "own prior rounds). Read all three before editing anything."
  echo
  echo "## The acceptance contract has not changed"
  echo "\`devtasks/LANE-ACCEPTANCE-DOCTRINE.md\` is binding. In particular:"
  echo "- Write the decisive test FIRST and record it FAILING against the current tree,"
  echo "  before you implement. A test written after the fix proves the mutation broke"
  echo "  something, not that the test binds to the property."
  echo "- Audit the three defect classes (doctrine §2) and report the audit even when"
  echo "  nothing changes: unknowns must fail closed and tri-states must not collapse to"
  echo "  booleans; never claim evidence you did not gather; assert on the axis the"
  echo "  property is stated in."
  echo "- Before reporting, try to defeat your own decisive test a DIFFERENT way than your"
  echo "  named counterfeit. If it still passes it is not binding — fix the test."
  echo "- Every reviewer diagnostic quoted below must land in the lane as a permanent test."
  echo
  echo "## Reviewer verdicts — VERBATIM, all rounds, oldest first"
  echo
  echo "These are the reviewer's own words. Where a coordinator note below disagrees with"
  echo "one of these, the verdict is the authority: it was produced by a cross-lineage"
  echo "reviewer reading the actual tree."
  echo
  for f in $(find "$ARCHIVE" -name 'sol-verdict-r*.md' | sort -V); do
    echo "### $(basename "$f")"
    echo
    echo '```'
    cat "$f"
    echo '```'
    echo
  done
} > "$OUT"

if [ -n "$NOTES" ] && [ -f "$NOTES" ]; then
  {
    echo "## Coordinator notes (secondary to the verdicts above)"
    echo
    cat "$NOTES"
  } >> "$OUT"
fi

echo "$OUT"
