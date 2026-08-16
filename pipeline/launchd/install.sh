#!/usr/bin/env bash
# Install the converged queue daemons from OmniAgentOS/pipeline.
#
# Default is a read-only source check.  --apply replaces the existing stable
# com.threeloops.* launchd identities in place; the labels stay stable so
# monitoring does not lose history, but every executable path moves into the
# serving OmniAgentOS checkout.  --restart-loops also recreates the three
# tmux loops from pipeline/prompts after the daemons are verified.
set -euo pipefail

MODE=check
RESTART_LOOPS=0
case "${1:-}" in
  --apply) MODE=apply; shift ;;
  --check|"") [ "$#" -eq 0 ] || shift ;;
  *) echo "usage: $0 [--check|--apply] [--restart-loops]" >&2; exit 2 ;;
esac
if [ "${1:-}" = "--restart-loops" ]; then
  RESTART_LOOPS=1
  shift
fi
[ "$#" -eq 0 ] || { echo "usage: $0 [--check|--apply] [--restart-loops]" >&2; exit 2; }

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=$(CDPATH= cd -- "$HERE/../.." && pwd)
PIPELINE="$REPO/pipeline"
QUEUE="$REPO/var/loopqueue"
DOMAIN="gui/$(id -u)"
DEST="$HOME/Library/LaunchAgents"

PLISTS=(
  com.threeloops.advice.plist
  com.threeloops.claim-reaper.plist
  com.threeloops.gate-loop.plist
  com.threeloops.publish-queue.plist
)

check_sources() {
  local plist
  [ "$(git -C "$REPO" symbolic-ref --short HEAD)" = main ] || {
    echo "refusing: serving checkout is not pinned to main: $REPO" >&2
    return 1
  }
  [ -x "$REPO/.venv/bin/python" ] || {
    echo "refusing: missing serving interpreter $REPO/.venv/bin/python" >&2
    return 1
  }
  "$REPO/.venv/bin/python" -c 'import jsonschema' || {
    echo "refusing: serving interpreter cannot import jsonschema" >&2
    return 1
  }
  for plist in "${PLISTS[@]}"; do
    plutil -lint "$HERE/$plist" >/dev/null
    if grep -q '/Users/youruser/Work/Ops/ThreeLoops' "$HERE/$plist"; then
      echo "refusing: $plist still points at the retired ThreeLoops checkout" >&2
      return 1
    fi
    grep -q "$REPO/pipeline" "$HERE/$plist" || {
      echo "refusing: $plist does not execute from $REPO/pipeline" >&2
      return 1
    }
  done
}

check_sources
if [ "$MODE" = check ]; then
  echo "convergence launchd source check: ok ($REPO @ $(git -C "$REPO" rev-parse --short HEAD))"
  exit 0
fi

local_head=$(git -C "$REPO" rev-parse HEAD)
remote_head=$(git -C "$REPO" rev-parse origin/main)
[ "$local_head" = "$remote_head" ] || {
  echo "refusing: serving main $local_head is not pinned to origin/main $remote_head" >&2
  exit 1
}

mkdir -p "$DEST" "$QUEUE/logs" "$REPO/var/log"
for plist in "${PLISTS[@]}"; do
  label=${plist%.plist}
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  install -m 0644 "$HERE/$plist" "$DEST/$plist"
  launchctl bootstrap "$DOMAIN" "$DEST/$plist"
  launchctl kickstart -k "$DOMAIN/$label"
done

for plist in "${PLISTS[@]}"; do
  label=${plist%.plist}
  installed=$(launchctl print "$DOMAIN/$label")
  printf '%s' "$installed" | grep -q "$REPO/pipeline/bridge" || {
    echo "refusing: installed $label does not point at pipeline/bridge" >&2
    exit 1
  }
done

if [ "$RESTART_LOOPS" = 1 ]; then
  "$HERE/restart-loops.sh" --apply
fi

echo "convergence launchd deploy: ok ($local_head)"
