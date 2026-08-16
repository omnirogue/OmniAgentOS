#!/usr/bin/env bash
# loop-watchdog — recreate any dead loop tmux session on this host.
#
# Idempotent: run-loop.sh exits 0 without side effects when the session already
# exists, so firing this on an interval is safe, and the singleton guard lives
# there (tmux has-session), not here. Kill switch: create bridge/WATCHDOG-OFF
# to stop recreation during a deliberate shutdown or a gate window.
#
# PARK-AWARE. A dead session is not automatically a session to recreate: when
# run-loop.sh exits 2 ("every seat failed — PARKING"), it leaves a durable
# marker at var/loopqueue/state/<role>.park.json. The watchdog refuses to
# restart an unexpired park and logs that refusal only once per park.
#
# Installed as ~/Library/LaunchAgents/com.omniagentos.loop-watchdog.plist —
# the com.omniagentos.* namespace is what health-sentinel builds its
# expected set from; a com.threeloops.* label would be invisible to it.
set -u
TL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -f "$TL/bridge/WATCHDOG-OFF" ]; then
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) watchdog disabled by WATCHDOG-OFF marker"
  exit 0
fi

WORKDIR="${LOOP_WORKDIR:-$HOME/OmniAgentOS}"
PARK_PY="${LOOP_GOV_PY:-$WORKDIR/.venv/bin/python}"
[ -x "$PARK_PY" ] || PARK_PY="$(command -v python3 || echo /usr/bin/python3)"

start() {
  local role="$1"; shift
  if tmux has-session -t "loop-$role" 2>/dev/null; then
    return 0
  fi
  local verdict rc
  verdict="$("$PARK_PY" "$TL/bridge/loop_park.py" check \
    --root "$WORKDIR/var/loopqueue" --role "$role" \
    --alerts-file "$WORKDIR/var/loopqueue/ALERTS.md" 2>&1)"
  rc=$?
  if [ -n "$verdict" ]; then
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $verdict"
  fi
  if [ "$rc" != "0" ]; then
    return 0
  fi
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) loop-$role is dead — restarting: run-loop.sh $role $*"
  "$TL/bridge/run-loop.sh" "$role" "$@"
}

if [ "${LOOP_WATCHDOG_SOURCE_ONLY:-0}" != "1" ]; then
  # The live 2026-08-10 rev4 ladder keeps planning off the scarce Anthropic
  # meter first (Kimi, then Codex/Sol), with distinct healthy Claude fallbacks.
  start planning    kimi codex claude7 claude12 claude8 claude9 claude10 claude11
  start reviewer    claude2 claude8 claude9 claude10 claude11 claude12
  start implementer claude4 claude1 claude2 claude3 claude6 claude7
fi
