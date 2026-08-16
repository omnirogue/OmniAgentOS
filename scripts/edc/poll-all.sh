#!/bin/sh
# shellcheck shell=sh
# Poll every EDC mail source once. This is safe to invoke from launchd or directly.

SCRIPT_DIR=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH='' cd -- "$SCRIPT_DIR/../.." && pwd)

set -a
# shellcheck disable=SC1091
. "$HOME/.config/omni/connections.env" 2>/dev/null
set +a
# shellcheck source=../launch-env.sh
# shellcheck disable=SC1091
. "$REPO_ROOT/scripts/launch-env.sh"

for source in gmail_ownera gmail_initech gmail_acmeuni gmail_hooli globex; do
    /Users/youruser/OmniAgentOS/.venv/bin/python -m omniagentos.comms.poll --source "$source" --once || true
done
