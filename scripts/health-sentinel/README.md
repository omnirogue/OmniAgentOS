# Agent Health Sentinel

One job that answers "is the fleet actually alive?" every 30 minutes. Every signal
it reads already existed on this box — the API health route, the routines log, the
account pool config, the `claude_accounts` table, `provider-health.json`, the
reflection briefings, `launchctl` — but nothing assembled them, so each of them
could go dark on its own without anyone noticing. This assembles them and shouts.

**No LLM calls, no provider CLI spawns.** The whole pass is file reads, one
localhost HTTP GET, read-only SQLite SELECTs, `ps` and `launchctl list`. Measured
runtime: **~0.1 s** of check work, ~2 s wall including the venv/env bootstrap
(budget is 60 s).

## What it checks

| Check | Passes when | Fails when |
| :--- | :--- | :--- |
| `api` | `GET http://127.0.0.1:$OMNIAGENTOS_API_PORT/api/health` returns 200, `status="ok"`, `db=true`, worker heartbeat < 2 min | unreachable, non-200, `degraded`, `db=false`, or heartbeat > 5 min (warn 2–5 min) |
| `runner` | a `python -m omniagentos.runner` process exists **whose command line contains this repo root** | no runner, or only a sibling checkout's runner (that case says so explicitly) |
| `scheduler` | newest `checked_at` in `var/log/routines.log` < 10 min old (cadence ~5 min) | > 15 min old, or no parseable tick record (warn 10–15 min) |
| `claude_pool` | every account `enabled: true` under `providers.claude` in `configs/accounts.yaml` has live auth | any is dead — lists which, versus pool size; warns for enabled DB rows missing from YAML, DB-disabled YAML accounts, active `paused_until`/`cooldown_until` windows, or missing DB rows |
| `memory` | `var/memories/OmniAgentOS/MEMORY.md` exists, non-empty, touched within 7 days; memlife store dir present; control-plane SQLite present with readable `memlife_*` tables | missing/empty MEMORY.md, missing store, missing/locked DB (warn: stale >7d, empty candidates) |
| `reflection` | `vault/briefings/reflection-<today>.md` **or** `-<yesterday>.md` exists and is non-empty (25 h window) | neither exists — names the latest `reflection-ALERT-*.md` for context |
| `providers` | `var/provider-health.json` parses, has a `results` mapping, and every entry is structurally valid with `ok: true` | unreadable/missing snapshot or missing results mapping; an entry with boolean `ok: false` fails, while a nonmapping entry or entry without boolean `ok` is unparseable and produces a WARN (warn: snapshot older than 36 h) |
| `launchd` | every `com.omniagentos.*` plist installed in `~/Library/LaunchAgents` is loaded with last-exit 0 | installed-but-not-loaded, or nonzero last exit (warn: rendered into `var/launchd/rendered` but never installed) |
| `disk` | `/System/Volumes/Data` free > 50 GB | below 50 GB (warn below 75 GB) |

### How the Claude pool is judged (and why two sources)

`claude_pool` **never spawns a CLI and never spends a token.** It reads two
independent signals and treats an account as dead if *either* condemns it:

1. **Credential file** — `~/.claude-account-N/.credentials.json` →
   `claudeAiOauth`. Only the two integer expiry stamps and the subscription
   label are read, never a token value. A healthy profile on this machine has
   `expiresAt: 0` plus a **future** `refreshTokenExpiresAt`; a dead one has a
   **past** `expiresAt` and no refresh stamp at all. A missing file means logged
   out.
2. **`claude_accounts` row** (migration 036) — the runtime's own verdict, written
   by `accounts.service.mark_status` after a real spawn. This is the cheapest
   possible live probe because a previous run already paid for it, and it carries
   the exact failure text (e.g. `401 OAuth access token has been revoked`).

Neither alone is sufficient. A **server-side revocation leaves the credential
file untouched**, so disk alone reports a revoked account as healthy; and the DB
row is only as fresh as the last spawn that used that profile, so the DB alone can
be silent about a profile nobody has touched since it broke. A yaml-enabled
account that is `enabled = 0` in `claude_accounts` is reported as a **warn-level
divergence** — that split is itself a way for the pool to quietly empty out.

The comparison also covers the reverse direction: an enabled `claude_accounts` row whose
`config_dir` is absent from the YAML pool is a **warn-level `config_missing` disagreement**.
That account is counted once in the disagreement evidence. Rows that are disabled in the DB are
ignored by this reverse-direction check. For YAML accounts present in the DB, an active
`paused_until` or `cooldown_until` window is a **warn-level** `db_paused` or `db_cooling`
disagreement, including its expiry in the evidence; expired or malformed timestamps are not
treated as active windows.

`discover_glob` in `configs/accounts.yaml` is deliberately not expanded: the
config lists every non-authenticating profile explicitly-and-disabled precisely so
the glob cannot re-admit it, and this check honours that.

## Where alerts land

Any check at `fail` produces all four of these; `warn` only produces the first two.

1. **Snapshot** — `var/health-sentinel/latest.json` (atomic write; full per-check
   evidence + detail).
2. **History** — `var/health-sentinel/ledger-YYYYMM.jsonl`, one line per run.
   Human log at `var/log/health-sentinel.log`.
3. **Notification + macOS banner** — a `kind="alert"`, `severity="high"`
   notification through `omniagentos.notifications.service.record_notification`
   with `ref_type="health_sentinel"` and a date-scoped `ref_id`
   (`<check>:YYYY-MM-DD`). **The banner is deduped per issue per day** by
   `var/health-sentinel/alert-state.json`: `record_notification` pushes even when
   its own DB dedupe suppresses the row, and at a 30-minute cadence that would be
   48 identical banners a day. Warns never banner.
4. **Briefing** — `vault/briefings/health-ALERT-YYYY-MM-DD.md`, in the same table
   style as `reflection-ALERT-*.md`, with a per-failure remediation list. It is
   **rewritten on every failing pass** so it always shows current state.

## Run it by hand

```sh
scripts/health-sentinel/health-sentinel.sh              # full pass, prints a summary
scripts/health-sentinel/health-sentinel.sh --json       # snapshot to stdout
scripts/health-sentinel/health-sentinel.sh --quiet      # log/snapshot only (what launchd uses)
scripts/health-sentinel/health-sentinel.sh --fail-exit  # exit 1 when anything fails
```

The wrapper sources `scripts/launch-env.sh` (so `OMNIAGENTOS_DB`, `OMNIAGENTOS_API_PORT`
and the account-pool env match every other runtime) and uses `.venv/bin/python`.
The exit code is **0 even when checks fail** unless `--fail-exit` is passed —
launchd's "last exit status" should mean *the sentinel ran*, not *the fleet is
healthy*; the fleet's health lives in the snapshot and the banner.

## Install / schedule

```sh
scripts/health-sentinel/install.sh          # render + lint + install + load (idempotent)
HEALTH_SENTINEL_NO_LOAD=1 scripts/health-sentinel/install.sh   # render + lint only
```

Renders `com.omniagentos.health-sentinel.plist` (`StartInterval 1800`) into
`var/launchd/rendered/` following the `install-agent-watchdog.sh` convention, then
— unlike the render-only installers in this repo — copies it into
`~/Library/LaunchAgents` and loads it, because a sentinel that is not loaded is
worse than none. Re-running boots the old copy out before bootstrapping the new
one, so it is safe to run repeatedly.

```sh
launchctl list | grep com.omniagentos.health-sentinel     # <pid|-> <last exit> <label>
launchctl kickstart -p gui/$(id -u)/com.omniagentos.health-sentinel   # run now
launchctl bootout gui/$(id -u)/com.omniagentos.health-sentinel        # stop
```

## Files

| Path | What |
| :--- | :--- |
| `health_sentinel.py` | the ten checks, snapshot/ledger/log/briefing writers, alerting |
| `health-sentinel.sh` | launchd wrapper — sources `launch-env.sh`, uses `.venv/bin/python` |
| `launchd.py` | dependency-free `StartInterval` plist renderer (render-only) |
| `com.omniagentos.health-sentinel.plist.template` | the plist template |
| `install.sh` | render + `plutil -lint` + install + bootout/bootstrap |
