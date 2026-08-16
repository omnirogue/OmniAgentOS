#!/bin/sh
# Install the daily model-intelligence refresh (launchd). Mirrors install.sh
# (morning report): pin a 3.12+ interpreter because launchd's minimal PATH
# resolves python3 to the system 3.9. XAI_API_KEY is NOT injected here —
# omniagentos.modelintel.config falls back to ~/.config/omni/connections.env.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_MODELINTEL_LABEL:-com.omniagentos.modelintel}
require_safe_launchd_label "$LABEL"
HOUR=${OMNIAGENTOS_MODELINTEL_HOUR:-7}
MINUTE=${OMNIAGENTOS_MODELINTEL_MINUTE:-15}
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
mkdir -p "$TARGET_DIR"
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi
python3 - "$SCRIPT_DIR/com.omniagentos.modelintel.plist.template" "$TARGET" "$LABEL" "$ROOT_DIR" "$HOUR" "$MINUTE" "$PYBIN" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).parent))
from launchd import render_template

template, target, label, root, hour, minute, pybin = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic
content = render_template(Path(template).read_text(), label=label,
                          program_args=[pybin, "-m",
                                        "omniagentos.modelintel.daily"],
                          working_dir=root, hour=int(hour), minute=int(minute))
write_plist_atomic(target, content)
PY
echo "Wrote $TARGET"
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"
echo "Loaded $TARGET"
