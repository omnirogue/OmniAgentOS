#!/bin/sh
# Install (and load) the daily North Star tier-1 certification job
# (com.omniagentos.nscert-t1 -- scripts/northstar-cert-cadence/t1_cadence.sh).
#
# Render step follows scripts/backlog-executor/install.sh exactly: fill the
# plist template via this package's launchd.py into $VAR/launchd/rendered, pin
# the project's .venv interpreter (launchd's PATH has neither `uv` nor `make`,
# and its system python3 is 3.9), and have the job source
# ~/.config/omni/connections.env itself.
#
# Like scripts/health-sentinel/install.sh -- and UNLIKE the render-only
# installers -- this one also copies into ~/Library/LaunchAgents and boots the
# job, because a cadence that is never loaded is not a cadence. On 2026-08-01 an
# empty ~/Library/LaunchAgents next to sixteen rendered-but-never-installed
# plists WAS the outage. Fully idempotent: re-running re-renders, re-copies,
# boots the old label out and the new one in.
#
# Schedule: 06:10 daily. Override with NSCERT_CADENCE_HOUR/MINUTE.
#
# DRY-RUN BY DEFAULT (deliberate): the job ships with NSCERT_GAPS_LIVE=0 baked
# into the rendered plist -- it certifies, records receipts and emits dry-run
# gap artifacts, and writes NOTHING to var/loopqueue. Arm it, after reviewing a
# dry-run day in var/northstar-cert/gaps-dryrun/, with the one-line flip:
#
#     NSCERT_GAPS_LIVE=1 sh scripts/northstar-cert-cadence/install.sh
#
# (re-renders + reloads with live filing on; flip back with =0). Set
# NSCERT_CADENCE_NO_LOAD=1 to stop after rendering + linting.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"

LABEL=${NSCERT_CADENCE_LAUNCHD_LABEL:-com.omniagentos.nscert-t1}
HOUR=${NSCERT_CADENCE_HOUR:-6}
MINUTE=${NSCERT_CADENCE_MINUTE:-10}
LIVE=${NSCERT_GAPS_LIVE:-0}
require_safe_launchd_label "$LABEL" "com.omniagentos.nscert-t1"

case "$LIVE" in
  0|1) ;;
  *)
    echo "error: NSCERT_GAPS_LIVE must be 0 or 1, got '$LIVE'" >&2
    exit 1
    ;;
esac

VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET="${TARGET_DIR}/${LABEL}.plist"
mkdir -p "$TARGET_DIR" "$ROOT_DIR/var/log" "$ROOT_DIR/var/northstar-cert"

JOB_SCRIPT="$SCRIPT_DIR/t1_cadence.sh"
chmod +x "$JOB_SCRIPT" "$SCRIPT_DIR/install.sh" 2>/dev/null || true

# launchd's system Python is 3.9 on supported macOS releases. OmniAgentOS
# requires 3.12+, so the job receives one pinned interpreter -- and the runner
# refuses to start if it is missing rather than falling back to /usr/bin/python3.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

# Render with whatever python3 is on PATH (render-only, never the runtime).
python3 - "$SCRIPT_DIR/com.omniagentos.nscert-t1.plist.template" "$TARGET" "$LABEL" \
    "$ROOT_DIR" "$HOUR" "$MINUTE" "$PYBIN" "$JOB_SCRIPT" "$LIVE" <<'PY'
import sys
from pathlib import Path

template, target, label, root, hour, minute, pybin, job_script, live = sys.argv[1:]
sys.path.insert(0, str(Path(sys.argv[1]).parent))
sys.path.insert(0, root)
from launchd import render_template
from scripts.lib.plist_write import write_plist_atomic

# connections.env is plain KEY=value (no `export`), so `set -a` is REQUIRED --
# a bare `. file` leaves the sourced vars shell-local. NSCERT_GAPS_LIVE is
# exported AFTER it on purpose: the rendered plist is the authority on whether
# this job may write to the loop queue, not an ambient value in connections.env.
script = (
    'set -a; . "$HOME/.config/omni/connections.env" 2>/dev/null; set +a; '
    f'. \"{root}/scripts/launch-env.sh\" 2>/dev/null; '
    f"export NSCERT_GAPS_LIVE={live}; "
    f'export NSCERT_VENV_PY=\"{pybin}\"; '
    f'exec "{job_script}"'
)
write_plist_atomic(
    target,
    render_template(
        Path(template).read_text(),
        label=label,
        program_args=["/bin/sh", "-lc", script],
        working_dir=root,
        hour=int(hour),
        minute=int(minute),
    ),
)
PY
echo "Wrote $TARGET (NSCERT_GAPS_LIVE=$LIVE, ${HOUR}:${MINUTE} daily)"

# Lint the rendered artifact before anything loads it.
# plutil is macOS-only; on Linux CI hosts skip the lint rather than fail the install.
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET"
else
  echo "plutil unavailable on this host; skipping lint of $TARGET"
fi

# The arming state must be verifiable in the artifact, not just in this script.
if ! grep -q "NSCERT_GAPS_LIVE=$LIVE" "$TARGET"; then
  echo "error: rendered plist does not carry NSCERT_GAPS_LIVE=$LIVE; refusing to install" >&2
  exit 1
fi

if [ "${NSCERT_CADENCE_NO_LOAD:-0}" = "1" ]; then
  echo "NSCERT_CADENCE_NO_LOAD=1 -- rendered only, not installed/loaded."
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

echo "Loaded ${LABEL} (daily at ${HOUR}:${MINUTE})."
echo "Verify:  launchctl list | grep ${LABEL}"
echo "Run now: launchctl kickstart -p gui/${UID_NUM}/${LABEL}"
echo "Log:     $ROOT_DIR/var/log/nscert-t1.log"
if [ "$LIVE" = "0" ]; then
  echo "DRY-RUN mode: dry-run gap artifacts only, zero loop-queue writes."
  echo "Arm it after reviewing a dry-run day:"
  echo "  NSCERT_GAPS_LIVE=1 sh scripts/northstar-cert-cadence/install.sh"
fi
