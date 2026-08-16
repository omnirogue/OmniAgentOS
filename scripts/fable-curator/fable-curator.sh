#!/bin/sh
# Historical launchd compatibility entrypoint.
#
# The old job was a single Fable session. Model policy is now centralized in
# configs/loop_models.yaml and this scheduled slot runs the ordered chain:
#
#   Kimi suggestions -> Opus 5 X High plan edit -> Fable final review
#
# The label and path remain stable so an already-rendered launchd plist picks up
# the new behavior without an unload/reload.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
RUN_LOG=${FABLE_CURATOR_RUN_LOG:-$ROOT_DIR/var/log/fable-curator.log}
PYBIN=${FABLE_CURATOR_VENV_PY:-$ROOT_DIR/.venv/bin/python}

mkdir -p "$(dirname -- "$RUN_LOG")"
# launchd supplies only /usr/bin:/bin:/usr/sbin:/sbin. Kimi is installed by
# Homebrew and Claude's preferred binary lives under ~/.local/bin.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
export OMNIAGENTOS_ACCOUNT_POOL=1

# Alert-credential prelude (X-B1 / plan-2 B3, tightened per review): this
# plist did not previously source the operator vault at all, so a
# parked-chain alert (steward.notify.send_slack) had no webhook credential to
# deliver through and could never actually page anyone. This script does NOT
# source scripts/launch-env.sh (it never has -- the chain needs no other
# runtime env), so only the two alert-webhook variables are extracted here,
# each in its own throwaway subshell, rather than `set -a`-exporting the
# entire vault into this process and everything it execs.
if [ -f "$HOME/.config/omni/connections.env" ]; then
    OPS_ALERT_SLACK_WEBHOOK_URL=$(
        set -a
        . "$HOME/.config/omni/connections.env" 2>/dev/null || true
        printf '%s' "${OPS_ALERT_SLACK_WEBHOOK_URL:-}"
    )
    SLACK_WEBHOOK_URL=$(
        set -a
        . "$HOME/.config/omni/connections.env" 2>/dev/null || true
        printf '%s' "${SLACK_WEBHOOK_URL:-}"
    )
    export OPS_ALERT_SLACK_WEBHOOK_URL SLACK_WEBHOOK_URL
fi

log() {
    printf '%s loop-plan-review %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$RUN_LOG"
}

if [ ! -x "$PYBIN" ]; then
    log "ABORT: python missing at $PYBIN"
    echo "loop-plan-review: python missing at $PYBIN" >&2
    exit 1
fi

cd "$ROOT_DIR"
log "start chain=Kimi->Opus5/xhigh->Fable5"
RC=0
"$PYBIN" -m omniagentos.improvement_chain >> "$RUN_LOG" 2>&1 || RC=$?
log "end rc=$RC"
exit "$RC"
