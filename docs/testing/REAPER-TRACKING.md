# Reaper tracking

the operator flagged that the **session reaper may be killing legitimate sessions**.
`scripts/livesim/reaper_tracker.py` is a **read-only observer** that makes that
concern measurable without touching any reaper (no product code changed).

## The reaper stack (four reapers)

| Reaper | Where | Cadence | Can it kill a process? | What it does |
|---|---|---|---|---|
| **A2 session reaper** | `omniagentos/sessions/supervisor.py` | per 0.5s poll | yes (idle, enforce-gated) | idle-kills RUNNING/RESUMING bridge sessions > 15m; **max-park** terminalizes awaiting-approval > 20m as FAILED (**always on**); STARTING stall > 5m → FAILED |
| **liveness-reaper** | `Work/OmniAgentOS/Ops/agent-observability/liveness-reaper.py` | launchd 30m | no (marks DB rows) | marks session rows `failed` when the pid is proven dead; closes orphaned `swarm_attempts` |
| **idle-reaper.sh** | `Work/Ops/bin/idle-reaper.sh` | launchd 5m | yes (SIGKILL flatlined agent CLIs; restarts Gemini crew) | reclaims RAM from runaway daemons; `ARMED=0` = report-only |
| **fleet-reaper.sh** | `Work/Ops/bin/fleet-reaper.sh` | launchd 60s | no | flags overdue fleet runs to `stalled-runs.jsonl` |

## What the tracker records (`var/livesim/reaper-ledger.jsonl`, `reaper-tracker.v1`)

- **From the live DB**: session counts by `killed_by`, the liveness-reaper's
  error-signature count, `max_park_last_7d`, and the newest 25 reaper kills with
  **`idle_at_kill_s`** (= `updated_at − last_activity_at`) — a *small* value means
  the session looked active right up to the kill (the legitimate-kill signature).
- **From logs**: A2 `reaper.kill` / `would_kill` / `defer` / `max_park` event
  counts + samples, and `idle-reaper.sh` JSONL event count.

## Commands

```bash
scripts/livesim/reaper_tracker.py snapshot     # append one snapshot + print summary
scripts/livesim/reaper_tracker.py summary       # newest snapshot summary
scripts/livesim/reaper_tracker.py legitimacy     # kills that looked legitimate
```

## Current finding (2026-08-06)

`legitimacy` flags **16 max-park kills, every one with `approval: none recorded`** —
the approval was never delivered (no `SLACK_WEBHOOK_URL`/paging), so no human could
approve inside the 20-minute window and the session was FAILED. These are
legitimate sessions starved of an approver, not idle agents. Logged as **LS-001**.

The A2 `reaper.*` log-event counts read 0 here: those events go to the Python
logs which rotate, so the **durable** signal is the DB `killed_by` attribution,
not the logs. A repair session that wants live idle-kill visibility should add a
durable JSONL sink to `_reap`/`_reap_parked_if_needed` (product change — not done
this session).

## Scheduling (optional, render-only per repo convention)

To keep a rolling record, run `snapshot` every 30 min. A launchd plist is NOT
auto-installed (repo convention: installers render, they do not load). To enable:
create `~/Library/LaunchAgents/com.livesim.reaper-tracker.plist` invoking
`.venv/bin/python scripts/livesim/reaper_tracker.py snapshot` with StartInterval
1800, then `launchctl bootstrap gui/501 <plist>`.
