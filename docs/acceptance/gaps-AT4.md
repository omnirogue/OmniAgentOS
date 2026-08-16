# AT4 gap report — trace (11), learning (13), benchmark (14), e2e (15)

Scope: what the acceptance suite in `tests/acceptance/` **could not assert**,
because the behaviour or the telemetry does not exist. Everything listed here
is either an `xfail(strict=True)` in the suite (so it turns green the moment
someone implements it, and fails loudly if it silently half-lands) or an
explicitly untested surface recorded below.

Nothing in this report is faked green. Verified with `uv run pytest -q
tests/acceptance/` → **69 passed, 1 xfailed**.

---

## Missing tests — behaviour that does not exist yet

### G1. Nothing suggests new tests from a failure  *(area 13.4)*

**Status:** `xfail(strict=True)` —
`tests/acceptance/test_13_learning.py::test_failures_suggest_new_eval_cases`.

The acceptance criterion "new tests are suggested" has no implementation.
`campaign.propose_experiments` only re-versions *existing* surfaces against an
*existing* suite: `_proposal_eval_context` (`omniagentos/lab/campaign/__init__.py:1212`)
reuses a prior experiment's `eval_suite_id`, and no code path anywhere calls
`LabStore.add_eval_case` in response to a rejection. A campaign that keeps
rejecting therefore keeps testing the same cases; the eval suite never learns
from what it failed to catch.

Nearest neighbours that exist but do not close this: `reliability.memory.propose_confirmed_fix`
(proposes a *fix*, not a *test*), `skills.router.propose_skill_update` (a skill
diff), `steward.suggest.generate_goal_suggestions` (goals).

**To close:** a `propose_eval_cases(store, discipline, failures)` seam that turns
a rejected experiment's failing `per_case` rows into new `EvalCase`s (DEV split,
expected held by `ProtectedGrader`). Then delete the `xfail`.

### G2. The `improve_*` and `configtest_*` schema has no Python callers

**Status:** partially covered at the schema layer; **not** covered at the
behaviour layer.

Migration `083_improve.sql` creates six tables — `configtest_runs`,
`configtest_hypotheses`, `healer_decisions`, `healer_outcomes`,
`improve_verdicts`, `improve_saga`. A repo-wide grep for all six names across
`*.py` hits exactly two files, and both are tests (`tests/improve/test_migration.py`
and this suite). **There is no DAL, store, or service module for any of them.**

Consequence: `test_11_trace.py` can assert the *constraints* (NOT NULL provenance
hashes, the saga state CHECK, the idempotency-key UNIQUE, the healer immutability
triggers) by driving raw `sqlite3`, but it cannot assert that any production code
path ever *writes* a row. The tests prove the schema will refuse bad data; they
cannot prove good data arrives. Writing a Python writer inside the test would be
the test implementing the feature, so it was deliberately not done.

### G3. `improve_verdicts` and `improve_saga` are mutable

`healer_decisions` and `healer_outcomes` carry `RAISE(ABORT, ...)` triggers on
UPDATE and DELETE (asserted in
`test_healer_decisions_and_outcomes_are_immutable_once_written`).
`improve_verdicts` and `improve_saga` carry **no such triggers**, even though a
verdict is exactly the kind of record an audit trail must not be able to rewrite.
`improve_saga` legitimately needs UPDATE (it is a state machine), but
`improve_verdicts` does not.

**To close:** an append-only trigger pair on `improve_verdicts` in a new
migration, plus an assertion mirroring the healer one.

### G4. Campaign never persists the blind presentation seed

`lab/eval/blind.py:41 build_blind_pairs` returns the presentation seed, and
`lab/store.py` has a column for it (`lab_verdict_provenance.blind_presentation_seed`,
migration 082). But `campaign/__init__.py:681-694` only **logs** it
(`"blind pairs for experiment %s used presentation seed %d"`) — it never calls
`record_verdict_provenance`. Nothing in `omniagentos/` calls
`record_verdict_provenance` at all.

So the reproducibility chain is complete in *parts* and broken in the *middle*:
`test_the_blind_presentation_seed_reproduces_the_exact_judging_order` proves the
seed replays, and `test_recorded_provenance_carries_everything_needed_to_re_run`
proves the store round-trips it — but a real campaign run leaves the seed in a
log line, not in the DB. A verdict from six weeks ago is therefore not replayable.

**Downstream effect:** `campaign._provenance_evidence` finds nothing, so
`_judge_evidence` always falls back to scorecard metrics, so the
`MIN_JUDGE_AGREEMENT` / `MIN_JUDGE_VALIDITY` floors are effectively inert on the
live path unless a suite happens to emit `judge_agreement` / `judge_validity`
metrics. The floors themselves are tested directly
(`test_evidence_floor_is_fail_closed`); their *wiring* is the gap.

### G5. Reproducibility is asserted structurally, not by re-execution

`test_an_experiment_records_the_snapshot_it_ran_against` proves the snapshot
hash covers its inputs and changes when they change. It does **not** re-run the
experiment from the recorded hashes and compare outputs, because nothing in the
repo can rehydrate a run from a `snapshot_hash` — the hash is a fingerprint, not
a manifest. `RunManifest.harness.env_hash` has the same shape: it pins the
environment for comparison, but there is no `restore_environment(env_hash)`.

### G6. Cross-lineage judge panels are not enforced at write time

`lab/store.py invalidated_verdicts()` flags `SINGLE_LINEAGE_PANEL` **after the
fact** (asserted in `test_recorded_provenance_carries_everything_needed_to_re_run`),
and `campaign._cross_lineage_panel` exists — but a single-lineage verdict can
still be recorded and can still be read by `_provenance_evidence` as valid
evidence. Detection is retrospective, not preventive.

---

## Missing telemetry — things a run should record and does not

| # | What is missing | Where it should live | Impact |
| --- | --- | --- | --- |
| T1 | **Blind presentation seed on the live path** (G4) | `lab_verdict_provenance.blind_presentation_seed` | past verdicts are not replayable; the judge-evidence floors are inert |
| T2 | **Judge agreement / validity are never computed on the live path** | `lab_verdict_provenance.agreement`, `.effective_n`, `.mde`, `.observed_effect` | `MIN_JUDGE_AGREEMENT`=0.60 and `MIN_JUDGE_VALIDITY`=0.80 have nothing to gate on |
| T3 | **No benchmark row is ever written** (G2) | `configtest_runs.wall_ms / tokens_in / tokens_out / cost_usd / status` | the columns that make "is xhigh worth it" answerable are never populated |
| T4 | **No merge saga row is ever written** (G2) | `improve_saga` | a dispatcher crash between MERGE_INTENT and VERIFIED leaves nothing to reconcile from |
| T5 | **No judged verdict row is ever written** (G2) | `improve_verdicts` (base/head/tree/diff/judge_config hashes) | a review verdict cannot be attributed to the exact diff it judged |
| T6 | **`learning.api` decision log has no run linkage and no fsync** | `omniagentos/learning/api.py` | rows are `{"kind", ...}` only — no `run_id`, no `trace_id`, no schema version; `path=None` appends to a process-global list (`_MEMORY_LOG`) that nothing resets |
| T7 | **Ledger records no per-step trace** | `RunManifest` | `steps` are absent, so `_skill_metadata_from_manifest` synthesizes the placeholder "No step-level detail was recorded" — learning captures a *shape*, not a *procedure* |
| T8 | **Orchestration approvals are not persisted** | `OrchestrationResult.approvals` | escalations exist only in the in-memory result; after the process exits there is no record that a money/delete action was parked |
| T9 | **`EffortStats.confidence` is not surfaced anywhere** | `swarm/costgreen.py` | the "measured / partial / sparse" band is computed but no API route, dashboard field or report reads it, so a sparse comparison presents identically to a measured one |
| T10 | **No cross-run experiment lineage** | `lab.experiments` | `Experiment` has no parent-experiment pointer, so a replication cannot be linked to the run it is replicating |

---

## Revert-tests performed

Each area's suite was verified to FAIL when the behaviour it claims to cover is
removed. Production code was restored after every run (`git status omniagentos/`
clean, re-verified green).

### Area 13 — `omniagentos/lab/campaign/__init__.py`

Removed `reproducible` from `_finish`'s PROMOTE condition
(`elif numeric_pass and reproducible and rollback_ok:` → `elif numeric_pass and rollback_ok:`).

```
E   AssertionError: a one-replicate experiment must never promote; got <Disposition.PROMOTE: 'promote'>
E   assert <Disposition.PROMOTE: 'promote'> is <Disposition.REJECT: 'reject'>
E   AssertionError: assert <Disposition.PROMOTE: 'promote'> is <Disposition.REJECT: 'reject'>
2 failed, 18 passed, 1 xfailed
```

Failing tests: `test_promotion_is_blocked_without_repeated_evidence`,
`test_reproducibility_term_is_load_bearing_in_the_disposition_gate`.

### Area 11 — `omniagentos/api/eventbus.py`

Made `Subscription.drain` swallow overflow (`lagged = self._lagged; if lagged: ...`
→ `lagged = False`).

```
E   AssertionError: an overflowed subscriber must be flagged as lagged
E   assert False is True
1 failed, 13 passed
```

Failing test: `test_the_hub_reports_lag_instead_of_silently_dropping_events`.

*(Also verified separately: deleting the `_contains_run_id` dedup guard in
`omniagentos/ledger/__init__.py` fails
`test_ledger_append_is_idempotent_and_never_rewrites_history` with a byte-diff
on the ledger file.)*

### Area 14 — `omniagentos/swarm/costgreen.py`

Made `EffortStats.confidence` always return `"measured"`.

```
E   AssertionError: assert 'measured' == 'partial'
E   AssertionError: assert 'measured' == 'sparse'
4 failed, 26 passed
```

Failing tests: all four non-`measured` parametrisations of
`test_confidence_label_tracks_how_much_data_was_actually_measured`.

### Area 15 — `omniagentos/orchestrator/core.py`

Made the quality gate ignore the reviewer
(`if last_review.verdict == "confirm":` → `if True:`).

```
E   AssertionError: two tasks + one corrective retry
E   assert 2 == 3
E   AssertionError: assert 'done' == 'failed'
2 failed, 3 passed
```

Failing tests: `test_one_complete_run_from_planning_through_promotion`,
`test_a_persistently_denied_task_fails_the_run_instead_of_passing_it`.

---

## What area 15 actually mocks

Recorded here because "it's an E2E test" is worthless without knowing where the
seams are. Exactly three things are faked, all of them model calls:

1. `planner_llm` — the planning model.
2. `reviewer` — the verification model.
3. `executor_runner` — the implementation agent's *decisions*. Its *effects* are
   real: it writes real files into a real `git worktree`, makes real commits, and
   drives the real `ApprovalGateway` with a safe action, a payment and an `rm -rf`.

Real, unmocked, in the E2E path: `estimate_complexity`, `resolve_execution`,
`plan_goal`, `render_spec_markdown`/`write_spec_doc`, `ApprovalGateway` +
`classify_hard_stop`, the corrective-retry loop, `_aggregate_status`,
`SubprocessWorktrees.merge_branch` (a real `git merge --no-ff`, including the
conflict/abort path), `append_manifest`/`read_manifests`,
`selfimprove.curator.curate` (including its `VerificationGate` hard rule), and
`lab.campaign.run_experiment` (the full promotion gate).
