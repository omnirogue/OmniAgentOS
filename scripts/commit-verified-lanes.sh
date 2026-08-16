#!/usr/bin/env bash
# Commit the uncommitted work of lanes a reviewer has already approved, then fetch them.
#
# WHY THIS STEP HAS TO EXIST
# --------------------------
# An agent cannot commit inside its own lane. The codex workspace-write sandbox denies every
# write under `.git` BY DESIGN, so that an agent cannot rewrite history. Probed directly in
# a live lane: `touch var/f` succeeds, `touch .git/f` returns "Operation not permitted".
#
# The consequence is structural, not incidental: EVERY lane ends with its work uncommitted,
# and something on the coordinator's side must land it. Nothing did. Eight reviewed and
# approved lanes sat with their work sitting dirty in a working tree, invisible to
# `git log`, one command away from being lost to a `git clean`.
#
# This does not decide anything. It commits work that a reviewer already approved and
# fetches it into the primary object store so the commits survive the lane. The merge
# decision stays with `merge-gate.sh`, which still demands a recorded Anthropic verdict.
#
#   commit-verified-lanes.sh            # every lane the ledger calls `mergeable`
#   commit-verified-lanes.sh a b c      # named lanes
set -uo pipefail
REPO="${REPO:-/Users/youruser/OmniAgentOS}"
cd "$REPO" || exit 1
PY="$REPO/.venv/bin/python"

lanes() {
  if [ "$#" -gt 0 ]; then printf '%s\n' "$@"; return; fi
  # Ask the ledger rather than re-deriving "which lanes are approved" — a second
  # implementation of that question is how the verdict parse acquired the same bug twice.
  "$PY" "$REPO/scripts/fleet-ledger.py" query mergeable 2>/dev/null |
    awk '!/^\(/ {print $1}'
}

ok=0; skipped=0; failed=0
while IFS= read -r LANE; do
  [ -z "$LANE" ] && continue
  D="$REPO/var/swarm/clones/$LANE"
  [ -d "$D/.git" ] || { printf '  %-20s no clone\n' "$LANE"; skipped=$((skipped+1)); continue; }

  BR=$(git -C "$D" rev-parse --abbrev-ref HEAD 2>/dev/null)
  DIRTY=$(git -C "$D" status --porcelain 2>/dev/null | grep -cvE '^\?\?')
  if [ "${DIRTY:-0}" = "0" ]; then
    printf '  %-20s clean — nothing to commit\n' "$LANE"; skipped=$((skipped+1)); continue
  fi

  LANE_COMMIT_MSG="$BR: lane work committed by the coordinator

The coder could not commit this itself — the codex workspace-write sandbox denies every
write under .git by design — so a lane's work always ends uncommitted and something on the
coordinator's side has to land it. A cross-lineage reviewer approved this lane before this
commit was made; the merge decision still belongs to merge-gate.sh.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
  export LANE_COMMIT_MSG

  if OUT=$("$REPO/scripts/land-lane.sh" --commit "$LANE" 2>&1); then
    printf '  %-20s committed %s file(s) on %s\n' "$LANE" "$DIRTY" "$BR"; ok=$((ok+1))
  else
    printf '  %-20s FAILED: %s\n' "$LANE" "$(printf '%s' "$OUT" | tail -1 | cut -c1-80)"; failed=$((failed+1))
  fi
done < <(lanes "$@")

echo
echo "committed=$ok skipped=$skipped failed=$failed"
[ "$failed" -gt 0 ] && exit 1
exit 0
