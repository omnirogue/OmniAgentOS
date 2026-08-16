#!/usr/bin/env bash
# Create an agent lane the agent can actually COMMIT in.
#
# Why not `git worktree add`: a linked worktree's real gitdir lives at
# <repo>/.git/worktrees/<name>, OUTSIDE the agent's sandbox root, so `git commit`
# fails with "Operation not permitted". Every lane on 2026-07-28 needed a
# hand-commit from the coordinator, and the pressure that created drove one agent
# to attempt GUI automation, which reached the operator's Notes app.
#
# Why a PLAIN clone and not `--shared`: `--shared` writes an alternates file
# pointing at origin's object store (verified: .git/objects/info/alternates ->
# <repo>/.git/objects). This origin commits, aborts merges and deletes branches
# mid-run, so a `gc --auto` there can prune objects a live lane depends on. A
# plain local clone has NO alternates and still HARDLINKS the packs on APFS
# (verified: same inode) — identical disk cost, zero gc hazard.
#
# Why under var/ and not /tmp: the F-015 workspace floor
# (api/routes/board_files.py) refuses paths outside <repo>/var, and /tmp does not
# survive reboot — "worktree missing -> requeue from branch tip" is only sound if
# the branch was already fetched back. var/* is gitignored (.gitignore:11), so
# clones never pollute git status.
set -euo pipefail

REPO="${REPO:-/Users/youruser/OmniAgentOS}"
NAME="${1:?usage: new-lane.sh <lane-name> [branch] [base]}"
BRANCH="${2:-fix/$NAME}"
BASE="${3:-main}"
DEST="${LANE_ROOT:-$REPO/var/swarm/clones}/$NAME"

[ -e "$DEST" ] && { echo "refusing: $DEST already exists" >&2; exit 1; }
mkdir -p "$(dirname "$DEST")"

git clone --no-checkout --no-tags --quiet "$REPO" "$DEST"
# A clone creates a LOCAL branch only for HEAD; every other branch exists as a
# remote-tracking ref. Fall back to origin/<base> so lanes can be cut from an
# integration branch, not just main.
if ! git -C "$DEST" checkout -q -b "$BRANCH" "$BASE" 2>/dev/null; then
  git -C "$DEST" checkout -q -b "$BRANCH" "origin/$BASE" || {
    echo "cannot resolve base '$BASE' (tried local and origin/)" >&2
    rm -rf "$DEST"; exit 1; }
fi
# A lane must never push back on its own; the coordinator fetches.
git -C "$DEST" remote remove origin 2>/dev/null || true
mkdir -p "$DEST/var"
# Declare this lane's CLASS. There are two lane creators (this script and
# omniagentos/swarm/spawn.py:323 write_task_md, which writes var/task.md); the
# audit's lane_brief check asks every clone older than an hour for a brief and
# only exempts a DECLARED class listed in configs/audit-checks.yaml. Without
# this line every clone new-lane.sh makes is an undeclared brief-less lane, and
# quarantining a whole creator's output is how that check gets muted in week two.
printf '%s\n' "${LANE_CLASS:-task}" > "$DEST/var/LANE-CLASS"

# --- Make the lane RUNNABLE, not merely committable -------------------------
# A fresh clone has no .venv and no node_modules (both gitignored, 679M + 498M).
# Without them an agent falls back to `uv run`/`npm`, which writes to
# ~/.cache/uv OUTSIDE its sandbox and fails with "Operation not permitted".
# Symlink rather than copy: 1.2G per lane is untenable.
# .git/info/exclude is LOCAL to the clone and can never be committed. Belt and
# braces: a .gitignore entry written as `.venv/` matches DIRECTORIES only, so it
# does not match a SYMLINK named .venv — which is exactly how both of these got
# committed once as mode 120000 pointing at an absolute path on this machine.
for dep in .venv dashboard/node_modules; do
  if [ -e "$REPO/$dep" ]; then
    mkdir -p "$(dirname "$DEST/$dep")"
    ln -sfn "$REPO/$dep" "$DEST/$dep"
    echo "$dep" >> "$DEST/.git/info/exclude"
  fi
done

# The venv carries an EDITABLE install whose .pth points at the MAIN repo
# (_editable_impl_omniagentos.pth -> $REPO). Python resolves cwd BEFORE .pth
# entries, so a lane running from its own root imports its OWN source — but that
# ordering is load-bearing, and if it ever changed the lane would silently test
# the main repo's unmodified code while the agent edited the clone. That is the
# "322 passed against an unmodified file" failure at infrastructure scale, so it
# is asserted here rather than assumed.
if [ -x "$DEST/.venv/bin/python" ]; then
  RESOLVED=$(cd "$DEST" && ./.venv/bin/python -c "import omniagentos, sys; sys.stdout.write(omniagentos.__file__)" 2>/dev/null || true)
  case "$RESOLVED" in
    "$DEST"/*) : ;;
    *) echo "FAILED: lane imports omniagentos from '$RESOLVED', not from $DEST" >&2
       echo "        tests here would verify the WRONG source tree." >&2
       exit 1 ;;
  esac
fi

[ -f "$DEST/.git/objects/info/alternates" ] && {
  echo "FAILED: alternates present — lane depends on origin's object store" >&2; exit 1; }
# A clone made with --no-checkout leaves an EMPTY tree. If the checkout below
# failed for any reason the lane still "exists", every file reads as DELETED, and
# the agent works in a void — a failure that reads as success, which is the exact
# class this repo exists to eliminate. Assert the tree actually materialised.
TRACKED=$(git -C "$DEST" ls-files | wc -l | tr -d ' ')
ON_DISK=$(ls -A "$DEST" | grep -vc '^var$' || true)
if [ "$TRACKED" = "0" ] || [ "$ON_DISK" = "0" ]; then
  echo "FAILED: lane tree is EMPTY (tracked=$TRACKED on-disk=$ON_DISK)." >&2
  echo "        base '$BASE' probably does not exist as a branch or origin/ ref." >&2
  rm -rf "$DEST"; exit 1
fi

[ -d "$DEST/.git" ] || { echo "FAILED: .git is not a directory — commits will not work" >&2; exit 1; }
echo "$DEST"
