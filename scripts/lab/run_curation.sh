#!/bin/sh
# Observe-first lab curation loop (com.omniagentos.lab-curation, daily 03:20).
#
# Thin wrapper for scripts/lab/curation_loop.py: it pins an interpreter, runs the
# N4r exec-bit self-test, then runs ONE observe-only proposal pass and exits.
# It never promotes, never executes an experiment, and never loads a launchd job.
#
# All output is appended to var/log/lab-curation.log; the rendered plist points
# StandardOut/ErrorPath at the same file so launchd-level failures land there too.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
LOG_FILE="$ROOT_DIR/var/log/lab-curation.log"
mkdir -p "$ROOT_DIR/var/log"
exec >>"$LOG_FILE" 2>&1

log() {
  printf '%s lab-curation: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

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

# N4r guard: refuse to run at all if this wrapper or the runner lost mode 0755.
if ! "$PYBIN" "$SCRIPT_DIR/curation_loop.py" self-test; then
  log "error: self-test failed; not running the curation pass"
  exit 3
fi

"$PYBIN" "$SCRIPT_DIR/curation_loop.py" run
log "done"
