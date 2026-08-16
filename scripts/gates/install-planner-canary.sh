#!/bin/sh
# Render the hourly planner-canary launchd job (com.omniagentos.planner-canary).
#
# Follows the install-reflection.sh / install-archi-morning.sh conventions exactly:
# render into $VAR/launchd/rendered, source ~/.config/omni/connections.env inside
# the job itself, pin the project's .venv/bin/python. Label prefix is
# com.omniagentos.* — every loaded job on the box uses it; the original
# com.omniagentos.* naming was the SIBLING product's prefix and would have left a
# permanently mismatched pair after a reinstall.
#
# RENDER ONLY, does not load. Prints the launchctl load line.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"

LABEL=${OMNIAGENTOS_PLANNER_CANARY_LABEL:-com.omniagentos.planner-canary}
require_safe_launchd_label "$LABEL" "com.omniagentos.planner-canary"
INTERVAL=${OMNIAGENTOS_PLANNER_CANARY_INTERVAL:-3600}

VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
mkdir -p "$TARGET_DIR" "$VAR_ROOT/log"
TARGET=${TARGET_DIR}/${LABEL}.plist

PYBIN="$ROOT_DIR/.venv/bin/python"
if [ ! -x "$PYBIN" ]; then
  echo "error: no project interpreter at $PYBIN; run 'uv sync' first" >&2
  exit 1
fi

python3 - "$SCRIPT_DIR/com.omniagentos.planner-canary.plist.template" "$TARGET" "$LABEL" "$ROOT_DIR" "$PYBIN" "$INTERVAL" <<'PY'
import sys
from pathlib import Path

template, target, label, root, pybin, interval = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[1]).parent))
sys.path.insert(0, root)
from launchd import render_template
from scripts.lib.plist_write import write_plist_atomic
script = (
    'set -a; . "$HOME/.config/omni/connections.env" 2>/dev/null; set +a; '
    f'. \"{root}/scripts/launch-env.sh\" 2>/dev/null; '
    f'exec "{pybin}" -m omniagentos.gates.planner_canary'
)
write_plist_atomic(
    target,
    render_template(
        Path(template).read_text(),
        label=label,
        program_args=["/bin/sh", "-lc", script],
        working_dir=root,
        interval=int(interval),
    ),
)
PY
echo "Wrote $TARGET (not loaded)"
echo "Load with: launchctl load $TARGET"
