# Comms ingestion — operator setup

Every external conversation (email, Telegram, Slack, generic webhook) is stored as
a complete transcript in SQLite the moment it arrives — always on, zero LLM. A
separate, offline batch job (`python -m omniagentos.comms.extract_batch`) later
selects a small curated slice (VIP senders, goal keywords, or an explicit operator
flag) and feeds it into the knowledge graph as **quarantined** episodes; nothing
here can promote a fact to active status.

Message bodies are attacker-controlled. Never paste a shared secret into
`configs/steward.yaml` — only the NAME of the environment variable that holds it.

## 1. Zapier / generic webhook

Add the source to `configs/steward.yaml` under `comms.sources`, naming (not
containing) the secret's env var:

```yaml
comms:
  inbound_max_bytes: 262144
  rate_limit_per_minute: 60
  sources:
    zapier: {secret_env: COMMS_WEBHOOK_SECRET_ZAPIER}
```

Then export the actual secret (a long random token you generate yourself, e.g.
`openssl rand -hex 32`) before starting the API process:

```bash
export COMMS_WEBHOOK_SECRET_ZAPIER="<paste a random 64-char hex token here>"
```

**Webhook URL** (point your Zapier "Webhooks by Zapier" action, or any HTTP step,
at this):

```
POST http://127.0.0.1:8485/api/comms/inbound?source=zapier
Header: X-Comms-Token: <the same token as COMMS_WEBHOOK_SECRET_ZAPIER>
Content-Type: application/json
```

**Sample payload** (explicit fields are preferred; the generic `{from, to,
subject, body|text|html}` shape is also accepted as a fallback — HTML bodies are
stripped to plain text automatically):

```json
{
  "external_id": "zap-run-12345",
  "sender": "customer@example.com",
  "recipients": ["support@example.com"],
  "subject": "Question about my order",
  "body_text": "Hi, can you check the status of order #4821?",
  "thread_id": "order-4821"
}
```

or the plain Zapier email-relay shape:

```json
{"from": "customer@example.com", "to": "support@example.com", "subject": "Hi", "body": "..."}
```

**Responses**: `202 {"id": <int>, "created": true}` on a new message, `200
{"id": <int>, "created": false}` when the same `(source, external_id)` was
already stored (safe for Zapier retries), `401` for an unknown source or bad
token, `413` for a body over `inbound_max_bytes`, `429` if the per-source rate
limit is exceeded.

## 2. Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`, and
   follow the prompts to get a bot token.
2. Export it:

   ```bash
   export TELEGRAM_BOT_TOKEN="123456:ABC-your-bot-token"
   ```
3. Add your bot to the chat(s) you want to ingest, or message it directly.
4. Run the poller:

   ```bash
   python -m omniagentos.comms.poll --source telegram --once
   ```

   Without a token, this prints `{"source": "telegram", "status":
   "pending_setup", "error": "missing environment variable: TELEGRAM_BOT_TOKEN",
   ...}` and exits `0` — safe to run in a cron job before setup is finished. Drop
   `--once` to long-poll continuously.

## 3. Slack bot

1. Create a Slack app at <https://api.slack.com/apps> → "OAuth & Permissions" →
   add these **Bot Token Scopes**:
   - `channels:history`, `channels:read`
   - `groups:history`, `groups:read` (for private channels the bot is invited to)
2. Install the app to your workspace and copy the **Bot User OAuth Token**
   (`xoxb-...`).
3. Export it and invite the bot to the channels you want ingested:

   ```bash
   export SLACK_BOT_TOKEN="xoxb-your-bot-token"
   ```
4. Run the poller:

   ```bash
   python -m omniagentos.comms.poll --source slack --once
   ```

   Missing the token behaves exactly like Telegram above: `pending_setup` +
   `last_error`, exit `0`.

### 3b. Slack Socket Mode (push) — and why the poller stays

Socket Mode gets a message into `comms_messages` sub-second instead of within a
poll interval. It does **not** replace the poller, and running it alone would be
strictly **worse** than the poller alone:

> **Slack does not replay events that occurred while the client was disconnected.**
> It fires and forgets. A socket process that dies at 02:00 and is noticed at
> 09:00 has lost seven hours of messages permanently, with no symptom. A poller's
> cursor would simply have caught up.

So the shipped shape is a **hybrid**, and both halves install together:

| job | cadence | owns |
| --- | --- | --- |
| `com.omniagentos.comms-slack-socket` | `KeepAlive` (one long-lived WebSocket) | **latency** |
| `com.omniagentos.comms-slack-sweep` | `StartInterval 300` | **determinism** |

Both write through the same `normalize_slack -> StewardStore.insert_comms_message`
code, and `UNIQUE(source, external_id)` on `comms_messages` makes double delivery
a no-op — that is a database constraint, not a convention, and it survives
process death. Because of it, `created > 0` on a sweep is *proof* the socket
missed something, not a guess; it is counted on the `slack` source row
(`reconciled_total` / `reconciled_last_at` / `reconciled_last_count`), logged at
WARNING, and turned into a health-sentinel FAIL.

**Setup**

1. In your Slack app → **Socket Mode** → enable it, then **Basic Information** →
   *App-Level Tokens* → generate a token with the `connections:write` scope
   (`xapp-...`).
2. **Event Subscriptions** → subscribe the bot to **`message.channels` only**.
   Do *not* also add `app_mention`: it fires for mentions in channels the bot is
   already in, carrying the same `ts`, so dedupe absorbs it as pure noise.
3. Put both tokens in `var/secrets/<name>.env` (mode `600`, gitignored) —
   `scripts/launch-env.sh` loads every `var/secrets/*.env` into the environment
   of any job whose plist sources it, so no new plumbing is needed:

   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   ```

   Neither token is ever read by `omniagentos/comms/sockets/slack.py` directly.
   Both are resolved through the credential broker's `slack_ingest.stream`
   capability (`configs/connectors.yaml`), which writes an intent→finalization
   audit pair per resolution and refuses names the connector does not declare.
4. **Invite the bot to the channels you want ingested.** This is not optional
   plumbing, it is the precondition for the whole feature:

   ```
   /invite @initech_jira        # in each channel, from Slack
   ```

   Both halves are membership-scoped. `message.channels` events only fire for
   channels the bot is in, and `conversations.history` answers `not_in_channel`
   for the rest. **Verified live on 2026-08-03** against this workspace
   (`auth.test` → team "Acme University", bot `B0000EXAMPLE`):
   `conversations.list` returns **23 public channels of which the bot is a member
   of 0**, and `conversations.history` on the first one answers
   `{"ok":false,"error":"not_in_channel"}`. Until at least one invite happens,
   the socket receives nothing and the sweep has nothing to reconcile — which is
   why `health_sentinel.check_slack_socket` FAILs outright on
   `member_channels == 0` rather than reporting a quiet, healthy-looking system.
5. Render both jobs and follow the printed `launchctl` commands:

   ```bash
   scripts/scheduler/install-comms-slack.sh
   ```

   Render-only by convention — it never loads anything.
6. **Seed the cursor before enabling the reconciliation alert.** `comms_sources`
   starts empty and the poller's cursor defaults to `"0"`, so the first sweep
   walks all reachable history and reports a large `created`. That is a rollout
   artefact, not a socket miss:

   ```bash
   python -m omniagentos.comms.poll --source slack --seed-cursor
   ```

   That subcommand **merges**. Never seed with an ad-hoc `upsert_comms_source`
   one-liner: `config_json` is replaced wholesale, so a re-run silently deletes
   `reconciled_total` and every per-channel cursor — the only durable record that
   the socket has missed messages, and the windows the sweep still owes.

**Foreground check** (never starts half-configured — a missing token is a
`pending_setup` source row plus exit `2`, never a healthy-looking idle process):

```bash
python -m omniagentos.comms.sockets.slack --once
python -m omniagentos.comms.poll --source slack --once   # read member_channels
```

The sweep's own JSON line is the preflight: `member_channels` must be > 0 and
`channel_errors` 0. A channel that cannot be read is *silently unreconciled*, so
it is counted onto the source row (`channel_errors`, `channel_error_count`) and
surfaced by the sentinel rather than aborting the pass — one unreadable channel
must never stop the other twenty-two.

**What this ingests, stated plainly**

* ✅ public channels the bot is a **member** of, including thread replies
  (Socket Mode only — see below), file shares, and other bots' messages.
* ❌ **private channels, DMs and group DMs — by BOTH paths, structurally.** The
  app has no `groups:history` / `im:history` / `mpim:history` scope, so it cannot
  even *subscribe* to `message.groups` / `message.im` / `message.mpim`, and
  `conversations.list` pins `types=public_channel`. Granting those scopes is a
  deliberate re-scoping decision, not a config tweak.
* ❌ **public channels the bot has not been invited to** — see step 4. They are
  listed (so the sweep can count them) and then skipped by `is_member`.
* ❌ **this app's own bot messages** (`bot_id` `B0000EXAMPLE`, overridable with
  `OMNIAGENTOS_SLACK_SELF_BOT_ID`). Filtered identically on **both** paths — a
  self-filter on one side only would make the reconciliation counter permanently
  non-zero and destroy the signal above.
* ❌ **edits and deletions**, by both paths. `message_changed` / `message_deleted`
  are skipped and counted (`skipped_hidden`); `conversations.history` never
  returns them at all. Skipping is the *consistent* choice, not a complete one —
  real edit tracking needs new schema.
* ⚠️ **thread replies are the hybrid's one genuine hole.** The Events API
  delivers every reply; `conversations.history` does **not** return them. So the
  socket ingests a strict superset, and **the sweep cannot backfill a thread
  reply lost during a socket outage.** "Self-heals" is true for top-level channel
  messages and false for thread replies until a `conversations.replies` pass
  lands in `poll_once`.

**Health.** The client writes a timer-driven (never message-driven — a quiet
channel must not look like a dead socket) heartbeat every 60s onto a **separate**
`slack-socket` source row, visible at `GET /api/comms/sources`. It never touches
`config["last_poll_ts"]` on the `slack` row: that value is the sweep's entire
backfill window, and advancing it from the socket would erase exactly the window
the sweep exists to re-read.

`health_sentinel.check_slack_socket` FAILs on: a stale heartbeat; a stale or
failing **sweep** (worse than a dead socket, because it is what makes a dead
socket survivable); `member_channels == 0`; a socket that is **flapping**; a
store failure; and any increase in the sweep's `reconciled_total`.

That last one is a **watermark**, not a reading of the last sweep's count, and
the difference matters: the sweep runs every 300s while the sentinel runs every
1800s, so a last-value field is overwritten roughly five times out of six before
anyone reads it. The sentinel persists the previous counters to
`var/health-sentinel/slack-watermark.json` and alarms on any *increase* since the
previous check, which cannot miss an event that happened entirely between two
observations. Its first run records a baseline instead of alarming on history.

**Operational notes, so nothing here is a surprise at 03:00**

* **`store_latency_ms_max` is a CONTENTION metric, not a database benchmark.**
  It measures wall time around `insert_comms_message`, which includes waiting on
  the process-wide store lock and SQLite's 5s `busy_timeout`. Multi-second
  values under concurrent API writes are expected. The sentinel therefore
  watches `store_slow_writes` (a monotonic count of writes over 1000ms) — a max
  never decays and would latch a warning forever. Measure the real p99 under
  load before treating any absolute number as a threshold.
* **`redelivered` resets on process restart.** The `event_id` ring is in memory
  by design (a persistent one would become a new source of loss), so after a
  crash Slack's redeliveries of pre-crash envelopes are not counted — though
  dedupe still makes them no-ops. `reconciled_total` is the durable, authoritative
  socket-miss signal; `redelivered` is colour.
* **Ack latency is bounded by store contention (accepted risk, declared).** The
  listener acks before it stores, but slack_sdk dispatches listeners through a
  10-worker pool whose workers all serialise on one store lock, so a queued
  envelope's ack can in principle exceed Slack's 3s budget under a pathological
  SQLite stall. The cost is a redelivery (free — dedupe is a DB constraint) and,
  if sustained, a connection cycle (repaired by the sweep). Decoupling the ack
  onto a bounded internal queue is the real fix and is deliberately **not** taken
  yet: it widens the acked-but-unwritten window from one message to the whole
  queue depth, and that window is repairable by the sweep for top-level messages
  but never for thread replies. `store_slow_writes` is what tells you the day
  that trade becomes worth making.
* **Rate limits are ordinary on the sweep, not exceptional.** Slack's 2025 change
  caps `conversations.history` far more tightly for non-Marketplace apps created
  after 2025-05-29. A 429 is retried with the API's own `Retry-After`, bounded
  per request (3 retries) and per pass (120s total). A channel that stays
  throttled degrades to a counted `channel_error`; it never takes the pass down.
  One pass costs `1 + member_channels` requests — verify the app's tier before
  lowering `OMNIAGENTOS_SLACK_RECONCILE_INTERVAL_SECONDS`.
* **A persistently unprovisioned credential retries every 300s, not every 60s**,
  because each `broker.resolve_for` writes a durable intent+finalization audit
  PAIR. Resolved tokens are cached across reconnects, re-resolved hourly, and
  dropped immediately on an auth-class failure so a rotated token still lands.
* **Neither log rotates.** `var/log/comms-slack-socket.log` and
  `var/log/comms-slack-sweep.log` follow the fleet convention (launchd
  `StandardOutPath`/`StandardErrorPath`, no rotation). Repeated errors are logged
  once and then at DEBUG, so a stuck job does not fill a disk with one sentence —
  but the files still need whatever rotation the rest of the fleet gets.

## 4. Gmail (or any IMAP mailbox) via app password

1. In your Google Account → Security, enable 2-Step Verification, then create an
   **App password** for "Mail".
2. Pick a short name for this mailbox (e.g. `gmail`) — this becomes both the
   `--source` argument and the env var suffix:

   ```bash
   export IMAP_HOST_GMAIL="imap.gmail.com"
   export IMAP_USER_GMAIL="you@example.com"
   export IMAP_PASSWORD_GMAIL="<the 16-character app password, no spaces>"
   ```
3. Run the poller:

   ```bash
   python -m omniagentos.comms.poll --source gmail --once
   ```

   The first run persists a `config_json` on the `comms_sources` row naming
   these three env vars (never the secrets); later runs read from that
   convention automatically. Only `UNSEEN` messages in `INBOX` are fetched, and
   dedupe on `Message-ID` makes repeated polls idempotent.

## Batch knowledge extraction

Curated extraction never runs on the request path. Run it on a schedule (e.g. a
cron job or the Steward's own scheduler once wired):

```bash
python -m omniagentos.comms.extract_batch
```

Prints a JSON summary — `{"scanned": N, "extracted": N, "skipped": N, "failed":
N}` — and always exits `0`, including when the knowledge subsystem itself is
disabled (`OMNIAGENTOS_KNOWLEDGE` unset). Only VIP senders (`alerts.vip_senders`
in `configs/steward.yaml`), goal keyword matches, or a message an operator
explicitly flagged (`kb_status = "selected"`, e.g. via a future dashboard action)
are ever extracted; every extracted episode lands **quarantined** — it can never
self-promote.
