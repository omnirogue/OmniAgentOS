# Onboarding — Bob and Alice

Step-by-step for the two of you specifically. Two machines, two different
roles, and the split is deliberate (the operator, 2026-08-11 — `ROUTING-DECISIONS.md`
§1):

| your machine | role | runs work? |
|---|---|---|
| **MacBook (Mac 1)** | submit + observe ONLY — `WQ_TOKEN` + tailnet, `wq enqueue --by <name>`, `wq status` | **No. Never enrolled as a worker.** |
| **Mac Studio (Mac 2)** | worker — enrolled with `enroll.sh`, claims and runs units | Yes, once Wave 2 starts |

the operator's own Mac Studio (`mac-studio`) is the queue server and the merger and is
**never** a worker — "all of us should be able to offload work to the Mac
Studios — *not mine*". Your laptop stays a client because a laptop that sleeps
mid-unit is a lease the pool has to reclaim; your Studio is the box that
actually earns its slot.

Copy-paste blocks below; nothing here is a placeholder you need to fill in
except the things marked `<...>`.

---

## Day 1 — Mac 1 (your MacBook): submit + observe

You do this on **Mac 1** (your MacBook), the machine you already do repo
work on. No queue infrastructure required for this part — and no `enroll.sh`,
ever, on this machine.

1. **Install Tailscale** (if not already) and accept the operator's invite to the
   tailnet: https://tailscale.com/download — sign in with the account the operator
   invited.

2. **Generate your own SSH keypair** — never reuse a work key you already
   have elsewhere, and never ask anyone for their private key:

   ```bash
   ssh-keygen -t 6dbe26e -C "<you>@initech"
   cat ~/.ssh/id_ed25519.pub
   ```

   Send the operator **only** the output of that `cat` (one line, starts with
   `ssh-6dbe26e`). That is the whole of what he needs from you to grant SSH
   access to the worker Macs (`ACCESS.md`).

3. **Get `WQ_TOKEN` from the operator** — a single `WQ_TOKEN=<hex>` line, not his
   full `connections.env`. Store it:

   ```bash
   mkdir -p ~/.config/omni && chmod 700 ~/.config/omni
   echo "WQ_TOKEN=<the value the operator gives you>" >> ~/.config/omni/connections.env
   chmod 600 ~/.config/omni/connections.env
   ```

4. **Clone the repo and sync** (if you have not already):

   ```bash
   git clone https://github.com/Globex/OmniAgentOS.git ~/OmniAgentOS
   cd ~/OmniAgentOS
   uv sync
   ```

5. **Confirm you can reach the queue**:

   ```bash
   set -a; source ~/.config/omni/connections.env; set +a
   curl -sf -H "Authorization: Bearer $WQ_TOKEN" http://mac-studio.tailnet-name:8487/v1/health
   uv run python -m omniagentos.workqueue.cli status --server "http://mac-studio.tailnet-name:8487"
   ```

   (Ask the operator for the primary's actual tailnet hostname — it will show as
   `mac-studio` or similar in the Tailscale admin console / `tailscale
   status`.) That's it — you can now read `wq status`, `wq machines`,
   `wq alerts`, and enqueue units from your own laptop.

6. **Set your name once, so every unit you offload is attributed to you.**
   Add it to your shell profile on Mac 1:

   ```bash
   echo 'export WQ_USER=alice' >> ~/.zshrc     # or bob
   echo 'export WQ_SERVER=http://mac-studio.tailnet-name:8487' >> ~/.zshrc
   ```

   Then submitting is just:

   ```bash
   set -a; source ~/.config/omni/connections.env; set +a
   uv run python -m omniagentos.workqueue.cli enqueue --file units.jsonl
   uv run python -m omniagentos.workqueue.cli enqueue --json '<one unit as JSON>'
   ```

   `--by <name>` overrides `WQ_USER` for a single command. Attribution is what
   makes the **OFFLOADS** block in `wq status` useful — it shows, per person,
   what is queued, what is running and on which box, so we all know when one of
   us has a pending job somewhere. Without it your work shows as
   `(unattributed)`, which is visible but anonymous.

---

## Day 2+ — Mac 2 (your Mac Studio) joins as a WORKER (Wave 2)

This is the only machine of yours that ever runs `enroll.sh`.

This is **S4** in `MACHINE-FLEET-PLAN.md` — needs SSH access to that box.

1. Confirm the operator has added your public key to `omniworker`'s
   `authorized_keys` on your Mac 2 (`ACCESS.md`), and that you're on the
   Tailscale ACL that grants `tag:wqworker`. Verify:

   ```bash
   ssh omniworker@<your-mac-2-tailnet-name>
   ```

   If that fails, that's the blocker — go back to the operator before anything else,
   don't debug the queue yet.

2. On **Mac 2**, as `omniworker`:

   ```bash
   git clone https://github.com/Globex/OmniAgentOS.git ~/OmniAgentOS
   cd ~/OmniAgentOS
   uv sync

   mkdir -p ~/.config/omni && chmod 700 ~/.config/omni
   echo "WQ_TOKEN=<same value as Mac 1>" >> ~/.config/omni/connections.env
   chmod 600 ~/.config/omni/connections.env
   # Plus whatever AI provider key(s) your declared agent_profile needs —
   # NOT the operator's full connections.env. Minimal env only (ACCESS.md).

   scripts/workqueue/enroll.sh --primary mac-studio.tailnet-name:8487 \
        --labels darwin,build,pytest,script --max-concurrent 3
   ```

   Those are the labels from the fleet table (`RUNBOOK.md` §1). Two of them are
   earned, not assumed: add `gate` only after your box has passed a parity
   check against the primary (a gate verdict from a host with different
   binaries is not evidence), and add `agent-codex` / `agent-claude` only if
   that CLI is installed and signed in there — the preflight checks it, and the
   label is a promise that a unit naming that profile can actually run.

3. `enroll.sh` runs a preflight (arch, `gtimeout`, `uv` + Python 3.12, git
   ≥2.39, the repo venv, `connections.env` present, reachability to
   `:8487/v1/health`, agent CLIs for any `agent-*` label you declared, a git
   mirror, 5G free disk) and **aborts on the first failure with a named
   remedy** — fix what it names, re-run, do not loop retrying the same
   command.

4. On success it installs a launchd LaunchAgent (`~/Library/LaunchAgents/
   com.omniagentos.wq-worker.plist`, `KeepAlive`) and confirms the machine
   shows up via `wq machines` within ~10s.

5. Verify from your own laptop:

   ```bash
   uv run python -m omniagentos.workqueue.cli machines
   ```

   You should see your Mac 2 with non-zero capacity and a recent `last_seen`.

---

## Reading `wq status` day to day

```bash
uv run python -m omniagentos.workqueue.cli status --watch
```

The number that matters most to you: **oldest unclaimed age**. If it's
climbing while your machine shows idle capacity, something's wrong with your
worker (check `~/wq/logs/wq-worker.err.log` on that box) — it's not that
there's no work.

The block that matters most to the three of us together is **OFFLOADS** — one
line per person, e.g. `alice: 1 running (alice-studio) · 2 queued`. That is how
we each know when someone has a pending job on a computer without asking. Your
line only appears if you submit with `WQ_USER` set or `--by <name>` (Day 1,
step 6).

A machine that is idle claims far more often than a busy one (the poll
stretches from 5 s to 60 s as its load approaches its ceiling), so if your
Studio is quiet it will pick up most of the pool's work by itself. Nobody
assigns units to a box — machines pull.

**When a unit parks:** read `docs/workqueue/RUNBOOK.md` §8 — the table maps
every `terminal_reason` to what to actually do. The short version: a
`storm-parked` or `terminal-instrument` unit needs the underlying cause
fixed, not a blind `unpark` — an unchanged input refuses again in under half
a second regardless of how many times you unpark it.

## Who to ask

- Queue core (store, server, client, contract) questions → Alice, or read
  `omniagentos/workqueue/contract.schema.json` directly — it's the frozen
  wire contract.
- Worker/CLI/gate-wrapper questions → Bob, or `docs/workqueue/RUNBOOK.md`.
- Machine access / Tailscale ACL / who's on the tailnet → the operator
  (`docs/workqueue/ACCESS.md` — he's the one who applies ACL changes).
