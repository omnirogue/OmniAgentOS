#!/bin/sh
# Render the AcmeUni PiedPiper pipeline-rollup collection launchd job.
#
# DELIBERATELY DOES NOT LOAD THE JOB. This script only renders the .plist
# file -- the lead loads it by hand AFTER review, and only once an operator
# has issued this collector a piedpiper_acmeuni.read grant (the collector itself
# preflights that grant and writes nothing without it, so loading this job
# before a grant exists is a harmless no-op every day, but the load step
# still stays a deliberate human action).
#
# Renders ONE job:
#   com.omniagentos.piedpiper-pipeline — StartCalendarInterval 03:00 (3 AM ET,
#                                  machine TZ is ET): the just-closed ET day's
#                                  read-only pipeline rollup.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
TARGET_DIR=${HOME}/Library/LaunchAgents
HOUR=${OMNIAGENTOS_PIEDPIPER_PIPELINE_HOUR:-3}
MINUTE=${OMNIAGENTOS_PIEDPIPER_PIPELINE_MINUTE:-0}
mkdir -p "$TARGET_DIR"

# launchd's minimal PATH resolves python3 to the system 3.9; OmniAgentOS needs
# 3.12+. Pin one interpreter for the generated job.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

"$PYBIN" - "$SCRIPT_DIR" "$ROOT_DIR" "$TARGET_DIR" "$PYBIN" "$HOUR" "$MINUTE" <<'PY'
import shlex
import sys
from pathlib import Path

script_dir = Path(sys.argv[1])
root = sys.argv[2]
target_dir = Path(sys.argv[3])
pybin = sys.argv[4]
hour = int(sys.argv[5])
minute = int(sys.argv[6])
sys.path.insert(0, str(script_dir))
sys.path.insert(0, root)

from launchd import render_template  # noqa: E402
from scripts.lib.plist_write import write_plist_atomic

# launchd jobs run with a sanitized environment: no connections.env, no
# credentials. Wrap the argv so it sources the operator env before exec'ing the
# pinned interpreter (set -a so plain KEY=value lines are exported to the child).
_SOURCE_ENV = (
    'set -a; . \"$HOME/.config/omni/connections.env\" 2>/dev/null; set +a; '
    f'. \"{root}/scripts/launch-env.sh\" 2>/dev/null; '
)


def wrapped_args(module_args):
    exec_cmd = " ".join(shlex.quote(part) for part in [pybin, *module_args])
    return ["/bin/sh", "-lc", _SOURCE_ENV + f"exec {exec_cmd}"]


module_args = ["-m", "omniagentos.piedpiper.pipeline_report", "--once"]

rendered = render_template(
    (script_dir / "com.omniagentos.piedpiper-pipeline.plist.template").read_text(encoding="utf-8"),
    label="com.omniagentos.piedpiper-pipeline",
    program_args=wrapped_args(module_args),
    working_dir=root,
    hour=hour,
    minute=minute,
)
write_plist_atomic(target_dir / "com.omniagentos.piedpiper-pipeline.plist", rendered)
PY

echo "Wrote $TARGET_DIR/com.omniagentos.piedpiper-pipeline.plist (3 AM ET pipeline rollup)"
echo
echo "NOT loaded -- this script only renders the plist. The collector itself"
echo "writes nothing until an operator issues it a piedpiper_acmeuni.read grant, so load"
echo "the plist by hand only after review AND after that grant exists (see"
echo "$TARGET_DIR/com.omniagentos.piedpiper-pipeline.plist for the load command)."
