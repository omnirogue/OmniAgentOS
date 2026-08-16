#!/bin/sh
# Refuse a candidate that edits a path another IN-FLIGHT lane holds.
#
#   scripts/gates/lane-claims-check.sh <base> <candidate>
#   LANE_ID=my-lane scripts/gates/lane-claims-check.sh main HEAD
#
# WHY
# ---
# devtasks/SESSION-CLAIMS.md is a human-prose table. Nothing reads it, so nothing
# enforces it, so it goes stale: on 2026-07-31 a lane edited paths inside the live
# `dashboard/**` claim and only a human caught it, after the work was finished.
# With 4-6 lanes running concurrently that stops being an incident and becomes a
# steady-state cost. devtasks/lane-claims.yaml is the machine-readable subset of
# that table, and this is the script that checks it.
#
# ARGUMENTS
#   base        base ref (e.g. main)
#   candidate   candidate ref (e.g. HEAD, or a lane branch)
#   LANE_ID     acting lane id — REQUIRED (or --lane), and it must exist in the
#               ledger (as a `lane:` or an `aliases:` entry). A lane is never blocked
#               by its own claims; without the id we cannot tell own from other, and
#               an id the ledger does not know is indistinguishable from a typo, so
#               it REFUSES (exit 2) rather than passing. A misspelled lane name must
#               never silently disable the check.
#   --ledger P  alternate ledger (default devtasks/lane-claims.yaml)
#   --repo P    alternate repo to diff in (default: this checkout)
#
# RENAMES ARE CHECKED ON BOTH SIDES
#   The diff uses `--name-status -M`, so moving a file OUT of another lane's claimed
#   tree is a violation too. `--name-only` reports only a rename's destination, which
#   made "git mv <their file> <my dir>" pass.
#
# ESCAPE HATCH (visible on purpose)
#   LANE_CLAIMS_OVERRIDE=1 turns a refusal into a loud printed warning and exit 0.
#   EXACTLY `1` — any other value (including `False`, `true`, `yes`) is a hard error,
#   not a bypass and not a silent ignore. Unset it to enforce.
#   Same idiom as devtasks/REACHABILITY-EXEMPT.txt: a violation becomes a RECORDED
#   decision someone can disagree with, never a silent bypass. If you use it, say so
#   in your lane evidence and tell the owning lane before you land.
#
# COVERAGE LIMIT — READ THIS
#   The ledger currently transcribes only SESSION-CLAIMS.md rows labelled "IN FLIGHT".
#   Five further rows carry active "do not edit" / "reserved" restrictions and are
#   recorded but NOT enforced (see the `# NOT TRANSCRIBED` block). A green result
#   means "no IN-FLIGHT claim was crossed", not "no claim was crossed".
#
# DELIBERATELY NOT WIRED INTO A GIT HOOK
#   `core.hooksPath` is repo-wide, and other sessions commit in the live checkout
#   continuously. An enforcing pre-commit/pre-merge hook installed from a lane would
#   break their in-flight work the moment it landed. This ships as a standalone
#   script only. Do not add it to .git/hooks or core.hooksPath.
#
# INTENDED FOLLOW-UP (NOT done by this lane, on purpose)
#   Wire it into scripts/merge-gate.sh as one more refusal probe. The snippet below
#   uses merge-gate.sh's OWN variable names, verified against it: $BRANCH (the lane
#   branch, argument 1), $CANDIDATE_SHA (the authenticated candidate tip) and
#   $STEP_DIR (defined inside the `if [ "$MERGE_OK" -eq 1 ]` block). Place it in that
#   block, after STEP_DIR is created. merge-gate.sh runs under `set -uo pipefail`, so
#   every variable below is either one it already defines or is defaulted here.
#
#     # --- lane claims: another in-flight lane's paths are not ours to edit -----
#     LC_OUT="$STEP_DIR/lane-claims.out"
#     LC_RC=0
#     LANE_ID="${LANE_ID:-$BRANCH}" \
#       "$REPO/scripts/gates/lane-claims-check.sh" HEAD "$CANDIDATE_SHA" \
#       >"$LC_OUT" 2>&1 || LC_RC=$?
#     case "$LC_RC" in
#       0) pass "lane-claims" "$(tail -n 1 "$LC_OUT")" ;;
#       1) fail "lane-claims" "$(tail -n 5 "$LC_OUT" | tr '\n' ' ')" ;;
#       *) fail "lane-claims" "probe could not run (exit $LC_RC) — not a pass" ;;
#     esac
#
#   Note: `HEAD` is the base because merge-gate.sh runs from the repo root on main;
#   it is the same base it uses for CHANGED_PATHS ("HEAD...$CANDIDATE_SHA").
#   Note: a lane whose BRANCH differs from its ledger `lane:` id needs an `aliases:`
#   entry, or the probe exits 2 (unknown lane) — which fails the gate by design.
#
#   scripts/merge-gate.sh is claimed by lanes `m8-gate-refusals` and
#   `m5-probe-refusal` in the very ledger this script reads, so editing it here
#   would have been the exact violation this gate exists to catch. It is left to
#   whichever of those lanes lands last.
#
# Exit codes: 0 no violations (or override) · 1 violation · 2 could not run.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

if [ -n "${OMNIAGENTOS_PYTHON:-}" ] && [ -x "${OMNIAGENTOS_PYTHON}" ]; then
  PY="${OMNIAGENTOS_PYTHON}"
elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PY="$ROOT_DIR/.venv/bin/python"
else
  # Worktrees usually share the product venv; resolve it the way the counterfeit
  # harness does rather than falling back to a system python without PyYAML.
  COMMON_DIR=$(git -C "$ROOT_DIR" rev-parse --git-common-dir 2>/dev/null || true)
  case "$COMMON_DIR" in
    "") PY="" ;;
    /*) PY=$(CDPATH= cd -- "$COMMON_DIR/.." 2>/dev/null && pwd)/.venv/bin/python ;;
    *)  PY=$(CDPATH= cd -- "$ROOT_DIR/$COMMON_DIR/.." 2>/dev/null && pwd)/.venv/bin/python ;;
  esac
  if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    PY=$(command -v python3 || true)
  fi
fi

if [ -z "${PY:-}" ] || [ ! -x "$PY" ]; then
  echo "LANE CLAIMS REFUSED: no python3 interpreter" >&2
  exit 2
fi

exec "$PY" "$SCRIPT_DIR/lane_claims_check.py" "$@"
