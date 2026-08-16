#!/bin/sh
# Nightly Reflection Loop Execution (com.omniagentos.reflection-nightly, daily 02:30).
#
# Runs: harvest -> propose -> validate -> apply(observe-aware) -> report.
#
# Invoked as `/bin/sh <this-script>` by launchd (D3 fix for exit 126). Does not
# require the executable bit, though install-reflection.sh keeps mode 0755.
#
# Observe-only: the runner is always launched with observe semantics unless an
# operator explicitly flips observe_only after the mandated one-shadow-week hold.
# Re-arm is gated by OMNIAGENTOS_REFLECTION_REARM_MODE (default off).
#
# All output is appended to var/log/reflection-nightly.log; the plist points
# StandardOut/ErrorPath at the same file so launchd-level failures land there too.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
LOG_FILE="$ROOT_DIR/var/log/reflection-nightly.log"
mkdir -p "$ROOT_DIR/var/log"
exec >>"$LOG_FILE" 2>&1

log() {
  printf '%s reflection-nightly: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

# Source operator connections (same as the former -lc wrapper).
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
    log "mode=off; not running reflection (set OMNIAGENTOS_REFLECTION_REARM_MODE=shadow|enforce to arm)"
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
log "start (root=$ROOT_DIR python=$PYBIN observe_only=1)"

# Always observe-only until the shadow-week gate is explicitly cleared by an
# operator. Pass the flag EXPLICITLY: relying on the runner default lets an
# inherited OMNIAGENTOS_REFLECTION_OBSERVE_ONLY=0 (this script sources
# connections.env with `set -a`) silently enable unattended apply while the log
# above still attests observe_only=1.
"$PYBIN" -m omniagentos.reflection.runner --observe-only

# Fable approval gate over pending LOW-RISK proposals. Default mode is shadow:
# verdicts are recorded to var/loop-review/fable-gate/ and the improvement log,
# and no proposal row changes. Flip OMNIAGENTOS_FABLE_GATE_MODE=on to let an
# approve verdict apply through the same path as a human dashboard approval
# (higher-risk kinds and anything uncertain always stay pending for the human).
# Never blocks the nightly: gate failures degrade to needs_human internally.
log "fable-gate mode=${OMNIAGENTOS_FABLE_GATE_MODE:-shadow}"
"$PYBIN" -m omniagentos.reflection.fable_gate || log "fable-gate failed (non-blocking)"
log "done"
