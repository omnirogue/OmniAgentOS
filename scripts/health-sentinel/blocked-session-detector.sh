#!/bin/bash
# blocked-session-detector -- one blocked-session sweep, run by launchd every
# 5 min (com.omniagentos.blocked-session-detector, StartInterval 300).
#
# WHY ITS OWN LABEL AND NOT THE SENTINEL'S
# The health sentinel's label stays at StartInterval 1800 and is NOT touched by
# this package. A 5-minute stall detector and a 30-minute drift audit have
# different SLOs, and coupling them means one slow audit starves detection for
# half an hour. Separate labels, separate logs, separate failure domains.
#
# DISARMED BY DEFAULT: --no-push is the default in health_sentinel.py, so this
# job DECIDES and RECORDS to var/log/blocked-session-alerts.jsonl but delivers
# nothing. See scripts/health-sentinel/ARM.md for the one command that arms it
# and its blast radius.
#
# bash (not sh) on purpose: scripts/launch-env.sh resolves its own location via
# ${BASH_SOURCE[0]} and must be sourced by a bash-compatible shell.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

RUN_LOG=${BLOCKED_DETECTOR_RUN_LOG:-$ROOT_DIR/var/log/blocked-session-detector.log}
mkdir -p "$(dirname -- "$RUN_LOG")"

log() {
    printf '%s blocked-session-detector %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$RUN_LOG"
}

# shellcheck source=scripts/launch-env.sh
if ! . "$ROOT_DIR/scripts/launch-env.sh"; then
    log "WARN: launch-env.sh returned nonzero; continuing with ambient env"
fi

VENV_PY=${HEALTH_SENTINEL_VENV_PY:-${OMNIAGENTOS_PYTHON:-$ROOT_DIR/.venv/bin/python}}
if [ ! -x "$VENV_PY" ]; then
    log "ABORT: venv python not found at $VENV_PY (run 'uv sync' first)"
    echo "blocked-session-detector: venv python not found at $VENV_PY" >&2
    exit 1
fi

# PREFLIGHT: fail fast and loudly rather than sweep with a broken predicate.
# A missing registry degrades to the built-in default (documented in
# blocked_sessions.load_detector_config) but is worth one loud line, because a
# detector silently running on a guess it was supposed to have replaced is the
# exact failure mode this program exists to remove.
if [ ! -f "$ROOT_DIR/configs/audit-checks.yaml" ]; then
    log "WARN: configs/audit-checks.yaml missing; T falls back to the built-in 15m default"
fi

# terminal-notifier (the banner transport inside sessions/notify.py) is resolved
# by NAME from PATH; launchd's PATH is minimal.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

cd "$ROOT_DIR"
log "sweep start cwd=$ROOT_DIR"
RC=0
"$VENV_PY" "$SCRIPT_DIR/health_sentinel.py" --watch-blocked "$@" || RC=$?
log "sweep end rc=$RC"
exit "$RC"
