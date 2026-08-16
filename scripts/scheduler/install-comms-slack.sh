#!/usr/bin/env bash
# Render BOTH halves of the hybrid Slack ingestion. They are one design.
#
#   com.omniagentos.comms-slack-socket   KeepAlive push client (latency)
#   com.omniagentos.comms-slack-sweep    5-minute reconciliation poll (determinism)
#
# Socket Mode does not replay events that occurred while the client was
# disconnected, so shipping the socket ALONE is strictly worse than the poller
# alone: a death at 02:00 nobody notices until 09:00 loses seven hours with no
# symptom. This script therefore refuses to render one without the other — if
# you only want one, you want the sweep.
#
# DELIBERATELY DOES NOT install, bootstrap, load, or kickstart anything. That is
# this repo's installer convention (see install-comms.sh); the operator reviews
# the rendered plists and loads them separately with the printed commands.
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)

VAR_ROOT=${OMNIAGENTOS_VAR_DIR:-$ROOT_DIR/var}
TARGET_DIR=${OMNIAGENTOS_LAUNCHD_TARGET_DIR:-$VAR_ROOT/launchd/rendered}
# Where the two jobs write StandardOutPath/StandardErrorPath. Overridable so an
# operator home outside the repo (e.g. ~/OmniAgentOS/Ops/slack-socket/run)
# can own the logs without a second renderer existing — there is exactly one
# place these plists are generated, and this keeps it that way. Nothing reads
# these files programmatically: health-sentinel's verdict comes from the
# `comms_sources` rows, never from the log text.
LOG_DIR=${OMNIAGENTOS_LAUNCHD_LOG_DIR:-$ROOT_DIR/var/log}
SOCKET_LABEL=com.omniagentos.comms-slack-socket
SWEEP_LABEL=com.omniagentos.comms-slack-sweep

# The sweep's cadence, and it bounds ONE thing: how long a socket gap can go
# unrepaired. 300s is a 5-minute worst case.
#
# It is deliberately NOT chosen against health-sentinel's 1800s interval. That
# reasoning is a trap and was wrong here once: running ~6x between two health
# checks means five of the six results are overwritten before anyone reads them,
# so any LAST-VALUE field (`reconciled_last_count`) is invisible ~83% of the
# time. The sentinel therefore watermarks the MONOTONIC `reconciled_total`
# instead, which cannot miss an event between observations at any cadence.
#
# The real ceiling on going finer is Slack's rate limit: `conversations.history`
# is capped far more tightly for non-Marketplace apps created after 2025-05-29
# (order of 1 request/minute), and one pass costs 1 + N_member_channels
# requests. Verify the app's actual tier before lowering this.
RECONCILE_INTERVAL=${OMNIAGENTOS_SLACK_RECONCILE_INTERVAL_SECONDS:-300}

mkdir -p "$TARGET_DIR" "$LOG_DIR"

# launchd's minimal PATH resolves python3 to the system 3.9; OmniAgentOS needs
# 3.12+. Pin one interpreter for the generated jobs.
if [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  PYBIN="$ROOT_DIR/.venv/bin/python"
elif command -v python3.12 >/dev/null 2>&1; then
  PYBIN=$(command -v python3.12)
else
  echo "error: no 3.12+ interpreter found (.venv/bin/python or python3.12); run 'make sync' first" >&2
  exit 1
fi

python3 - "$ROOT_DIR" "$TARGET_DIR" "$PYBIN" "$RECONCILE_INTERVAL" "$LOG_DIR" <<'PY'
import html
import shlex
import sys
from pathlib import Path

root, target_dir, pybin, interval, log_dir = sys.argv[1:]
sys.path.insert(0, root)
from scripts.lib.plist_write import write_plist_atomic

# The runtime block is LOAD-BEARING and copied verbatim from
# com.omniagentos.runner.plist: var/omniagentos.db is a stale
# default_db_path() fallback that LIES about migration state. The live DB is
# var/runtime/state.sqlite3.
RUNTIME = f"{root}/var/runtime"
ENVIRONMENT = {
    "OMNIAGENTOS_DB": f"{RUNTIME}/state.sqlite3",
    "OMNIAGENTOS_LEDGER_DIR": f"{RUNTIME}/ledger",
    "OMNIAGENTOS_VAR": RUNTIME,
    "OMNIAGENTOS_VAR_DIR": RUNTIME,
    "OMNIAGENTOS_VAULT_DIR": f"{RUNTIME}/vault",
}

# The exec prelude sources connections.env FIRST and scripts/launch-env.sh
# second. That order is load-bearing and pinned by
# tests/installer_launch_env_test.py. The second half is what puts
# SLACK_BOT_TOKEN / SLACK_APP_TOKEN (var/secrets/, via connectors.secrets_env)
# into the environment the broker resolves from — a plist that omits it leaves
# both jobs pending_setup forever, which is the 2026-08-01 durability defect.
PRELUDE = (
    'set -a; . "$HOME/.config/omni/connections.env" 2>/dev/null; set +a; '
    f'. {shlex.quote(root + "/scripts/launch-env.sh")}; '
)


def plist(label: str, argv: list[str], *, keep_alive: bool, start_interval: int | None) -> str:
    command = PRELUDE + "exec " + " ".join(shlex.quote(part) for part in argv)
    args_xml = "\n".join(
        f"\t\t<string>{html.escape(part)}</string>"
        for part in ["/bin/sh", "-lc", command]
    )
    env_xml = "\n".join(
        f"\t\t<key>{key}</key>\n\t\t<string>{html.escape(value)}</string>"
        for key, value in sorted(ENVIRONMENT.items())
    )
    log = f"{log_dir}/{label.rsplit('.', 1)[-1]}.log"
    cadence = (
        "\t<key>KeepAlive</key>\n\t<true/>"
        if keep_alive
        else f"\t<key>StartInterval</key><integer>{start_interval}</integer>"
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>EnvironmentVariables</key>
\t<dict>
{env_xml}
\t</dict>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProgramArguments</key>
\t<array>
{args_xml}
\t</array>
\t<key>StandardErrorPath</key>
\t<string>{html.escape(log)}</string>
\t<key>StandardOutPath</key>
\t<string>{html.escape(log)}</string>
{cadence}
\t<key>WorkingDirectory</key>
\t<string>{html.escape(root)}</string>
</dict>
</plist>
"""


target = Path(target_dir)
# KeepAlive covers PROCESS death. It does NOT cover a live process with a dead
# connection thread — that is the heartbeat's job (slack-socket comms_sources
# row) and the sentinel's.
write_plist_atomic(
    target / "com.omniagentos.comms-slack-socket.plist",
    plist(
        "com.omniagentos.comms-slack-socket",
        [pybin, "-m", "omniagentos.comms.sockets.slack"],
        keep_alive=True,
        start_interval=None,
    ),
)
# The sweep runs in its OWN process and writes only its own `slack` source row.
# It must never be folded into the socket process: a socket crash would take the
# repair mechanism with it.
write_plist_atomic(
    target / "com.omniagentos.comms-slack-sweep.plist",
    plist(
        "com.omniagentos.comms-slack-sweep",
        [
            pybin,
            "-m",
            "omniagentos.comms.poll",
            "--source",
            "slack",
            "--once",
        ],
        keep_alive=False,
        start_interval=int(interval),
    ),
)
print(f"Wrote {target}/com.omniagentos.comms-slack-socket.plist (KeepAlive)")
print(f"Wrote {target}/com.omniagentos.comms-slack-sweep.plist (every {interval}s)")
PY

cat <<EOF

NOT loaded (render-only, by convention). Review, then:

  cp "$TARGET_DIR/$SOCKET_LABEL.plist" ~/Library/LaunchAgents/
  cp "$TARGET_DIR/$SWEEP_LABEL.plist"  ~/Library/LaunchAgents/
  launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$SOCKET_LABEL.plist
  launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/$SWEEP_LABEL.plist

ROLLOUT — do this BEFORE enabling the sentinel's reconciliation alert:
  comms_sources is empty and the stored cursor defaults to "0", so the FIRST
  sweep walks all reachable history and reports a large \`created\` count. That is
  NOT a socket miss. Seed the cursor first:

    .venv/bin/python -m omniagentos.comms.poll --source slack --seed-cursor

  That subcommand MERGES (read-modify-write). Never seed with an ad-hoc
  upsert_comms_source one-liner: config_json is replaced wholesale, so a re-run
  would delete reconciled_total and every per-channel cursor — the only durable
  record that the socket has missed messages, and the windows still owed.

PREFLIGHT — run one sweep by hand and read the counts before loading anything:

    .venv/bin/python -m omniagentos.comms.poll --source slack --once

  Expect \`member_channels\` > 0. If it is 0 the bot is in no public channel, the
  sweep reconciles nothing, and the socket has no safety net (health-sentinel
  FAILs on exactly this). \`channel_errors\` should be 0; a non-zero count names
  the channels that are silently unreconciled.
EOF
