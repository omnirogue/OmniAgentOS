# Fleet session capture

Fleetcap captures CLI transcript bytes from the whole estate, attributes each parsed session to its dispatcher, and produces a deterministic morning health brief. It is additive to the legacy 02:15 extractor. It never changes source transcripts and does not replace or re-arm the reflection loop.

## Architecture and data locations

Pull devices are read through one BatchMode SSH preflight followed by conservative `rsync -a --update` commands. Push devices use the same no-delete rsync behavior from their local scheduler. Both land below:

```text
~/.omniagentos/ops/telemetry/
├── ingest/<device>/<cli>/<account>/...  raw copied transcript bytes (0700 parent)
├── spool/hooks-YYYYMMDD.jsonl           local append-only Claude hook sidecars
├── briefs/session-improvements-*.md     morning briefs outside any checkout
├── fleet.sqlite                         shared legacy + fleetcap session store
└── logs/                                launchd output
```

`profiles.py` knows the default and multi-account layouts for Claude, Codex, Kimi, Grok, and Gemini. The stray bare `~/.claude-account-` is deliberately ignored. New account labels are `default`, `account-1`, `account-2`, and named-profile directory names. Historical rows retain their old incorrect `acct1`-style labels; fleetcap does not rewrite history.

`extract.py` parses Claude, Codex, and Kimi v1 transcripts, joins local and ingested per-device hook sidecars, and writes device and dispatch attribution. Grok and Gemini capture is restricted to `sessions/`, `memtrace/`, and `history/`; their CLI-specific semantic parsers remain follow-up work and one stable estimator row per device/CLI/account is refreshed in place. Ingest-tree IDs use `native:<device>:<cli>:<account>:<stem>`. Hub-local files deliberately use the legacy bare stem so fleetcap enriches the existing row instead of duplicating it. Firm outcomes remain unchanged while attribution fields are refreshed.

## Attribution semantics

Evidence is applied in this order:

1. A `subagents/agent-*.jsonl` sidechain is `subagent:<parent-session>`.
2. An explicit interactive hook signal always identifies a human session, even inside a worktree. A hook sidecar with `interactive: false` (or normalized-empty TTY) is non-interactive and therefore `daemon:<best-label>`; real daemon paths (`/private/tmp/bld-*`, `*-wt`, `var/swarm`, and `var/runtime`) refine the label.
3. A real user turn on an owned device is attributed to that device's employee owner.
4. Sessions without decisive evidence are `unknown` and remain visible in the coverage percentage.

Every row stores the class, dispatcher, and a short mechanical evidence explanation. Device ownership is configuration, not guessed from transcript text.

## Hub setup and operation

Review `configs/fleetcap/devices.yaml`, keeping remote roots absolute. Install the two plist templates from `configs/fleetcap/` into `~/Library/LaunchAgents`, update their repository path if the live checkout differs, create `~/.omniagentos/ops/telemetry/logs` with mode 0700, and bootstrap them with `launchctl`.

Useful safe checks:

```sh
python -m omniagentos.fleetcap.pull --dry-run
python -m omniagentos.fleetcap.migrate
python -m omniagentos.fleetcap.extract
python -m omniagentos.fleetcap.daily --dry-run
```

A dark SSH device is logged and skipped when at least one peer is reachable; a wholly dark fleet exits nonzero. Pull never deletes or writes remotely. Every rsync command carries a hard credential filename deny-list, and configured roots outside known safe per-CLI subpaths are rejected. The database connection uses a 5000 ms busy timeout to coexist with the legacy extractor.

## Enroll a new MacBook or Linux device

From a checkout containing fleetcap, run:

```sh
omniagentos/fleetcap/enroll.sh --device <tailscale-name> --owner emp_<name>
```

The script copies fail-open hooks to `~/.local/lib/fleetcap`, idempotently merges them into every Claude profile without disturbing existing hook entries, and installs a launchd job (macOS) or cron entry (Linux). Device names are validated before interpolation. When enrollment runs on the configured hub, it also installs the vendored `rrsync` at `/Users/youruser/Work/Ops/bin/rrsync` with mode 0755. Before the first push, the hub operator must create the jail with `mkdir -p /Users/youruser/Work/Ops/telemetry/ingest/<device>` and otherwise copy the vendored `omniagentos/fleetcap/vendor/rrsync` to `/Users/youruser/Work/Ops/bin/rrsync` with mode 0755. Verify the install with `test -x /Users/youruser/Work/Ops/bin/rrsync && /Users/youruser/Work/Ops/bin/rrsync -h >/dev/null`. Then install the exact restricted entry printed by enrollment: `command="/Users/youruser/Work/Ops/bin/rrsync -wo /Users/youruser/Work/Ops/telemetry/ingest/<device>",restrict,no-agent-forwarding,no-port-forwarding`. Never grant the push key an unrestricted shell. The push job uses only target-relative rsync destinations so it operates inside that jail. Run `~/.local/bin/fleetcap-push` once, verify `ingest/<device>/DEVICE.json`, and add the device to `devices.yaml` as `mode: push` for freshness monitoring.

Hooks append metadata only and never touch SQLite. Their spool directory is mode 0700; errors always exit zero so capture cannot prevent Claude Code from starting or ending.

## Morning post

At 07:12, `daily.py` reads the last 24 hours, the matching `var/reflection/<date>/digest.md` when present, and ingest freshness. It writes `~/.omniagentos/ops/telemetry/briefs/session-improvements-YYYY-MM-DD.md` before attempting Slack delivery. The compact post is capped at 15 lines and contains coverage first, reliability, at most three ranked improvements with session evidence, and the full brief path. Missing backing columns are reported as unavailable, never as favourable zeroes. Slack delivery failure exits nonzero and logs the channel/result.

All transcript-derived strings have Slack mentions and token-shaped secrets removed. `FLEETCAP_SLACK_CHANNEL` overrides the default `#dev-agentic-alerts` channel; credentials are loaded at runtime by the existing team report helper and are never stored here.
