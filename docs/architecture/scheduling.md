# Scheduling — launchd jobs, routines, twice-daily cadences

All background work runs as macOS `launchd` agents: plist TEMPLATES rendered by
Python scripts, installed into `~/Library/LaunchAgents/` by shell installers
(`scripts/<subsystem>/install-*.sh`), executed on fixed schedules
(`StartInterval` seconds, or `StartCalendarInterval` — a single dict or an ARRAY of
dicts for twice-daily cadences). `omniagentos.archdocs.generate` scans this directory
for the `<!-- generated:begin:launchd -->` block in `ARCHI.md` — see that file for the
live label + schedule list, which is more current than any hand-maintained copy here.

## Installer conventions

- Every job sources `~/.config/omni/connections.env` via
  `set -a; . "$HOME/.config/omni/connections.env"; set +a` (a bare `. file` leaves
  vars shell-local — a recurring gotcha).
- Interpreter is pinned to `.venv/bin/python`, falling back to `python3.12` (system
  Python 3.9 is too old); installers exit with an error if neither is found.
- **Split-decision pattern**: safe jobs (keep-alive services, operator-approved
  recurring jobs) auto-`launchctl load`; risky jobs that spend budget by CREATING
  real tasks/runs (`com.omniagentos.routines`) render-only, requiring a manual load
  after review. V2's audit/apply jobs follow the SAME pattern — `watch` and `audit`
  auto-load; `daily`/`weekly` are rendered and loaded only after the first audit is
  reviewed (see the production-readiness checklist in `V2-DESIGN.md` §14).
- No explicit launchd dependency keys (`After`/`Requires`) — ordering is encoded only
  in installer sequencing.

## Routines engine (`omniagentos/scheduler/`)

`com.omniagentos.routines` (every 300s) ticks `routines_tick.py`: checks
`routines.trigger_type='cron'` rows against `last_fired` + current time
(`should_fire`/`validate_routine` in `routines.py`), creates `routine_runs` + a
control-plane task on match. Acceptance-rate tracking auto-pauses a routine on floor
breach — a proto-autonomy-level control that V2's `autonomy_settings` scope
resolution (see `reliability.md`) generalizes.

**Claim, then dispatch (M3).** A tick CLAIMS each due firing sequentially — validate,
mint the `run_id`, stamp `last_fired`, write the `routine_runs` row for a dispatched
task — and hands built-in/loop EXECUTION to a bounded pool
(`OMNIAGENTOS_ROUTINE_DISPATCH_CONCURRENCY`, default 8) instead of running it inside the
loop. Before that split, project B's loop was not launched until project A's loop
returned, so the slowest project set every other project's latency (a loop may run for
its whole `timeout_s`, up to an hour). `last_fired` moved to claim time in the same
change: an overlapping tick — an operator running the module by hand while the launchd
job is mid-flight — must see the trigger as already served rather than start a second
worker for the same loop instance. The pool is drained before the settle pass, so a
tick still settles everything it fired.

## Established cadences (pre-V2)

- `com.omniagentos.modelintel` — 07:15 daily, model-capability registry refresh.
- `com.omniagentos.selfimprove-curator` — twice daily (default 03:30 + 15:30),
  captures reusable skills from completed runs.
- `com.omniagentos.steward.alerts` — every 900s, deterministic failure-rule scan
  (ROAS floor, spend spike, payment failures, revenue crash) with cooldown-aware
  alerting (magnitude jump ×1.5 OR severity upgrade escalates; same-severity ≤1.5×
  is suppressed).
- `com.omniagentos.steward.briefing` — 07:30 daily digest.
- `com.omniagentos.steward.comms` — 02:30 daily inbound extraction.
- `com.omniagentos.steward.metrics` — hourly goal-metric collection.
- `com.omniagentos.banking` / `.hourly`, `com.omniagentos.revenue` / `.hourly` —
  daily authoritative snapshot + hourly refresh (ET calendar).
- `com.omniagentos.cache-gc`, `com.omniagentos.filesearch-index` — daily / every 2h
  maintenance.

## V2 reliability cadence (design §11)

**Status: `python -m omniagentos.reliability {watch|audit|daily|weekly}` CLI exists**
(`reliability/cli.py`/`__main__.py`, dispatching to `reliability/audit.py`'s
`watch`/`twice_daily`/`daily_summary`/`weekly_architecture`); **the launchd plists +
installer under `scripts/reliability/` are integration-wave (W10) work** — check
`~/Library/LaunchAgents/` for `com.omniagentos.reliability-*` before assuming they're
loaded.

| Label | Schedule | Command |
|---|---|---|
| `com.omniagentos.reliability-watch` | every 600s | `python -m omniagentos.reliability watch` — detect → dedup → safe recovery → critical alerts → monitoring-window checks/auto-rollback |
| `com.omniagentos.reliability-audit` | 06:30 + 18:30 | `... audit` — full sweep + department reviews + CTO quick pass + proposals → sandbox → judges → queue/apply + vault report |
| `com.omniagentos.reliability-daily` | 08:05 | `... daily` — consolidated daily improvement summary |
| `com.omniagentos.reliability-weekly` | Sun 09:00 | `... weekly` — CTO deep architecture review + scorecard trends + doc staleness |

**Dead-man's switch** (§11, M5): a rule added to the EXISTING steward alerts monitor
(`com.omniagentos.steward.alerts`, independent code path, every 900s, Tier P
protected) fires `critical` if the reliability watch cursor hasn't advanced in >45min
or no audit row exists in >14h — silence from the reliability system is itself a
detected failure. Every reliability mode is also callable on demand: CLI
(`--once`) or `POST /api/reliability/audit/run`.

## Notification policy across cadences (design §11)

Every scheduled job maps its findings onto the existing sealed 6-kind
`notifications` enum (`migration 030_notifications.sql`) — no new kind, no new
migration for notifications: critical failure ⇒ immediate `alert`; a proposal
reaching `awaiting_human` ⇒ `approval`; a successful apply ⇒ `done`; rollback or
finalize-quarantine ⇒ `escalation`; everything low-risk batches into the next
`daily` summary as `info`. Cooldown suppression follows the same steward semantics
(`governance.md`) EXCEPT `critical` severity, which is exempt from cooldown
suppression by design (m5) — a repeated critical failure never gets silently
swallowed just because an earlier one already alerted.

## Why launchd, not a Python scheduler in-process

The repo deliberately keeps scheduling OS-native (launchd `StartInterval`/
`StartCalendarInterval`) rather than an in-process cron/celery-style scheduler:
jobs survive a crashed/restarted API or runner process, macOS handles catch-up on
wake from sleep, and each job is independently loadable/unloadable/inspectable via
`launchctl list` without touching running application state. The cost is the
render-template-then-install indirection above, and the twice-daily
`StartCalendarInterval` array-of-dicts quirk (see `selfimprove-curator.plist` for
the canonical working shape) — both worth documenting explicitly since they're
easy to get wrong when adding a new job.

## Notes (human)
