# Team production report — runbook

The 07:00 report is the **one** daily production message. It ranks the operator, Alice and Bob by
verified output over a rolling 7 days, prints each person's `production_x` against their
pre-launch baseline, names one bottleneck each, and posts the result to Slack.

- Code: `omniagentos/team/report.py` (`gather` → `render` → `post`)
- Score: `omniagentos/team/scoring.py`, `SCORE_VERSION = "v1"`
- Diagnostics: `omniagentos/team/diagnostics.py` (descriptive only — never scored)
- Snapshot table: `prod_snapshots` (one row per person per day, upserted)
- Report file: `var/team-reports/YYYY-MM-DD.md`
- Log: `var/log/team-report.log`

## What it counts (and what it refuses to)

Points come from ONE thing: a **top-level card, owned by a person, `done`, and `verified_at`
inside the window**, worth its size — **S=1, M=3, L=8**.

Worth exactly zero: commits, lines changed, sessions, tokens, PRs opened, task count, status
changes, and every subtask (a parent's size already prices the whole job). Evidence at
`quality_gate` `rejected` / `reverted` / `excessive_attempts` never adds, and a card whose
*only* evidence is of that kind cannot be counted at all.

The seeded `BASE-*` cards (`source = baseline-2026-08-03`) are the DENOMINATOR, not output:
they appear in each person's `excluded` list with reason `baseline_period` and are summed by
`baseline_points()` instead. `production_x` is `None` — rendered "no baseline" — when a person
has no baseline card. It is never a fabricated `1.0x`.

## Install

Both jobs run as the login user (`gui/501`).

```sh
cp /Users/youruser/OmniAgentOS/configs/launchd/com.omniagentos.team-report.plist \
   ~/Library/LaunchAgents/
cp /Users/youruser/OmniAgentOS/configs/launchd/com.omniagentos.team-sweep.plist \
   ~/Library/LaunchAgents/

mkdir -p /Users/youruser/OmniAgentOS/var/log

launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.omniagentos.team-report.plist
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.omniagentos.team-sweep.plist

launchctl print gui/501/com.omniagentos.team-report | head -20
launchctl print gui/501/com.omniagentos.team-sweep  | head -20
```

Reloading after an edit is `bootout` then `bootstrap` (`launchctl load -w` is the deprecated
spelling and silently does nothing on a job that is already bootstrapped):

```sh
launchctl bootout gui/501/com.omniagentos.team-report
launchctl bootstrap gui/501 ~/Library/LaunchAgents/com.omniagentos.team-report.plist
```

Fire one immediately without waiting for 07:00:

```sh
launchctl kickstart -p gui/501/com.omniagentos.team-report
```

### Retire the superseded scoreboard

`com.omni.daily-dev-scoreboard` (07:45, `~/.omniagentos/ops/bin/daily-dev-scoreboard.py`) scored
`real_commits + 3 × merged_PRs` — both of which this system deliberately values at zero, and
both of which are inflatable without finishing anything. **It never ran in production.** Retire
it in the same change that installs this one; two daily scoreboards disagreeing about the same
week is worse than either alone:

```sh
launchctl bootout gui/501/com.omni.daily-dev-scoreboard
# then, so it cannot come back on next login:
mv ~/Library/LaunchAgents/com.omni.daily-dev-scoreboard.plist \
   ~/Library/LaunchAgents/disabled-com.omni.daily-dev-scoreboard.plist 2>/dev/null || true
```

`launchctl bootout` on a job that was never loaded prints `No such process` — that is the
expected, successful outcome here, not an error.

### The 07:30 briefing keeps comms and metrics

The steward briefing (`omniagentos/briefing/`) still sends at 07:30 and still carries
**Metrics and urgent items** + **Communications digest**. Its **Operations** section (run
counts, run cost, promoted knowledge, reliability events) was removed in P4 — that content is
this report's job now. The briefing's gathered data is unchanged, so its anti-fabrication
number check is exactly as strict as before.

## Manual runs

```sh
cd /Users/youruser/OmniAgentOS

# Print today's report. Writes NOTHING: no file, no snapshot, no Slack.
.venv/bin/python -m omniagentos.team.report --dry-run

# A past day.
.venv/bin/python -m omniagentos.team.report --day 2026-08-11 --dry-run

# Write the file and the prod_snapshots rows, but do not post.
.venv/bin/python -m omniagentos.team.report --no-post

# The real thing.
.venv/bin/python -m omniagentos.team.report
```

Flags: `--day YYYY-MM-DD`, `--db PATH` (default `OMNIAGENTOS_DB`, else `var/omniagentos.db`),
`--out-dir` (default `var/team-reports`), `--channel` (default `$OMNI_TEAM_REPORT_CHANNEL`,
else `C0000EXAMPLE` = `#dev-agentic-alerts`), `--dry-run`, `--no-post`.

Re-running for the same day is safe: snapshots upsert, the file is overwritten, and the numbers
are recomputed from the board. It will, however, post to Slack again.

## Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| `0` | Report written, snapshots upserted, posted (or `--no-post`). | Nothing. |
| `1` | Everything was written; **Slack delivery failed**. | Read `var/team-reports/<day>.md` — the report exists. Fix the token (`SLACK_BOT_TOKEN` in `~/.config/omni/connections.env`) and re-run. |
| `2` | **Score-version pin.** A `prod_snapshots` row for this day was computed by a different `SCORE_VERSION`. Nothing was written or posted. | Do NOT retry unchanged — it will refuse identically. Either delete/archive that day's rows, or run the build whose version wrote them. |

Exit 2 is a do-not-retry code by design: re-running a refused gate on an unchanged input buys
the same answer twice.

## Reading the output

```
DAILY PRODUCTION — 2026-08-14
1. ALICE — 1.4x (14% to 10x)
   major contribution: UP-3 — Shared queue spec
   verified outcomes: 4 · avg sessions: 2.3 · first-pass: 80%
   queue: 3 active / 6 ready / 0 blocked / 1 in review
   #1 bottleneck: none / recommended: keep going
```

- `n/a` means the rate had **no samples** (e.g. no merged PR recorded a `gate_attempts`). It is
  not `0%`. A rate computed from zero samples is a fiction.
- `no baseline` means the person has no `BASE-*` card. It is not `0.0x`.
- `major contribution` is the highest-point counted card, latest verified on a tie.

## Bottleneck rules

First match wins, in this order. Only one is ever printed — a report that lists every true
statement is a report nobody reads to the end.

| # | Condition | Text |
| --- | --- | --- |
| 1 | ≥ 2 blocked cards | `blocked: <refs>` |
| 2 | oldest `awaiting_approval` card > 24h | `review latency: <ref>` |
| 3 | fewer than 5 ready cards | `queue starvation (<n> ready)` |
| 4 | zero verified outcomes in 48h | `no verified output 48h` |
| 5 | — | `none` |

The team bottleneck is the most common person-class; ties break by severity
(blocked > review latency > no verified output > queue starvation).

## Troubleshooting

**Nothing posted, log says `no SLACK_BOT_TOKEN`.** launchd has no shell profile; the report
reads `~/.config/omni/connections.env` itself. Check the file is readable by the login user and
that the line is `SLACK_BOT_TOKEN=xoxb-...`.

**Everyone reads `no baseline`.** The `BASE-*` cards were not imported. Run
`scripts/import-reset-queue.py` (idempotent) against the same database the plist points at, and
confirm with `sqlite3 var/omniagentos.db "SELECT ref, size, verified_at FROM board_tasks WHERE
source='baseline-2026-08-03'"`.

**Everyone reads `0` points but work clearly happened.** Points require `verified_at`. Check
`SELECT ref, status, verified_at FROM board_tasks WHERE status='done' AND verified_at IS NULL` —
those cards are in each person's `excluded` list with reason `done_not_verified`. Verification is
a `POST /api/team/tasks/{id}/verify`, not a status change.

**Sessions/PR diagnostics are all `n/a`.** The sweep is not running. Check
`var/log/team-sweep.log` and `launchctl print gui/501/com.omniagentos.team-sweep`.

**The wrong database.** Both plists set `OMNIAGENTOS_DB` explicitly; the report defaults to it.
If the API and the report disagree, they are on two files — compare
`launchctl print gui/501/com.omniagentos.team-report | grep OMNIAGENTOS_DB` against the
API's environment.

## Live API

Same computation, no side effects, behind the session-token boundary:

- `GET /api/team/scoreboard?window=7d` — points, `production_x`, and the full counted/excluded
  breakdown per person.
- `GET /api/team/diagnostics?owner=emp_alice&window=7d` — sessions, concurrency, merged PRs,
  first-pass rate.
- `GET /api/team/report/preview` — today's report text, rendered live. Writes nothing.
