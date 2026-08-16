#!/bin/sh
# provider-sentinel -- nightly provider & account health preflight, run by
# launchd at 22:30 local time -- 30 minutes before fable-curator's 23:00, so
# every overnight job starts with fresh provider truth (ARCHI.md
# "Scheduling"). See sentinel.py's module docstring for the full pass
# (provider doctor -> usage refresh -> alerting -> curator handoff).
#
# Makes NO LLM calls of its own (build brief item 5): every CLI subprocess
# here is provider_doctor's own LIVE auth/process check against codex/grok/
# gemini/kimi, never a Claude/Fable session for narration. Claude (the fifth
# provider) is deliberately never live-doctored -- see sentinel.py.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

VENV_PY=${PROVIDER_SENTINEL_VENV_PY:-$ROOT_DIR/.venv/bin/python}
RUN_LOG=${PROVIDER_SENTINEL_RUN_LOG:-$ROOT_DIR/var/log/provider-sentinel.log}

mkdir -p "$(dirname -- "$RUN_LOG")"

log() {
    printf '%s provider-sentinel %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$RUN_LOG"
}

if [ ! -x "$VENV_PY" ]; then
    log "ABORT: venv python not found at $VENV_PY (run 'uv sync' first)"
    echo "provider-sentinel: venv python not found at $VENV_PY" >&2
    exit 1
fi

# provider_doctor spawns the bare CLI names (codex/grok/gemini/kimi) and
# relies on PATH to find them; launchd's own PATH is minimal (the same
# gotcha fable-curator.sh's claude-binary resolver documents). The three
# known install locations on this machine are prepended explicitly --
# codex/gemini live under nvm's node bin, kimi under homebrew, grok under
# its own dedicated dir. This is the same PATH the api/runner/dashboard
# launchd jobs already export, PLUS grok's directory (the one gap those
# jobs have that this job does not repeat): a "binary not found" PATH miss
# must never masquerade as the CLI exit-semantics failures this job expects
# to observe and alert on.
export PATH="$HOME/.nvm/versions/node/v22.22.0/bin:$HOME/.grok/bin:/opt/homebrew/bin:$PATH"

cd "$ROOT_DIR"
log "run start cwd=$ROOT_DIR"
RC=0
"$VENV_PY" "$SCRIPT_DIR/sentinel.py" --log-path "$RUN_LOG" || RC=$?
log "run end rc=$RC"
exit "$RC"
