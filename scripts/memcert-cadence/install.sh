#!/bin/sh
# Install (and load) the daily memcert tier-1 memory-certification job
# (com.omniagentos.memcert-t1 -- scripts/memcert-cadence/t1_cadence.sh).
#
# Follows scripts/northstar-cert-cadence/install.sh exactly (render via that
# package's launchd.py -- ONE render carrier, not a copy), pins the project's
# .venv interpreter, and has the job source ~/.config/omni/connections.env
# itself. Copies into ~/Library/LaunchAgents and boots the job: a cadence that
# is never loaded is not a cadence (2026-08-01 outage).
#
# Schedule: 06:40 daily (after nscert-t1 at 06:10). Override with
# MEMCERT_CADENCE_HOUR/MINUTE.
#
# DRY-RUN BY DEFAULT: MEMCERT_LIVE=0 is baked into the rendered plist -- the
# job certifies, records junit + benchmark artifacts under var/memcert/, and
# the hypothesizer writes its would-be proposals to the OUTBOX only. Arm the
# loop-queue leg, after reviewing a dry-run day in
# var/memcert/hypotheses/outbox/, with:
#
#     MEMCERT_LIVE=1 sh scripts/memcert-cadence/install.sh
#
# Set MEMCERT_CADENCE_NO_LOAD=1 to stop after rendering + linting.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"

LABEL=${MEMCERT_CADENCE_LAUNCHD_LABEL:-com.omniagentos.memcert-t1}
HOUR=${MEMCERT_CADENCE_HOUR:-6}
MINUTE=${MEMCERT_CADENCE_MINUTE:-40}
LIVE=${MEMCERT_LIVE:-0}
require_safe_launchd_label "$LABEL" "com.omniagentos.memcert-t1"

case "$LIVE" in
  0|1) ;;
  *)
    echo "error: MEMCERT_LIVE must be 0 or 1, got '$LIVE'" >&2
    exit 1
    ;;
esac

VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
TARGET="${TARGET_DIR}/${LABEL}.plist"
mkdir -p "$TARGET_DIR" "$ROOT_DIR/var/log" "$ROOT_DIR/var/memcert"

JOB_SCRIPT="$SCRIPT_DIR/t1_cadence.sh"
chmod +x "$JOB_SCRIPT" "$SCRIPT_DIR/install.sh" 2>/dev/null || true

if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

# Render with whatever python3 is on PATH (render-only, never the runtime).
# The render helper is the nscert cadence's launchd.py -- one carrier.
python3 - "$SCRIPT_DIR/com.omniagentos.memcert-t1.plist.template" "$TARGET" "$LABEL" \
    "$ROOT_DIR" "$HOUR" "$MINUTE" "$PYBIN" "$JOB_SCRIPT" "$LIVE" <<'PY'
import sys
from pathlib import Path

template, target, label, root, hour, minute, pybin, job_script, live = sys.argv[1:]
sys.path.insert(0, str(Path(root) / "scripts" / "northstar-cert-cadence"))
sys.path.insert(0, root)
from launchd import render_template
from scripts.lib.plist_write import write_plist_atomic

# connections.env is plain KEY=value (no `export`), so `set -a` is REQUIRED.
# MEMCERT_LIVE is exported AFTER it on purpose: the rendered plist is the
# authority on whether this job's hypothesizer may write to the loop queue.
script = (
    'set -a; . "$HOME/.config/omni/connections.env" 2>/dev/null; set +a; '
    f'. \"{root}/scripts/launch-env.sh\" 2>/dev/null; '
    f"export MEMCERT_LIVE={live}; "
    f'export MEMCERT_VENV_PY=\"{pybin}\"; '
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
echo "Wrote $TARGET (MEMCERT_LIVE=$LIVE, ${HOUR}:${MINUTE} daily)"

# plutil is macOS-only; on Linux CI hosts skip the lint rather than fail the install.
if command -v plutil >/dev/null 2>&1; then
  plutil -lint "$TARGET"
else
  echo "plutil unavailable on this host; skipping lint of $TARGET"
fi

# The arming state must be verifiable in the artifact, not just in this script.
if ! grep -q "MEMCERT_LIVE=$LIVE" "$TARGET"; then
  echo "error: rendered plist does not carry MEMCERT_LIVE=$LIVE; refusing to install" >&2
  exit 1
fi

if [ "${MEMCERT_CADENCE_NO_LOAD:-0}" = "1" ]; then
  echo "MEMCERT_CADENCE_NO_LOAD=1 -- rendered only, not installed/loaded."
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

echo "Loaded ${LABEL} (daily at ${HOUR}:${MINUTE})."
echo "Verify:  launchctl list | grep ${LABEL}"
echo "Run now: launchctl kickstart -p gui/${UID_NUM}/${LABEL}"
echo "Log:     $ROOT_DIR/var/log/memcert-t1.log"
if [ "$LIVE" = "0" ]; then
  echo "DRY-RUN mode: hypothesizer proposals land in var/memcert/hypotheses/outbox/ only."
  echo "Arm the loop-queue leg after reviewing a dry-run day:"
  echo "  MEMCERT_LIVE=1 sh scripts/memcert-cadence/install.sh"
fi
