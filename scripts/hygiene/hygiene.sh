#!/bin/sh
# ESTATE HYGIENE -- nightly cleanup sweep (com.omniagentos.hygiene, daily
# 04:15). Runs scripts/hygiene/hygiene.py under the pinned repo venv
# interpreter and appends everything to var/log/hygiene.log.
#
# ARCHIVE, NEVER DELETE: hygiene.py never removes anything from its working
# location without an archive copy landing first (see its module docstring
# for the two narrowly-scoped exceptions: git branch -d on already-merged
# branches, and worktree removal gated on clean+merged). This wrapper is
# pure plumbing -- it does no cleanup itself.
#
# Follows the install-swarm-optimizer.sh idiom (ARCHI.md "How to extend" --
# New launchd job): pin the project's .venv/bin/python (system python3 is
# 3.9 on macOS, too old for this repo's 3.12+ requirement), source
# ~/.config/omni/connections.env with `set -a` (launchd's own environment is
# minimal/sanitized -- a bare `. file` would leave sourced vars shell-local).
# hygiene.py itself needs no external credentials (pure git/fs/sqlite3
# local ops), but the wrapper sources connections.env anyway for parity with
# every other scheduled job in this repo and in case a future sweep needs it.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
LOG_FILE="$ROOT_DIR/var/log/hygiene.log"
mkdir -p "$ROOT_DIR/var/log"
exec >>"$LOG_FILE" 2>&1

log() {
  printf '%s hygiene.sh: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
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
RC=0
"$PYBIN" "$SCRIPT_DIR/hygiene.py" --repo-root "$ROOT_DIR" || RC=$?
log "done rc=$RC"
exit "$RC"
