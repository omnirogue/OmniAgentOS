#!/usr/bin/env bash
# serve.sh — start wq-server on the primary Mac, or install/manage the
# launchd jobs that keep it (and its reverse tunnels) running.
#
# wq-server is the standalone FastAPI process on :8487 (loopback only,
# configs/workqueue.yaml:server.bind) that fronts var/workqueue.sqlite3 for
# every enrolled machine. This script never touches omniagentos/api — that
# is a different, live process on :8485.
#
# Usage:
#   scripts/workqueue/serve.sh run                         # foreground, Ctrl-C to stop
#   scripts/workqueue/serve.sh install                      # render + bootstrap the launchd job
#   scripts/workqueue/serve.sh uninstall                    # stop + remove the launchd job
#   scripts/workqueue/serve.sh tunnel --host <ssh-target>   # render + bootstrap ONE reverse
#                                                            # tunnel plist for a non-tailnet
#                                                            # remote (e.g. mw0001-owner, acmeuni)
#   scripts/workqueue/serve.sh tunnel --host <ssh-target> --uninstall
#
# All paths used at runtime are absolute — launchd runs with a stripped
# environment where `wis`/`uv`/`ledger` do not resolve (AGENTS.md, SPEC §1.3).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
CONNECTIONS_ENV="${HOME}/.config/omni/connections.env"
LOG_DIR="${REPO_ROOT}/var/workqueue/logs"
CONFIG_PATH="${REPO_ROOT}/configs/workqueue.yaml"
LABEL="com.omniagentos.wq-server"
PLIST_DEST="${HOME}/Library/LaunchAgents/${LABEL}.plist"

die() { echo "serve.sh: $*" >&2; exit 1; }

require_darwin() {
  [ "$(uname -s)" = "Darwin" ] || die "wq-server launchd install is darwin-only; on Linux run 'serve.sh run' under your own supervisor (systemd unit, etc.)."
}

mkdir -p "$LOG_DIR"

cmd="${1:-}"
shift || true

case "$cmd" in
  run)
    [ -x "$PYTHON_BIN" ] || die "no venv at ${PYTHON_BIN} — run 'uv sync' at repo root first."
    set -a
    # shellcheck disable=SC1090
    [ -f "$CONNECTIONS_ENV" ] && source "$CONNECTIONS_ENV"
    set +a
    exec "$PYTHON_BIN" -m omniagentos.workqueue.server --config "$CONFIG_PATH"
    ;;

  install)
    require_darwin
    [ -x "$PYTHON_BIN" ] || die "no venv at ${PYTHON_BIN} — run 'uv sync' at repo root first."
    TEMPLATE="${SCRIPT_DIR}/wq-server.plist.template"
    [ -f "$TEMPLATE" ] || die "missing template: $TEMPLATE"
    mkdir -p "$(dirname "$PLIST_DEST")"
    sed \
      -e "s#__LABEL__#${LABEL}#g" \
      -e "s#__PYTHON_BIN__#${PYTHON_BIN}#g" \
      -e "s#__REPO_ROOT__#${REPO_ROOT}#g" \
      -e "s#__CONNECTIONS_ENV__#${CONNECTIONS_ENV}#g" \
      -e "s#__CONFIG_PATH__#${CONFIG_PATH}#g" \
      -e "s#__LOG_DIR__#${LOG_DIR}#g" \
      "$TEMPLATE" > "$PLIST_DEST"
    launchctl bootout "gui/$(id -u)" "$PLIST_DEST" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST_DEST"
    launchctl enable "gui/$(id -u)/${LABEL}"
    echo "serve.sh: installed and bootstrapped ${LABEL} → ${PLIST_DEST}"
    echo "serve.sh: check with: launchctl print gui/$(id -u)/${LABEL} | head -20"
    ;;

  uninstall)
    require_darwin
    launchctl bootout "gui/$(id -u)" "$PLIST_DEST" >/dev/null 2>&1 || true
    rm -f "$PLIST_DEST"
    echo "serve.sh: removed ${LABEL}"
    ;;

  tunnel)
    require_darwin
    HOST=""
    UNINSTALL=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --host) HOST="$2"; shift 2 ;;
        --uninstall) UNINSTALL=1; shift ;;
        *) die "unknown tunnel argument: $1" ;;
      esac
    done
    [ -n "$HOST" ] || die "tunnel requires --host <ssh-target> (e.g. owner@203.0.113.10 or root@203.0.113.12)"
    HOST_SLUG="$(printf '%s' "$HOST" | tr -c 'A-Za-z0-9' '-')"
    TLABEL="com.omniagentos.wq-tunnel.${HOST_SLUG}"
    TPLIST_DEST="${HOME}/Library/LaunchAgents/${TLABEL}.plist"

    if [ "$UNINSTALL" -eq 1 ]; then
      launchctl bootout "gui/$(id -u)" "$TPLIST_DEST" >/dev/null 2>&1 || true
      rm -f "$TPLIST_DEST"
      echo "serve.sh: removed tunnel ${TLABEL}"
      exit 0
    fi

    ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$HOST" true \
      || die "SSH preflight to ${HOST} failed — confirm the key is trusted (ssh-copy-id or grant-access.sh --add-key) before installing the tunnel."

    TEMPLATE="${SCRIPT_DIR}/wq-tunnel.plist.template"
    [ -f "$TEMPLATE" ] || die "missing template: $TEMPLATE"
    mkdir -p "$(dirname "$TPLIST_DEST")"
    sed \
      -e "s#__HOST_SLUG__#${HOST_SLUG}#g" \
      -e "s#__REMOTE_SSH_TARGET__#${HOST}#g" \
      -e "s#__LOG_DIR__#${LOG_DIR}#g" \
      "$TEMPLATE" > "$TPLIST_DEST"
    launchctl bootout "gui/$(id -u)" "$TPLIST_DEST" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$(id -u)" "$TPLIST_DEST"
    launchctl enable "gui/$(id -u)/${TLABEL}"
    echo "serve.sh: installed reverse tunnel ${TLABEL} → ${HOST} (forwards ${HOST}:127.0.0.1:8487 to this Mac's 127.0.0.1:8487)"
    ;;

  *)
    echo "usage: serve.sh {run|install|uninstall|tunnel --host <ssh-target> [--uninstall]}" >&2
    exit 1
    ;;
esac
