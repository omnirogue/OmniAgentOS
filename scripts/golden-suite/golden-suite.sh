#!/bin/sh
# golden-suite -- the GOLDEN-SUITE SENTINEL's nightly runner, invoked by
# launchd at 01:00 machine-local time (com.omniagentos.golden-suite --
# scripts/golden-suite/install-golden-suite.sh + its plist template).
#
# Runs `run_golden.py` against the ALREADY-RUNNING local API
# (com.omniagentos.api, 127.0.0.1:8485 -- this job never starts/stops it)
# using the session token at var/secrets/sessions-token, following the same
# "pin .venv/bin/python, source connections.env with set -a" idiom every
# other python-based scheduled job in this repo uses (see
# scripts/swarm/install-swarm-optimizer.sh's own docstring).
#
# NO LLM calls happen in this script or in run_golden.py itself -- see
# run_golden.py's module docstring. The three fixed benchmark briefs it
# dispatches route through the system NORMALLY (real Fable/Codex/etc calls
# happen on the far side of the API); this script and its driver only make
# plain HTTP calls, poll, and run `sh -c` acceptance checks.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

RUN_LOG=${GOLDEN_SUITE_RUN_LOG:-$ROOT_DIR/var/log/golden-suite.log}
IMPROVEMENT_LOG=${GOLDEN_SUITE_IMPROVEMENT_LOG:-$ROOT_DIR/var/improvement-log.jsonl}

mkdir -p "$(dirname -- "$RUN_LOG")"
mkdir -p "$(dirname -- "$IMPROVEMENT_LOG")"

log() {
    printf '%s golden-suite %s
' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$RUN_LOG"
}

abort() {
    log "ABORT: $*"
    echo "golden-suite: $*" >&2
    exit 1
}

# launchd's system Python is 3.9 on supported macOS releases. OmniAgentOS
# requires 3.12+, so the job receives one pinned interpreter (same
# resolution order as scripts/swarm/install-swarm-optimizer.sh).
PYBIN=${GOLDEN_SUITE_VENV_PY:-}
if [ -z "$PYBIN" ]; then
    if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
        PYBIN="$ROOT_DIR/.venv/bin/python"
    elif command -v python3.12 >/dev/null 2>&1; then
        PYBIN=$(command -v python3.12)
    else
        abort "no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first"
    fi
fi
[ -x "$PYBIN" ] || abort "resolved interpreter is not executable: $PYBIN"

# `set -a; . file; set +a` -- a bare `. file` leaves the sourced vars
# shell-local (the recurring gotcha every other installer in this repo
# guards against; see docs/architecture/scheduling.md).
set -a
. "$HOME/.config/omni/connections.env" 2>/dev/null || true
set +a
. "$ROOT_DIR/scripts/launch-env.sh" 2>/dev/null || true

cd "$ROOT_DIR"
log "run start cwd=$ROOT_DIR pybin=$PYBIN"

# The improvement log's primary writer is run_golden.py itself (its final
# act, win or lose). Snapshot the line count so this runner can detect a
# driver that died before writing its line (a hard crash before it even gets
# there) and append a fallback entry -- every scheduled run leaves exactly
# one JSONL record either way (mirrors scripts/fable-curator/fable-curator.sh's
# own fallback idiom).
IMPROVEMENT_LINES_BEFORE=0
if [ -f "$IMPROVEMENT_LOG" ]; then
    IMPROVEMENT_LINES_BEFORE=$(wc -l < "$IMPROVEMENT_LOG" | tr -d ' ')
fi

RC=0
"$PYBIN" "$SCRIPT_DIR/run_golden.py" "$@" >> "$RUN_LOG" 2>&1 || RC=$?

if [ "$RC" -ne 0 ]; then
    log "run FAILED rc=$RC"
fi

IMPROVEMENT_LINES_AFTER=0
if [ -f "$IMPROVEMENT_LOG" ]; then
    IMPROVEMENT_LINES_AFTER=$(wc -l < "$IMPROVEMENT_LOG" | tr -d ' ')
fi
if [ "$IMPROVEMENT_LINES_AFTER" -le "$IMPROVEMENT_LINES_BEFORE" ]; then
    printf '{"ts":"%s","improver":"golden-sentinel","changes":[],"notes":"runner fallback: driver did not append its improvement-log line (rc=%s)"}
' \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        "$RC" >> "$IMPROVEMENT_LOG"
    log "improvement-log: appended runner fallback line (driver wrote none)"
fi

log "run end rc=$RC"
exit "$RC"
