#!/bin/sh
# Self-enroll a Mac or Linux fleetcap push device. POSIX sh + Python stdlib only.
set -eu

usage() {
  echo "usage: enroll.sh --device NAME --owner EMPLOYEE [--hub owner@mac-studio] [--repo PATH]"
  echo "Installs Claude hooks and an hourly read-only rsync push job."
}

DEVICE=""
OWNER=""
HUB="owner@mac-studio"
REPO=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
while [ "$#" -gt 0 ]; do
  case "$1" in
    --device) DEVICE=$2; shift 2 ;;
    --owner) OWNER=$2; shift 2 ;;
    --hub) HUB=$2; shift 2 ;;
    --repo) REPO=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done
[ -n "$DEVICE" ] && [ -n "$OWNER" ] || { usage >&2; exit 64; }
case "$DEVICE" in
  *[!A-Za-z0-9._-]*|'') echo "invalid --device (use 1-64 letters, digits, dot, underscore, hyphen)" >&2; exit 64 ;;
esac
[ "${#DEVICE}" -le 64 ] || { echo "invalid --device (maximum 64 characters)" >&2; exit 64; }

INSTALL_DIR="${HOME}/.local/lib/fleetcap"
JOB_DIR="${HOME}/.local/bin"
mkdir -p "$INSTALL_DIR" "$JOB_DIR"
chmod 700 "$INSTALL_DIR"
cp "$REPO/omniagentos/fleetcap/hooks/session-start.sh" "$INSTALL_DIR/session-start.sh"
cp "$REPO/omniagentos/fleetcap/hooks/session-end.sh" "$INSTALL_DIR/session-end.sh"
chmod 700 "$INSTALL_DIR/session-start.sh" "$INSTALL_DIR/session-end.sh"

HUB_HOST=${HUB##*@}
LOCAL_HOST=$(hostname -s 2>/dev/null || hostname)
if [ "$HUB_HOST" = "$LOCAL_HOST" ] || [ "$HUB_HOST" = "$(hostname 2>/dev/null)" ]; then
  mkdir -p /Users/youruser/Work/Ops/bin
  cp "$REPO/omniagentos/fleetcap/vendor/rrsync" /Users/youruser/Work/Ops/bin/rrsync
  chmod 0755 /Users/youruser/Work/Ops/bin/rrsync
fi

python3 - "$REPO" "$HOME" "$INSTALL_DIR" <<'PY'
import pathlib, subprocess, sys
repo, home, hooks = map(pathlib.Path, sys.argv[1:])
sys.path.insert(0, str(repo))
from omniagentos.fleetcap import profiles as module
patcher = repo / "omniagentos/fleetcap/hooks/settings-patch.py"
for profile in module.enumerate_profiles(home):
    if profile.cli == "claude" and (profile.root.is_dir() or profile.account_label == "default"):
        subprocess.run([sys.executable, str(patcher), str(profile.root / "settings.json"), "--hooks-dir", str(hooks)], check=True)
PY

PUSH_SCRIPT="$JOB_DIR/fleetcap-push"
python3 - "$PUSH_SCRIPT" "$DEVICE" "$OWNER" "$HUB" <<'PY'
import os, pathlib, sys
path, device, owner, hub = pathlib.Path(sys.argv[1]), *sys.argv[2:]
body = f'''#!/bin/sh
set +e
RSYNC=/usr/bin/rsync
[ -x "$RSYNC" ] || RSYNC=$(command -v rsync)
EXCLUDES="--exclude auth.json --exclude config.toml --exclude config.json --exclude .env* --exclude credentials* --exclude *.key --exclude *.pem --exclude oauth* --exclude settings.json"
for SPEC in "claude/default/.claude/projects" "codex/default/.codex/sessions" "kimi/default/.kimi-code/sessions" "grok/default/.grok/sessions" "grok/default/.grok/memtrace" "gemini/default/.gemini/history" "spool/default/Work/Ops/telemetry/spool"; do
  PREFIX=${{SPEC%%/*}}; REST=${{SPEC#*/}}; ACCOUNT=${{REST%%/*}}; REL=${{REST#*/}}
  SRC="${{HOME}}/${{REL}}"
  [ -d "$SRC" ] || continue
  # shellcheck disable=SC2086 -- the fixed deny-list intentionally expands to arguments.
  "$RSYNC" -a --update --timeout=30 --bwlimit=4096 --max-size=512m $EXCLUDES "$SRC/" "{hub}:$PREFIX/$ACCOUNT/"
done
DEVICE_FILE=${{TMPDIR:-/tmp}}/fleetcap-device-$$.json
trap 'rm -f "$DEVICE_FILE"' EXIT HUP INT TERM
umask 077
printf '%s\n' '{{"device":"{device}","owner":"{owner}"}}' > "$DEVICE_FILE"
"$RSYNC" -a "$DEVICE_FILE" "{hub}:DEVICE.json"
exit 0
'''
path.write_text(body); os.chmod(path, 0o700)
PY

case $(uname -s) in
  Darwin)
    PLIST="${HOME}/Library/LaunchAgents/com.example-org.fleetcap-push.plist"
    mkdir -p "${HOME}/Library/LaunchAgents"
    python3 - "$PLIST" "$PUSH_SCRIPT" <<'PY'
import pathlib, sys
path, script = map(pathlib.Path, sys.argv[1:])
path.write_text(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>com.example-org.fleetcap-push</string>
<key>ProgramArguments</key><array><string>{script}</string></array>
<key>StartCalendarInterval</key><dict><key>Minute</key><integer>37</integer></dict>
<key>LowPriorityIO</key><true/></dict></plist>''')
PY
    echo "Installed $PLIST (load it with: launchctl bootstrap gui/$(id -u) $PLIST)"
    ;;
  Linux)
    CRON="37 * * * * $PUSH_SCRIPT >/tmp/fleetcap-push.log 2>&1"
    (crontab -l 2>/dev/null | grep -v 'fleetcap-push' || true; echo "$CRON") | crontab -
    echo "Installed hourly cron job"
    ;;
  *) echo "unsupported OS: $(uname -s)" >&2; exit 1 ;;
esac

echo "Operator action: add this restricted line to the hub account authorized_keys for $HUB:"
echo "First on the hub: mkdir -p /Users/youruser/Work/Ops/telemetry/ingest/$DEVICE"
echo "Deploy $REPO/omniagentos/fleetcap/vendor/rrsync to /Users/youruser/Work/Ops/bin/rrsync (mode 0755)."
echo "command=\"/Users/youruser/Work/Ops/bin/rrsync -wo /Users/youruser/Work/Ops/telemetry/ingest/$DEVICE\",restrict,no-agent-forwarding,no-port-forwarding <device-public-key>"
echo "Then run $PUSH_SCRIPT once and verify ingest/$DEVICE/DEVICE.json on the hub."
