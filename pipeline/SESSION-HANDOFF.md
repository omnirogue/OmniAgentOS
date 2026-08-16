# Session handoff — 2026-08-07

**Read this first, then `MISSION.md`, then `prompts/PROMPT-implementer-loop.md`. You are the Implementer.**

## The topology

| role | who | produces |
|---|---|---|
| **Planner** | the operator's session on `claude5` | `proposals/`, `inquiries/`, research |
| **Reviewer** | Alice's fleet, his host, his accounts | PRs and `findings/` |
| **Implementer** | you | `candidates/` → `main` — **the only writer on main** |

Everything flows to the Implementer. **Your job is orchestration, not execution:** delegating is the
default, doing work yourself needs a reason you can state. There should never be a moment where no
agent is working unless the plan and PR queues are genuinely clear. See the memory
`orchestrate-dont-execute`.

## Running unattended (launchd, survives this session)

| job | cadence |
|---|---|
| `com.threeloops.bridge` | 15m — GitHub issues/PRs → inquiries/findings |
| `com.threeloops.governor` | 5m — writes `budget.json` (disk, load, codex window, live accounts) |
| `com.threeloops.janitor` | daily — retention; parked artifacts exempt |
| `com.threeloops.integrity-{liveness,invariants,reachability,absence}` | 15m / 1h / daily / daily |

`launchctl list | grep threeloops` — column 2 is the last exit code.

## What was in flight when this session compacted

Leads and workflows do **not** survive a session boundary. If a notification never arrives, the work
was lost and must be relaunched — check for output before assuming otherwise:

- **PR lead** — DELIVERED. Batch 1 (#53, #45, #82) verified together: 757 dashboard tests, `tsc`
  clean, each fails on revert. #11 superseded → close. #72 conflicts with #89. #15 needs a ruling.
- **Plan lead** — reviewing the 5 proposals the Planner wrote *under the old prompt, with the quota
  still in it*. Expect `drop` verdicts; that is the test of whether removing the quota mattered.
- **Research grounding pass** — seeding `inquiries/` with evidence-backed questions.
- **Adapter audit / gate-ladder / landing-plan** — workflow `wa3891ecw`.

## DO NOT RUN `integration.py --apply` — it can reach the serving checkout

Found by adversarial audit, verified by execution. **Dry-run only until B1 and B2 are fixed.**

**B1 — the child environment is not scrubbed.** `integration.py:990` does `env = dict(os.environ)`.
`merge-gate.sh:101` is `REPO="${REPO:-$GATE_WS}"`, so an **inherited `REPO` beats the gate
workspace** — the gate then runs `git worktree add`, `git merge --no-ff` and `git checkout --`
**inside the serving checkout**. `merge-gate.sh:662` also runs `git update-ref` unscrubbed, so an
inherited `MERGE_GATE_TEST_RETARGET_REF=main` moves `refs/heads/main`. `REPO` is a maximally
generic name and **this package's own `bootstrap.sh:7` sets it.**

**B2** — `--gate-workspace` accepts the serving checkout; it was blocked only incidentally because
that tree is currently dirty.

**What reduces the urgency** (both verified): no launchd job invokes `integration.py`, and dry-run
is the default and wins even when `--apply` is also passed. So the exposure is **a human typing
`--apply`**, not a background process.

**Three more, correctness-fatal but not main-reaching:** a missing/zero-byte `ledger.jsonl`
republishes `queue.json` as `wip:0` silently (B3); admission-time *instrument* errors are written as
**permanent** rejections and the TTL is decorative, so a resubmission — same payload, same id — is
permanently dead (B4); "signed receipt" is `Path.exists()`, so a zero-byte file promotes an
instrument error to a candidate defect (B5).

## Land the ladder FIRST — and two branches compete

Six of seven lanes add tests into directories **no blocking gate executes**, so a green gate would
prove only that the tests were never selected. `tests/reflection`, `tests/contracts` appear **0
times** in `merge-gate.sh`; CI's `test` job is `continue-on-error: true`.

**Two ladder branches exist, 1 commit ahead each, same two files, different commits** —
`chore/ladder-reflection-selfimprove-0807` (`00000000`) and `...-v2-0807` (`00000000`). Pick one.
Neither adds `tests/contracts/` or `tests/scripts/`, so **step 0 is only half-built** — extend the
chosen branch before submitting.

**Landing order after that:** `fix/reflection-doc-target-hardstop-0807` (live privilege escalation)
→ `fix/ci-gate-identity-0807` (repairs the instrument; **then flip branch protection to require
`merge-gate`** — landing the branch alone is cosmetic) → `fix/gate-reachability-selfexplain-0807`
→ `lane/scratch-blockers-0807` → `fix/gate-receipt-golden-0807`. HOLD
`lane/sibling-enum-generalise-0807` — inert, referenced nowhere on main.

**Unverified and it matters:** the clean-merge simulation did **not** include either ladder branch,
and the ladder edit sits near the reachability branch's hunks in `merge-gate.sh`. Run the simulation
in a worktree before submitting those two back to back.

**Landing is not a manual merge** — `./scripts/merge-request.sh submit <branch> "<why>"` then
`poll`. The requester never merges.

## Account rotation does NOT cover subagents — measured 2026-08-07

The ladder in `bridge/run-loop.sh` rotates accounts on repeated failure. **It only covers processes
that runner spawns.** Subagents and Workflow agents inherit the session's account directly, so when
the weekly limit hit, five agents died mid-flight and nothing rotated:

- workflow `wa3891ecw`: 3 of 4 agents done, the synthesis agent killed
- workflow `wz60ioi4k`: 3 of 5 done, `routine-cost-accounting` and one verifier killed
- the standing plan lead and the research-grounding pass: both killed

A guard that exists and does not cover the path that failed is the estate's #1 defect class, and
this is an instance of it in the orchestration layer rather than in the product.

**What actually works today:** launch heavy fan-out on non-Claude seats — `terra-coder`,
`sol-coder`, `luna-scout`, `grok-coder` — which draw on Codex/xAI quota, not the session's. Wave 2
did this and its Codex agents completed while the Claude ones died.

**Unfixed.** The real fix is either a pre-flight quota check before fan-out, or routing agent
spawns through per-account launchers. Neither exists yet.

## Open decisions that are the operator's, not yours

1. **PR #15** (auth) — `replan`: 303 commits stale, and it 403s the live dashboard because nothing
   sends the principal header. Must land with #65 as one rebased change.
2. **The weekend scope** — Planner-only is GO. Full three-loop is NO-GO: nothing drains
   `proposals/` while the Reviewer is remote.

## Live defects, verified, unlanded

- `fix/reflection-doc-target-hardstop-0807` — privilege escalation: a proposal could append shell to
  `scripts/merge-gate.sh`. 28 pass / 23 fail on revert.
- **`tests/reflection` is ungated** — zero occurrences in `merge-gate.sh`. That is *why* the above
  survived. Land the ladder change before any reflection work.
- `fix/spend-counter-truth-0807` — correct but incomplete; landing it halts 6 budgeted routines
  because routine cost accounting does not exist.

## Where things live

- Loop package + prompts: `~/OmniAgentOS/pipeline` (part of `Globex/OmniAgentOS`)
- Work queue: `~/OmniAgentOS/var/loopqueue/` — git-ignored, host-local, ephemeral
- Research: `~/.omniagentos/ops/Research/` — durable, Drive-mirrored, estate-wide
- Accounts: `claudeN` launchers. `claude4` = the operator's interactive, `claude5` = Planner. Never symlink a
  config dir; never copy `.credentials.json`.

## The two defect classes, checked first every time

**Favourable absence** — an abnormal condition rendering as a normal value. A missing file reading
as "fine", an unknown cost as `$0.00`, a skipped test as a pass. **64 of 90 gate refusals here were
instrument errors, not code.**

**Incomplete propagation** — a correct fix reaching its target and not its sibling. 518 clone
families; a fix is structurally 2–13 fixes.
