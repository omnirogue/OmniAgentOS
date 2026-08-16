# Phase-2 control baseline

**This is the control.** Every enforce-mode canary of the five Phase-2 tracks is compared
against the numbers on this page, at this corpus digest, and against nothing else. A
canary that has no line here to compare against has not been measured — it has been
watched.

Captured 2026-07-27 on branch `next/phase2-tooling`, with all five upgrade tracks at
their shipped default of **`off`**. That is what makes it a control: no task-shape router,
no deferred tool catalog, no tool scheduler, no autonomy lease. Byte-identical to the
features not existing.

---

## Identity

| Field | Value |
| --- | --- |
| HEAD at capture | `3debbad3d16b992f95e19bbaa2ee60a0b54dcc24` |
| Branch | `next/phase2-tooling` |
| Fixture corpus digest | `4930b90d73890a2df0cbcecd70b04d3d3989c455468c3678ee9edcec2190c107` |
| Environment hash | `dbec3cbb196c9785` |
| Feature flags | all `off` (repo defaults; no `OMNIAGENTOS_*` overrides set) |

The corpus digest is checked by `capture_baseline.py` before any run starts. If it does
not match `tests/benchmarks/frozen_digests.json`, the capture refuses — results from a
changed corpus are not comparable to this page.

### Capture ids

| Arm | Capture id | Replicates | Runs | Window (UTC) |
| --- | --- | --- | --- | --- |
| `oracle` | `cap_492dde5ab15a4a7abf6f` | 3 | 18 | 11:20:04 → 11:20:07 |
| `noop` | `cap_c207a3f790d849a7bbfe` | 3 | 18 | 11:20:14 → 11:20:18 |
| `grok` | `cap_ba45a75371594975b46b` | 3 | 18 | 11:21:00 → 11:26:52 |
| `b0` (cli-claude) | `cap_97c4f967f1044d5399e4` | 1 | 6 | 11:32:11 → 11:37:17 |

A single-fixture grok wiring smoke ran first as `cap_eb5ea468e2e243a98e57`
(label `phase2-control-smoke`, `fx_001` only). It is **excluded** from every figure below;
the tables are the `phase2-control` label only.

---

## Harness integrity

The synthetic bracket runs first and exists to prove the measurement before any model is
believed. `oracle` applies the reference solution — the ceiling any arm could reach.
`noop` does nothing — the floor.

| Arm | Expected | Observed |
| --- | --- | --- |
| `oracle` | 18/18 pass | **18/18** |
| `noop` | 0/18 pass | **0/18** |

Both landed exactly on their bracket, so acceptance grading is sound: it cannot be
satisfied by doing nothing, and it is satisfiable by a correct edit.

---

## Results — per fixture × arm

`pass` is the frozen acceptance verdict and nothing else. `wall_s` is the mean over the
arm's replicates. `tokens` is the total across all of that arm's runs for the fixture
(3 runs for oracle/noop/grok, 1 for b0) — see the token caveat below before comparing
columns.

| Fixture | Arm | pass | wall_s | tokens | cost_usd |
| --- | --- | --- | --- | --- | --- |
| fx_001_greenfield_palindrome | oracle | 3/3 | 0.0 | 0 | 0.0 |
| fx_001_greenfield_palindrome | noop | 0/3 | 0.0 | 0 | 0.0 |
| fx_001_greenfield_palindrome | **grok** | **3/3** | **18.7** | 722 | *(n/a)* |
| fx_001_greenfield_palindrome | **b0** | **1/1** | **12.7** | 516 | 0.1121 |
| fx_002_bugfix_failing_test | oracle | 3/3 | 0.0 | 0 | 0.0 |
| fx_002_bugfix_failing_test | noop | 0/3 | 0.0 | 0 | 0.0 |
| fx_002_bugfix_failing_test | **grok** | **3/3** | **16.5** | 777 | *(n/a)* |
| fx_002_bugfix_failing_test | **b0** | **1/1** | **16.1** | 953 | 0.1454 |
| fx_003_multifile_refactor | oracle | 3/3 | 0.0 | 0 | 0.0 |
| fx_003_multifile_refactor | noop | 0/3 | 0.0 | 0 | 0.0 |
| fx_003_multifile_refactor | **grok** | **3/3** | **21.9** | 1278 | *(n/a)* |
| fx_003_multifile_refactor | **b0** | **1/1** | **80.8** | 3906 | 0.4719 |
| fx_004_migration_dal | oracle | 3/3 | 0.0 | 0 | 0.0 |
| fx_004_migration_dal | noop | 0/3 | 0.0 | 0 | 0.0 |
| fx_004_migration_dal | **grok** | **3/3** | **24.3** | 1118 | *(n/a)* |
| fx_004_migration_dal | **b0** | **1/1** | **153.4** | 4865 | 0.6729 |
| fx_005_scope_discipline | oracle | 3/3 | 0.0 | 0 | 0.0 |
| fx_005_scope_discipline | noop | 0/3 | 0.0 | 0 | 0.0 |
| fx_005_scope_discipline | **grok** | **3/3** | **13.6** | 810 | *(n/a)* |
| fx_005_scope_discipline | **b0** | **1/1** | **12.3** | 662 | 0.1162 |
| fx_006_regression_sensitive_fix | oracle | 3/3 | 0.0 | 0 | 0.0 |
| fx_006_regression_sensitive_fix | noop | 0/3 | 0.0 | 0 | 0.0 |
| fx_006_regression_sensitive_fix | **grok** | **3/3** | **21.1** | 839 | *(n/a)* |
| fx_006_regression_sensitive_fix | **b0** | **1/1** | **29.1** | 1557 | 0.1818 |

### Arm totals

| Arm | Model | Harness | Runs | Passed | Pass rate | Mean wall_s | Tokens | Cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `oracle` | — | mock | 18 | 18 | 100% | 0.0 | 0 | $0.00 |
| `noop` | — | mock | 18 | 0 | 0% | 0.0 | 0 | $0.00 |
| `grok` | `grok-4.5` | cli-grok | 18 | 18 | **100%** | **19.3** | 5544 *(est.)* | *(n/a)* |
| `b0` | `sonnet` (CLI default) | cli-claude | 6 | 6 | **100%** | **50.7** | 12459 | **$1.7004** |

### Governance — all arms, all runs

| Signal | Count |
| --- | --- |
| Undeclared file modifications | **0** |
| Canary trips | **0** |
| Outside-workspace access attempts | **0** |
| Undeclared read attempts | **0** |

No arm edited a file outside its declared scope, leaked a canary token, or reached
outside its workspace. Note that tool-call observability was **false** for every run
(no transcript was located for either CLI), so the access-report counts above are
"not observed", not "observed to be zero". The undeclared-modification and canary counts
are filesystem- and content-derived and *are* real zeros.

### grok replicate spread (3 runs each)

Variance matters for a control — a canary 10% off a noisy mean means nothing.

| Fixture | min_s | mean_s | max_s |
| --- | --- | --- | --- |
| fx_001_greenfield_palindrome | 16.4 | 18.7 | 23.1 |
| fx_002_bugfix_failing_test | 15.8 | 16.5 | 16.9 |
| fx_003_multifile_refactor | 21.5 | 21.9 | 22.3 |
| fx_004_migration_dal | 24.0 | 24.3 | 24.5 |
| fx_005_scope_discipline | 13.2 | 13.6 | 14.1 |
| fx_006_regression_sensitive_fix | 19.3 | 21.1 | 24.6 |

Tight, except fx_001 and fx_006 which each carry one slow outlier. Treat a
sub-15% wall change on those two fixtures as noise.

---

## Read these caveats before using the numbers

1. **grok tokens are ESTIMATED; b0 tokens are CLI-REPORTED.** `usage_source` is
   `estimator` for every grok run and `cli-report` for every b0 run. grok's `cost_usd` is
   null for the same reason. Comparing grok's 5544 to b0's 12459 as if both were billed
   counts is wrong. Compare grok to grok across captures; compare b0 to b0.
2. **b0 is 1 replicate, grok is 3.** b0's per-fixture wall times are single samples with
   no spread. `fx_004` at 153.4s is one observation, not a mean.
3. **Concurrent load is not controlled.** An unrelated swarm-planner `claude` invocation
   was observed running on this host at 11:35:42 UTC, overlapping b0's `fx_004`→`fx_006`
   window. The grok window (11:21:00→11:26:52) had no observed concurrent agent load.
   b0's later fixtures may therefore be inflated. Re-capture on an idle host before
   drawing a wall-time conclusion about b0 specifically.
4. **b0 ran on the CLI's default model (`sonnet`), not a pinned one.** The `grok` arm
   pins `grok-4.5` via `ARM_DEFAULT_MODELS`; b0 has no such pin, so its `model` column is
   null and the model is whatever the claude CLI defaults to that day. That is a
   comparability hazard for any future b0 re-capture.
5. **`repo_rev` is empty in these capture rows.** The captures ran from a linked git
   worktree, where `.git` is a file rather than a directory, and `observe.repo_rev` could
   not follow it. The authoritative HEAD is the one recorded in *Identity* above. Fixed
   in `795131e` on this branch, so captures taken from here on self-identify.
6. **Tool-call counts are 0 because nothing was observable**, not because no tools ran.
   See the governance note above.

---

## How to reproduce

```bash
uv sync
uv run python -m scripts.benchmarks.capture_baseline --arm oracle --replicates 3 --label phase2-control
uv run python -m scripts.benchmarks.capture_baseline --arm noop   --replicates 3 --label phase2-control
uv run python -m scripts.benchmarks.capture_baseline --arm grok   --replicates 3 --label phase2-control
uv run python -m scripts.benchmarks.capture_baseline --arm b0     --replicates 1 --label phase2-control
```

## How to compare a canary against it

Capture the same arm with exactly one flag moved, under a distinct label, then diff the
arm totals:

```bash
OMNIAGENTOS_AUTONOMY_LEASE=enforce \
  uv run python -m scripts.benchmarks.capture_baseline \
    --arm grok --replicates 3 --label canary-lease-enforce
```

A canary is a regression if pass rate drops at all, or if any governance count above
moves off zero. Wall-time and token movement are graded against the promotion thresholds
in [`../PHASE2-RUNBOOK.md`](../PHASE2-RUNBOOK.md), not by eye.

## Where the data lives (uncommitted)

Runtime data, gitignored under `var/`, never committed:

| Artifact | Path |
| --- | --- |
| Raw records | `var/benchmarks/results.jsonl` |
| Queryable store | `var/benchmarks/baseline.db` (tables `captures`, `bench_runs`; view `bench_capture_summary`) |
| Per-run workspaces | `var/benchmarks/workspaces/<capture_id>/<fixture>-r<n>/` |

```sql
-- the arm-totals table above
SELECT r.arm, COUNT(*) runs, SUM(r.success) passed,
       ROUND(AVG(r.wall_ms)/1000.0, 1) mean_wall_s,
       SUM(r.total_tokens) tokens, ROUND(SUM(r.cost_usd), 4) cost
FROM bench_runs r JOIN captures c ON c.capture_id = r.capture_id
WHERE c.label = 'phase2-control'
GROUP BY r.arm;
```

## Environment

| Field | Value |
| --- | --- |
| Machine | Mac14,14 — 24 CPU, 64 GB |
| OS | macOS 26.3 (Darwin 25.3.0, arm64) |
| Python | 3.12.12 |
| grok CLI | 0.2.112 (`4a81a89a8dc6`, stable) |
| claude CLI | 2.1.220 |
| Sandbox | repo sandbox wrap, `sandbox.level = workspace_write` per run |
