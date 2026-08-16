#!/usr/bin/env bash
# S04 PUMP PREFLIGHT — the dispatch runaway must be structurally impossible.
#
# WHAT THIS PROVES (and why each check exists)
#   A   a brief-less lane is QUARANTINED, exactly once, and NEVER dispatched
#   B   POSITIVE CONTROL: a lane WITH a brief still dispatches — a pump that quarantines
#       everything would pass A and be worthless
#   C   the brief's BODY is inside the DISPATCH PAYLOAD, not merely somewhere in the log
#   C2  MUTATION: change the brief body, re-run, the dispatched prompt CHANGES
#       (this is what makes the filename non-load-bearing rather than merely resolved)
#   D   verdict-hash dedup against the REAL DAL API (open_attempt / record_attempt_usage /
#       close_attempt — there is no `record_attempt`): identical verdict bodies produce
#       identical verdict_hash values, and the second dispatch is quarantined
#   D2  WIRING: the PUMP writes the row, not the DAL — for EACH of the four pumps
#   D2b TIMEOUT: a real dispatch to the shared fake provider records end_reason=timeout
#   D2c KILL: a TERM-ignoring fake reaches gtimeout's rc=137 and still records timeout
#   D2d INNER WATCHDOG: dispatch-verifier's own timeout writes FAILED and records timeout
#   D3  THE READER SEES THE WRITE: three attempt rows for one lane, the cap refuses the 4th
#   E   every brief-less clone ON DISK is quarantined and none dispatched
#   F   RESTART SURVIVAL: kill the pump, re-run it, the quarantine persists
#
# ARMING BOUNDARY: every pump invocation except D2b-D2d runs with REWORK_DRY_RUN=1. Those arms
# start local fake providers from tests/entrypoints/conftest.py; D2b honors the outer TERM, D2c
# ignores it until -k sends KILL, and D2d exercises dispatch-verifier's inner watchdog. They
# contact no API and spend no token. Ledger assertions use THROWAWAY sqlite databases.
#
# `set -e` is deliberately off: this is a suite, and it must report EVERY failure rather
# than the first one. `set -u` is on (per contract) and the exit code is the verdict —
# exit 0 ONLY on a full pass.
set -uo pipefail

ROOT=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
REPO="${REPO:-$ROOT}"
cd "$REPO" || { echo "FATAL: cannot cd $REPO" >&2; exit 1; }
PY="$REPO/.venv/bin/python"

SB=$(mktemp -d "${TMPDIR:-/tmp}/s04-pump-preflight.XXXXXX") || exit 1
cleanup() { [ "${S04_KEEP:-0}" = "1" ] || rm -rf "$SB"; }
trap cleanup EXIT

FAILURES=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILURES=$((FAILURES+1)); }
note() { printf '      %s\n' "$*"; }

# --- preflight: fail fast and loudly, never half-run ---------------------------------
[ -x "$PY" ] || { echo "FATAL(preflight): no python at $PY" >&2; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "FATAL(preflight): sqlite3 not on PATH" >&2; exit 1; }
for f in scripts/rework-pump.sh scripts/review-pump.sh scripts/verdict-pump.sh scripts/sim-pump.sh; do
  [ -x "$REPO/$f" ] || { echo "FATAL(preflight): $f missing or not executable" >&2; exit 1; }
  bash -n "$REPO/$f" || { echo "FATAL(preflight): $f fails bash -n" >&2; exit 1; }
done

# Throwaway ledger, built by the REAL migrator so the schema under test is the shipped one.
DB="$SB/state.sqlite3"
if ! "$PY" -m omniagentos.db.migrate "$DB" > "$SB/migrate.out" 2>&1; then
  echo "FATAL(preflight): migration failed" >&2; cat "$SB/migrate.out" >&2; exit 1
fi
if ! sqlite3 "$DB" "PRAGMA table_info(swarm_attempts);" | grep -q '|verdict_hash|'; then
  echo "FATAL(preflight): migration 109 did not add swarm_attempts.verdict_hash" >&2; exit 1
fi
export OMNIAGENTOS_DB="$DB"

# --- fixtures --------------------------------------------------------------------------
mk_lane() {  # root lane brief_relpath brief_body [branch]  (empty brief_relpath = brief-less lane)
  # The default clone is a BARE-MINIMUM directory with a `.git` folder: enough for the
  # pumps' `[ -d "$D/.git" ]` precondition, and deliberately cheap. Pass a BRANCH when the
  # arm exercises anything that resolves HEAD — a fake `.git` resolves to no branch at all,
  # and a test built on that is testing a broken clone, not the behaviour it names.
  local ROOT="$1" LANE="$2" REL="$3" BODY="$4" BRANCH="${5:-}" D
  D="$ROOT/$LANE"; mkdir -p "$D/.git" "$D/var"
  if [ -n "$REL" ]; then mkdir -p "$(dirname "$D/$REL")"; printf '%s\n' "$BODY" > "$D/$REL"; fi
  if [ -n "$BRANCH" ]; then
    git -C "$D" init -q -b "$BRANCH" \
      && git -C "$D" add -A \
      && git -C "$D" -c user.email=s04@example.invalid -c user.name=s04 \
           commit -q -m "s04 lane fixture" \
      || { echo "FATAL(fixture): cannot build a real clone for $LANE" >&2; exit 1; }
  fi
}
mk_verdict() {  # stage lane body
  mkdir -p "$1"; printf '# %s\n\nVERDICT: REJECT\n\n%s\n' "$2" "$3" > "$1/$2.md"
}

run_rework() {  # stage clones quarantine cycles logfile
  REPO="$REPO" \
  REWORK_STAGE="$1" REWORK_CLONES="$2" REWORK_QUARANTINE="$3" \
  REWORK_CYCLES="$4" REWORK_LOG="$SB/pump-internal.log" REWORK_DRY_RUN=1 \
  bash "$REPO/scripts/rework-pump.sh" > "$5" 2>"$5.err"
}

count_re() { grep -cE "$1" "$2" 2>/dev/null | tr -d ' '; }

install_fake_provider_clis() {  # bin_dir log_path — shared entrypoint helper, not a duplicate fake
  "$PY" - "$REPO" "$1" "$2" <<'PY'
import importlib.util
import sys
from pathlib import Path

repo, bin_dir, log_path = map(Path, sys.argv[1:])
# Plain-module exec relies on conftest having no module-level pytest-plugin import side effects; one would break this shell suite with a misleading traceback.
spec = importlib.util.spec_from_file_location("s04_entrypoint_conftest", repo / "tests/entrypoints/conftest.py")
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.install_fake_provider_clis(bin_dir, log_path)
PY
}

# =========================================================================================
# A — brief-less lane: exactly one QUARANTINE, zero DISPATCH, across 3 cycles
# =========================================================================================
A_CLONES="$SB/A/clones"; A_STAGE="$SB/A/stage"; A_Q="$SB/A/quarantine"
mkdir -p "$A_CLONES" "$A_STAGE" "$A_Q"
mk_lane "$A_CLONES" p1-counterfeit "" ""
mk_verdict "$A_STAGE" p1-counterfeit "the reviewer said the same thing 726 times"
run_rework "$A_STAGE" "$A_CLONES" "$A_Q" 3 "$SB/A.log"
A_Q_N=$(count_re '^QUARANTINE lane=p1-counterfeit reason=missing-brief$' "$SB/A.log")
A_D_N=$(count_re '^DISPATCH lane=' "$SB/A.log")
if [ "$A_Q_N" = "1" ]; then pass "A quarantine announced exactly once over 3 cycles"
else fail "A expected exactly 1 'QUARANTINE lane=p1-counterfeit reason=missing-brief', got $A_Q_N"; fi
if [ "$A_D_N" = "0" ]; then pass "A zero DISPATCH lines for a brief-less lane"
else fail "A expected 0 'DISPATCH lane=' lines, got $A_D_N"; note "$(grep -E '^DISPATCH lane=' "$SB/A.log" | head -3)"; fi
if [ -f "$A_Q/p1-counterfeit/reason" ]; then pass "A quarantine is durable on disk"
else fail "A no quarantine marker at $A_Q/p1-counterfeit/reason"; fi

# =========================================================================================
# B/C/C2 — positive control, body-in-payload, and mutation
# =========================================================================================
BRIEF_V1="CONTRACT-MARKER-ALPHA own omniagentos/widget.py and nothing else"
BRIEF_V2="CONTRACT-MARKER-BETA own omniagentos/gadget.py and nothing else"

payload_of() {  # logfile lane -> the bytes between the payload delimiters
  awk -v lane="$2" '
    $0 == "--- DISPATCH-PAYLOAD BEGIN lane=" lane " ---" { inblk=1; next }
    $0 == "--- DISPATCH-PAYLOAD END lane=" lane " ---"   { inblk=0 }
    inblk { print }
  ' "$1"
}

for V in 1 2; do
  eval "BODY=\$BRIEF_V$V"
  R="$SB/B$V"; mkdir -p "$R/clones" "$R/stage" "$R/quarantine"
  # A DIFFERENT brief filename per variant on purpose: if the filename were still
  # load-bearing, one of these two would fail to resolve.
  REL="var/task.md"; [ "$V" = "2" ] && REL="LANE-BRIEF.md"
  mk_lane "$R/clones" positive-lane "$REL" "$BODY"
  mk_verdict "$R/stage" positive-lane "reviewer verdict variant $V"
  # Each variant gets its OWN ledger, so variant 2 is not refused as a verdict repeat.
  OMNIAGENTOS_DB="$SB/state-B$V.sqlite3" "$PY" -m omniagentos.db.migrate "$SB/state-B$V.sqlite3" >/dev/null 2>&1
  OMNIAGENTOS_DB="$SB/state-B$V.sqlite3" run_rework "$R/stage" "$R/clones" "$R/quarantine" 1 "$SB/B$V.log"
  payload_of "$SB/B$V.log" positive-lane > "$SB/payload$V.txt"
done

B_D_N=$(count_re '^DISPATCH lane=positive-lane$' "$SB/B1.log")
if [ "$B_D_N" -ge 1 ] 2>/dev/null; then pass "B positive control dispatched (pump does not quarantine everything)"
else fail "B expected >=1 'DISPATCH lane=positive-lane', got $B_D_N"; note "$(tail -5 "$SB/B1.log")"; fi
if [ "$(count_re '^QUARANTINE lane=' "$SB/B1.log")" = "0" ]; then pass "B a lane with a brief is not quarantined"
else fail "B a lane WITH a brief was quarantined"; fi

if [ -s "$SB/payload1.txt" ] && grep -qF "$BRIEF_V1" "$SB/payload1.txt"; then
  pass "C the brief BODY is inside the DISPATCH PAYLOAD block"
else fail "C brief body not found inside the payload block"; note "payload bytes: $(wc -c < "$SB/payload1.txt" 2>/dev/null)"; fi
# Guard against a payload that merely echoes the path instead of the contents.
if grep -qE '(var/task\.md|LANE-BRIEF\.md)' "$SB/payload1.txt"; then
  fail "C the payload still NAMES a brief file — the filename is still load-bearing"
else pass "C the payload names no brief file (filename is not load-bearing)"; fi

if [ -s "$SB/payload2.txt" ] && grep -qF "$BRIEF_V2" "$SB/payload2.txt"; then
  pass "C2 the mutated brief body reached the payload"
else fail "C2 mutated brief body not found in the second payload"; fi
if cmp -s "$SB/payload1.txt" "$SB/payload2.txt"; then
  fail "C2 the dispatched prompt did NOT change when the brief body changed"
else pass "C2 a different brief body produces a different dispatched prompt"; fi

# =========================================================================================
# D — verdict-hash dedup against the REAL DAL API
# =========================================================================================
"$PY" - "$SB" <<'PY' > "$SB/D.out" 2>&1
import os, sys, sqlite3
sb = sys.argv[1]
db = os.path.join(sb, "state-D.sqlite3")
from omniagentos.db.migrate import migrate
migrate(db)
from omniagentos.swarm.dal import SwarmDal, PUMP_LEDGER_RUN_ID, pump_verdict_hash

body = "VERDICT: REJECT\nthe same paragraph, twice\n"
h1, h2 = pump_verdict_hash(body), pump_verdict_hash(body)
assert h1 == h2, "identical verdict bodies must hash identically"
assert pump_verdict_hash(body + "x") != h1, "different bodies must hash differently"

dal = SwarmDal(db)
task = dal.ensure_pump_lane("dedup-lane")
# THE REAL API, in the shipped order. There is no `record_attempt`; calling one would
# die with AttributeError on the first line, which is exactly what this asserts against.
for name in ("open_attempt", "record_attempt_usage", "close_attempt"):
    assert callable(getattr(dal, name, None)), f"SwarmDal.{name} missing"
assert getattr(dal, "record_attempt", None) is None, "SwarmDal.record_attempt should NOT exist"

a = dal.open_attempt(PUMP_LEDGER_RUN_ID, task, provider="grok", model="grok-4.5",
                     verdict_hash=h1, source="test")
dal.record_attempt_usage(a["id"], wall_ms=11, usage_source="pump")
dal.close_attempt(a["id"], "review_denied", "first pass")
assert dal.last_pump_verdict_hash(task) == h1
rows = sqlite3.connect(db).execute(
    "SELECT verdict_hash, wall_ms FROM swarm_attempts WHERE board_task_id=?", (task,)).fetchall()
assert rows and rows[0][0] == h1 and rows[0][1] == 11, rows
print("DAL-OK", h1)
PY
if grep -q '^DAL-OK ' "$SB/D.out"; then pass "D identical verdict bodies hash identically through the real DAL API"
else fail "D real-DAL verdict-hash check failed"; note "$(tail -6 "$SB/D.out")"; fi

# ... and the PUMP must refuse the second dispatch of an unchanged verdict.
D_ROOT="$SB/Dp"; mkdir -p "$D_ROOT/clones" "$D_ROOT/stage" "$D_ROOT/quarantine"
mk_lane "$D_ROOT/clones" repeat-lane "var/task.md" "CONTRACT-MARKER-DELTA"
mk_verdict "$D_ROOT/stage" repeat-lane "an unchanged reviewer complaint"
"$PY" -m omniagentos.db.migrate "$SB/state-Dp.sqlite3" >/dev/null 2>&1
OMNIAGENTOS_DB="$SB/state-Dp.sqlite3" run_rework "$D_ROOT/stage" "$D_ROOT/clones" "$D_ROOT/quarantine" 1 "$SB/Dp1.log"
OMNIAGENTOS_DB="$SB/state-Dp.sqlite3" run_rework "$D_ROOT/stage" "$D_ROOT/clones" "$D_ROOT/quarantine" 1 "$SB/Dp2.log"
if [ "$(count_re '^DISPATCH lane=repeat-lane$' "$SB/Dp1.log")" -ge 1 ] 2>/dev/null \
   && [ "$(count_re '^DISPATCH lane=repeat-lane$' "$SB/Dp2.log")" = "0" ] \
   && [ "$(count_re '^QUARANTINE lane=repeat-lane reason=verdict-repeat$' "$SB/Dp2.log")" = "1" ]; then
  pass "D an unchanged verdict body quarantines the SECOND dispatch"
else
  fail "D the repeat dispatch was not quarantined on verdict-repeat"
  note "run1: $(grep -cE '^DISPATCH' "$SB/Dp1.log") dispatch / run2: $(grep -E '^(DISPATCH|QUARANTINE|SKIP)' "$SB/Dp2.log" | head -3)"
fi

# =========================================================================================
# D2 — WIRING: the PUMP writes the row, for EACH of the four pumps
# =========================================================================================
rows_with_hash() { sqlite3 "$1" "SELECT COUNT(*) FROM swarm_attempts WHERE verdict_hash IS NOT NULL;" 2>/dev/null; }

W_CLONES="$SB/W/clones"; W_STAGE="$SB/W/stage"; W_Q="$SB/W/quarantine"
mkdir -p "$W_CLONES" "$W_STAGE" "$W_Q"
mk_lane "$W_CLONES" wired-lane "var/task.md" "CONTRACT-MARKER-WIRE"
mk_verdict "$W_STAGE" wired-lane "wiring probe"

check_pump_writes_row() {  # label db_path command...
  local LABEL="$1" WDB="$2"; shift 2
  "$PY" -m omniagentos.db.migrate "$WDB" >/dev/null 2>&1
  local BEFORE AFTER
  BEFORE=$(rows_with_hash "$WDB"); : "${BEFORE:=0}"
  OMNIAGENTOS_DB="$WDB" "$@" > "$SB/$LABEL.log" 2>"$SB/$LABEL.err"
  AFTER=$(rows_with_hash "$WDB"); : "${AFTER:=0}"
  if [ "$AFTER" -gt "$BEFORE" ] 2>/dev/null; then
    pass "D2 $LABEL-pump wrote a swarm_attempts row with a non-null verdict_hash ($BEFORE -> $AFTER)"
  else
    fail "D2 $LABEL-pump wrote NO verdict_hash row ($BEFORE -> $AFTER)"
    note "$(tail -4 "$SB/$LABEL.err" 2>/dev/null)"; note "$(tail -4 "$SB/$LABEL.log" 2>/dev/null)"
  fi
}

check_pump_writes_row rework "$SB/w-rework.sqlite3" \
  env REPO="$REPO" REWORK_STAGE="$W_STAGE" REWORK_CLONES="$W_CLONES" \
      REWORK_QUARANTINE="$W_Q/rework" REWORK_CYCLES=1 REWORK_DRY_RUN=1 \
      REWORK_LOG="$SB/w-rework-internal.log" bash "$REPO/scripts/rework-pump.sh"

check_pump_writes_row review "$SB/w-review.sqlite3" \
  env REPO="$REPO" REVIEW_STAGE="$SB/W/review-stage" REVIEW_CLONES="$W_CLONES" \
      REVIEW_QUARANTINE="$W_Q/review" REVIEW_CYCLES=1 REWORK_DRY_RUN=1 \
      REVIEW_QUEUE_CMD="echo wired-lane" REVIEW_LOG="$SB/w-review-internal.log" \
      bash "$REPO/scripts/review-pump.sh"

check_pump_writes_row verdict "$SB/w-verdict.sqlite3" \
  env REPO="$REPO" PUMP_CLONES="$W_CLONES" PUMP_QUARANTINE="$W_Q/verdict" \
      PUMP_PAYLOAD_DIR="$SB/W/payloads" PUMP_CYCLES=1 REWORK_DRY_RUN=1 \
      PUMP_QUEUE_CMD="echo wired-lane" PUMP_LOG="$SB/w-verdict-internal.log" \
      bash "$REPO/scripts/verdict-pump.sh"

check_pump_writes_row sim "$SB/w-sim.sqlite3" \
  env REPO="$REPO" SIM_CYCLES=1 SIM_MAX=1 REWORK_DRY_RUN=1 \
      SIM_QUARANTINE="$W_Q/sim" SIM_LOG="$SB/w-sim-internal.log" \
      bash "$REPO/scripts/sim-pump.sh"

# =========================================================================================
# D2b — a real dispatch that times out is queryable as timeout, not crashed
# =========================================================================================
T_ROOT="$SB/timeout"; T_CLONES="$T_ROOT/clones"; T_STAGE="$T_ROOT/stage"; T_Q="$T_ROOT/quarantine"
mkdir -p "$T_CLONES" "$T_STAGE" "$T_Q"
mk_lane "$T_CLONES" timeout-lane "var/task.md" "CONTRACT-MARKER-TIMEOUT"
mk_verdict "$T_STAGE" timeout-lane "a timeout should be classified, not merely described"
T_DB="$SB/state-timeout.sqlite3"
"$PY" -m omniagentos.db.migrate "$T_DB" >/dev/null 2>&1
install_fake_provider_clis "$T_ROOT/bin" "$T_ROOT/fake-cli.log"
PATH="$T_ROOT/bin:$PATH" FAKE_PROVIDER_SLEEP_S=3 OMNIAGENTOS_DB="$T_DB" \
  REPO="$REPO" REWORK_STAGE="$T_STAGE" REWORK_CLONES="$T_CLONES" \
  REWORK_QUARANTINE="$T_Q" REWORK_CYCLES=1 REWORK_DRY_RUN=0 \
  REWORK_DISPATCH_TIMEOUT_S=2 REWORK_LOG="$T_ROOT/pump.log" \
  bash "$REPO/scripts/rework-pump.sh" > "$T_ROOT/run.log" 2> "$T_ROOT/run.err"
T_REASON=""
for _ in 1 2 3 4 5 6; do
  T_REASON=$(sqlite3 "$T_DB" "SELECT end_reason FROM swarm_attempts WHERE provider = 'grok' AND ended_at IS NOT NULL ORDER BY started_at DESC LIMIT 1;" 2>/dev/null)
  [ "$T_REASON" = "timeout" ] && break
  sleep 1
done
if [ "$T_REASON" = "timeout" ] && grep -q '"cli": "grok"' "$T_ROOT/fake-cli.log"; then
  pass "D2b timed-out rework dispatch records end_reason=timeout through the real ledger"
else
  fail "D2b timed-out rework dispatch did not record end_reason=timeout (got ${T_REASON:-none})"
  note "$(tail -4 "$T_ROOT/run.log" 2>/dev/null)"; note "$(tail -4 "$T_ROOT/run.err" 2>/dev/null)"
fi

# =========================================================================================
# D2c — a TERM-ignoring provider reaches gtimeout's -k SIGKILL path (rc=137)
# =========================================================================================
TK_ROOT="$SB/timeout-kill"; TK_CLONES="$TK_ROOT/clones"; TK_STAGE="$TK_ROOT/stage"
TK_Q="$TK_ROOT/quarantine"
mkdir -p "$TK_CLONES" "$TK_STAGE" "$TK_Q"
mk_lane "$TK_CLONES" timeout-kill-lane "var/task.md" "CONTRACT-MARKER-TIMEOUT-KILL"
mk_verdict "$TK_STAGE" timeout-kill-lane "TERM-ignoring timeout should still be classified"
TK_DB="$SB/state-timeout-kill.sqlite3"
"$PY" -m omniagentos.db.migrate "$TK_DB" >/dev/null 2>&1
install_fake_provider_clis "$TK_ROOT/bin" "$TK_ROOT/fake-cli.log"
PATH="$TK_ROOT/bin:$PATH" FAKE_PROVIDER_SLEEP_S=60 FAKE_PROVIDER_IGNORE_TERM=1 \
  OMNIAGENTOS_DB="$TK_DB" REPO="$REPO" REWORK_STAGE="$TK_STAGE" \
  REWORK_CLONES="$TK_CLONES" REWORK_QUARANTINE="$TK_Q" REWORK_CYCLES=1 \
  REWORK_DRY_RUN=0 REWORK_DISPATCH_TIMEOUT_S=2 REWORK_LOG="$TK_ROOT/pump.log" \
  bash "$REPO/scripts/rework-pump.sh" > "$TK_ROOT/run.log" 2> "$TK_ROOT/run.err"
TK_ROW=""
for (( TK_WAIT=0; TK_WAIT<40; TK_WAIT++ )); do
  TK_ROW=$(sqlite3 "$TK_DB" "SELECT end_reason || '|' || detail FROM swarm_attempts WHERE provider = 'grok' AND ended_at IS NOT NULL ORDER BY started_at DESC LIMIT 1;" 2>/dev/null)
  [ "$TK_ROW" = "timeout|rework agent exit rc=137" ] && break
  sleep 1
done
if [ "$TK_ROW" = "timeout|rework agent exit rc=137" ] \
   && grep -q '"cli": "grok"' "$TK_ROOT/fake-cli.log"; then
  pass "D2c TERM-ignoring dispatch rc=137 records end_reason=timeout"
else
  fail "D2c TERM-ignoring dispatch did not record timeout with rc=137 (got ${TK_ROW:-none})"
  note "$(tail -4 "$TK_ROOT/run.log" 2>/dev/null)"; note "$(tail -4 "$TK_ROOT/run.err" 2>/dev/null)"
fi

# =========================================================================================
# D2d — dispatch-verifier's shorter inner watchdog remains visible to the verdict ledger
# =========================================================================================
TV_ROOT="$SB/verdict-timeout"; TV_REPO="$TV_ROOT/repo"
TV_CLONES="$TV_REPO/var/swarm/clones"; TV_LANE="verdict-timeout-lane"
mkdir -p "$TV_REPO/var/swarm" "$TV_CLONES"
ln -s "$REPO/.venv" "$TV_REPO/.venv"
ln -s "$REPO/scripts" "$TV_REPO/scripts"
TV_BRANCH="lane/verdict-timeout"
# A REAL branch. This fixture used a `.git`-directory clone whose HEAD resolved to
# nothing, so the verdict landed at `var/swarm/verdicts/.md` and the assertion below
# HARDCODED that path — pinning a defect as expected behaviour. dispatch-verifier.sh now
# refuses an unresolvable branch outright, so the watchdog path this arm exists to prove
# is only reachable from a clone that is actually on a branch.
mk_lane "$TV_CLONES" "$TV_LANE" "var/task.md" "CONTRACT-MARKER-VERDICT-TIMEOUT" "$TV_BRANCH"
TV_DB="$SB/state-verdict-timeout.sqlite3"
"$PY" -m omniagentos.db.migrate "$TV_DB" >/dev/null 2>&1
install_fake_provider_clis "$TV_ROOT/bin" "$TV_ROOT/fake-cli.log"
PATH="$TV_ROOT/bin:$PATH" FAKE_PROVIDER_SLEEP_S=60 OMNIAGENTOS_DB="$TV_DB" \
  REPO="$TV_REPO" PUMP_CLONES="$TV_CLONES" PUMP_QUARANTINE="$TV_ROOT/quarantine" \
  PUMP_PAYLOAD_DIR="$TV_ROOT/payloads" PUMP_QUEUE_CMD="echo $TV_LANE" PUMP_CYCLES=1 \
  PUMP_LOG="$TV_ROOT/pump.log" REWORK_DRY_RUN=0 VERIFIER_TIMEOUT=3 \
  VERDICT_DISPATCH_TIMEOUT_S=10 bash "$REPO/scripts/verdict-pump.sh" \
  > "$TV_ROOT/run.log" 2> "$TV_ROOT/run.err"
TV_ROW=""
for (( TV_WAIT=0; TV_WAIT<12; TV_WAIT++ )); do
  TV_ROW=$(sqlite3 "$TV_DB" "SELECT end_reason || '|' || detail FROM swarm_attempts WHERE provider = 'verifier' AND ended_at IS NOT NULL ORDER BY started_at DESC LIMIT 1;" 2>/dev/null)
  [ "$TV_ROW" = "timeout|dispatch-verifier rc=3" ] && break
  sleep 1
done
TV_VERDICT="$TV_REPO/var/swarm/verdicts/$(printf '%s' "$TV_BRANCH" | tr '/' '_').md"
if [ "$TV_ROW" = "timeout|dispatch-verifier rc=3" ] \
   && grep -q '^VERDICT: FAILED' "$TV_VERDICT" \
   && grep -q '"cli": "claude"' "$TV_ROOT/fake-cli.log"; then
  pass "D2d inner verifier watchdog rc=3 records timeout with a FAILED artifact"
else
  fail "D2d inner verifier watchdog did not record timeout rc=3 with FAILED artifact (got ${TV_ROW:-none})"
  note "$(tail -5 "$TV_ROOT/pump.log" 2>/dev/null)"
fi

# =========================================================================================
# D3 — THE READER SEES THE WRITE: three rows for one lane, the cap refuses the fourth
# =========================================================================================
"$PY" - "$SB" <<'PY' > "$SB/D3.out" 2>&1
import os, sys
sb = sys.argv[1]
db = os.path.join(sb, "state-D3.sqlite3")
from omniagentos.db.migrate import migrate
migrate(db)
from omniagentos.swarm.dal import SwarmDal, PUMP_LEDGER_RUN_ID, pump_verdict_hash
from omniagentos.swarm.scheduler import (
    DEFAULT_RETRY_CAP, pump_attempt_count, retry_cap_exceeded,
)

dal = SwarmDal(db)
task = dal.ensure_pump_lane("cap-lane")
assert DEFAULT_RETRY_CAP == 2, DEFAULT_RETRY_CAP
assert not retry_cap_exceeded(dal, task), "an untouched lane must not be capped"
for i in range(3):
    a = dal.open_attempt(PUMP_LEDGER_RUN_ID, task, provider="grok", model="grok-4.5",
                         verdict_hash=pump_verdict_hash(f"verdict {i}"), source="test")
    dal.close_attempt(a["id"], "review_denied", f"attempt {i}")
    if i < 2:
        assert not retry_cap_exceeded(dal, task), f"capped too early at {i + 1} attempts"
assert pump_attempt_count(dal, task) == 3, pump_attempt_count(dal, task)
assert retry_cap_exceeded(dal, task), "3 attempt rows must refuse the 4th dispatch"

# A COMPLETED attempt resets the consecutive count: the cap is a stall detector, not an
# expiry date. Without this a healthy lane would block on its fourth successful pass.
b = dal.open_attempt(PUMP_LEDGER_RUN_ID, task, provider="grok", model="grok-4.5",
                     verdict_hash=pump_verdict_hash("verdict fixed"), source="test")
dal.close_attempt(b["id"], "completed", "shipped")
assert pump_attempt_count(dal, task) == 0
assert not retry_cap_exceeded(dal, task)
print("CAP-OK")
PY
if grep -q '^CAP-OK$' "$SB/D3.out"; then pass "D3 the scheduler's retry cap refuses the 4th dispatch after 3 recorded attempts"
else fail "D3 retry cap did not read the rows the pumps write"; note "$(tail -6 "$SB/D3.out")"; fi

# The pump's own gate must reach the SAME verdict through the CLI the pumps actually call.
G=$("$PY" -W ignore::RuntimeWarning -m omniagentos.swarm.dal --db "$SB/state-D3.sqlite3" \
      pump-gate --lane cap-lane --verdict-hash brand-new 2>&1)
case "$G" in
  *"decision=allow"*) pass "D3 the pump gate allows again after a completed attempt (cap resets)" ;;
  *) fail "D3 the pump gate disagreed with the scheduler cap"; note "$G" ;;
esac

# =========================================================================================
# E — known synthetic brief-less clones are quarantined; real-disk clones are a bonus sweep
# =========================================================================================
E_ROOT="$SB/E"; E_CLONES="$E_ROOT/clones"; E_STAGE="$E_ROOT/stage"; E_Q="$E_ROOT/quarantine"
E_SYNTH="$E_ROOT/synthetic-briefless.txt"
mkdir -p "$E_CLONES" "$E_STAGE" "$E_Q"
: > "$E_SYNTH"
for LANE in e-known-briefless-one e-known-briefless-two; do
  mk_lane "$E_CLONES" "$LANE" "" ""
  mk_verdict "$E_STAGE" "$LANE" "s04 known brief-less fixture verdict"
  printf '%s\n' "$LANE" >> "$E_SYNTH"
done
"$PY" -m omniagentos.db.migrate "$SB/state-E-synthetic.sqlite3" >/dev/null 2>&1
OMNIAGENTOS_DB="$SB/state-E-synthetic.sqlite3" \
  run_rework "$E_STAGE" "$E_CLONES" "$E_Q" 1 "$SB/E-synthetic.log"
E_SYNTH_N=$(wc -l < "$E_SYNTH" | tr -d ' ')
E_SYNTH_MISSED=0
while IFS= read -r LANE; do
  grep -qE "^QUARANTINE lane=$LANE reason=missing-brief$" "$SB/E-synthetic.log" || {
    E_SYNTH_MISSED=$((E_SYNTH_MISSED+1)); note "synthetic fixture not quarantined: $LANE"; }
done < "$E_SYNTH"
E_SYNTH_DISPATCHED=$(count_re '^DISPATCH lane=' "$SB/E-synthetic.log")
if [ "$E_SYNTH_MISSED" = "0" ]; then
  pass "E quarantined all $E_SYNTH_N known synthetic brief-less clones"
else
  fail "E missed $E_SYNTH_MISSED of $E_SYNTH_N known synthetic brief-less clones"
fi
if [ "$E_SYNTH_DISPATCHED" = "0" ]; then
  pass "E dispatched zero known synthetic brief-less clones"
else
  fail "E dispatched $E_SYNTH_DISPATCHED known synthetic brief-less clone(s)"
fi

# Keep the old real-corpus sweep as an additional assertion when this checkout has clones.
REAL_CLONES="$REPO/var/swarm/clones"
E_REAL_STAGE="$E_ROOT/real-stage"; E_REAL_Q="$E_ROOT/real-quarantine"
E_REAL_BRIEFLESS="$E_ROOT/real-briefless.txt"
mkdir -p "$E_REAL_STAGE" "$E_REAL_Q"
: > "$E_REAL_BRIEFLESS"
if [ -d "$REAL_CLONES" ]; then
  for d in "$REAL_CLONES"/*/; do
    [ -d "$d" ] || continue
    LANE=$(basename "$d")
    FOUND=0
    for c in var/task.md LANE-BRIEF.md var/LANE-BRIEF.md TASK.md; do
      [ -s "$d/$c" ] && { FOUND=1; break; }
    done
    [ "$FOUND" = "0" ] && printf '%s\n' "$LANE" >> "$E_REAL_BRIEFLESS"
  done
fi
E_REAL_N=$(wc -l < "$E_REAL_BRIEFLESS" | tr -d ' '); : "${E_REAL_N:=0}"
if [ "$E_REAL_N" -ge 1 ] 2>/dev/null; then
  # Only the brief-less lanes are queued, so no real clone with a brief is written to.
  while IFS= read -r LANE; do
    [ -z "$LANE" ] && continue
    mk_verdict "$E_REAL_STAGE" "$LANE" "s04 real-disk sweep verdict"
  done < "$E_REAL_BRIEFLESS"
  "$PY" -m omniagentos.db.migrate "$SB/state-E-real.sqlite3" >/dev/null 2>&1
  OMNIAGENTOS_DB="$SB/state-E-real.sqlite3" \
    run_rework "$E_REAL_STAGE" "$REAL_CLONES" "$E_REAL_Q" 1 "$SB/E-real.log"
  E_REAL_MISSED=0
  while IFS= read -r LANE; do
    [ -z "$LANE" ] && continue
    grep -qE "^QUARANTINE lane=$LANE reason=(missing-brief|missing-clone)$" "$SB/E-real.log" || {
      E_REAL_MISSED=$((E_REAL_MISSED+1)); note "real-disk clone not quarantined: $LANE"; }
  done < "$E_REAL_BRIEFLESS"
  E_REAL_DISPATCHED=$(count_re '^DISPATCH lane=' "$SB/E-real.log")
  if [ "$E_REAL_MISSED" = "0" ]; then
    pass "E real-disk bonus quarantined all $E_REAL_N brief-less clones"
  else
    fail "E real-disk bonus missed $E_REAL_MISSED of $E_REAL_N brief-less clones"
  fi
  if [ "$E_REAL_DISPATCHED" = "0" ]; then
    pass "E real-disk bonus dispatched zero brief-less clones"
  else
    fail "E real-disk bonus dispatched $E_REAL_DISPATCHED brief-less clone(s)"
  fi
else
  note "E real-disk bonus not exercised: no brief-less clones under $REAL_CLONES"
fi

# Independently: no DISPATCH anywhere in this suite may name a lane without a brief.
BRIEFLESS="$E_ROOT/all-briefless.txt"
cat "$E_SYNTH" "$E_REAL_BRIEFLESS" > "$BRIEFLESS"
for L in "$SB"/*.log; do
  [ -f "$L" ] || continue
  grep -E '^DISPATCH lane=' "$L" 2>/dev/null | sed 's/^DISPATCH lane=//' | while IFS= read -r LN; do
    printf '%s\n' "$LN"
  done
done | sort -u > "$SB/dispatched-lanes.txt"
if grep -qxFf "$BRIEFLESS" "$SB/dispatched-lanes.txt" 2>/dev/null; then
  fail "E a brief-less lane appears in a DISPATCH line somewhere in this suite"
else
  pass "E no brief-less lane appears in ANY DISPATCH line"
fi

# =========================================================================================
# F — RESTART SURVIVAL: kill the pump, re-run it, the quarantine persists
# =========================================================================================
F_CLONES="$SB/F/clones"; F_STAGE="$SB/F/stage"; F_Q="$SB/F/quarantine"
mkdir -p "$F_CLONES" "$F_STAGE" "$F_Q"
mk_lane "$F_CLONES" restart-lane "" ""
mk_verdict "$F_STAGE" restart-lane "a verdict nobody can act on"
"$PY" -m omniagentos.db.migrate "$SB/state-F.sqlite3" >/dev/null 2>&1
# Run it as a real daemon (CYCLES=0, infinite) and KILL it, exactly as an operator would.
OMNIAGENTOS_DB="$SB/state-F.sqlite3" REPO="$REPO" \
  REWORK_STAGE="$F_STAGE" REWORK_CLONES="$F_CLONES" REWORK_QUARANTINE="$F_Q" \
  REWORK_CYCLES=0 REWORK_DRY_INTERVAL=1 REWORK_DRY_RUN=1 \
  REWORK_LOG="$SB/F-internal.log" bash "$REPO/scripts/rework-pump.sh" > "$SB/F1.log" 2>&1 &
F_PID=$!
sleep 3
kill -9 "$F_PID" 2>/dev/null
wait "$F_PID" 2>/dev/null
if [ -f "$F_Q/restart-lane/reason" ]; then pass "F the killed pump left a durable quarantine marker"
else fail "F no quarantine marker survived the kill"; fi
OMNIAGENTOS_DB="$SB/state-F.sqlite3" run_rework "$F_STAGE" "$F_CLONES" "$F_Q" 1 "$SB/F2.log"
F_NEWQ=$(count_re '^QUARANTINE lane=restart-lane' "$SB/F2.log")
F_SKIP=$(count_re '^SKIP lane=restart-lane reason=quarantined$' "$SB/F2.log")
F_DISP=$(count_re '^DISPATCH lane=' "$SB/F2.log")
if [ "$F_NEWQ" = "0" ] && [ "$F_SKIP" -ge 1 ] 2>/dev/null && [ "$F_DISP" = "0" ]; then
  pass "F the restarted pump READ the quarantine from disk (skip, no re-decide, no dispatch)"
else
  fail "F restart did not honour the persisted quarantine (requarantined=$F_NEWQ skips=$F_SKIP dispatches=$F_DISP)"
  note "$(head -8 "$SB/F2.log")"
fi

# =========================================================================================
printf '\n'
if [ "$FAILURES" = "0" ]; then
  printf 'S04 PUMP PREFLIGHT: ALL CHECKS PASSED\n'
  exit 0
fi
printf 'S04 PUMP PREFLIGHT: %s CHECK(S) FAILED\n' "$FAILURES"
[ "${S04_KEEP:-0}" = "1" ] && printf 'artifacts kept at %s\n' "$SB"
exit 1
