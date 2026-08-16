#!/bin/sh
# Install the fable-curator launchd job (rail 5: launchd schedule).
#
# Renders com.omniagentos.fable-curator.plist.template into
# ~/Library/LaunchAgents, lints the rendered plist with plutil, then
# loads it. Review README.md and fable-curator.sh BEFORE running this.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${FABLE_CURATOR_LAUNCHD_LABEL:-com.omniagentos.fable-curator}
require_safe_launchd_label "$LABEL"
# 23:00 machine-local time = 11PM ET nightly (this machine runs US Eastern).
HOUR=${FABLE_CURATOR_HOUR:-23}
MINUTE=${FABLE_CURATOR_MINUTE:-0}
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
mkdir -p "$TARGET_DIR"

# The job script is plain POSIX sh and resolves its own claude binary, so
# unlike the python-based siblings there is no interpreter to pin. Render
# the plist with whatever python3 is on PATH (render-only, not the runtime).
python3 - "$SCRIPT_DIR/com.omniagentos.fable-curator.plist.template" "$TARGET" "$LABEL" \
    "$SCRIPT_DIR/fable-curator.sh" "$ROOT_DIR" "$HOUR" "$MINUTE" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).parent))
from launchd import render_template

template, target, label, job, root, hour, minute = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic
content = render_template(
    Path(template).read_text(),
    label=label,
    program_args=["/bin/sh", job],
    working_dir=root,
    hour=int(hour),
    minute=int(minute),
)
write_plist_atomic(target, content)
PY
echo "Wrote $TARGET"

# Lint before load -- a malformed plist would otherwise fail silently at
# the next scheduled fire time.
# plutil is macOS-only; on Linux CI hosts skip the lint rather than fail the install.
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET"
else
  echo "plutil unavailable on this host; skipping lint of $TARGET"
fi

launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"
echo "Loaded $TARGET (daily at $HOUR:$(printf '%02d' "$MINUTE"))"
