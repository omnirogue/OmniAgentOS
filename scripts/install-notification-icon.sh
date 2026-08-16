#!/usr/bin/env bash
# Give OmniAgentOS notifications the OmniAgentOS icon instead of the generic
# terminal-notifier one.
#
# macOS does not let a notification carry an arbitrary image: the icon on a
# banner is the icon of the app that POSTED it. terminal-notifier's `-appIcon`
# has been ignored since Big Sur. The supported route is `-sender <bundle-id>`,
# which posts as an installed app -- so the banner shows THAT app's icon and
# clicking it activates THAT app. /Applications/OmniAgentOS.app already exists
# (the launcher), so this script simply gives it the brand icon and registers it
# with LaunchServices; sessions/notify.py passes `-sender` to match.
#
# Idempotent. Safe to re-run after changing assets/omniagentos-icon.png.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_PNG="${1:-$REPO_ROOT/assets/omniagentos-icon.png}"
APP="/Applications/OmniAgentOS.app"
BUNDLE_ID="com.omniagentos.launcher"

if [[ ! -f "$SOURCE_PNG" ]]; then
  echo "error: icon source not found: $SOURCE_PNG" >&2
  exit 1
fi
if [[ ! -d "$APP" ]]; then
  echo "error: $APP is missing -- run scripts/launch-omniagentos.sh first" >&2
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ICONSET="$WORK/OmniAgentOS.iconset"
mkdir -p "$ICONSET"

# The full set macOS expects; omitting sizes gives a blurry banner on Retina.
for size in 16 32 64 128 256 512; do
  sips -z "$size" "$size" "$SOURCE_PNG" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$SOURCE_PNG" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
mv "$ICONSET/icon_512x512@2x.png" "$ICONSET/icon_1024x1024.png" 2>/dev/null || true

iconutil -c icns "$ICONSET" -o "$WORK/OmniAgentOS.icns"
cp "$WORK/OmniAgentOS.icns" "$APP/Contents/Resources/OmniAgentOS.icns"

# Bump the bundle mtime + re-register, or Finder/Notification Center keep serving
# the cached old icon.
touch "$APP"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Versions/A/Frameworks/LaunchServices.framework/Versions/A/Support/lsregister"
if [[ -x "$LSREGISTER" ]]; then
  "$LSREGISTER" -f "$APP" || true
fi
rm -rf "$HOME/Library/Caches/com.apple.iconservices.store" 2>/dev/null || true
killall Dock 2>/dev/null || true

echo "installed icon -> $APP/Contents/Resources/OmniAgentOS.icns"
echo "notifications post as $BUNDLE_ID (see omniagentos/sessions/notify.py)"
