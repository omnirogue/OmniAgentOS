# Three Loops — start here

Autonomous loops that keep a codebase improving without a human coordinating them.

| Role | Question it answers | Writes | Never |
|---|---|---|---|
| **Planning** | *What should we build next?* | `proposals/` | code, merge |
| **Repair** | *What is broken right now?* | `candidates/`, `inquiries/` | merge, push `main` |
| **Executor** | *Build an approved plan* | `candidates/` | merge, push `main` |
| **Integration** | *What is safe to land?* | `receipts/`, `rejected/`, `inquiries/`, `main` | write features |

One process may play several roles. The Repair Loop commonly plays Executor too — its main job is
exact repairs, and it builds plans when the repair queue is empty.

They never talk to each other directly. **They share a folder.** Everything one needs from another
is a file on disk — so the system survives a crash, a reboot, and a provider outage, and a human
can read the entire state with `ls` and `jq`.

---

## The one picture

```
              ┌──── inquiries/ ◄──── Repair / Integration / you
              │     "needs study, no fix yet"
              ▼
          Planning ──── proposals/ ────► Executor ──┐
                                                     ├──► candidates/ ──► Integration ──► main
   findings/ ──────────► Repair ────────────────────┘                          │
   (bugs, PR comments, CI)   ▲                                                 │
                             └──────────── rejected/ ◄──────────────────────────┘
                                     so nobody re-proposes a dead idea

                       everything appends to ledger.jsonl
```

Work flows forward as **plans and fixes**, and backward as **inquiries** — the only reverse edge.
Anyone who notices something they shouldn't fix can push it back to Planning without derailing
their own iteration.

---

## Where everything lives

All paths under `var/loopqueue/`. **It is git-ignored** — runtime state, not source.

| Path | What it is | Written by |
|---|---|---|
| `ledger.jsonl` | **append-only history** — every event, ever | everyone, through `bridge/ledger_write.py` |
| `state/budget.json` | spend, disk, load, WIP caps | the governor process |
| `state/queue.json` | derived snapshot (rebuildable) | Integration |
| `inquiries/` | *"this area needs study"* — a question, not a fix | Repair, Integration, humans |
| `findings/` | observed breakage: bugs, PR review comments, red CI | External (CI, telemetry, humans) + Repair's own harvest |
| `proposals/` | plans awaiting an Executor | Planning |
| `candidates/` | branches ready to land | Repair, Executor |
| `claims/` | atomic claim markers | whoever holds one |
| `rejected/` | dead ideas + reason + **TTL** | Integration (Planning, for inquiries) |
| `parked/` | awaiting a human decision | Integration + any producer (its own items) |
| `receipts/` | evidence of what actually ran | Integration |

**`rejected/` is the most important folder here.** Without it, loops rediscover the same idea
forever. One symbol on this estate drew **28 identical refusals**, ~10 minutes each — about
4.5 hours — because nothing recorded that the answer was already known.

---

## The three questions you'll actually ask

A crash can leave a torn last line in the ledger, so read it with `jq -R 'fromjson?'`, which skips
an unparseable line instead of aborting — and follow it with `select(type=="object")`, which skips a
line that parses but is not an object. `fromjson?` alone is not enough: a bare `123` or `null` is
valid JSON, survives it, and then `.event` fails with `Cannot index number with string`, exiting 5:

**"What's been done?"**
```sh
jq -R 'fromjson? | select(type=="object") | select(.event=="merged") | .ts+"  "+.id' var/loopqueue/ledger.jsonl | tail -20
```

**"What's in flight?"**
```sh
jq -r '.items[] | .id+"  "+.status' var/loopqueue/state/queue.json
```

**"Why is nothing happening?"**
```sh
jq . var/loopqueue/state/budget.json     # spend / disk / load / WIP caps
df -h . ; uptime                     # the two that trip most often
cat var/loopqueue/ALERTS.md              # anything parked for a human
```

A stalled loop is usually **correct behaviour**. The governor stops work when disk is low, load is
high, spend is capped, or Integration is backed up. A parallel run on this host once died with
`ENOSPC` at 100% disk, and the error read exactly like a code defect.

---

## Files in this folder

| File | For | Contents |
|---|---|---|
| `README.md` | humans | this |
| `MISSION.md` | **every loop** | what the system is for — the tiebreaker when two options look equal |
| `CONTRACT.md` | **anyone building a loop** | layout, schemas, claim protocol, retry semantics — no doctrine |
| `DESIGN.md` | architects | why it's shaped this way |
| `ROUTING.md` | operators | multi-provider failover so one provider's limits can't stop the loops |
| `prompts/PROMPT-planning-loop.md` | Planning | drop-in system prompt |
| `prompts/PROMPT-reviewer-loop.md` | Repair | drop-in system prompt |
| `prompts/PROMPT-implementer-loop.md` | Implementation | drop-in system prompt |
| `schema/*.json` | machines | **minimal** interop schemas |
| `profile/omniagentos.schema.json` | this estate only | optional house-doctrine overlay |
| `bootstrap.sh` | setup | creates the tree, the ignore rule, the governor file |

**Building your own loop?** Read `CONTRACT.md` and validate against `schema/`. That is the whole
requirement. `PROMPT-*`, `profile/`, and `MISSION.md` are one house's style, doctrine, and
direction — you are not required to adopt any of them, and nothing in the contract assumes you
have.

---

## Setup

```sh
./bootstrap.sh /path/to/repo
```

Then run each loop under a supervisor that restarts it (launchd, systemd, or a `while true`
wrapper). **The loops are designed to be killed at any moment** — state lives in files, never in a
model's context, so a restart costs at most the current iteration.

### This estate's converged deployment

The serving checkout is `~/OmniAgentOS` on `main`; the queue is its ignored
`var/loopqueue/`, and all daemon/prompt code is under `pipeline/`. After a fully gated pipeline
change has fast-forwarded remote `main` and the serving checkout has advanced to that exact SHA:

```sh
pipeline/launchd/install.sh --check
pipeline/launchd/install.sh --apply --restart-loops
```

The installer retains the existing `com.threeloops.*` launchd labels so monitoring history stays
continuous, but replaces every executable and working-directory path with
`~/OmniAgentOS/pipeline/**`. It refuses a non-main or stale serving checkout and verifies the
installed launchd programs before declaring success. The loop restart is explicit because it
terminates the three tmux sessions; new iterations then read `pipeline/prompts/`, never the
retired `~/.omniagentos/ops/ThreeLoops` checkout.

Two hard requirements:

- **Local filesystem, single host.** Claiming and the ledger rely on POSIX `O_EXCL` and `O_APPEND`.
  NFS, SMB, Dropbox, iCloud and Google Drive break them *silently*.
- **One ledger transport.** Never append `ledger.jsonl` directly. Pass one JSON object on stdin to
  `python3 pipeline/bridge/ledger_write.py append --queue "$PWD/var/loopqueue"`; exit 0 is the only
  durable-success result, explicit exit 2 means this invocation wrote no bytes, and every other
  outcome (including a signal, timeout, or missing result) is indeterminate and must be reconciled
  before retry.
- **Exactly one deterministic gate-daemon instance per landing machine.** It holds the local
  lander lock; the two Macs run distinct gate slots and only the landing machine may fast-forward
  `main` after re-checking the train base.

---

## The five rules that matter most

1. **`exit 2` means do not retry this input.** Not "retry later" — the *input* is wrong. Change the
   input or the action; never repeat the same `(id, base_sha)`.
2. **A tool failing is not the code failing.** Classify every failure as `candidate-defect`,
   `instrument-error`, or `blocked-on-human` before reacting. Measured here: **64 of 90 gate
   refusals were instrument errors**, not code.
3. **Artifacts are immutable.** Claim with an atomic `O_EXCL` marker, never by editing the file —
   editing is a read-modify-write race, and both racers win it.
4. **Every claim is `verified_by: execution` or `reading`.** A `reading` may never carry a blocker.
5. **Terminal errors are terminal.** Quota, auth, suspension, billing: max 5 attempts, park, alert
   **once**. A sibling system fired 3,951 launches at a terminal error, cost $600, and completed
   zero work.

---

## Reading your own track record

Every loop can see what happened to its work. This is what makes "self-learning" a mechanism
rather than a claim — and it answers two different questions.

**What happened to a specific idea** (stops repeating an *idea*):
```sh
jq -R 'fromjson? | select(type=="object") | select(.id=="sha256:…")' var/loopqueue/ledger.jsonl
```

**Why my proposals keep getting refused** (stops repeating a *mistake*):
```sh
jq -R 'fromjson? | select(type=="object") | select(.event=="rejected") | .detail.reason' var/loopqueue/ledger.jsonl \
  | sort | uniq -c | sort -rn | head
```

That second one is the higher-value query and the one nobody runs. Four rejections for the same
reason is **one systematic defect**, not four unlucky items — and fixing it improves every future
proposal, where fixing the items improves four.

**Accepted vs refused, at a glance:**
```sh
jq -R 'fromjson? | select(type=="object") | select(.event | IN("merged","rejected","parked")) | .event' \
  var/loopqueue/ledger.jsonl | sort | uniq -c
```
