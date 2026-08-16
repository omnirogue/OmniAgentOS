# Shared work queue

A durable work queue, stored in one SQLite file on the primary Mac, served over
HTTP on the LAN/tailnet, that any enrolled machine can pull the next unit of
work from, execute against a fresh checkout at a pinned base sha, and report
back — with no human dispatching anything.

Full design: `docs/operations/FOR-ALICE/SPEC-shared-queue.md`
(713 lines — the authoritative source; this doc is the day-to-day summary).
Lane contract for this build: `devtasks/wq-build/LANES.md`.

It is not a scheduler, a model router, or an orchestrator — those already
exist (`omniagentos/dispatch/gate.py`, `omniagentos/execution/policy.py`,
`omniagentos/swarm/`). It is the thing that lets a second and third machine
take work off the first one, with a structural guarantee against re-running
work a gate already refused (the "anti-retry contract", §4 of the spec).

## What it is not

- Not a scheduler/orchestrator/model router (see above).
- Not `var/omniagentos.db` — the queue has its **own** DB
  (`var/workqueue.sqlite3`) and its own migration set
  (`omniagentos/workqueue/migrations/`), independent of the packaged 121
  migrations that serve the live API on :8485. Never wrap `SqliteStore`
  against the queue DB.
- Not bound to `0.0.0.0` — `wq-server` binds `127.0.0.1:8487` only. Machines
  off the tailnet reach it through a reverse SSH tunnel
  (`scripts/workqueue/wq-tunnel.plist.template`), never an exposed port.
- Not a second `dashboard/` page this week — `wq status` (terminal) and
  `GET /v1/status` (JSON) are the whole observability surface (SPEC §6).

## How the pieces fit

```
                    ┌─────────────────────────────┐
                    │  Primary Mac (Mac-Studio)     │
                    │  var/workqueue.sqlite3        │
                    │  wq-server  :8487 (loopback)  │
                    └──────────────┬────────────────┘
                                   │ HTTP, bearer WQ_TOKEN
              ┌────────────────────┼────────────────────┐
       tailnet│                    │ reverse SSH tunnel  │ reverse SSH tunnel
              │                    │ (wq-tunnel.plist)   │ (non-tailnet Linux)
    ┌─────────▼─────────┐ ┌────────▼─────────┐ ┌─────────▼──────────┐
    │ worker (darwin)     │ │ worker (darwin)   │ │ worker (linux)      │
    │ omniagentos.workqueue│ │ mw0001-owner, etc.  │ │ acmeuni, prod hosts    │
    │ .worker              │ │                   │ │                     │
    └───────────────────┘ └───────────────────┘ └────────────────────┘
```

- **`WorkQueueStore`** (`omniagentos/workqueue/store.py`, Lane A): the only
  code that touches `var/workqueue.sqlite3`. Fenced claim/heartbeat/result,
  the reaper, the refusal ledger.
- **`wq-server`** (`omniagentos/workqueue/server.py`, Lane A): standalone
  FastAPI on `:8487`, a thin wrapper over the store — no logic of its own.
  Separate process from `omniagentos/api/main.py` (:8485, live, do not touch).
- **`HttpQueueClient`** (`omniagentos/workqueue/client.py`, Lane A): same
  method surface as `WorkQueueStore`, over HTTP. A worker never knows whether
  it is talking to a local file or a remote server.
- **`omniagentos.workqueue.worker`** (Lane B): the claim → clone → run →
  verify → report loop that runs on every enrolled machine.
- **`scripts/accurate-gate.py`** + `wq_refusals` (Lane B, §4 of the spec):
  the anti-retry contract — a gate that refused an input refuses it again in
  under 0.5 s, no gate spent, until the input itself changes.
- **This lane (ops/access/docs)**: `enroll.sh`, `serve.sh`, the launchd/
  systemd templates, `mint-token.sh`, `grant-access.sh`, and this doc set.

## The wire contract

`omniagentos/workqueue/contract.schema.json` is the frozen JSON shape for
every `/v1/*` endpoint (`unit_submit`, `claim_request`/`claim_response`,
`heartbeat_request`, `result_request`, `machine_enroll`, `machine_beat`,
`refusal_row`, `status_response`). Alice owns it; changes go through a
reviewed PR. `GET /v1/health` is unauthenticated; everything else requires
`Authorization: Bearer $WQ_TOKEN` (constant-time compare,
`secrets.compare_digest`), minted with `scripts/workqueue/mint-token.sh` and
never printed by any script in this repo.

## Quick start (see RUNBOOK.md for the full walkthrough)

On the primary:

```bash
cd ~/OmniAgentOS
scripts/workqueue/mint-token.sh
uv run python -m omniagentos.workqueue.cli init --db var/workqueue.sqlite3
scripts/workqueue/serve.sh install     # launchd, KeepAlive
uv run python -m omniagentos.workqueue.cli status
```

On a joining machine:

```bash
git clone https://github.com/Globex/OmniAgentOS.git ~/OmniAgentOS
cd ~/OmniAgentOS && uv sync
# copy the WQ_TOKEN=<hex> line (only that line) into ~/.config/omni/connections.env
scripts/workqueue/enroll.sh --primary mac-studio.local:8487 --labels build,gate --max-concurrent 3
```

## More

- `RUNBOOK.md` — enroll each real machine, drain, unpark, read `wq status`,
  the tunnel/tailscale commands, minting a token, and what to do for each
  `terminal_reason`.
- `ACCESS.md` — who gets on the tailnet, the ACL, per-person keys, and what
  never goes on a worker box.
- `ONBOARDING.md` — step-by-step for Bob and Alice specifically.
