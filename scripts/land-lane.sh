#!/usr/bin/env bash
# Fetch a lane's branch back into the repo. The lane never pushes; the
# coordinator pulls, which keeps every merge decision on this side.
#
# Fetch-before-merge is NOT optional: the lane's commits exist only in the lane's
# own object store, so merging or deleting the lane before fetching destroys them.
#
# --commit: commit the lane's work on its behalf before fetching.
#   Agents CANNOT commit inside their own lane. The codex workspace-write sandbox
#   denies every write under `.git` BY DESIGN, so an agent cannot rewrite history.
#   Probed directly in a live lane: `touch var/f` -> rc=0,
#   `touch .git/f` -> "Operation not permitted". codex's own answer is its
#   `apply` subcommand — the agent emits a diff, the unsandboxed side commits it.
#   No clone layout changes this; it is a policy, not a path problem.
#   So the commit stays coordinator-side, but it is MECHANICAL, not judgment:
#   by the time we get here the lane's work is written and revert-tested.
#
# usage: land-lane.sh <lane-name> [branch] [--commit]
#        LANE_COMMIT_MSG="..." land-lane.sh <lane-name> --commit
set -euo pipefail

REPO="${REPO:-/Users/youruser/OmniAgentOS}"

# Parse --commit out of the positionals FIRST. Getting this wrong made `$2`
# resolve to "--commit" and produced a refspec of `--commit:refs/heads/--commit`.
DO_COMMIT=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--commit" ]; then DO_COMMIT=1; else ARGS+=("$a"); fi
done

NAME="${ARGS[0]:?usage: land-lane.sh <lane-name> [branch] [--commit]}"
DEST="${LANE_ROOT:-$REPO/var/swarm/clones}/$NAME"
[ -d "$DEST/.git" ] || { echo "no lane at $DEST" >&2; exit 1; }
BRANCH="${ARGS[1]:-$(git -C "$DEST" rev-parse --abbrev-ref HEAD)}"

if [ "$DO_COMMIT" = "1" ]; then
  MSG="${LANE_COMMIT_MSG:?set LANE_COMMIT_MSG when using --commit}"
  if [ -n "$(git -C "$DEST" status --porcelain)" ]; then
    git -C "$DEST" add -A
    # node_modules and .venv are SYMLINKS in a lane (env wiring), so `add -A` follows
    # them into the real directory and stages build caches. Unstage unconditionally —
    # a build artifact in git is how a committed .venv symlink once destroyed 679M of
    # environment on checkout.
    git -C "$DEST" reset -q -- node_modules .venv dashboard/node_modules 2>/dev/null || true
    git -C "$DEST" -c user.name="${LANE_AUTHOR:-swarm-lane}" \
        -c user.email="${LANE_EMAIL:-4580856+omniagentos-bot[bot]@users.noreply.github.com}" commit -q -m "$MSG"
    echo "committed in lane: $(git -C "$DEST" log --oneline -1)"
  else
    echo "nothing to commit in lane (already committed or empty)"
  fi
fi

DIRTY=$(git -C "$DEST" status --porcelain | wc -l | tr -d ' ')
[ "$DIRTY" != "0" ] && echo "WARNING: $DIRTY uncommitted file(s) — they will NOT be fetched" >&2

LANE_SHA=$(git -C "$DEST" rev-parse "$BRANCH")
git -C "$REPO" fetch -q "$DEST" "$BRANCH:refs/heads/$BRANCH" --force
FETCHED=$(git -C "$REPO" rev-parse "$BRANCH")
[ "$LANE_SHA" = "$FETCHED" ] || { echo "FAILED: fetched $FETCHED != lane $LANE_SHA" >&2; exit 1; }
git -C "$REPO" cat-file -e "$FETCHED^{commit}" || { echo "FAILED: object missing after fetch" >&2; exit 1; }
echo "landed $BRANCH @ $FETCHED (fetched into origin — NOT MERGED)"
# "landed" means the branch is FETCHED, not merged. That distinction cost two
# stranded branches today: fix/ceiling-parity and fix/sched-scope were reported as
# landed, sat unmerged, and a later lane branched from a main that lacked them and
# re-implemented the same work. Say the unmerged part out loud.
if git -C "$REPO" merge-base --is-ancestor "$BRANCH" main 2>/dev/null; then
  echo "  (already an ancestor of main)"
else
  echo "  NEXT STEP: git merge --no-ff $BRANCH   <- required; nothing is on main yet"
fi
