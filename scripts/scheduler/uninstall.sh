#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
. "$ROOT_DIR/scripts/lib/launchd-label.sh"

LABEL=${OMNIAGENTOS_LAUNCHD_LABEL:-com.omniagentos.morning}
# This one matters most in the set: the label does not select what gets WRITTEN,
# it selects what gets `rm`'d two lines below.
require_safe_launchd_label "$LABEL"
TARGET=${HOME}/Library/LaunchAgents/${LABEL}.plist
echo "Unloading $TARGET"
launchctl unload "$TARGET" 2>/dev/null || true
if [ -f "$TARGET" ]; then
    rm "$TARGET"
    echo "Removed $TARGET"
else
    echo "Already absent: $TARGET"
fi
