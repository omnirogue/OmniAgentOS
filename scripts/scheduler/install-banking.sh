#!/bin/sh
# Render the Cash / banking collection launchd jobs.
#
# DELIBERATELY DOES NOT `launchctl load`. This is a real financial job that makes
# live (READ-ONLY) external calls to Slash; the lead loads it by hand AFTER
# review. This script only writes the .plist files and prints the exact load
# commands.
#
# It renders TWO jobs:
#   1. com.omniagentos.banking          — StartCalendarInterval 02:00 (2 AM ET,
#                                          machine TZ is ET): the authoritative
#                                          snapshot of the just-closed ET day's
#                                          balances, deposits and true expenses.
#   2. com.omniagentos.banking.hourly   — StartInterval 3600: keeps the day fresh
#                                          as late transactions post (the store
#                                          UPSERTs, so this is safe).
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
TARGET_DIR=${HOME}/Library/LaunchAgents
HOUR=${OMNIAGENTOS_BANKING_HOUR:-2}
MINUTE=${OMNIAGENTOS_BANKING_MINUTE:-0}
mkdir -p "$TARGET_DIR"

# launchd's minimal PATH resolves python3 to the system 3.9; OmniAgentOS needs
# 3.12+. Pin one interpreter for the generated jobs.
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


module_args = ["-m", "omniagentos.banking.collect", "--once"]

# 1) Authoritative 2 AM ET snapshot (StartCalendarInterval template).
authoritative = render_template(
    (script_dir / "com.omniagentos.banking.plist.template").read_text(encoding="utf-8"),
    label="com.omniagentos.banking",
    program_args=wrapped_args(module_args),
    working_dir=root,
    hour=hour,
    minute=minute,
)
write_plist_atomic(target_dir / "com.omniagentos.banking.plist", authoritative)

# 2) Hourly refresh (StartInterval 3600) — reuse the steward metrics template,
#    which is the StartInterval variant.
hourly = render_template(
    (script_dir / "com.omniagentos.steward.metrics.plist.template").read_text(encoding="utf-8"),
    label="com.omniagentos.banking.hourly",
    program_args=wrapped_args(module_args),
    working_dir=root,
    hour=0,
    minute=0,
)
write_plist_atomic(target_dir / "com.omniagentos.banking.hourly.plist", hourly)
PY

echo "Wrote $TARGET_DIR/com.omniagentos.banking.plist (2 AM ET authoritative snapshot)"
echo "Wrote $TARGET_DIR/com.omniagentos.banking.hourly.plist (hourly refresh)"
echo
echo "NOT loaded. This makes live read-only financial API calls — load after review:"
echo "  launchctl load $TARGET_DIR/com.omniagentos.banking.plist"
echo "  launchctl load $TARGET_DIR/com.omniagentos.banking.hourly.plist"
