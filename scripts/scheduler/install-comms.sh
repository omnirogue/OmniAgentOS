#!/usr/bin/env bash
# Render the Harper IMAP communications launchd job.
#
# DELIBERATELY DOES NOT install, bootstrap, load, or kickstart the job. The
# operator reviews the rendered plist and loads it separately.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"
TARGET_DIR=${HOME}/Library/LaunchAgents
LABEL=com.omniagentos.comms-harper
require_safe_launchd_label "$LABEL"
TARGET="$TARGET_DIR/$LABEL.plist"
mkdir -p "$TARGET_DIR"

# launchd's minimal PATH resolves python3 to the system 3.9; OmniAgentOS needs
# 3.12+. Pin one interpreter for the generated job.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'uv sync' first" >&2
  exit 1
fi

"$PYBIN" - "$ROOT_DIR" "$TARGET" "$PYBIN" <<'PY'
import html
import shlex
import sys
from pathlib import Path

root, target_name, pybin = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic
label = "com.omniagentos.comms-harper"
source_env = (
    'set -a; . \"$HOME/.config/omni/connections.env\" 2>/dev/null; set +a; '
    f'. \"{root}/scripts/launch-env.sh\" 2>/dev/null; '
)
exec_cmd = " ".join(
    shlex.quote(part)
    for part in [pybin, "-m", "omniagentos.comms.poll", "--source", "harper", "--interval", "60"]
)
program_args = ["/bin/sh", "-lc", source_env + f"exec {exec_cmd}"]
args_xml = "\n".join(f"        <string>{html.escape(arg)}</string>" for arg in program_args)
plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>WorkingDirectory</key><string>{html.escape(root)}</string>
    <key>StartInterval</key><integer>60</integer>
    <key>StandardOutPath</key><string>/tmp/{label}.out.log</string>
    <key>StandardErrorPath</key><string>/tmp/{label}.err.log</string>
</dict>
</plist>
'''
write_plist_atomic(target_name, plist)
PY

echo "Wrote $TARGET (every 60 seconds)"
echo
echo "NOT loaded. Review and load manually when ready:"
echo "  launchctl load $TARGET"
