#!/usr/bin/env bash
# Render the daily bank-balance snapshot launchd job.
#
# Runs scripts/banking/estimate_balances.py once a day (08:15 local): reads the
# QBO book balances from the Google Sheet (refreshed by the native Zapier Zap,
# over headless-capable Google OAuth), applies configs/bank_anchors.json, and
# writes the real estimates to var/bank_balances_latest.txt for the agent fleet.
# DELIBERATELY does not load the job; the operator loads it (see bottom).
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
TARGET_DIR=${HOME}/Library/LaunchAgents
LABEL=com.omniagentos.bank-balances
require_safe_launchd_label "$LABEL"
TARGET="$TARGET_DIR/$LABEL.plist"
HOUR=${OMNIAGENTOS_BALANCES_HOUR:-8}
MINUTE=${OMNIAGENTOS_BALANCES_MINUTE:-15}
mkdir -p "$TARGET_DIR" "$ROOT_DIR/var"

if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

# CRITICAL (H4 lesson): connections.env is plain KEY=value with no `export`, so
# it MUST be sourced with `set -a; . …; set +a` or the broker runs credential-blind.
SRC='set -a; . \"$HOME/.config/omni/connections.env\" 2>/dev/null; set +a; . \"$ROOT_DIR/scripts/launch-env.sh\" 2>/dev/null; '
CMD="cd $(printf %q "$ROOT_DIR") && $(printf %q "$PYBIN") scripts/banking/estimate_balances.py > var/bank_balances_latest.txt 2>&1"

cat > "$TARGET" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/sh</string>
        <string>-lc</string>
        <string>${SRC}${CMD}</string>
    </array>
    <key>WorkingDirectory</key><string>${ROOT_DIR}</string>
    <key>StartCalendarInterval</key>
    <dict><key>Hour</key><integer>${HOUR}</integer><key>Minute</key><integer>${MINUTE}</integer></dict>
    <key>StandardOutPath</key><string>/tmp/${LABEL}.out.log</string>
    <key>StandardErrorPath</key><string>/tmp/${LABEL}.err.log</string>
</dict>
</plist>
PLIST

echo "Wrote $TARGET (daily ${HOUR}:${MINUTE} -> var/bank_balances_latest.txt)"
echo
echo "Load it:  launchctl bootstrap gui/\$(id -u) $TARGET  (or: launchctl load $TARGET)"
