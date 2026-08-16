#!/bin/bash
# health-sentinel -- one agent-health check cycle, run by launchd every 30 min
# (com.omniagentos.health-sentinel, StartInterval 1800).
#
# See health_sentinel.py's module docstring for the ten checks. Makes NO LLM
# calls and spawns no provider CLI: every signal is a file read, a localhost
# HTTP GET, a read-only SQLite SELECT, `ps`, or `launchctl list`.
#
# bash (not sh) on purpose: scripts/launch-env.sh resolves its own location via
# ${BASH_SOURCE[0]} and must be sourced by a bash-compatible shell to pick the
# repo root rather than $(pwd).
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

RUN_LOG=${HEALTH_SENTINEL_RUN_LOG:-$ROOT_DIR/var/log/health-sentinel.log}
mkdir -p "$(dirname -- "$RUN_LOG")"

log() {
    printf '%s health-sentinel %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$RUN_LOG"
}

# Canonical OMNIAGENTOS_DB / OMNIAGENTOS_API_PORT / account-pool env (idempotent, never
# clobbers a preset). Sourced BEFORE resolving python so a preset venv still wins.
# shellcheck source=scripts/launch-env.sh
if ! . "$ROOT_DIR/scripts/launch-env.sh"; then
    log "WARN: launch-env.sh returned nonzero; continuing with ambient env"
fi

VENV_PY=${HEALTH_SENTINEL_VENV_PY:-${OMNIAGENTOS_PYTHON:-$ROOT_DIR/.venv/bin/python}}
if [ ! -x "$VENV_PY" ]; then
    log "ABORT: venv python not found at $VENV_PY (run 'uv sync' first)"
    echo "health-sentinel: venv python not found at $VENV_PY" >&2
    exit 1
fi

# launchd's PATH is minimal; the sentinel itself only shells out to absolute
# /bin/ps and /bin/launchctl, but terminal-notifier (the banner transport, under
# /opt/homebrew/bin) is resolved by name from PATH inside sessions/notify.py.
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

cd "$ROOT_DIR"
log "run start cwd=$ROOT_DIR db=${OMNIAGENTOS_DB:-unset} api_port=${OMNIAGENTOS_API_PORT:-unset}"
RC=0
"$VENV_PY" "$SCRIPT_DIR/health_sentinel.py" "$@" || RC=$?
log "run end rc=$RC"
exit "$RC"
