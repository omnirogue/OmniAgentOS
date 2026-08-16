#!/bin/zsh
# Installs/updates the work-drive sync+pull automation from this directory:
# copies scripts to ~/bin, renders plist templates, (re)loads launchd jobs.
# Idempotent — safe to re-run after any edit here.
set -euo pipefail
HERE="${0:A:h}"

mkdir -p "$HOME/bin" "$HOME/Library/LaunchAgents"

for s in work-drive-sync.sh work-drive-pull.sh; do
  cp "$HERE/$s" "$HOME/bin/$s"
  chmod +x "$HOME/bin/$s"
done

for t in com.owner.work-drive-sync com.owner.work-drive-pull; do
  python3 - "$HERE/$t.plist.template" "$HOME/Library/LaunchAgents/$t.plist" "$HOME" "$HERE/../.." <<'PY'
import sys
from pathlib import Path

template, target, home, root = sys.argv[1:]
sys.path.insert(0, str(Path(root).resolve()))
from scripts.lib.plist_write import write_plist_atomic

write_plist_atomic(target, Path(template).read_text(encoding="utf-8").replace("__HOME__", home))
PY
  launchctl bootout "gui/$(id -u)/$t" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/$t.plist"
done

echo "installed: com.owner.work-drive-sync (daily 08:30) + com.owner.work-drive-pull (daily 07:00)"
launchctl list | grep work-drive || true

# Verify the copy actually landed byte-identical, right after making it —
# closes the loop check-installed.sh existed to satisfy but wasn't wired
# into anything (review finding, 2026-08-08). A non-zero exit here means
# the copy step above did not do what it claimed.
"$HERE/check-installed.sh"
