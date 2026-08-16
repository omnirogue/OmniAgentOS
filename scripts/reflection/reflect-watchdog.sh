#!/bin/sh
# Reflection Loop Watchdog Inspection (com.omniagentos.reflection-watchdog, daily 07:30).
#
# Verifies completion, budget sizes, briefing, valid proposals schema, and git hard-stops.
#
# Invoked as `/bin/sh <this-script>` by launchd (D3 fix for exit 126).
# Re-arm is gated by OMNIAGENTOS_REFLECTION_REARM_MODE (default off).
#
# All output is appended to var/log/reflection-watchdog.log; the plist points
# StandardOut/ErrorPath at the same file so launchd-level failures land there too.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
LOG_FILE="$ROOT_DIR/var/log/reflection-watchdog.log"
mkdir -p "$ROOT_DIR/var/log"
exec >>"$LOG_FILE" 2>&1

log() {
  printf '%s reflection-watchdog: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

if [ -f "$HOME/.config/omni/connections.env" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$HOME/.config/omni/connections.env"
  set +a
fi

MODE=${OMNIAGENTOS_REFLECTION_REARM_MODE:-off}
log "mode=${MODE}"
case "$MODE" in
  off)
    log "mode=off; not running watchdog (set OMNIAGENTOS_REFLECTION_REARM_MODE=shadow|enforce to arm)"
    exit 0
    ;;
  shadow|enforce)
    ;;
  *)
    log "unknown mode=${MODE}; treating as off"
    exit 0
    ;;
esac

if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  log "error: no 3.12+ interpreter (.venv/bin/python or python3.12); run 'uv sync' first"
  exit 1
fi

cd "$ROOT_DIR"
log "start (root=$ROOT_DIR python=$PYBIN)"

"$PYBIN" -m omniagentos.reflection.watchdog
log "done"
