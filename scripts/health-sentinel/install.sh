#!/bin/sh
# Install (and load) the health-sentinel launchd job.
#
# Render step follows install-agent-watchdog.sh exactly: fill the template with
# scripts/health-sentinel/launchd.py into $VAR/launchd/rendered, and have the job
# itself source ~/.config/omni/connections.env.
#
# Unlike the render-only installers in this repo, this one ALSO installs into
# ~/Library/LaunchAgents and loads the job, because the sentinel is worthless
# unloaded -- and on 2026-08-01 an empty ~/Library/LaunchAgents next to sixteen
# rendered-but-never-installed plists was itself the outage. Fully idempotent:
# re-running re-renders, re-copies, boots the old job out and boots the new one
# in. Set HEALTH_SENTINEL_NO_LOAD=1 to stop after rendering + linting.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"

LABEL=${HEALTH_SENTINEL_LAUNCHD_LABEL:-com.omniagentos.health-sentinel}
INTERVAL=${HEALTH_SENTINEL_INTERVAL:-1800}
require_safe_launchd_label "$LABEL" "com.omniagentos.health-sentinel"

VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET="${TARGET_DIR}/${LABEL}.plist"
mkdir -p "$TARGET_DIR" "$ROOT_DIR/var/log" "$ROOT_DIR/var/health-sentinel"

JOB_SCRIPT="$SCRIPT_DIR/health-sentinel.sh"
chmod +x "$JOB_SCRIPT" "$SCRIPT_DIR/install.sh" 2>/dev/null || true

# Render with whatever python3 is on PATH (render-only, never the runtime).
python3 - "$SCRIPT_DIR/com.omniagentos.health-sentinel.plist.template" "$TARGET" "$LABEL" \
    "$ROOT_DIR" "$JOB_SCRIPT" "$INTERVAL" <<'PY'
import sys
from pathlib import Path

template, target, label, root, job_script, interval = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[1]).parent))
sys.path.insert(0, root)
from launchd import render_template
from scripts.lib.plist_write import write_plist_atomic
# --quiet under launchd: StandardOutPath is the SAME file the sentinel already
# writes its human log to, so without it every check line lands twice.
script = (
    'set -a; . "$HOME/.config/omni/connections.env" 2>/dev/null; set +a; '
    f'. \"{root}/scripts/launch-env.sh\" 2>/dev/null; '
    f'exec "{job_script}" --quiet'
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
echo "Wrote $TARGET"

# Lint the rendered artifact before anything loads it.
# plutil is macOS-only; on Linux CI hosts skip the lint rather than fail the install.
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET"
else
  echo "plutil unavailable on this host; skipping lint of $TARGET"
fi

if [ "${HEALTH_SENTINEL_NO_LOAD:-0}" = "1" ]; then
  echo "HEALTH_SENTINEL_NO_LOAD=1 -- rendered only, not installed/loaded."
  echo "Load manually with: launchctl bootstrap gui/\$(id -u) $TARGET"
  exit 0
fi

AGENTS_DIR="$HOME/Library/LaunchAgents"
INSTALLED="$AGENTS_DIR/${LABEL}.plist"
mkdir -p "$AGENTS_DIR"
cp "$TARGET" "$INSTALLED"
echo "Installed $INSTALLED"

UID_NUM=$(id -u)
# Idempotent: bootout an already-loaded copy first (a plain bootstrap over a
# live label fails with "service already loaded" and silently keeps the OLD
# plist -- which is how an "installed" fix never actually takes effect).
launchctl bootout "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/${UID_NUM}" "$INSTALLED"
launchctl enable "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true

echo "Loaded ${LABEL} (every ${INTERVAL}s)."
echo "Verify:  launchctl list | grep ${LABEL}"
echo "Run now: launchctl kickstart -p gui/${UID_NUM}/${LABEL}"

# The blocked-session detector is its OWN label at StartInterval 300 -- the line
# above must never be re-pointed at it. See ARM.md for what loading it does.
sh "$SCRIPT_DIR/install-blocked-session-detector.sh"
