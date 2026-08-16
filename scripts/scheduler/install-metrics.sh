#!/bin/sh
# Render the ten-minute swarm measurement tick.  This installer never loads it.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_METRICS_LABEL:-com.omniagentos.metrics}
require_safe_launchd_label "$LABEL" "com.omniagentos.metrics"
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var/runtime}
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

"$PYBIN" - "$SCRIPT_DIR" "$ROOT_DIR" "$TARGET" "$LABEL" "$PYBIN" <<'PY'
import os
import shlex
import sys
from pathlib import Path

script_dir = Path(sys.argv[1])
root = Path(sys.argv[2])
target = Path(sys.argv[3])
label = sys.argv[4]
pybin = sys.argv[5]
sys.path.insert(0, str(script_dir))

from launchd import render_template
sys.path.insert(0, str(root))
from scripts.lib.plist_write import write_plist_atomic

# launchd starts with a sanitized environment.  Preserve the installer
# caller's product runtime values (the production DB is commonly outside a
# private worktree), then source launch-env.sh for its remaining defaults.
database = os.environ.get("OMNIAGENTOS_DB") or str(root / "var" / "runtime" / "state.sqlite3")
var_root = os.environ.get("OMNIAGENTOS_VAR_DIR") or os.environ.get("OMNIAGENTOS_VAR") or str(Path(database).parent)
runtime = {
    "OMNIAGENTOS_DB": database,
    "OMNIAGENTOS_VAR_DIR": var_root,
    "OMNIAGENTOS_VAR": os.environ.get("OMNIAGENTOS_VAR") or var_root,
    "OMNIAGENTOS_LEDGER_DIR": os.environ.get("OMNIAGENTOS_LEDGER_DIR") or str(Path(var_root) / "ledger"),
    "OMNIAGENTOS_VAULT_DIR": os.environ.get("OMNIAGENTOS_VAULT_DIR") or str(Path(var_root) / "vault"),
}
exports = " ".join(
    f"export {name}={shlex.quote(value)};" for name, value in runtime.items()
)
source_env = exports + f" . {shlex.quote(str(root / 'scripts' / 'launch-env.sh'))}; "
exec_cmd = " ".join(shlex.quote(part) for part in [pybin, "-m", "omniagentos.scheduler.metrics_tick"])
program_args = ["/bin/sh", "-lc", source_env + f"exec {exec_cmd}"]

rendered = render_template(
    (script_dir / "com.omniagentos.metrics.plist.template").read_text(encoding="utf-8"),
    label=label,
    program_args=program_args,
    working_dir=str(root),
    hour=0,
    minute=0,
)
write_plist_atomic(target, rendered)
PY

echo "Wrote $TARGET (every 10 minutes)"
echo
echo "NOT loaded. Review, then bootstrap manually:"
echo "  launchctl bootstrap gui/$(id -u) $TARGET"
