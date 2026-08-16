# provider-sentinel — nightly provider & account health check

This is the identity/instructions doc for the provider-sentinel: a
scheduled, unattended, **LLM-free** preflight (`sentinel.py`) that runs at
22:30 local time — thirty minutes before the 23:00 fable-curator — so every
overnight job inherits fresh provider truth instead of yesterday's guess.
Unlike fable-curator's `prompt.md`, this file is never handed to a live
Claude/Fable session as a system prompt; `sentinel.py` reads it directly, at
runtime, for the machine-parsed policy block below. The prose above the
policy block is the sentinel's mission statement for humans and for any
future agent that reads this directory.

## Mission

Each run, in order:

1. **Provider doctor** — live-launch the four non-Claude CLI providers
   (codex, grok, gemini, kimi) via
   `omniagentos.swarm.provider_exec.ProviderSessionRunner().provider_doctor()`
   and persist the full result to `var/provider-health.json`
   (`{ts, results}`, atomic tmp+rename). Claude is the fifth provider in the
   roster but is never live-doctored here — that would itself be the LLM
   call this job is built to avoid; its health rides the usage snapshot
   below instead.
2. **Usage refresh** — read `omniagentos.accounts.usage.collect_all()`
   (strictly read-only; it never writes its own cache) so tonight's alerting
   and the curator handoff see whatever quota snapshot each CLI last wrote
   to disk — refreshed for codex as a direct side effect of step 1's live
   spawn.
3. **Alerting** — for every provider/account that fails doctor with an
   auth-shaped error, or shows session quota below the remaining-percent
   floor, or has failed doctor `consecutive_fail_nights` nights running (see
   policy below), record a `kind="alert"` notification, deduped per
   (provider, issue) per calendar night. An auth-shaped doctor failure ALSO
   disables the account (`accounts.service.mark_status(..., "error")`) — but
   only when `disable_on_auth_failure` is true, and never for a transient
   (non-auth) failure shape, no matter how many nights it repeats.
4. **Curator handoff** — write a 3-line human summary to
   `var/provider-health-latest-summary.txt` and append one line to
   `var/improvement-log.jsonl` (`improver: "provider-sentinel"`).

No step makes an LLM call of any kind — no Claude/Fable session, no
narrative pass. Everything above is plain Python plus the CLI subprocess
spawns `provider_doctor` itself performs as a LIVE auth/process check.

## Policy (parsed at runtime)

`sentinel.py`'s `load_policy()` parses the fenced `yaml` block below on
every run. Editing the numbers here changes tonight's behavior directly —
no code change, no redeploy. A missing file, missing block, malformed YAML,
missing `policy` key, or an individual bad field falls back to that field's
shipped default (logged, never fatal) — the rest of a partially-valid block
still applies.

```yaml
policy:
  session_remaining_alert_pct: 10
  consecutive_fail_nights: 2
  disable_on_auth_failure: true
```

- `session_remaining_alert_pct` — alert when a provider/account's session
  (5h-style) usage window has LESS than this percent remaining.
- `consecutive_fail_nights` — how many consecutive nights a provider/account
  key must fail `provider_doctor` (`ok: false`) before the "failing N nights
  running" alert fires. The comparison walks `var/provider-health/<date>.json`
  archives backward from tonight; a missing or unreadable archive for any
  required night breaks the streak conservatively (never asserts a repeat it
  cannot prove).
- `disable_on_auth_failure` — when true (default), an auth-shaped doctor
  failure additionally calls `accounts.service.mark_status(account_id,
  "error", ...)` for that account. When false, the alert still fires but the
  account is left enabled — an explicit operator override, not a default.
