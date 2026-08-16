#!/bin/sh
# Render the nightly backlog-executor launchd job
# (com.omniagentos.backlog-executor -- scripts/backlog-executor/executor.py).
#
# Follows the install-swarm-optimizer.sh idiom (ARCHI.md "How to extend" --
# New launchd job): render a plist template, source
# ~/.config/omni/connections.env inside the job itself (launchd's own
# environment is minimal/sanitized), and pin the project's .venv/bin/python.
#
# Schedule: 00:30 daily. Override with OMNIAGENTOS_BACKLOG_HOUR/MINUTE.
#
# DRY-RUN FIRST NIGHT (deliberate): the job ships with
# OMNIAGENTOS_BACKLOG_DRY_RUN=1 baked into the plist -- night 1 collects
# candidates, runs grok selection, logs + digests the picks, and dispatches
# NOTHING. After reviewing var/backlog/digest-<date>.md, ARM it with the
# one-line flip:
#
#     OMNIAGENTOS_BACKLOG_DRY_RUN=0 sh scripts/backlog-executor/install.sh
#
# (re-renders the plist with dry-run off; review it and replace any live job
# explicitly; flip back with =1).
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
LABEL=${OMNIAGENTOS_BACKLOG_LAUNCHD_LABEL:-com.omniagentos.backlog-executor}
HOUR=${OMNIAGENTOS_BACKLOG_HOUR:-0}
MINUTE=${OMNIAGENTOS_BACKLOG_MINUTE:-30}
DRY_RUN=${OMNIAGENTOS_BACKLOG_DRY_RUN:-1}
require_safe_launchd_label "$LABEL" "com.omniagentos.backlog-executor"
VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET=${TARGET_DIR}/${LABEL}.plist
mkdir -p "$TARGET_DIR"

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

python3 - "$SCRIPT_DIR/com.omniagentos.backlog-executor.plist.template" "$TARGET" "$LABEL" \
  "$ROOT_DIR" "$HOUR" "$MINUTE" "$PYBIN" "$SCRIPT_DIR/executor.py" "$DRY_RUN" <<'PY'
import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).parent))
from launchd import render_template

template, target, label, root, hour, minute, pybin, executor, dry_run = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic

# connections.env is plain KEY=value (no `export`), so `set -a` is REQUIRED --
# a bare `. file` leaves the sourced vars shell-local (the same gotcha the
# steward/swarm installers guard against). The dry-run flag is baked into the
# rendered plist so the armed/dry state is explicit in the rendered artifact.
script = (
    'set -a; . "$HOME/.config/omni/connections.env" 2>/dev/null; set +a; '
    f'. \"{root}/scripts/launch-env.sh\" 2>/dev/null; '
    f"export OMNIAGENTOS_BACKLOG_DRY_RUN={dry_run}; "
    f'exec "{pybin}" "{executor}"'
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
echo "Wrote $TARGET (OMNIAGENTOS_BACKLOG_DRY_RUN=$DRY_RUN)"

# Lint the rendered artifact before an operator considers loading it.
# plutil is macOS-only; on Linux CI hosts skip the lint rather than fail the install.
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET"
else
  echo "plutil unavailable on this host; skipping lint of $TARGET"
fi

echo "NOT loaded. Review this exact product-scoped plist before loading:"
echo "  launchctl load $TARGET"
if [ "$DRY_RUN" = "1" ]; then
  echo "DRY-RUN mode: selection only, zero dispatches."
  echo "Arm it after reviewing night-1 picks:"
  echo "  OMNIAGENTOS_BACKLOG_DRY_RUN=0 sh scripts/backlog-executor/install.sh"
fi
