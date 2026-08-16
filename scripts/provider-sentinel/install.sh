#!/bin/sh
# Render the provider-sentinel launchd job (ARCHI.md "How to extend": New
# launchd job -- product-scoped under com.omniagentos.
# fable-curator's daily schedule shape, but never load it here). Schedule:
# 22:30 daily, 30 minutes before fable-curator's 23:00,
# so every overnight job starts with fresh provider truth.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${PROVIDER_SENTINEL_LAUNCHD_LABEL:-com.omniagentos.provider-sentinel}
HOUR=${PROVIDER_SENTINEL_HOUR:-22}
MINUTE=${PROVIDER_SENTINEL_MINUTE:-30}
require_safe_launchd_label "$LABEL" "com.omniagentos.provider-sentinel"
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
mkdir -p "$TARGET_DIR"

# The job script is plain POSIX sh and resolves its own venv python + CLI
# PATH internally, so (like fable-curator's installer) there is no
# interpreter to pin here -- render with whatever python3 is on PATH
# (render-only, not the runtime).
python3 - "$SCRIPT_DIR/com.omniagentos.provider-sentinel.plist.template" "$TARGET" "$LABEL" \
    "$SCRIPT_DIR/provider-sentinel.sh" "$ROOT_DIR" "$HOUR" "$MINUTE" <<'PY'
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

# Lint the rendered artifact before an operator considers loading it.
# plutil is macOS-only; on Linux CI hosts skip the lint rather than fail the install.
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET"
else
  echo "plutil unavailable on this host; skipping lint of $TARGET"
fi

echo "NOT loaded. Review this exact product-scoped plist before loading:"
echo "  launchctl load $TARGET"
