# Offload before overload

the operator's ruling (2026-08-13): **heavy execution defaults to the fleet. The main Mac keeps only the
DB-anchored daemons, the serving checkout, and interactive sessions** — and every spawner backs
off BEFORE overload, not at saturation. This page is the repo-side how-to; the estate doctrine
(placement rationale, runner-fleet traps) lives at
`~/.omniagentos/ops/Offload-Before-Overload-Doctrine-2026-08-13.md`.

Why: the main Mac overloads daily while the pool idles, and an overloaded box turns green tests
red — the estate's most-measured false-red class. Backing off is cheaper than debugging a
phantom defect.

## Placement table

| runs on the MAIN MAC (only) | runs on the FLEET (default) |
|---|---|
| DB-anchored daemons (sweep, pulse, liveness, decisions, API, wq-server) | full test suites, validation ladders, builds |
| the serving checkout + gate-loop daemon | builder/coder seats (SSH CLIs; wq agent units) |
| interactive Claude sessions | all merge-gate CI runs (4 estate runners, label `estate`) |
| small edits, recon, `git`, `gh` | long workflows' worker seats; bulk mechanical fan-outs |
| anything that must read/write local-only files (live DB, local creds) | anything self-contained given a repo URL + SHA |

**Litmus: if the job still makes sense given only a repo URL, a SHA, and a brief, it belongs on
the fleet.** Only file-anchored work earns the main box.

## The 60% back-off rule

Before any heavy spawn (a test suite, a build, >2 concurrent agents, a fan-out), check
**1-min load ÷ cores** on the box you are about to spawn on:

- **< 0.6 — green**: proceed.
- **0.6–0.8 — amber**: halve the fan-out, or route to the fleet instead.
- **> 0.8 — red**: do NOT spawn locally. Offload, queue, or wait. No "it's quick" exception.

The gate is one command; its exit code IS the verdict (0 green / 1 amber / 2 red), so it drops
straight into shell logic:

```sh
python scripts/ops/estate_load.py            # this box: "<load1> <cores> <ratio> <verdict>"
python scripts/ops/estate_load.py --fleet    # + one line per enrolled wq machine, and "best: <machine>"
python scripts/ops/estate_load.py --json     # same facts for tooling (add --fleet for machines)

python scripts/ops/estate_load.py || echo "no local spawn — offload or halve"
```

Notes that keep the numbers honest:

- The exit code is always the LOCAL verdict — "may I spawn HERE". The `--fleet` listing is the
  "then where instead" answer (`best:` = lowest ratio among fresh, non-draining machines).
- A machine whose heartbeat is older than 10 minutes reads `unknown`, never healthy, and is
  never `best`. An unmeasurable local load exits amber, never green.
- `--fleet` reads `var/workqueue.sqlite3` read-only and degrades gracefully when the DB is
  absent: the fleet section reports unavailable and the local verdict still decides.
- The worker cap stands on top of this: **max 3 concurrent heavy agent spawns per session**
  (the operator, 2026-08-11), with the load check binding first.

## Offloading a test run to the compute pool

`scripts/ops/wq_offload.py` turns "run these tests" into a fail-closed wq unit (`script`
profile: no agent turn, the pytest command IS the acceptance command) and enqueues it on the
pool server (127.0.0.1:8487, bearer `WQ_TOKEN` from `~/.config/omni/connections.env`):

```sh
python scripts/ops/wq_offload.py test --ref <sha|branch> --tests tests/taskcontract --wait
python scripts/ops/wq_offload.py test --ref main --tests "tests/scheduler -k retry" --label pytest
python scripts/ops/wq_offload.py wait --unit wq_...        # re-attach to a unit already in flight
```

What it enforces so a fleet run can be trusted:

- `--ref` must resolve on **origin** (workers clone from GitHub; an unpushed commit refuses on
  every box). A locally-resolvable-but-unpushed ref is refused with the remedy named.
- The unit passes only when the pytest process exits 0; a write outside `owned_paths` fails the
  scope check regardless of exit code. There is no wrapper that can swallow a red.
- Re-running the same (repo, sha, command, labels) dedupes to the existing unit via the
  idempotency key — the queue's own "never buy the same answer twice". `--fresh` salts the key
  when a genuinely new run is wanted after an environment repair.
- Default labels `["pytest"]` route to the darwin studios; use `--label pytest-linux --label
  linux` for the Linux CI-parity boxes. `wq machines` shows what each box declares.
- On a pass the worker pushes the (commit-free, pinned at `base_sha`) result branch
  `wq/offload-…` to origin — delete it after harvesting the verdict.

Pool visibility: `python -m omniagentos.workqueue.cli status` (or `wq status`) shows depth,
per-machine load, and everyone's in-flight offloads.

## Multi-lineage research fan-outs (external harness audits)

Auditing an external agent harness against omni is exactly the shape of heavy
fan-out this doctrine governs — the 2026-08-13 DeepSeek Harness audit ran 12
independent research seats (dual-lineage blind pairs on the four high-impact
lanes, single-lineage on the rest). Apply the same back-off rule before
spawning that many seats at once: check `estate_load.py` first, and prefer
offloading seats to the fleet over saturating the main Mac. The audit's own
reusable procedure — lane decomposition, dual-lineage blind seats, synthesis
with spot-verification, and a durable disposition record with a retest guard
so a later audit never re-spends a seat re-testing an already-rejected
mechanism — is `docs/operations/external-harness-audit.md`, with the durable
record at `devtasks/harness-audits/RECORD.md`.

## Estate CI runners

Merge-gate CI runs on the 4 self-hosted `estate` runners (`mw0001-estate` 24c mac,
`mw0002-estate` 16c mac, `roi-calc-estate` 16c linux, `acmeuni-claude-estate` 8c linux) — never on
the main Mac. Runners run with **isolated HOME dirs** (a shared HOME let a box user's
`insteadOf` gitconfig break token-authed checkout, and let CI clobber user git state); keep it
that way when enrolling new ones. Full trap list is in the Ops doctrine.
