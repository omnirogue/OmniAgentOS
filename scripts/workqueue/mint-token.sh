#!/usr/bin/env bash
# mint-token.sh — create a WQ_TOKEN and store it in ~/.config/omni/connections.env.
#
# The token is a 32-byte hex bearer secret used by wq-server (:8487) and every
# HttpQueueClient / worker / enroll.sh call to authenticate. This script:
#   1. Generates a new token with `openssl rand -hex 32` IF one is not already
#      present in the target env file (idempotent — running it twice never
#      rotates a live token by accident).
#   2. Appends `WQ_TOKEN=<hex>` to the file.
#   3. chmod 600s the file.
#
# It NEVER prints the token value, on success or failure — the whole point of
# a bearer secret is that it does not end up in a terminal scrollback, a log
# file, or a CI artifact. Use `wq-token-status` (below) to confirm presence
# without ever echoing the value.
#
# Usage:
#   scripts/workqueue/mint-token.sh [--env-file PATH] [--force]
#
#   --env-file PATH   Target env file. Default: ~/.config/omni/connections.env
#   --force            Rotate: replace an existing WQ_TOKEN with a fresh one.
#                       Rotating invalidates every already-enrolled worker's
#                       token until it is re-minted onto that box too — use
#                       deliberately, not as a default habit.
set -euo pipefail

ENV_FILE="${HOME}/.config/omni/connections.env"
FORCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --env-file)
      ENV_FILE="$2"
      shift 2
      ;;
    --force)
      FORCE=1
      shift
      ;;
    -h|--help)
      sed -n '2,25p' "$0"
      exit 0
      ;;
    *)
      echo "mint-token.sh: unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

if ! command -v openssl >/dev/null 2>&1; then
  echo "mint-token.sh: openssl not found on PATH — install it (macOS ships it; Linux: apt/yum install openssl)." >&2
  exit 1
fi

mkdir -p "$(dirname "$ENV_FILE")"
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

EXISTING=0
if grep -q '^WQ_TOKEN=' "$ENV_FILE" 2>/dev/null; then
  EXISTING=1
fi

if [ "$EXISTING" -eq 1 ] && [ "$FORCE" -ne 1 ]; then
  echo "mint-token.sh: WQ_TOKEN already present in $ENV_FILE — leaving it as-is (pass --force to rotate)." >&2
  exit 0
fi

NEW_TOKEN="$(openssl rand -hex 32)"

if [ "$EXISTING" -eq 1 ] && [ "$FORCE" -eq 1 ]; then
  # Replace the existing line in place, BSD/GNU sed both handled.
  TMP_FILE="$(mktemp "${ENV_FILE}.XXXXXX")"
  awk -v tok="WQ_TOKEN=${NEW_TOKEN}" '
    BEGIN { done = 0 }
    /^WQ_TOKEN=/ { print tok; done = 1; next }
    { print }
    END { if (!done) print tok }
  ' "$ENV_FILE" > "$TMP_FILE"
  mv "$TMP_FILE" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "mint-token.sh: WQ_TOKEN rotated in $ENV_FILE (value not shown). Re-run enroll.sh or redeploy the token on every worker that must trust the new value." >&2
else
  printf 'WQ_TOKEN=%s\n' "$NEW_TOKEN" >> "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  echo "mint-token.sh: WQ_TOKEN minted into $ENV_FILE (value not shown)." >&2
fi

unset NEW_TOKEN
