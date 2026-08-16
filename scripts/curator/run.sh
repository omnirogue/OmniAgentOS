#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_CURATOR_LAUNCHD_LABEL:-com.omniagentos.curator}
HOUR1=${OMNIAGENTOS_CURATOR_HOUR_1:-7}
MINUTE1=${OMNIAGENTOS_CURATOR_MINUTE_1:-0}
HOUR2=${OMNIAGENTOS_CURATOR_HOUR_2:-19}
MINUTE2=${OMNIAGENTOS_CURATOR_MINUTE_2:-0}
require_safe_launchd_label "$LABEL" "com.omniagentos.curator"
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
mkdir -p "$TARGET_DIR"
# launchd runs with a minimal PATH where `python3` is system 3.9, which lacks
# the 3.11+ `datetime.UTC` the project needs (council OPS-002 -- the same
# gotcha the H1 scheduler works around). Pin an explicit 3.12+ interpreter:
# the project's uv venv if present, else python3.12 on PATH. This resolved
# PYBIN is what actually runs in the plist's ProgramArguments -- the plist
# uses the venv python directly, exactly like the H1 scheduler.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi
# Defense-in-depth (BINDING, contracts/lab-interfaces.md HD-001): the curator
# agent must never see the L02 protected-store pointer. A launchd job already
# starts from launchd's own minimal environment (this plist sets no
# EnvironmentVariables key), but unset it here too in case an operator's
# interactive shell had it exported before running this installer --
# omniagentos.lab.curator.env re-scrubs it again at call time regardless.
unset OMNIAGENTOS_EVAL_PROTECTED 2>/dev/null || true
python3 - "$SCRIPT_DIR/com.omniagentos.curator.plist.template" "$TARGET" "$LABEL" "$ROOT_DIR" \
  "$HOUR1" "$MINUTE1" "$HOUR2" "$MINUTE2" "$PYBIN" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[4])
from scripts.curator.launchd import render_template
from scripts.lib.plist_write import write_plist_atomic

template, target, label, root, hour1, minute1, hour2, minute2, pybin = sys.argv[1:]
content = render_template(
    Path(template).read_text(),
    label=label,
    program_args=[pybin, "-m", "omniagentos.lab.curator"],
    working_dir=root,
    hour1=int(hour1),
    minute1=int(minute1),
    hour2=int(hour2),
    minute2=int(minute2),
)
write_plist_atomic(target, content)
PY
echo "Wrote $TARGET"
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET"
fi
echo "NOT loaded. Review this exact product-scoped plist before loading:"
echo "  launchctl load $TARGET"
