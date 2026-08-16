#!/bin/sh
# Render the daily cache-GC launchd agent WITHOUT loading it.
#
# Deliberately "rendered, not loaded": this writes the .plist so an operator can
# review the retention window and schedule first, then load it by hand. Unlike
# install.sh it never calls `launchctl load` — deleting rows on a schedule is an
# opt-in the operator turns on explicitly.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_CACHE_GC_LABEL:-com.omniagentos.cache-gc}
require_safe_launchd_label "$LABEL"
HOUR=${OMNIAGENTOS_CACHE_GC_HOUR:-3}
MINUTE=${OMNIAGENTOS_CACHE_GC_MINUTE:-0}
TARGET_DIR=${HOME}/Library/LaunchAgents
TARGET=${TARGET_DIR}/${LABEL}.plist
mkdir -p "$TARGET_DIR"
# launchd runs with a minimal PATH where `python3` is system 3.9, which lacks the
# 3.11+ `datetime.UTC` the project needs. Pin an explicit 3.12+ interpreter: the
# project's uv venv if present, else python3.12 on PATH.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi
python3 - "$SCRIPT_DIR/com.omniagentos.cache-gc.plist.template" "$TARGET" "$LABEL" "$ROOT_DIR" "$HOUR" "$MINUTE" "$PYBIN" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).parent))
from launchd import render_template

template, target, label, root, hour, minute, pybin = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic
content = render_template(Path(template).read_text(), label=label,
                          program_args=[pybin, "-m",
                                        "omniagentos.maintenance.cache_gc"],
                          working_dir=root, hour=int(hour), minute=int(minute))
write_plist_atomic(target, content)
PY
echo "Wrote $TARGET (not loaded)."
echo "Review it, then enable with:  launchctl load \"$TARGET\""
