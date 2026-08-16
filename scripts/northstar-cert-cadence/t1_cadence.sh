#!/bin/bash
# One daily North Star tier-1 certification cycle, run by launchd at 06:10
# (com.omniagentos.nscert-t1, StartCalendarInterval).
#
# This is `make nscert-t1` WITHOUT make and WITHOUT uv, because neither is on
# launchd's PATH: the recipe's steps are replicated here against the pinned
# interpreter at .venv/bin/python. If you change the make recipe, change this
# too -- they are two carriers of one procedure.
#
# bash (not sh) on purpose: scripts/launch-env.sh resolves its own location via
# ${BASH_SOURCE[0]} and must be sourced by a bash-compatible shell to pick the
# repo root rather than $(pwd).
#
# Arming: NSCERT_GAPS_LIVE=1 turns on live gap emission + queue filing. The
# rendered plist bakes =0. Nothing here can arm itself.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

TIER=${NSCERT_TIER:-t1}
MANIFEST=${NSCERT_MANIFEST:-$ROOT_DIR/configs/northstar-cert/manifest.yaml}
RESULTS_DIR=${NSCERT_RESULTS_DIR:-$ROOT_DIR/var/northstar-cert}
GAPS_DIR=${NSCERT_GAPS_DIR:-$RESULTS_DIR/gaps}
QUEUE=${NSCERT_QUEUE:-$ROOT_DIR/var/loopqueue}
LIVE=${NSCERT_GAPS_LIVE:-0}
RUN_LOG=${NSCERT_RUN_LOG:-$ROOT_DIR/var/log/nscert-t1.log}
mkdir -p "$(dirname -- "$RUN_LOG")" "$RESULTS_DIR"

log() {
    printf '%s nscert-%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$TIER" "$*" >> "$RUN_LOG"
}

# Canonical OMNIAGENTOS_DB / account-pool env (idempotent; never clobbers a
# preset). Sourced BEFORE resolving python so a preset venv still wins.
# shellcheck source=scripts/launch-env.sh
if ! . "$ROOT_DIR/scripts/launch-env.sh"; then
    log "WARN: launch-env.sh returned nonzero; continuing with ambient env"
fi

VENV_PY=${NSCERT_VENV_PY:-${OMNIAGENTOS_PYTHON:-$ROOT_DIR/.venv/bin/python}}
if [ ! -x "$VENV_PY" ]; then
    log "ABORT: venv python not found at $VENV_PY (run 'uv sync' first)"
    echo "nscert-cadence: venv python not found at $VENV_PY" >&2
    exit 1
fi
if [ ! -f "$MANIFEST" ]; then
    log "ABORT: manifest not found at $MANIFEST"
    echo "nscert-cadence: manifest not found at $MANIFEST" >&2
    exit 1
fi

cd "$ROOT_DIR"
RUN_ID="nscert-${TIER}-$(date -u +%Y%m%dT%H%M%SZ)"
JUNIT="$RESULTS_DIR/${TIER}-junit.xml"
log "run start run_id=$RUN_ID live=$LIVE py=$VENV_PY"

if [ "$TIER" = "t1" ]; then
    "$VENV_PY" "$ROOT_DIR/scripts/northstar_cert/seed_holdout.py" >> "$RUN_LOG" 2>&1
fi

# Operator-declared satisfied requirements (space separated), forwarded to BOTH
# target selection and grading -- if the two disagree about what is masked, the
# run collects nodes it then refuses to grade. launchd: tokens need no flag:
# the recorder probes them itself.
AVAIL_ARGS=()
for requirement in ${NSCERT_AVAILABLE_REQUIREMENTS:-}; do
    AVAIL_ARGS+=(--available-requirement "$requirement")
done

# Target selection is the RECORDER's, never a second YAML reader: a requires-
# masked or pending node id aborts pytest COLLECTION for the whole run, so the
# selector and the grader have to be one implementation. This is the same
# --list-targets call the Makefile makes.
RC=0
TARGETS=$("$VENV_PY" "$ROOT_DIR/scripts/northstar_cert/record_results.py" \
    --manifest "$MANIFEST" --tier "$TIER" --list-targets \
    ${AVAIL_ARGS[@]+"${AVAIL_ARGS[@]}"} 2>> "$RUN_LOG") || RC=$?
if [ "$RC" -ne 0 ]; then
    log "ABORT: target selection failed (exit $RC); no results recorded"
    echo "nscert-cadence: target selection failed (exit $RC)" >&2
    exit "$RC"
fi
if [ -z "${TARGETS//[[:space:]]/}" ]; then
    log "ABORT: manifest selected zero runnable $TIER pytest targets"
    echo "nscert-cadence: manifest selected zero runnable $TIER pytest targets" >&2
    exit 70
fi

# pytest exit codes: 0 all passed, 1 tests failed. BOTH are certification
# evidence and the run continues. Anything else (2 interrupted, 3 internal
# error, 4 usage error, 5 nothing collected) means the instrument did not run --
# recording that junit would mark every check NOT_EVALUABLE and, when armed,
# file a queue full of findings about a pytest invocation. Refuse instead.
# The junit is removed first so a failed pytest can never leave a STALE file
# for the recorder to read as this run's evidence.
rm -f "$JUNIT"
RC=0
# The -m expression is a deliberate TAUTOLOGY, and it is load-bearing.
# pyproject.toml's addopts carry a default marker exclusion (counterfeit_gate,
# e2e, livesim, perf, live*) for the DEFAULT whole-suite selection. These
# TARGETS are not a default selection: every one is an explicit node id the
# manifest binds a check to -- and pytest applies -m filters to explicit node
# ids as well. So the counterfeit-bound hard gates (C11-01, C43-01/02, C43-08)
# were silently DESELECTED, pytest still exited 0, and the recorder read a junit
# with no row for them and rendered NOT_EVALUABLE(not_executed) instead of a
# verdict -- a certification hole that looks like a mechanics refusal.
# A command-line -m overrides the addopts one (addopts are prepended, so the
# last -m wins) while preserving every OTHER addopt. What the manifest selects,
# the run executes.
# TARGETS is a deliberate word-split argument list (one pytest node id per check).
# shellcheck disable=SC2086
"$VENV_PY" -m pytest -q -m "counterfeit_gate or not counterfeit_gate" \
    --junitxml="$JUNIT" $TARGETS >> "$RUN_LOG" 2>&1 || RC=$?
if [ "$RC" -gt 1 ]; then
    log "ABORT: pytest could not run (exit $RC); no results recorded"
    echo "nscert-cadence: pytest could not run (exit $RC)" >&2
    exit "$RC"
fi
log "pytest rc=$RC junit=$JUNIT"

# The recorder's rc IS the run's verdict (0 CERTIFIED/MEASURED, 1 FAILED,
# 2 INCONCLUSIVE, 70 VOID), so it is captured, not swallowed, and it is what
# this script finally exits with. Under `set -e` the `|| RECORD_RC=$?` is what
# keeps the nonzero verdict from killing the run before its gaps are emitted.
RECORD_RC=0
"$VENV_PY" "$ROOT_DIR/scripts/northstar_cert/record_results.py" \
    --manifest "$MANIFEST" --tier "$TIER" --junitxml "$JUNIT" --run-id "$RUN_ID" \
    ${AVAIL_ARGS[@]+"${AVAIL_ARGS[@]}"} >> "$RUN_LOG" 2>&1 || RECORD_RC=$?
log "results recorded run_id=$RUN_ID recorder_rc=$RECORD_RC"

if [ "$RECORD_RC" -eq 70 ]; then
    # VOID: the run was not measurable. Emitting gaps here would file
    # instrument faults as candidate product defects.
    log "ABORT: run is VOID (recorder exit 70); no gaps emitted"
    echo "nscert-cadence: run is VOID (recorder exit 70)" >&2
    exit 70
fi

STAGE_RC=0
if [ "$LIVE" = "1" ]; then
    "$VENV_PY" "$ROOT_DIR/scripts/northstar_cert/emit_gaps.py" \
        --run-id "$RUN_ID" --output-dir "$GAPS_DIR" --live --resolve >> "$RUN_LOG" 2>&1 \
        || STAGE_RC=$?
    if [ "$STAGE_RC" -eq 0 ]; then
        "$VENV_PY" "$ROOT_DIR/scripts/northstar_cert/file_gap_findings.py" \
            --gaps-dir "$GAPS_DIR" --queue "$QUEUE" --live >> "$RUN_LOG" 2>&1 || STAGE_RC=$?
    fi
    log "live: gaps emitted to $GAPS_DIR and filed into $QUEUE (stage rc=$STAGE_RC)"
else
    "$VENV_PY" "$ROOT_DIR/scripts/northstar_cert/emit_gaps.py" --run-id "$RUN_ID" \
        >> "$RUN_LOG" 2>&1 || STAGE_RC=$?
    log "dry-run: gaps emitted to var/northstar-cert/gaps-dryrun, queue untouched (stage rc=$STAGE_RC)"
fi

# The verdict wins; a broken emit/file stage can only speak when the verdict was
# clean, so this can never report success for a run that failed either way.
EXIT_RC=$RECORD_RC
if [ "$EXIT_RC" -eq 0 ]; then
    EXIT_RC=$STAGE_RC
fi
log "run end run_id=$RUN_ID rc=$EXIT_RC"
exit "$EXIT_RC"
