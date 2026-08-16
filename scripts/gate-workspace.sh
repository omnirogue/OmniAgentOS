#!/bin/sh
# Create or refresh the dedicated objective-gate workspace.
#
# WHAT THIS DIRECTORY IS — AND IS NOT
# ------------------------------------
# `omniagentos/scheduler/gate_runner.py` re-runs a routine's declared verifier
# itself, because the routine's own run output is the thing being graded and so
# can never also be the grader. This checkout is the SOURCE OF THE PIN for that:
# the runner reads its SHA, checks it is clean, and then materialises a
# THROWAWAY worktree at that SHA to actually execute in. Verifiers never run
# here, so nothing they do can be left behind for the next run to inherit.
#
# It still has to be a checkout nobody works in. The runner refuses a dirty
# workspace — an uncommitted edit is exactly how a gate is made to "pass"
# without the work — and that refusal is classified as absence: the run settles
# NULL and is excluded from the acceptance floor rather than counted as a
# failure. So a shared checkout does not produce false failures; it produces NO
# VERDICTS, silently, for as long as somebody is mid-merge in it. This script
# therefore makes a checkout with exactly one writer: itself.
#
# The checkout is DETACHED on purpose. `main` is usually checked out somewhere
# else on this box (git refuses two worktrees on one branch), and a gate that
# runs against a pinned SHA is reproducible: the evidence names the SHA it
# graded. Re-run this script to advance the pin after a merge.
#
#   scripts/gate-workspace.sh            # create/refresh at origin-free `main`
#   scripts/gate-workspace.sh <ref>      # pin some other committish
#
# VENV ISOLATION
# ---------------
# The gate workspace has its own .venv, created via `uv sync` to match the
# repository's pinned dependencies (uv.lock). This isolates gate verdict from
# mutations in the shared live interpreter, making gate judgement evidence-grade:
# the receipt records the venv's lockfile digest, proving which dependency tree
# judged a candidate.
#
# Prints the resolved path and the export line `scripts/launch-env.sh` probes.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
REF=${1:-main}
DEFAULT_MAIN=0
if [ "$#" -eq 0 ]; then
  DEFAULT_MAIN=1
fi
WORKSPACE=${OMNIAGENTOS_GATE_WORKSPACE:-${ROOT_DIR}-gate}

if ! SHA=$(git -C "$ROOT_DIR" rev-parse --verify "${REF}^{commit}" 2>/dev/null); then
  echo "error: '$REF' does not name a commit in $ROOT_DIR" >&2
  exit 1
fi

if [ ! -e "$WORKSPACE" ]; then
  git -C "$ROOT_DIR" worktree add --detach "$WORKSPACE" "$SHA" >&2
else
  if [ ! -d "$WORKSPACE" ]; then
    echo "error: $WORKSPACE exists and is not a directory" >&2
    exit 1
  fi
  if ! git -C "$WORKSPACE" rev-parse --verify HEAD >/dev/null 2>&1; then
    echo "error: $WORKSPACE exists but is not a git checkout; remove it first" >&2
    exit 1
  fi
  # Refuse rather than --force: this workspace is supposed to have one writer,
  # and a dirty tree means that assumption already broke. Discarding the diff
  # would destroy whatever evidence there is of who else is writing here.
  if [ -n "$(git -C "$WORKSPACE" status --porcelain=v1 --untracked-files=all)" ]; then
    echo "error: $WORKSPACE has uncommitted or untracked files — the gate" >&2
    echo "       workspace must have exactly one writer. Inspect it, then:" >&2
    echo "         git -C $WORKSPACE status" >&2
    exit 1
  fi
  git -C "$WORKSPACE" checkout --quiet --detach "$SHA"
fi

# Post-condition, not a hope: prove the checkout the runner will see is clean
# and tip-bound, or say so instead of publishing a pin that yields no verdicts.
ACTUAL=$(git -C "$WORKSPACE" rev-parse --verify HEAD)
if [ "$ACTUAL" != "$SHA" ]; then
  echo "error: $WORKSPACE is at $ACTUAL, expected $SHA" >&2
  exit 1
fi
if [ -n "$(git -C "$WORKSPACE" status --porcelain=v1 --untracked-files=all)" ]; then
  echo "error: $WORKSPACE is dirty after checkout; the gate would produce no verdicts" >&2
  exit 1
fi
ACTUAL_SHORT=$(printf '%.8s' "$ACTUAL")

# A pin must never claim to certify `main` after `main` has moved.  This is a
# deliberate post-condition rather than an implicit second checkout: silently
# advancing here would make the SHA named by concurrently-collected evidence
# ambiguous.  A normal re-run resolves a fresh main SHA; an explicit ref is a
# reproducible point-in-time pin and is intentionally exempt.
if [ "$DEFAULT_MAIN" -eq 1 ]; then
  if ! MAIN_SHA=$(git -C "$ROOT_DIR" rev-parse --verify 'main^{commit}' 2>/dev/null); then
    echo "error: cannot verify whether gate workspace is current with main" >&2
    echo "       workspace: $WORKSPACE" >&2
    echo "       pinned SHA: $ACTUAL_SHORT ($ACTUAL)" >&2
    echo "       Rerun \`scripts/gate-workspace.sh main\` to advance the pin" >&2
    exit 1
  fi

  if [ "$ACTUAL" != "$MAIN_SHA" ]; then
    if git -C "$ROOT_DIR" merge-base --is-ancestor "$ACTUAL" "$MAIN_SHA" >/dev/null 2>&1; then
      RELATION="behind main"
    else
      RELATION="not verifiably current with main"
    fi
    echo "error: gate workspace is stale ($RELATION)" >&2
    echo "       workspace: $WORKSPACE" >&2
    echo "       pinned SHA: $ACTUAL_SHORT ($ACTUAL)" >&2
    echo "       current main SHA: $(printf '%.8s' "$MAIN_SHA") ($MAIN_SHA)" >&2
    echo "       Rerun \`scripts/gate-workspace.sh main\` to advance the pin" >&2
    exit 1
  fi
fi

# --- create or update the .venv in the gate workspace --------------------------
# The gate venv is isolated from the shared live interpreter, so gate judgement
# is evidence-grade: the receipt records the lockfile digest, proving exactly
# which dependency tree judged a candidate.
UV_CMD=""
if command -v uv >/dev/null 2>&1; then
  UV_CMD="uv"
elif command -v python3 -m uv >/dev/null 2>&1; then
  UV_CMD="python3 -m uv"
fi

if [ -n "$UV_CMD" ]; then
  echo "  creating isolated .venv in gate workspace..." >&2
  if (cd "$WORKSPACE" && $UV_CMD sync --frozen 2>&1 | tail -5); then
    echo "  gate .venv created successfully" >&2
  else
    echo "warning: gate .venv creation failed (merge-gate will degrade to shared interpreter)" >&2
  fi
else
  echo "warning: uv not found; gate .venv will not be created (merge-gate will degrade to shared interpreter)" >&2
fi

echo "$WORKSPACE"
echo "  pinned at $SHA ($REF)" >&2
echo "  scripts/launch-env.sh exports this automatically while it stays clean." >&2
echo "  Elsewhere: export OMNIAGENTOS_GATE_WORKSPACE=\"$WORKSPACE\"" >&2
