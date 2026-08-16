# Access — who gets on the tailnet, and how

Source doctrine: `docs/operations/FOR-OPERATOR/MACHINE-ACCESS.md`.
This doc is the operational summary for the workqueue specifically.

## The short version

**Yes to access. No to the operator's private key — never share a private key with
anyone, for any reason.** Each person generates their own SSH keypair and
sends the **public** half only. Tailscale is the transport: per-person
identity, one-click revoke, no exposed port 22, ACLs that keep the operator's primary
Mac off-limits while granting the two spare worker Macs.

## Tailnet ACL — apply in the admin console

the operator applies this in the Tailscale admin console (Access Controls). It grants
`bob@` and `alice@` SSH access to `tag:wqworker` machines, as the
`omniworker` user only — never as `root` or an admin account, and never to
the primary.

```json
{
  "tagOwners": {
    "tag:wqworker": ["autogroup:admin"]
  },
  "acls": [
    {
      "action": "accept",
      "src": ["bob@", "alice@"],
      "dst": ["tag:wqworker:8487"],
      "proto": "tcp"
    }
  ],
  "ssh": [
    {
      "action": "check",
      "src": ["bob@", "alice@"],
      "dst": ["tag:wqworker"],
      "users": ["omniworker"]
    }
  ]
}
```

**Notes on the snippet:**

- `tag:wqworker` is applied to the **worker** boxes only — `mw0001-owner`,
  `mw0002`, the two Vultr servers, and (once enrolled) Bob's and Alice's own
  Mac **Studios**. Never to `mac-studio`, which is the queue server and
  serialized merger and is never a worker, and never to their **MacBooks**,
  which are submit + observe clients that run nothing (`RUNBOOK.md` §1). The
  primary stays untagged and out of both the `acls` and `ssh` blocks above, so
  neither developer has a path to it through this policy.
- The `acls` block additionally allows `bob@`/`alice@` to reach
  `tag:wqworker` machines on `:8487` directly (submit/observe the queue from
  their laptops without needing SSH at all, once `wq-server` is reachable —
  see RUNBOOK.md §3). If you want the queue API reachable ONLY through the
  primary, drop this block; SSH access is what actually unblocks S4.
  Enrollment traffic under LANES.md still requires the bearer `WQ_TOKEN`
  regardless of network path — Tailscale grants reachability, not
  authentication.
- `"users": ["omniworker"]` is the whole point: SSH access lands them in the
  worker account, never an admin shell.
- **This is the policy as JSON; the operator is the one who pastes it into the
  Tailscale admin console.** No script in this repo applies it automatically
  — that is a deliberate human checkpoint on who reaches production-adjacent
  boxes.

## Per-person keys — the doctrine

```bash
# THEY run this, on their own laptop — never on a shared or admin machine:
ssh-keygen -t 6dbe26e -C "alice@initech"
cat ~/.ssh/id_ed25519.pub                     # they send YOU this one line

# YOU (or they, once they have SSH access as omniworker) run this,
# on each spare Mac — scripts/workqueue/grant-access.sh wraps it:
scripts/workqueue/grant-access.sh --add-key '<their public key line>' --user omniworker
```

`grant-access.sh --add-key` refuses anything that does not look like an
OpenSSH public key line (`ssh-6dbe26e AAAA... comment`) — it will not accept
a private key by mistake.

## The `omniworker` account

**Every worker Mac/Linux box gets a dedicated, non-admin `omniworker`
account.** The queue worker runs as that user; developers connect as that
user, never as themselves-with-sudo and never as an admin/root account on
the machine.

```bash
scripts/workqueue/grant-access.sh --create-worker-account --user omniworker
scripts/workqueue/grant-access.sh --add-key '<pubkey line>' --user omniworker
```

Idempotent — safe to re-run; it no-ops if the account or key already exists.

## Hardening — opt-in, never automatic

```bash
scripts/workqueue/grant-access.sh --harden --yes
```

Sets `PasswordAuthentication no` and `PermitRootLogin no`, then reloads
`sshd`. **`--harden` is never run by any other script in this lane, is never
a default, and refuses outright if the invoking session is root** — both
Vultr Linux boxes (`initech-roi-calculator`, `acmeuni`) are root-access today,
and hardening from a root session risks locking out the very session doing
it. The sequence that is safe:

1. `--create-worker-account` and `--add-key` for every person who needs in.
2. From a **separate terminal**, confirm `ssh omniworker@<host>` works with a
   key, for at least one real person.
3. Only then, from a **non-root** session, run `--harden --yes`.

Without `--yes`, `--harden` prints what it would change and exits — a dry run.

## What NEVER goes on a worker box

- **`~/.config/omni/connections.env` stays off workers.** That file holds
  Slack bot tokens, Stripe/Vandelay credentials, Cloudflare, Freshdesk,
  Meta — a queue worker needs *repo access and AI provider keys*, not the
  payment processor. A worker box gets a **minimal env file**: `WQ_TOKEN` and
  the AI provider key(s) its declared `agent_profile`s need. Nothing else.
- No admin account password, ever, for anyone but the machine's owner.
- No port 22 opened on a router. Reach workers through Tailscale.
- No `0.0.0.0` bind for `wq-server` — loopback only, tunneled or
  `tailscale serve`d out (RUNBOOK.md §3).

## Revocation

Ten minutes, and clean, which is the whole reason to set it up this way:

1. Remove the person's device from the Tailscale admin console.
2. Delete their line from `omniworker`'s `~/.ssh/authorized_keys` on every
   box they had access to.
3. Rotate `WQ_TOKEN` if they had it (`scripts/workqueue/mint-token.sh
   --force`, then re-copy the new value onto every remaining worker and
   restart its worker service — see RUNBOOK.md §9).
4. Rotate any AI provider credential that lived in that box's minimal env
   file, if the departure was not amicable.

## Do they even need machine access this week?

| Task | Needs machine access? |
|---|---|
| Bring their **Mac Studio** up as a queue worker | **Yes** — enroll.sh runs on that box. |
| Configure the reverse tunnel on the primary | Yes, for whoever owns the primary side. |
| Submitting work from their **MacBook** (`wq enqueue --by <name>`) | **No** — the laptops are submit + observe only and are never enrolled. `WQ_TOKEN` + tailnet reach is the whole requirement. |
| Everything else — proposal generation, repo work, reading `wq status` remotely | **No.** Repo work happens on their own Mac 1; `wq status`/`wq machines` work over the tailnet against the primary's `:8487` without needing a shell on any worker box. |

## What a worker box needs, per role

| box | tailnet tag | `omniworker` account | `WQ_TOKEN` | AI provider keys |
|---|---|---|---|---|
| mac-studio (primary) | untagged | n/a (the operator's own machine) | yes — it serves the queue | n/a |
| worker Macs / Linux servers | `tag:wqworker` | yes | yes | only the ones its declared `agent-*` labels need |
| Bob's / Alice's MacBook | untagged | n/a | yes | no — they submit, they do not run agents |
