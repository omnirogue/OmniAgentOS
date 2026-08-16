#!/bin/bash
# OmniAgentOS one-press launcher: starts the control-plane API + runner + dashboard,
# opens the UI in your browser, and streams the dashboard log. Ctrl-C stops everything.
#
# M6(c) ownership note: this launcher's Steward daemons (alert-monitor loop, comms
# pollers) are the INTERACTIVE / one-press DEV path — they run in-process, in the
# foreground session, and die with Ctrl-C. scripts/scheduler/install-steward.sh
# installs the HEADLESS / production path (launchd jobs for briefing, metrics,
# alerts, and comms extract-batch that run independent of any open terminal). Do
# not run both against the same machine at the same time — e.g. install-steward.sh's
# alerts launchd job and this launcher's alert-monitor loop would both poll and
# could double-fire the same alert. Pick one: this script for a live dev session,
# install-steward.sh for an always-on deploy.
set -euo pipefail

ROOT="$HOME/OmniAgentOS"
cd "$ROOT"

# M6(b): generic crash supervisor for the daemons started below. Runs `cmd...`
# in a loop, appending its stdout/stderr to `log`; if it exits non-zero (crashes)
# the failure is recorded in the log and the command restarts after 5s rather
# than being left permanently dead. A clean/expected exit is not expected here
# (all daemons below are meant to run forever), so any exit — 0 or not — gets a
# restart; only non-zero exits get an explicit "crashed" log line.
supervise() {
  local log="$1" desc="$2" status
  shift 2
  while true; do
    # `if cmd; then ... else ...` (not a bare `cmd; status=$?`) is required here:
    # this script runs under `set -e`, and a bare failing simple command on its
    # own line would kill this loop's subshell via errexit before `status=$?`
    # ever executed. An if-condition is one of the contexts errexit exempts.
    if "$@" >>"$log" 2>&1; then
      status=0
    else
      status=$?
    fi
    if [ "$status" -ne 0 ]; then
      echo "[supervisor $(date -u +%FT%TZ)] $desc crashed (exit $status) — restarting in 5s" >>"$log"
    else
      echo "[supervisor $(date -u +%FT%TZ)] $desc exited cleanly — restarting in 5s" >>"$log"
    fi
    sleep 5
  done
}

echo "▶ OmniAgentOS — starting…"
mkdir -p "$ROOT/var/log"

# Credential broker resolves connector secrets from its own environment and never
# hands them to agents (connectors/broker.py). Canonical secrets file per
# vault/sources/connections.md:
if [ -f "$HOME/.config/omni/connections.env" ]; then
  set -a; . "$HOME/.config/omni/connections.env"; set +a
  echo "✓ connector credentials loaded into broker env"
fi

# Synapse knowledge subsystem (Horizon 3): ENABLED. The runner recalls relevant memories
# into every agent session and writes a reflection back on run completion; the consolidator
# (curator cadence) promotes corroborated knowledge. Local Postgres is trust-auth, so the
# knowledge_agent/knowledge_admin roles connect passwordless. To harden to scram before any
# multi-uid / networked exposure, run scripts/knowledge/pg-auth-setup.sh (H4+ container gate).
export OMNIAGENTOS_KNOWLEDGE=1
export OMNIAGENTOS_KNOWLEDGE_PG_DSN="postgresql://knowledge_agent@localhost/omniagentos_knowledge"
export OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN="postgresql://knowledge_admin@localhost/omniagentos_knowledge"
echo "✓ Synapse knowledge memory enabled (OMNIAGENTOS_KNOWLEDGE=1)"
export OMNIAGENTOS_CASCADE=1
export OMNIAGENTOS_REFLEXION=1
export OMNIAGENTOS_COMPRESS=basic
echo "✓ Verified Compute enabled (cascade + reflexion + compress=basic; ADR-009)"
export OMNIAGENTOS_RUNNER_CONCURRENCY=4
export OMNIAGENTOS_REAPER_ENFORCE=1
export OMNIAGENTOS_ORCH_STALE_MINUTES=2
# Parity with the launchd plists (com.omniagentos.api/sessions) — dev runs must
# route exactly like prod: swarm activation on, fast-dispatch classifier on,
# per-run slot cap 10, start-tier learner at 3 samples.
export OMNIAGENTOS_SWARM_EXECUTE=1
export OMNIAGENTOS_FAST_DISPATCH=1
export OMNIAGENTOS_SWARM_TARGET_CAP=10
export OMNIAGENTOS_ROUTE_LEARN_MIN_SAMPLES=3
echo "✓ Swarm: runner K=4, idle-reaper ENFORCING, orch stale 2m, execute+fast-dispatch ON"

# Backend: control-plane API (:8485) + durable runner. Logged; killed on exit.
make api    > "$ROOT/var/log/api.log"    2>&1 &  API_PID=$!
make runner > "$ROOT/var/log/runner.log" 2>&1 &  RUN_PID=$!

# Steward daemons (Horizon 4). Logged + backgrounded exactly like api/runner
# above, and killed on exit by the same trap. A daemon exiting (even 0) must
# never take the launcher down with it — everything here is `&`-backgrounded
# (not `wait`-ed on), and each one runs under a restart loop (`supervise` above,
# or the alert monitor's own crash/interval loop below) so a transient error
# (e.g. Postgres briefly down, a flaky API call) is logged and retried instead
# of silently killing the daemon for the rest of the session (M6(b)).

# Alert monitor: the module only supports one-shot `--once` (no built-in
# daemon/interval mode). Normal cadence is 15 minutes between cycles. M6(b):
# on a crash (non-zero exit) log it and retry fast (5s) instead of the daemon
# staying silently dead for up to 15 minutes; a clean cycle still waits the
# full interval before the next one.
( while true; do
    # See the `supervise()` comment above re: why this is `if cmd; then ... else`
    # rather than a bare `cmd; status=$?` — this runs under `set -e`.
    if uv run python -m omniagentos.steward.alerts.monitor --once \
         >> "$ROOT/var/log/steward-alerts.log" 2>&1; then
      status=0
    else
      status=$?
    fi
    if [ "$status" -ne 0 ]; then
      echo "[supervisor $(date -u +%FT%TZ)] alert monitor crashed (exit $status) — retrying in 5s" >> "$ROOT/var/log/steward-alerts.log"
      sleep 5
    else
      sleep 900
    fi
  done ) & ALERTS_PID=$!
echo "✓ alert monitor loop started (every 15 min, crash-retry in 5s, log: var/log/steward-alerts.log)"

# M6(a): configs/steward.yaml's `comms.sources` are INBOUND WEBHOOK auth entries
# (e.g. `zapier: {secret_env: ...}`, consumed by the API's webhook route) — NOT
# poll targets. Iterating them here as `--source` values was the bug: a webhook
# name is neither a telegram/slack KIND nor a registered IMAP mailbox, so it fell
# through to comms/poll.py's IMAP fallback and polled a mailbox named "zapier"
# that was never meant to exist. Telegram and Slack are the two fixed,
# single-account pollers (omniagentos/comms/pollers/{telegram,slack}.py, KIND
# constants "telegram"/"slack") and self-degrade to a logged 'pending_setup'
# status when their token env var is absent, so starting them unconditionally
# is safe. IMAP mailboxes are named, per-source rows in the comms_sources DB
# table with no static list in steward.yaml today, so there is nothing here to
# auto-discover an IMAP poll target from yet — add a named-mailbox list to
# steward.yaml (and read it here) before wiring IMAP into this loop.
#
# M6(b): each poller runs under `supervise` (defined above). comms/poll.py's
# own internal loop does not catch a raised exception from a single poll cycle
# (that fix belongs to RC, who owns poll.py — RB does not edit it here), so
# without this wrapper a crash mid-cycle kills the poller for the rest of the
# session. `supervise` logs the crash and restarts the poller after 5s instead.
#
# PIDs are collected as a plain space-separated string (NOT a bash array) —
# /bin/bash on macOS is 3.2, where `"${arr[@]}"` on an empty array is an
# unbound-variable error under `set -u`.
COMMS_PIDS=""
for source in telegram slack; do
  ( supervise "$ROOT/var/log/comms-poll-$source.log" "comms poller ($source)" \
      uv run python -m omniagentos.comms.poll --source "$source" ) &
  COMMS_PIDS="$COMMS_PIDS $!"
  echo "✓ comms poller started for source '$source' (pending-setup is fine if creds absent — log: var/log/comms-poll-$source.log)"
done

# Daily briefing, hourly metric collector, and the comms extract-batch curator
# run on a SCHEDULE, not as always-on launcher daemons — install their launchd
# jobs once with:
echo "ℹ scheduled steward jobs (briefing/metrics/extract-batch): run scripts/scheduler/install-steward.sh once to install the launchd jobs (headless/production path — see ownership note at the top of this file; don't also run this dev launcher's daemons on the same machine)"

echo "▶ started: control-plane API (:8485), runner, alert-monitor loop, comms pollers (telegram, slack) — dashboard starts next in the foreground"

cleanup() {
  echo; echo "■ Stopping OmniAgentOS…"
  kill "$API_PID" "$RUN_PID" "$ALERTS_PID" $COMMS_PIDS 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Open the browser once the dashboard answers.
( for _ in $(seq 1 90); do
    if curl -fsS -o /dev/null http://localhost:3000 2>/dev/null; then
      echo "✓ UI up — opening http://localhost:3000"; open "http://localhost:3000"; break
    fi; sleep 1
  done ) &

# Dashboard in the foreground so this window shows its log and Ctrl-C stops the whole stack.
echo "▶ Building/serving the dashboard (first launch compiles; later launches are instant)…"
cd "$ROOT/dashboard"
if [ ! -d ".next" ]; then npm ci --no-audit --no-fund >/dev/null 2>&1 && npm run build; fi
npm run start 2>/dev/null || npm run dev
