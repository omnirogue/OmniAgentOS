#!/bin/sh
# ONE-COMMAND CONTRACT REFRESH — regenerate contracts/openapi.json and restamp
# contracts/fixtures/mission/v1/fixture-parity.json's three digests.
#
# WHY THIS EXISTS (measured, not theoretical)
# -------------------------------------------
# Four lanes landed on main in one day. Every multi-lane rebase hit a conflict in
# contracts/openapi.json (each route-touching lane regenerates the WHOLE artifact,
# so git sees thousands of conflicting lines) and in fixture-parity.json (each lane
# rewrites openapi_sha, so the same line conflicts every time). Neither conflict has
# any semantic content: both files are DERIVED. Resolving them by hand is busywork
# that also risks committing a hash that matches neither side.
#
#   >>> CONFLICT-RESOLUTION RECIPE — the whole point of this script <<<
#
#   When a rebase/merge conflicts on contracts/openapi.json and/or
#   contracts/fixtures/mission/v1/fixture-parity.json:
#
#     1. Take EITHER side, wholesale. Do not hand-merge; the content is generated.
#          git checkout --ours   contracts/openapi.json contracts/fixtures/mission/v1/fixture-parity.json
#        (or --theirs; it genuinely does not matter which)
#     2. git add those two paths, finish the rebase/merge.
#     3. ./scripts/refresh-contracts.sh
#     4. Commit whatever it changed. Done.
#
#   Step 3 recomputes both artifacts from the merged SOURCE, so the result is
#   correct regardless of which side you picked in step 1. Never resolve these two
#   files by editing hunks.
#
# WHAT IT DOES
#   1. contracts/openapi.json  <- scripts/generate_openapi.py (offline FastAPI schema).
#      Uses ./.venv/bin/python, NOT `uv run` like the Makefile's `openapi` target, so
#      it works from a git worktree that shares the product venv.
#   2. fixture-parity.json's openapi_sha / fixture_set_sha / contract_source_sha <-
#      the digest helpers _digest / _fixture_set_digest / _contract_source_digest
#      IMPORTED BY FILE PATH from tests/api/test_mission_fixture_parity.py and called.
#      The hashing is never reimplemented here: that test is the authority, and a
#      second implementation would be a counterfeit of it that drifts silently.
#   3. Rewrites only the three digest VALUES in place, so fixture-parity.json keeps
#      its exact formatting — including the fact that it has NO trailing newline.
#      (A previous ad-hoc refresh script re-serialised the whole file and appended
#      "\n", which is a spurious one-line diff on every run.)
#   4. Prints what changed and exits non-zero if it could not run.
#
# ATOMIC: both artifacts are produced and self-checked in a staging dir and copied
# into the tree only after BOTH succeed; a restoring trap puts the originals back on
# any failure. A half-write — openapi.json updated, parity left stale — is exactly the
# state this script exists to prevent, and it would strand whoever ran it in the
# middle of a conflict resolution.
#
# Idempotent: running it twice in a row leaves both files byte-identical.
# tests/scripts/test_refresh_contracts.py proves that — it is what makes the script
# safe to run in the middle of a conflict resolution.
#
# Exit codes: 0 ok · 1 could not run (missing interpreter/file/field, wrong tree).
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

OPENAPI="contracts/openapi.json"
PARITY="contracts/fixtures/mission/v1/fixture-parity.json"
PARITY_TEST="tests/api/test_mission_fixture_parity.py"

log() {
  printf 'refresh-contracts: %s\n' "$*"
}

die() {
  printf 'refresh-contracts: REFUSED: %s\n' "$*" >&2
  exit 1
}

cd "$ROOT_DIR"

# --- interpreter ------------------------------------------------------------
# Worktrees usually have no .venv of their own and share the product one; the
# counterfeit harness resolves it the same way (git-common-dir -> product root).
if [ -n "${OMNIAGENTOS_PYTHON:-}" ] && [ -x "${OMNIAGENTOS_PYTHON}" ]; then
  PY="${OMNIAGENTOS_PYTHON}"
elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PY="$ROOT_DIR/.venv/bin/python"
else
  COMMON_DIR=$(git -C "$ROOT_DIR" rev-parse --git-common-dir 2>/dev/null || true)
  case "$COMMON_DIR" in
    "") PY="" ;;
    /*) PY=$(CDPATH= cd -- "$COMMON_DIR/.." 2>/dev/null && pwd)/.venv/bin/python ;;
    *)  PY=$(CDPATH= cd -- "$ROOT_DIR/$COMMON_DIR/.." 2>/dev/null && pwd)/.venv/bin/python ;;
  esac
  [ -n "$PY" ] && [ -x "$PY" ] || die "no interpreter: set OMNIAGENTOS_PYTHON or run 'uv sync'"
fi

[ -f "$PARITY_TEST" ] || die "missing digest authority $PARITY_TEST"
[ -f "$PARITY" ] || die "missing $PARITY"

# The editable install of the product checkout outranks a worktree's own sources
# unless PYTHONPATH says otherwise. Regenerating a contract from the WRONG tree is
# the silent failure this guard exists to make impossible.
PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
"$PY" - "$ROOT_DIR" <<'PY' || die "omniagentos does not resolve to this checkout (PYTHONPATH/editable-install shadow)"
import sys
from pathlib import Path

import omniagentos

root = Path(sys.argv[1]).resolve()
resolved = Path(omniagentos.__file__).resolve()
if root not in resolved.parents:
    print(f"omniagentos resolves to {resolved}, not under {root}", file=sys.stderr)
    raise SystemExit(1)
PY

log "root=$ROOT_DIR python=$PY"

# --- atomicity ---------------------------------------------------------------
# Both artifacts are STAGED outside the tree and only copied in once BOTH have
# been produced and self-checked. A snapshot + restoring trap is the second
# layer, covering a failure during the copy itself.
#
# The earlier version wrote openapi.json immediately and then ran the digest
# stage: a malformed parity doc left openapi.json updated and parity stale —
# precisely the half-state this script exists to prevent, and it would have
# stranded whoever ran it mid-conflict-resolution.
SNAPSHOT=$(mktemp -d)
STAGE=$(mktemp -d)
COMMITTED=0

restore_and_cleanup() {
  rc=$?
  if [ "$rc" -ne 0 ] && [ "$COMMITTED" -eq 0 ]; then
    if [ -f "$SNAPSHOT/openapi.json" ]; then
      cp "$SNAPSHOT/openapi.json" "$ROOT_DIR/$OPENAPI" 2>/dev/null || true
    fi
    if [ -f "$SNAPSHOT/fixture-parity.json" ]; then
      cp "$SNAPSHOT/fixture-parity.json" "$ROOT_DIR/$PARITY" 2>/dev/null || true
    fi
    printf 'refresh-contracts: failed (rc=%s) — restored both artifacts; no half-state\n' \
      "$rc" >&2
  fi
  rm -rf "$SNAPSHOT" "$STAGE"
  exit "$rc"
}
trap restore_and_cleanup EXIT HUP INT TERM

if [ -f "$OPENAPI" ]; then cp "$OPENAPI" "$SNAPSHOT/openapi.json"; fi
cp "$PARITY" "$SNAPSHOT/fixture-parity.json"

# --- 1. STAGE openapi.json (written under $STAGE, not into the tree) ---------
mkdir -p "$STAGE/contracts"
"$PY" - "$STAGE" <<'PY' >/dev/null || die "openapi generation failed"
import sys
from pathlib import Path

from scripts.generate_openapi import generate_openapi_artifact

generate_openapi_artifact(Path(sys.argv[1]))
PY
[ -s "$STAGE/contracts/openapi.json" ] || die "staged openapi.json is empty"

# --- 2. STAGE fixture-parity.json digests -----------------------------------
"$PY" - "$ROOT_DIR" "$STAGE" <<'PY' || die "digest refresh failed"
"""Stage a restamped fixture-parity.json using the parity test's OWN helpers.

Imported by file path and called, so the values cannot drift from the
definitions the test asserts against. Only the three digest values are
rewritten, in place, so the file's formatting (notably: no trailing newline)
survives byte-for-byte. Nothing in the repo tree is written here — the caller
copies the staged files in only after BOTH stages succeed.
"""

import importlib.util
import json
import re
import sys
from pathlib import Path

root = Path(sys.argv[1])
stage = Path(sys.argv[2])
test_path = root / "tests" / "api" / "test_mission_fixture_parity.py"
parity_path = root / "contracts" / "fixtures" / "mission" / "v1" / "fixture-parity.json"
staged_openapi = stage / "contracts" / "openapi.json"

spec = importlib.util.spec_from_file_location("_refresh_mission_fixture_parity", test_path)
if spec is None or spec.loader is None:
    raise SystemExit(f"cannot load digest helpers from {test_path}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# _digest of the STAGED openapi: same authority helper, applied to the bytes we
# are about to install, so the parity file can never describe a different file
# than the one that lands.
digests = {
    "openapi_sha": module._digest(staged_openapi.read_text()),
    "fixture_set_sha": module._fixture_set_digest(),
    "contract_source_sha": module._contract_source_digest(),
}

original = parity_path.read_text(encoding="utf-8")
current = json.loads(original)
updated = original
for key, value in digests.items():
    pattern = re.compile(r'("' + key + r'"\s*:\s*")[0-9a-f]{64}(")')
    updated, hits = pattern.subn(r"\g<1>" + value + r"\g<2>", updated, count=1)
    if hits != 1:
        raise SystemExit(f"refuse: {parity_path} has no rewritable {key!r} digest field")
    was = current.get(key)
    state = "unchanged" if was == value else f"CHANGED (was {str(was)[:12]}…)"
    print(f"  {key:<20} {value[:12]}…  {state}")

# Self-check the STAGED content before anyone installs it.
reloaded = json.loads(updated)
for key, value in digests.items():
    if reloaded[key] != value:
        raise SystemExit(f"refuse: {key} did not take: {reloaded[key]!r} != {value!r}")
if updated.endswith("\n") != original.endswith("\n"):
    raise SystemExit("refuse: trailing-newline shape changed")

(stage / "fixture-parity.json").write_text(updated, encoding="utf-8")
PY
[ -s "$STAGE/fixture-parity.json" ] || die "staged fixture-parity.json is empty"

# --- 3. INSTALL both, only now that both staged artifacts exist and check out -
cp "$STAGE/contracts/openapi.json" "$ROOT_DIR/$OPENAPI" || die "cannot install $OPENAPI"
cp "$STAGE/fixture-parity.json" "$ROOT_DIR/$PARITY" || die "cannot install $PARITY"
COMMITTED=1

# --- 4. report --------------------------------------------------------------
changed=0
report() {
  if [ ! -f "$SNAPSHOT/$2" ]; then
    log "  CREATED    $1"
    changed=1
  elif cmp -s "$SNAPSHOT/$2" "$1"; then
    log "  unchanged  $1"
  else
    log "  CHANGED    $1"
    changed=1
  fi
}
report "$OPENAPI" "openapi.json"
report "$PARITY" "fixture-parity.json"

if [ "$changed" = "0" ]; then
  log "contracts already current — nothing to commit"
else
  log "contracts refreshed — commit $OPENAPI and $PARITY"
fi
