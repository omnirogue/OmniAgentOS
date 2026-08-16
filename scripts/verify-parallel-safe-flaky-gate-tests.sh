#!/usr/bin/env bash
# Mechanical acceptance for fix/gate-flaky-parallel-tests-0808.
#
# Proves the two gate-blocking flaky tests named in
# var/loopqueue/inquiries/sha256_d614ce65b222b464e7a1156d0417f48aadc6f3babc0543af1f51d68d26e02c9b.json
# are parallel-safe: zero flakes across N repeated xdist runs, each run
# alongside its full sibling test file for realistic contention (the
# collision the inquiry describes only showed up under real parallelism,
# never serially or in a lone-test run).
#
#   (1) tests/scripts/test_launch_env.py::
#         test_coherent_sim_env_gets_isolated_ports_and_no_production_bases
#       -- was racing a `cksum(campaign) % 1000` computed port; fixed in
#       scripts/launch-env.sh to bind an OS-assigned ephemeral port and read
#       it back (never collides), cached per campaign root for repeatability.
#   (2) tests/swarm/test_scheduler_races.py::TestAllCooling::
#         test_all_cooling_parks_with_stall_event_and_no_hot_loop
#       -- was racing a claim that slipped past the rate-limit park check
#       before the park was recorded; fixed in
#       omniagentos/swarm/scheduler.py (_next_work) to re-validate the park
#       under the same lock immediately before dispatch, and release (not
#       execute) a claim that lost the race.
#
# Usage: scripts/verify-parallel-safe-flaky-gate-tests.sh [iterations]
#
# Exits 0 only if BOTH named tests passed on EVERY iteration. Unrelated
# failures elsewhere in the neighbor suites (there is one pre-existing,
# out-of-scope gate-workspace-pinning flake in
# tests/scripts/test_merge_gate_worker_env_isolation.py, reproducible on
# unmodified main and unrelated to ports or the scheduler) are reported but
# do not fail this check -- it is scoped to the two tests this fix targets.
set -euo pipefail

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT_DIR"

ITER="${1:-5}"
# A run that proves nothing must not print PASS. Reject a non-integer or an
# iteration count below 1 (BSD `seq 1 0` even emits "1 0" — two runs — while a
# naive banner would still claim zero; both are lies about work done).
case "$ITER" in
  ''|*[!0-9]*) echo "FATAL: iterations must be a positive integer, got '$ITER'" >&2; exit 1 ;;
esac
if [ "$ITER" -lt 1 ]; then
  echo "FATAL: iterations must be >= 1, got $ITER" >&2
  exit 1
fi

PYBIN="${OMNIAGENTOS_PYTHON:-$ROOT_DIR/.venv/bin/python}"
if [ ! -x "$PYBIN" ]; then
  echo "FATAL: no executable project Python at $PYBIN" >&2
  exit 1
fi

TEST1_FILE="tests/scripts/test_launch_env.py"
TEST1_NAME="test_coherent_sim_env_gets_isolated_ports_and_no_production_bases"
TEST1_WORKERS="${VERIFY_TEST1_WORKERS:-4}"

TEST2_FILE="tests/swarm/test_scheduler_races.py"
TEST2_NAME="test_all_cooling_parks_with_stall_event_and_no_hot_loop"
TEST2_WORKERS="${VERIFY_TEST2_WORKERS:-8}"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

fails=0

# Checks that a testcase named $2 exists in junit xml $1 and neither failed
# nor errored. Prints OK/FAILED/MISSING and returns a matching exit code.
check_outcome() {
  local xml="$1" name="$2"
  "$PYBIN" - "$xml" "$name" <<'PY'
import sys
import xml.etree.ElementTree as ET

xml_path, target_name = sys.argv[1], sys.argv[2]
tree = ET.parse(xml_path)
matches = [tc for tc in tree.iter("testcase") if tc.get("name") == target_name]
if not matches:
    print(f"MISSING: no testcase named {target_name!r} in {xml_path}")
    sys.exit(1)
bad = [tc for tc in matches if tc.find("failure") is not None or tc.find("error") is not None]
if bad:
    print(f"FAILED: {target_name} failed/errored")
    sys.exit(1)
# A SKIPPED testcase carries no evidence the fix works — treat it as failure,
# not a pass. Otherwise a run that skips both targets prints PASS having proven
# nothing (favourable absence).
skipped = [tc for tc in matches if tc.find("skipped") is not None]
if skipped:
    print(f"SKIPPED: {target_name} did not run — no evidence, treated as failure")
    sys.exit(1)
print(f"OK: {target_name}")
sys.exit(0)
PY
}

run_check() {
  local file="$1" name="$2" workers="$3" label="$4"
  echo "== $label: $file::$name  (-n $workers, $ITER iterations) =="
  local i xml log
  # Portable counter — do NOT use `seq 1 "$ITER"`: BSD `seq 1 0` emits "1 0"
  # (running an unrequested iteration) where GNU emits nothing.
  i=1
  while [ "$i" -le "$ITER" ]; do
    xml="$WORKDIR/${label}-$i.xml"
    log="$WORKDIR/${label}-$i.log"
    "$PYBIN" -m pytest "$file" -q -n "$workers" --junitxml="$xml" >"$log" 2>&1 || true
    if check_outcome "$xml" "$name"; then
      echo "  iter $i: OK"
    else
      echo "  iter $i: FAIL (log follows)"
      cat "$log"
      fails=$((fails + 1))
    fi
    i=$((i + 1))
  done
}

run_check "$TEST1_FILE" "$TEST1_NAME" "$TEST1_WORKERS" "test1"
run_check "$TEST2_FILE" "$TEST2_NAME" "$TEST2_WORKERS" "test2"

if [ "$fails" -gt 0 ]; then
  echo "FAILED: $fails flaky iteration(s) detected across $((ITER * 2)) runs" >&2
  exit 1
fi
echo "PASS: zero flakes across $((ITER * 2)) iterations"
