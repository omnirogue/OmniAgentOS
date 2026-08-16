#!/bin/sh
# Render the ESTATE HYGIENE nightly launchd job
# (com.omniagentos.hygiene -- scripts/hygiene/hygiene.sh, daily 04:15).
#
# Follows the install-swarm-optimizer.sh idiom (ARCHI.md "How to extend" --
# New launchd job): render a plist template, source ~/.config/omni/connections.env
# inside the job itself (launchd's own environment is minimal/sanitized), and
# pin the project's .venv/bin/python (the wrapped script re-resolves it too,
# but the installer fails fast here so a missing venv is caught at install
# time, not silently at 04:15). Single daily StartCalendarInterval (not the
# twice-daily array-of-dicts shape) -- see scripts/hygiene/launchd.py.
#
# Schedule: 04:15 daily by default -- after the 23:00 fable-curator night and
# well clear of the 02:00-03:45 banking/revenue/selfimprove-curator/swarm-
# optimizer slots, before the 06:30 reliability-audit and 07:05 archi-morning
# runs (a hygiene sweep should be settled before the morning docs refresh
# reads the estate). Override with OMNIAGENTOS_HYGIENE_HOUR/MINUTE.
#
# hygiene.py's ONLY two git/fs-mutating sweeps (merged-branch delete,
# worktree remove) are both narrowly safety-gated (merged-only, clean-only,
# never forced) -- see its module docstring and scripts/hygiene/prompt.md.
# Everything else is archive-then-move, never delete. Per the split-decision
# pattern (docs/architecture/scheduling.md) it may be suitable for an operator
# to load after reviewing the exact rendered plist. This script never loads it.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_HYGIENE_LAUNCHD_LABEL:-com.omniagentos.hygiene}
HOUR=${OMNIAGENTOS_HYGIENE_HOUR:-4}
MINUTE=${OMNIAGENTOS_HYGIENE_MINUTE:-15}
require_safe_launchd_label "$LABEL" "com.omniagentos.hygiene"
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
JOB_SCRIPT="$SCRIPT_DIR/hygiene.sh"
mkdir -p "$TARGET_DIR"

# Fail fast if the pinned interpreter is missing (hygiene.sh checks again at
# run time, but a broken install should not load silently).
if [ ! -x "$ROOT_DIR/.venv/bin/python" ] && ! command -v python3.12 >/dev/null 2>&1; then
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

python3 - "$SCRIPT_DIR/com.omniagentos.hygiene.plist.template" "$TARGET" "$LABEL" \
  "$ROOT_DIR" "$HOUR" "$MINUTE" "$JOB_SCRIPT" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[4])
from scripts.hygiene.launchd import render_template
from scripts.lib.plist_write import write_plist_atomic

template, target, label, root, hour, minute, job_script = sys.argv[1:]

# connections.env is plain KEY=value (no `export`), so `set -a` is REQUIRED --
# a bare `. file` leaves the sourced vars shell-local and the job would run
# credential-blind (same gotcha every installer in this repo guards against).
script = (
    'set -a; . "$HOME/.config/omni/connections.env" 2>/dev/null; set +a; '
    f'. \"{root}/scripts/launch-env.sh\" 2>/dev/null; '
    f'exec "{job_script}"'
)
content = render_template(
    Path(template).read_text(),
    label=label,
    program_args=["/bin/sh", "-lc", script],
    working_dir=root,
    hour=int(hour),
    minute=int(minute),
)
write_plist_atomic(target, content)
PY
echo "Wrote $TARGET"
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET"
fi
echo "NOT loaded. Review this exact product-scoped plist before loading:"
echo "  launchctl load $TARGET"
