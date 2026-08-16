#!/bin/sh
# Install the WP8 twice-daily swarm optimizer launchd job
# (com.omniagentos.swarm-optimizer -- `python -m omniagentos.swarm.optimize`).
#
# Follows the install-steward.sh idiom (ARCHI.md "How to extend" -- New
# launchd job): render a plist template, source ~/.config/omni/connections.env
# inside the job itself (launchd's own environment is minimal/sanitized), and
# pin the project's .venv/bin/python (system python3 is 3.9 on macOS, too old
# for this repo's 3.12+ requirement).
#
# Schedule: 3:45 + 15:45 by default -- 15 minutes staggered off the
# selfimprove-curator's 3:30/15:30 slots (plan doc WP8) so the two twice-daily
# jobs never contend for the same DB/vault write window. Override with
# OMNIAGENTOS_SWARM_OPTIMIZER_HOUR_1/MINUTE_1/HOUR_2/MINUTE_2.
#
# This job is read-only analysis + local file writes (playbook.md, learned.json)
# plus at most one bounded Fable call -- it never spawns swarm runs/sessions or
# spends run budget, so (like selfimprove-curator and reliability-audit) it is
# safe to auto-load, unlike a job that creates real tasks (com.omniagentos.routines,
# render-only by design -- see docs/architecture/scheduling.md's split-decision
# pattern). It NEVER edits configs/swarm.yaml or any other config -- see
# omniagentos/swarm/optimize.py's module docstring.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_SWARM_OPTIMIZER_LAUNCHD_LABEL:-com.omniagentos.swarm-optimizer}
require_safe_launchd_label "$LABEL"
HOUR1=${OMNIAGENTOS_SWARM_OPTIMIZER_HOUR_1:-3}
MINUTE1=${OMNIAGENTOS_SWARM_OPTIMIZER_MINUTE_1:-45}
HOUR2=${OMNIAGENTOS_SWARM_OPTIMIZER_HOUR_2:-15}
MINUTE2=${OMNIAGENTOS_SWARM_OPTIMIZER_MINUTE_2:-45}
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
mkdir -p "$TARGET_DIR" "$ROOT_DIR/var/log" "$ROOT_DIR/var/swarm"

# launchd's system Python is 3.9 on supported macOS releases. OmniAgentOS
# requires 3.12+, so the job receives one pinned interpreter.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

python3 - "$SCRIPT_DIR/com.omniagentos.swarm-optimizer.plist.template" "$TARGET" "$LABEL" \
  "$ROOT_DIR" "$HOUR1" "$MINUTE1" "$HOUR2" "$MINUTE2" "$PYBIN" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).parent))
from launchd import render_template

template, target, label, root, hour1, minute1, hour2, minute2, pybin = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic

# connections.env is plain KEY=value (no `export`), so `set -a` is REQUIRED --
# a bare `. file` leaves the sourced vars shell-local and the job would run
# credential-blind (same gotcha install-steward.sh/reliability's installer
# both guard against).
script = (
    'set -a; . "$HOME/.config/omni/connections.env" 2>/dev/null; set +a; '
    f'. \"{root}/scripts/launch-env.sh\" 2>/dev/null; '
    f'exec "{pybin}" -m omniagentos.swarm.optimize'
)
content = render_template(
    Path(template).read_text(),
    label=label,
    program_args=["/bin/sh", "-lc", script],
    working_dir=root,
    hour1=int(hour1),
    minute1=int(minute1),
    hour2=int(hour2),
    minute2=int(minute2),
)
write_plist_atomic(target, content)
PY
echo "Wrote $TARGET"
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load "$TARGET"
echo "Loaded $TARGET"
