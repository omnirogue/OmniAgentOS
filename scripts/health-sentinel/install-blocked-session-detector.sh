#!/bin/sh
# Install (and load) the blocked-session-detector launchd job.
#
# Mirrors install.sh exactly -- render with launchd.py into $VAR/launchd/rendered,
# lint with plutil, copy into ~/Library/LaunchAgents, bootout the old label, then
# bootstrap the new one. Fully idempotent: re-running re-renders and re-loads.
#
# THIS JOB IS SEPARATE FROM THE HEALTH SENTINEL ON PURPOSE. The sentinel's label
# stays at StartInterval 1800; this one runs at 300. A 5-minute stall detector
# and a 30-minute drift audit have different SLOs, and one slow audit must never
# starve stall detection.
#
# DISARMED BY DEFAULT. health_sentinel.py defaults to --no-push, so once loaded
# this job DECIDES and RECORDS blocked sessions to
# var/log/blocked-session-alerts.jsonl and delivers nothing. Arming pushes is a
# separate, explicit act -- see scripts/health-sentinel/ARM.md.
#
# BLOCKED_DETECTOR_NO_LOAD=1 stops after rendering + linting.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"

LABEL=${BLOCKED_DETECTOR_LAUNCHD_LABEL:-com.omniagentos.blocked-session-detector}
INTERVAL=${BLOCKED_DETECTOR_INTERVAL:-300}
require_safe_launchd_label "$LABEL" "com.omniagentos.blocked-session-detector"

VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET="${TARGET_DIR}/${LABEL}.plist"
mkdir -p "$TARGET_DIR" "$ROOT_DIR/var/log"

JOB_SCRIPT="$SCRIPT_DIR/blocked-session-detector.sh"
chmod +x "$JOB_SCRIPT" "$SCRIPT_DIR/install-blocked-session-detector.sh" 2>/dev/null || true

python3 - "$SCRIPT_DIR/com.omniagentos.blocked-session-detector.plist.template" "$TARGET" "$LABEL" \
    "$ROOT_DIR" "$JOB_SCRIPT" "$INTERVAL" <<'PY'
import sys
from pathlib import Path

template, target, label, root, job_script, interval = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[1]).parent))
sys.path.insert(0, root)
from launchd import render_template
from scripts.lib.plist_write import write_plist_atomic
# --quiet: StandardOutPath is the same file the job already writes its human log
# to, so without it every sweep line lands twice.
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

# plutil is macOS-only; on Linux CI hosts skip the lint rather than fail the install.
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET"
else
  echo "plutil unavailable on this host; skipping lint of $TARGET"
fi

if [ "${BLOCKED_DETECTOR_NO_LOAD:-0}" = "1" ]; then
  echo "BLOCKED_DETECTOR_NO_LOAD=1 -- rendered only, not installed/loaded."
  echo "Load manually with: launchctl bootstrap gui/\$(id -u) $TARGET"
  exit 0
fi

AGENTS_DIR="$HOME/Library/LaunchAgents"
INSTALLED="$AGENTS_DIR/${LABEL}.plist"
mkdir -p "$AGENTS_DIR"
cp "$TARGET" "$INSTALLED"
echo "Installed $INSTALLED"

UID_NUM=$(id -u)
launchctl bootout "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/${UID_NUM}" "$INSTALLED"
launchctl enable "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true

echo "Loaded ${LABEL} (every ${INTERVAL}s, push DISARMED -- see ARM.md)."
echo "Verify:  launchctl list | grep ${LABEL}"
echo "Run now: launchctl kickstart -p gui/${UID_NUM}/${LABEL}"
