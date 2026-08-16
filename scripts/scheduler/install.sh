#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_LAUNCHD_LABEL:-com.omniagentos.morning}
HOUR=${OMNIAGENTOS_MORNING_HOUR:-8}
MINUTE=${OMNIAGENTOS_MORNING_MINUTE:-0}
# Override with a fake dir in tests/CI; never auto-load into launchd.
require_safe_launchd_label "$LABEL" "com.omniagentos.morning"
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
mkdir -p "$TARGET_DIR"
# launchd runs with a minimal PATH where `python3` is system 3.9, which lacks the
# 3.11+ `datetime.UTC` the project needs (council OPS-002). Pin an explicit 3.12+
# interpreter: the project's uv venv if present, else python3.12 on PATH.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi
python3 - "$SCRIPT_DIR/com.omniagentos.morning.plist.template" "$TARGET" "$LABEL" "$ROOT_DIR" "$HOUR" "$MINUTE" "$PYBIN" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).parent))
from launchd import render_template

template, target, label, root, hour, minute, pybin = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic
content = render_template(Path(template).read_text(), label=label,
                          program_args=[pybin, "-m",
                                        "omniagentos.scheduler.morning_report"],
                          working_dir=root, hour=int(hour), minute=int(minute))
write_plist_atomic(target, content)
PY
echo "Wrote $TARGET"
echo "NOT loaded. Review the plist, then load manually if intended:"
echo "  launchctl load $TARGET"
