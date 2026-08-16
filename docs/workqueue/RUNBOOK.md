# Workqueue runbook

Operational reference: enroll a real machine, drain it, unpark a stuck unit,
read `wq status`, mint a token, run the primary's tailnet/tunnel commands,
and what to do for each `terminal_reason`. Written for Bob and Alice.

Copy-paste blocks below assume you are `cd ~/OmniAgentOS` unless noted.

---

## 1. The real fleet and who does what (the operator's rulings, 2026-08-11)

Roles come from `devtasks/wq-build/ROUTING-DECISIONS.md` §1/§4 and are
binding. Three kinds of box, and the difference matters:

| machine_id | os | cores (perf) | RAM | reach | role | labels | mc | ceiling |
|---|---|---|---|---|---|---|---|---|
| mac-studio (the operator, primary) | darwin | 24 (16P) | 64G | local | **queue server + serialized merger. NEVER a worker.** | — | **0** | — |
| mw0001-owner | darwin | 24 (16P) | 128G | ssh 203.0.113.10 (tunnel) | worker (workhorse; CLIs verified live) | darwin, gate, build, pytest, agent-codex, agent-claude | 3 | 0.75 |
| mw0002 (user cloud) | darwin | 16 | 128G | ssh 203.0.113.11 (tunnel) | worker | darwin, gate, pytest, script | 3 | 0.75 |
| bob-studio | darwin | TBD at enroll | — | tailnet (invite) | worker | darwin, build, pytest, script (+`agent-*` per his seats) | 3 | 0.75 |
| alice-studio | darwin | TBD at enroll | — | tailnet (invite) | worker | darwin, build, pytest, script (+`agent-*` per his seats) | 3 | 0.75 |
| initech-roi-calculator | linux | 16 | 125G | tailnet 203.0.113.20 | worker (production host) | linux, linux-ci, script, pytest-linux | 2 | 0.6 |
| acmeuni (acmeuni.example) | linux | 16 | 31G | ssh root@203.0.113.12 (tunnel; no tailscale yet) | worker (production host; PR #240's parity box) | linux, linux-ci, script, pytest-linux | 2 | 0.6 |
| bobs-macbook-pro | darwin | — | — | tailnet | **submit + observe ONLY — never enrolled as a worker** | — | — | — |
| alices-macbook-pro | darwin | — | — | tailnet | **submit + observe ONLY — never enrolled as a worker** | — | — | — |

Three rules that are easy to get wrong:

- **`mac-studio` is not in the worker pool.** It is the one writer to `main`
  (branch protection), the one host that runs `wq-server`, and the one
  serialized merger. Do not run `enroll.sh` on it; if a row exists for
  visibility it carries `max_concurrent 0`, which the store's claim path reads
  as "never claims". "All of us should be able to offload work to the Mac
  Studios — *not mine*" (the operator).
- **The two MacBooks submit, they do not run work.** They hold `WQ_TOKEN` and
  tailnet reach and run `wq enqueue --by <name>` / `wq status`. The workers are
  Bob's and Alice's **Mac Studios** — see `ONBOARDING.md`, which separates the
  two setups explicitly.
- **Adding a machine is a config row, never a code change.** Every future
  server joins with `scripts/workqueue/enroll.sh --labels ... --max-concurrent
  ...`; nothing in `omniagentos/workqueue/` learns about hosts.

Prod-host workers (`initech-roi-calculator`, `acmeuni`) run production traffic
alongside the worker — enroll them with `--ceiling-fraction 0.6` so the load
gate backs off well before it competes with the site. The same fraction also
sets their claim CADENCE: below the ceiling a worker's idle poll stretches from
5 s (idle) to 60 s (near the ceiling), so the machine with the most free compute
statistically wins the claim without anyone placing work on it.

No class of machine is reserved for a class of work. Correctness is carried by
**labels** (a unit's labels must all be declared by the claiming machine — the
`gate` label goes on a box only after a parity check, and `linux-ci` names the
servers' CI-parity environment), efficiency by **load-gated claiming**, and
importance by **priority** (gate/merge-path P1, standard P2, bulk P3–4).

---

## 2. Start the server (primary Mac only, once)

```bash
cd ~/OmniAgentOS
scripts/workqueue/mint-token.sh                 # idempotent — no-ops if WQ_TOKEN exists
uv run python -m omniagentos.workqueue.cli init --db var/workqueue.sqlite3
scripts/workqueue/serve.sh install               # renders wq-server.plist.template,
                                                   # launchctl bootstrap gui/$(id -u), KeepAlive
launchctl print "gui/$(id -u)/com.omniagentos.wq-server" | head -20
curl -sf -H "Authorization: Bearer $WQ_TOKEN" http://127.0.0.1:8487/v1/health
```

To run it in the foreground instead (debugging): `scripts/workqueue/serve.sh run`.
To stop the launchd job: `scripts/workqueue/serve.sh uninstall`.

---

## 3. Make the server reachable off-box

**Tailnet machines** (mw* laptops once invited, Bob/Alice Mac Studios,
`initech-roi-calculator`): on the primary,

```bash
tailscale serve --tcp 8487 tcp://127.0.0.1:8487
```

**Non-tailnet machines** (mw0001-owner, mw0002, acmeuni — SSH-only today): install
a reverse tunnel **on the primary**, one per remote host:

```bash
scripts/workqueue/serve.sh tunnel --host owner@203.0.113.10     # mw0001-owner
scripts/workqueue/serve.sh tunnel --host owner@203.0.113.11     # mw0002
scripts/workqueue/serve.sh tunnel --host root@203.0.113.12 # acmeuni
```

Each command runs an SSH preflight (`ssh -o BatchMode=yes ... true`) before
installing, renders `wq-tunnel.plist.template`, and bootstraps a launchd
KeepAlive job that keeps `ssh -N -R 127.0.0.1:8487:127.0.0.1:8487 <host>` up
(`ServerAliveInterval 30`, `ExitOnForwardFailure yes` — a dead tunnel fails
fast and gets restarted, it never sits silently forwarding nowhere). The
listen-side bind is pinned to `127.0.0.1` explicitly so the remote's port
8487 stays loopback-only regardless of that host's sshd `GatewayPorts`
setting. On the remote box, the worker then points `--server` at
`http://127.0.0.1:8487` — its own loopback, forwarded back to the primary.

Remove a tunnel: `scripts/workqueue/serve.sh tunnel --host <target> --uninstall`.

---

## 4. Enroll a worker (on the joining machine)

Run this only on a machine from the **worker** rows of §1 — the two Mac
Studios that are not the operator's, `mw0001-owner`, `mw0002`, the two Vultr servers, and
any future server. Never on `mac-studio`, never on Bob's or Alice's MacBook.

A machine now enrolls as a **worker** only if its `machine_id` is on
`configs/workqueue.yaml:worker_allowlist`; anything else enrolls as a
**dispatcher**, which submits and observes but never claims (§12). Enrollment
succeeds either way and prints nothing about it, so if a box you meant to be a
worker is not claiming, check its `role` in `wq machines` first.

```bash
git clone https://github.com/Globex/OmniAgentOS.git ~/OmniAgentOS
cd ~/OmniAgentOS && uv sync

# Get WQ_TOKEN onto this machine — copy ONLY the WQ_TOKEN line, never the
# primary's full connections.env (ACCESS.md):
mkdir -p ~/.config/omni && chmod 700 ~/.config/omni
echo "WQ_TOKEN=<the value the operator gives you>" >> ~/.config/omni/connections.env
chmod 600 ~/.config/omni/connections.env

# Tailnet Mac Studio (Bob's / Alice's) — no `gate` label until a parity check:
scripts/workqueue/enroll.sh --primary mac-studio.tailnet-name:8487 \
     --labels darwin,build,pytest,script --max-concurrent 3

# Non-tailnet Mac reached via the reverse tunnel installed above (mw0001-owner):
scripts/workqueue/enroll.sh --primary 127.0.0.1:8487 \
     --labels darwin,gate,build,pytest,agent-codex,agent-claude --max-concurrent 3

# Linux server (production host — headroom guarded, CI-parity capability).
# Both Vultr boxes are root-access today: enroll.sh refuses to install the
# systemd unit as root, so create the non-root worker account FIRST, then
# pass --worker-user so the unit runs acceptance commands as that user, not
# as root (ACCESS.md):
sudo scripts/workqueue/grant-access.sh --create-worker-account --user omniworker
scripts/workqueue/enroll.sh --primary 127.0.0.1:8487 \
     --labels linux,linux-ci,script,pytest-linux \
     --max-concurrent 2 --ceiling-fraction 0.6 --worker-user omniworker
```

Declare an `agent-*` label only after that machine's CLI is verified at enroll
time (the preflight checks it): the label is a promise that a unit naming that
profile can actually run there, and `agent-*` capacity is bounded by AI **seats**
per person, not by silicon (ROUTING-DECISIONS §5).

`enroll.sh` runs the full §5.1 preflight (arch, coreutils/`gtimeout`, `uv`
Python 3.12, git ≥2.39, the repo venv, `connections.env` present at mode 600,
a live route to `:8487/v1/health`, agent CLIs for any `agent-*` label
declared, a bare git mirror at `~/wq/repos/<slug>.git`, 5G free disk),
**aborting on the first failure with a named remedy** — read the remedy line,
fix that, re-run. It does not retry loops or guess.

On success it: POSTs `/v1/machines`, installs the launchd LaunchAgent
(darwin) or systemd unit (linux, system-wide if run as root, `--user` unit
otherwise — remember `loginctl enable-linger $(whoami)` so a user unit
survives logout), and does one `sleep 10` + one `curl /v1/machines` to
confirm the machine is visible. A missing appearance is a warning, not a
hard failure — `machine_beat` can be slow on first start; verify manually if
it warns.

When `enroll.sh` is invoked as root on Linux (both Vultr boxes today), the
systemd unit runs submitter-supplied acceptance commands as an ordinary
user, never root: it takes the account from `--worker-user <name>` (the
account must already exist — `grant-access.sh --create-worker-account`), or
falls back to `logname` ONLY when that succeeds and is non-root. A headless
`ssh root@host bash enroll.sh ...` run has no controlling tty, so `logname`
fails there — pass `--worker-user omniworker` explicitly, or `enroll.sh`
aborts with the remedy rather than installing a worker that would run as
root.

**Do not run `make migrate` or `cli init` on a joining machine.** There is no
local DB there — it is stateless apart from its git mirror and logs.

---

## 5. Read `wq status`

```bash
uv run python -m omniagentos.workqueue.cli status           # one snapshot
uv run python -m omniagentos.workqueue.cli status --watch    # 5s refresh
uv run python -m omniagentos.workqueue.cli status --json     # the GET /v1/status payload
uv run python -m omniagentos.workqueue.cli machines           # per-machine capacity/last-seen
uv run python -m omniagentos.workqueue.cli alerts --json | jq 'length'
```

What to look at, in order: **oldest unclaimed age** (>15 min with idle
capacity is the alert), **attempts/completion** (>2.0 means the pool is
re-running, not producing), **instrument share of refusals** (falling toward
<20% is the point of this whole build), **double executions** (must be 0 —
if not, stop the pool and read `tests/workqueue/test_claim_exclusive.py`'s
doctrine again before touching anything), **alerts sent vs parks** (must be
1:1).

`parks` in that last ratio counts **terminal** parks only — a soft park
(`unchanged-retry`: nothing ran, nothing was spent, `terminal_reason` NULL)
alerts nobody by design, so it is counted in `depth: parked` and not in
`parks`. If the two ever disagree, an alert was owed and not sent; that is the
serious direction.

The **OFFLOADS** block answers "is one of us waiting on something": one line
per person, e.g.

```
OFFLOADS
  owner: 2 running (mw0001-owner, mw0002) · 3 queued
  alice: 1 in review
  (unattributed): 4 queued
```

Attribution comes from `wq enqueue --by <name>` (default: env `WQ_USER`).
Work with no `--by` and no `WQ_USER` is shown as `(unattributed)` rather than
dropped — a backlog nobody owns is exactly what would otherwise go unnoticed.
It is never guessed from the machine that ran it: that is a fact about WHERE
the unit ran, not about who asked for it.

---

## 6. Drain a machine for maintenance

```bash
uv run python -m omniagentos.workqueue.cli drain <machine-id>
# workers finish in-flight work, claim nothing new, stop heartbeating;
# wq status shows "draining (N in flight)" then "drained"
uv run python -m omniagentos.workqueue.cli drain <machine-id> --undo
```

No lease is broken and nothing is force-reclaimed by a drain — that would be
the exact fail-open behaviour the lease design avoids (SPEC §3.4, §5.4).

Softer version — keep existing workers running but stop spawning
replacements: set `max_concurrent` to `0` for that machine (re-run
`enroll.sh` with `--max-concurrent 0`, or edit the row directly if you have
DB access on the primary).

---

## 7. Unpark a stuck unit

```bash
uv run python -m omniagentos.workqueue.cli unpark <unit-id> --because "<what changed>"
```

`--because` is required and must be non-empty — it is the human accountability
line for "why is this safe to run again." Unparking clears the refusal row
for that unit's **last** `input_key` only; if the underlying cause repeats,
it re-parks on the next refusal, it does not loop.

There is no automatic unpark. A `storm-parked` unit with an unchanged input
key will refuse again in <0.5s even after `unpark` if the input truly has
not changed — fix the input first, or the unpark just buys one more refusal.

**`submit` is not `unpark`.** A *soft* park (`state='parked'` with
`terminal_reason` NULL — what an `unchanged-retry` leaves behind: nothing ran)
goes back with `wq submit --unit <id>`, which re-queues it and **leaves the
refusal ledger alone**. That is deliberate: the refusal count is the only
counter that can reach the storm cap of 5, so re-queueing a soft park through
`unpark` instead would delete the row, make every resubmit look like the first
one, and spend a real gate run every other time — for as long as anyone keeps
submitting, with no park and no alert. `unpark` stays what it is: the human
amnesty for a TERMINAL park, and `wq submit --because "<what changed>"` is how
you spell it on a unit that is already parked terminally.

---

## 8. What to do for each `terminal_reason`

| `terminal_reason` | What happened | What to do |
|---|---|---|
| `accepted` | Normal pass. Not a park — informational on the completed unit. | Nothing. |
| `attempts-exhausted` | `attempt >= max_attempts` (default 3) on real `candidate-defect`s. | Read `wq_attempts` for the unit, fix the actual code issue, re-enqueue as a **new** unit (a changed tree hash produces a new `input_key` automatically) or `unpark` if the fix genuinely doesn't change the tree (rare). |
| `terminal-instrument` | 3 `instrument_retries` exhausted (env/instrument/contention, with 60/300/900s backoff), OR an auth/suspend signature hit terminal-at-1. | This is a box/credential problem, not a code problem. Check the named remedy in the alert (`wq_attempts.remedy`). Common causes: quota/rate-limit, `401`/`403`/suspended account, `database is locked`/disk I/O, SSH/connect failures, a moved merge base, a dirty workspace. Fix the instrument, then `unpark --because "<fix>"`. |
| `storm-parked` | `wq_refusals.count >= 5` on one `(input_key, gate)` pair — the gate refused this exact input five times. | **Do not just unpark and retry.** The input must change (code fix, or the gate script itself upgrades — the `self:` fingerprint component changes on any gate edit) or the same refusal fires again in <0.5s. Read the `remedy` column; the canonical trap is `devtasks/REACHABILITY-EXEMPT.txt` landing on the wrong checkout — land the fix on `main` first, then re-gate. |
| `cancelled` | `wq cancel <unit-id>` was called. | Nothing automatic. Re-enqueue if the work is still needed. |
| `superseded` | Reserved for a future re-enqueue-replaces-old-unit flow. Not produced by Phase 1–3. | N/A this week. |
| `unclaimable-no-capable-machine` | The unit's `labels` are not declared by any enrolled machine. | Enroll a machine with that label, or `enqueue` with different labels. Never left silently `queued` — it always surfaces here instead. |

---

## 9. Minting and rotating the token

```bash
scripts/workqueue/mint-token.sh              # mints WQ_TOKEN if absent, never prints it
scripts/workqueue/mint-token.sh --force       # rotates — invalidates every currently
                                               # enrolled worker's token until re-copied
```

Rotation requires re-copying the new `WQ_TOKEN=` line onto every enrolled
machine and restarting their worker service (`launchctl kickstart -k
gui/$(id -u)/com.omniagentos.wq-worker` or `systemctl restart wq-worker`).
Coordinate rotations — a worker holding the old token gets `401` on every
call until it is updated.

---

## 10. Kill test / partition test (from SPEC §7, §Phase 2 acceptance)

```bash
# Kill a worker mid-unit; another worker should reclaim within the 120s lease TTL:
kill -9 <worker-pid>
sqlite3 var/workqueue.sqlite3 \
  "SELECT unit_id, outcome FROM wq_attempts WHERE unit_id='<id>' ORDER BY id;"
# expect: one 'abandoned', then one 'pass'

# Partition test — SIGSTOP the server, no sudo needed:
kill -STOP <wq-server-pid>
sleep 150
kill -CONT <wq-server-pid>
```

---

## 11. Where the logs live

- Worker: `~/wq/logs/<unit>/<attempt>.log` (per-attempt agent output) and
  `~/wq/logs/wq-worker.{out,err}.log` (the supervisor itself).
- Server: `~/wq/logs/wq-server.{out,err}.log` on the primary, or wherever
  `serve.sh` was pointed via `--log-dir` if customized.
- Tunnels: `~/wq/logs/wq-tunnel.<host-slug>.{out,err}.log` on the primary.

---

## 12. Machine roles: dispatcher-only enforcement (D8a)

the operator's ruling, 2026-08-13: **personal machines are dispatchers — outgoing
only.** They enqueue and they watch. Execution happens on fleet workers.

Two columns on `wq_machines` (migration `002_machine_roles.sql`) and one config
key carry it:

| thing | where | what it decides |
|---|---|---|
| `worker_allowlist` | `configs/workqueue.yaml` | **the authority.** Which `machine_id`s may execute. |
| `wq_machines.role` | `'worker'` \| `'dispatcher'` | server-derived at enroll from the allowlist. A `role` in the enroll body is **stripped**. |
| `wq_machines.device_class` | free text | **informational/audit only.** Never a security control; nothing branches on it. |

Enrolling while not allowlisted **succeeds**, as a dispatcher — a dispatcher is
a visible member of the pool that submits and observes. Editing the allowlist
and re-running `enroll.sh` is the promotion/demotion path, because
re-enrollment re-derives the role.

A claim is refused for one of **two distinct named reasons** — they are
separate because they want opposite actions from you:

| reason | means | what you do |
|---|---|---|
| `dispatcher-role-cannot-claim` | the pool has this box enrolled as a dispatcher | nothing. This is the design working. |
| `machine-not-allowlisted` | its `machine_id` is absent from `worker_allowlist` | **go look.** Either it is a row from before 002 (every one of those carries the column DEFAULT `'worker'`), or someone is claiming that nobody put on the list. |

The allowlist is read at **claim time**, cached for **30 s**
(`WORKER_ALLOWLIST_TTL_S` in `store.py`). So adding or revoking a machine takes
effect within 30 seconds and needs **no server restart** — revocation
especially, which is the one you want fast.

### The flag

`WQ_ROLE_ENFORCE` on the **server** process:

- **unset, or anything other than `1` — LOG-ONLY (the default).** The claim
  proceeds exactly as it did before this existed; a would-deny is logged. Only
  the exact string `1` enforces, so a stray `true`/`0 `/typo in the plist
  cannot idle the pool by accident.
- **`1` — enforce.** A refused claim is `403` with the named reason as the
  message. No lease, no attempt row, no generation bump.

Every would-deny is one line on the server's stderr, in both modes:

```
[wq-server] wq-role-deny mode=log-only machine_id='owners-macbook-pro' worker_id='w1' reason=dispatcher-role-cannot-claim
```

A worker that gets the `403` does not crash: its claim loop treats it as a
transport failure, logs `wq-worker: claim failed: ... dispatcher-role-cannot-claim`
and backs off to the idle poll. So a refused box sits there printing the reason
rather than dying — which is what you want to find, but it is also why you
should stop that worker service rather than leave it polling forever.

### Honest scope

The gate is on **`POST /v1/claim`**, which is the only way a remote machine ever
reaches the queue — every worker off the primary talks HTTP, and the pool token
gets it no further than the role check. What it is *not* is a gate on the SQLite
file: `wq worker --db var/workqueue.sqlite3` opens the store directly and never
passes the server, so it is unaffected. That path needs local filesystem access
to the primary's DB — i.e. you are already on `mac-studio` — and a shared
network filesystem for the queue DB is forbidden outright (SPEC §7). Same shape
as `--reserved-for`: this closes the accident, not an adversary who is already
sitting on the primary.

### Flipping it on — the 72h-clean procedure

Do not skip the preflight. The failure mode of a wrong allowlist is *every
worker idling at once*, and the log-only window is the only cheap way to find
that out.

**1. Rehearse for 72 h in log-only mode** (the default — nothing to do but
leave it unset), then count:

```bash
grep -c wq-role-deny ~/wq/logs/wq-server.err.log
grep wq-role-deny ~/wq/logs/wq-server.err.log | awk '{print $4, $6}' | sort | uniq -c | sort -rn
```

**Clean = zero lines for 72 h.** Any line is a machine that would have stopped
working. Resolve every one before going further — either add it to
`worker_allowlist` (and re-run `enroll.sh` on it), or accept that it should
stop claiming and stop its worker.

**2. Mandatory preflight — the machine table must agree with the allowlist.**
Every `wq_machines` row NOT on the allowlist must be **deleted or set to
`role='dispatcher'` BEFORE the flip.** This is the step that makes the flip a
no-op rather than an event:

```bash
# On the primary. What does the pool currently think?
sqlite3 -header -column var/workqueue.sqlite3 \
  "SELECT machine_id, role, device_class, max_concurrent, drain, last_seen_at
     FROM wq_machines ORDER BY role, machine_id;"

# Cross-check against the shipped list — these must be identical sets:
python3 -c "import yaml;print('\n'.join(sorted(yaml.safe_load(open('configs/workqueue.yaml'))['worker_allowlist'])))"
sqlite3 var/workqueue.sqlite3 \
  "SELECT machine_id FROM wq_machines WHERE role='worker' ORDER BY machine_id;"
```

For each row in the second output that is not in the first, pick one:

```bash
# (a) it is a real machine that simply is not a worker — demote it:
sqlite3 var/workqueue.sqlite3 \
  "UPDATE wq_machines SET role='dispatcher' WHERE machine_id='<id>';"

# (b) it is gone / a test row / a typo — delete it:
sqlite3 var/workqueue.sqlite3 "DELETE FROM wq_machines WHERE machine_id='<id>';"

# Re-run the cross-check. Proceed only when it comes back empty.
```

**3. Flip.** Add `WQ_ROLE_ENFORCE=1` to the server's environment and restart it
(the value is read per request, but the environment itself only changes on
restart):

```bash
# launchd on the primary — add to the EnvironmentVariables dict of the plist:
#   <key>WQ_ROLE_ENFORCE</key><string>1</string>
launchctl kickstart -k gui/$(id -u)/com.omniagentos.wq-server
```

**4. Verify within 5 minutes**, before you walk away:

```bash
wq status                       # every worker still claiming; depth going down
grep wq-role-deny ~/wq/logs/wq-server.err.log | tail -20   # expect mode=enforce lines only from boxes you meant to refuse
```

**Rollback is one line:** remove `WQ_ROLE_ENFORCE` (or set it to `0`) and
restart the server. It reverts to log-only instantly; nothing in the DB needs
undoing, because the preflight only ever demoted rows that were not supposed to
claim.
