#!/bin/sh
# Render the daily MORNING repo-map launchd job
# (com.omniagentos.archi-morning -- scripts/archi-morning/archi-morning.sh).
#
# Follows the install-steward.sh idiom (ARCHI.md "How to extend" -- New
# launchd job): render a plist template, source ~/.config/omni/connections.env
# inside the job itself (launchd's own environment is minimal/sanitized), and
# pin the project's .venv/bin/python (the wrapped script re-resolves it too,
# but the installer fails fast here so a missing venv is caught at install
# time, not silently at 05:30).
#
# Schedule: 05:30 daily by default -- after the 23:00 curator night and the
# nightly maintenance jobs, before the workday (and before the 07:15
# modelintel / 07:30 steward briefing slots, so the morning reads see a fresh
# map). Override with OMNIAGENTOS_ARCHI_MORNING_HOUR/MINUTE.
#
# This job is read-only scans + owned-doc writes (ARCHI.md,
# docs/architecture/system-map.*) + a diff-guarded local commit -- it never
# creates tasks/runs/sessions or spends budget, so per the split-decision
# pattern (docs/architecture/scheduling.md) an operator may choose to load it
# after reviewing the exact rendered plist. This script never loads it.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_ARCHI_MORNING_LAUNCHD_LABEL:-com.omniagentos.archi-morning}
HOUR=${OMNIAGENTOS_ARCHI_MORNING_HOUR:-5}
MINUTE=${OMNIAGENTOS_ARCHI_MORNING_MINUTE:-30}
# Override with a fake dir in tests/CI; never auto-load into launchd.
require_safe_launchd_label "$LABEL" "com.omniagentos.archi-morning"
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
JOB_SCRIPT="$SCRIPT_DIR/archi-morning.sh"
mkdir -p "$TARGET_DIR"

# Fail fast if the pinned interpreter is missing (the job script checks again
# at run time, but a broken install should not load silently).
if [ ! -x "$ROOT_DIR/.venv/bin/python" ] && ! command -v python3.12 >/dev/null 2>&1; then
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

python3 - "$SCRIPT_DIR/com.omniagentos.archi-morning.plist.template" "$TARGET" "$LABEL" \
  "$ROOT_DIR" "$HOUR" "$MINUTE" "$JOB_SCRIPT" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).parent))
from launchd import render_template

template, target, label, root, hour, minute, job_script = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic

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
echo "NOT loaded. Review the plist, then load manually if intended:"
echo "  launchctl load $TARGET"
