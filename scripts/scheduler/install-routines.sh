#!/bin/sh
# Render the routines-fire tick job (launchd).
#
# DELIBERATELY DOES NOT `launchctl load`. Firing a routine creates a real
# task + run per its declared task_template — potentially spending money via
# whatever harness the routine names — so the lead loads it by hand AFTER
# review. This script only writes the .plist file and prints the exact load
# command.
#
# com.omniagentos.routines — StartInterval 300 (every 5 minutes): runs
# `python -m omniagentos.scheduler.routines_tick`, which fires every routine
# whose trigger is due right now and skips everything paused, over its hard
# cap, or under the 50% auto-pause acceptance floor. A 5-minute cadence means
# a cron trigger's minute field should generally be a multiple of 5 to fire
# reliably (see routines_tick's DUE-check docstring for the exact semantics).
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_ROUTINES_LABEL:-com.omniagentos.routines}
# Override with a fake dir in tests/CI; deliberately never launchctl load.
require_safe_launchd_label "$LABEL" "com.omniagentos.routines"
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var/runtime}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
mkdir -p "$TARGET_DIR"

# launchd's minimal PATH resolves python3 to the system 3.9; OmniAgentOS
# needs 3.12+. Pin one interpreter for the generated job.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

"$PYBIN" - "$SCRIPT_DIR" "$ROOT_DIR" "$TARGET" "$LABEL" "$PYBIN" <<'PY'
import shlex
import sys
from pathlib import Path

script_dir = Path(sys.argv[1])
root = sys.argv[2]
target = Path(sys.argv[3])
label = sys.argv[4]
pybin = sys.argv[5]
sys.path.insert(0, str(script_dir))
sys.path.insert(0, str(root))

from launchd import render_template  # noqa: E402
from scripts.lib.plist_write import write_plist_atomic

# launchd jobs run with a sanitized environment: no connections.env, no
# credentials, none of the product defaults. Wrap the argv so it sources the
# operator env (set -a so plain KEY=value lines are exported to the child) and
# then the canonical product include — launch-env.sh honours presets, so
# operator overrides still win while the tick gains the same OMNIAGENTOS_DB
# (var/runtime/state.sqlite3), vault dir, and capability flags that
# `make api` runs with: one world, one DB the API serves.
_SOURCE_ENV = (
    'set -a; . "$HOME/.config/omni/connections.env" 2>/dev/null; set +a; '
    f'. {shlex.quote(str(Path(root) / "scripts" / "launch-env.sh"))}; '
)
exec_cmd = " ".join(
    shlex.quote(part) for part in [pybin, "-m", "omniagentos.scheduler.routines_tick"]
)
program_args = ["/bin/sh", "-lc", _SOURCE_ENV + f"exec {exec_cmd}"]

rendered = render_template(
    (script_dir / "com.omniagentos.routines.plist.template").read_text(encoding="utf-8"),
    label=label,
    program_args=program_args,
    working_dir=root,
    hour=0,
    minute=0,
)
write_plist_atomic(target, rendered)
PY

echo "Wrote $TARGET (every 5 minutes)"
echo
echo "NOT loaded. Firing a routine enqueues real tasks/runs — load after review:"
echo "  launchctl load $TARGET"
