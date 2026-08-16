# Acceptance Suites — how to run them

Two named suites live in `tests/acceptance/`. Membership is defined by pytest
markers registered in `pyproject.toml`, and the runnable definitions live in
`tests/acceptance/suites/smoke.py` so the commands below cannot drift from what
actually executes.

Everything in both suites is **token-free and offline**: no LLM call, no
network, no provider CLI. `git` runs as a local subprocess inside `tmp_path`.

---

## Smoke Test Suite — run after EVERY commit

Budget: **under 60 seconds.** Fails the commit if red.

```bash
uv run pytest -q tests/acceptance -m "acceptance_smoke" --timeout=60
```

or, equivalently:

```bash
uv run python -m tests.acceptance.suites.smoke
```

Current membership (11 tests, ~1.2s):

| Area | Test | What breaking it means |
| --- | --- | --- |
| 11 | `test_every_event_reaches_a_live_subscriber_in_order` | events are being dropped |
| 11 | `test_artifacts_stay_linked_to_their_run_through_the_ledger` | artifacts lost their run |
| 11 | `test_decisions_and_outcomes_form_an_append_only_linked_chain` | decisions are untraceable |
| 11 | `test_the_blind_presentation_seed_reproduces_the_exact_judging_order` | judging is not replayable |
| 13 | `test_promotion_is_blocked_without_repeated_evidence` | **a single lucky run can promote** |
| 13 | `test_promotion_succeeds_once_the_evidence_repeats` | the gate rejects everything |
| 13 | `test_only_verified_runs_are_captured_as_learned_skills` | failures are being learned from |
| 14 | `test_a_benchmark_run_row_records_time_cost_tokens_and_outcome` | benchmark telemetry is missing |
| 14 | `test_a_single_replicate_yields_no_interval_rather_than_a_zero_width_one` | fake confidence intervals |
| 14 | `test_effort_summary_reports_quality_time_cost_and_confidence` | the cost/quality rollup is wrong |
| 15 | `test_one_complete_run_from_planning_through_promotion` | the orchestration spine is broken |
| 17 | `test_the_blocked_exemption_is_scoped_to_the_integration_task_only` (+ AT-17 smoke pins) | control-plane barriers/invariants silently regress; see `gaps-AT17.md` |

---

## Daily Regression Suite — run nightly

One representative test per task category, plus the whole smoke suite.

```bash
uv run pytest -q tests/acceptance -m "acceptance_daily or acceptance_smoke" --timeout=300
```

or:

```bash
uv run python -m tests.acceptance.suites.smoke --daily
```

Print either suite without running it:

```bash
uv run python -m tests.acceptance.suites.smoke --list
uv run python -m tests.acceptance.suites.smoke --daily --list
```

Current membership: 49 passed + 1 xfail, ~3.4s.

### Category coverage

| Task category | Representative tests |
| --- | --- |
| event delivery / backpressure | `test_the_hub_reports_lag_instead_of_silently_dropping_events` |
| ledger durability | `test_a_corrupt_ledger_line_never_hides_the_surrounding_trace` |
| verdict provenance | `test_a_judged_verdict_records_the_exact_code_state_it_judged` |
| merge-saga reconciliation | `test_the_merge_saga_state_is_reconcilable_and_idempotent` |
| append-only enforcement | `test_healer_decisions_and_outcomes_are_immutable_once_written` |
| reproducibility payload | `test_recorded_provenance_carries_everything_needed_to_re_run` |
| experiment snapshotting | `test_an_experiment_records_the_snapshot_it_ran_against` |
| promotion gate (unit) | `test_reproducibility_term_is_load_bearing_in_the_disposition_gate` |
| reward-hack guard | `test_audit_flags_can_never_reach_promote` |
| judge-evidence floor | `test_evidence_floor_is_fail_closed` |
| failure ledger | `test_successful_runs_and_failures_both_reach_the_append_only_ledger` |
| hypothesis creation | `test_experiment_proposals_carry_a_hypothesis_and_a_policy_mix` |
| failure → hypothesis | `test_a_rejected_experiment_is_visible_to_the_proposal_loop` |
| learning stall detection | `test_stall_check_reports_a_campaign_with_no_promotions` |
| **new-test suggestion** | `test_failures_suggest_new_eval_cases` — **xfail(strict)**, see `gaps-AT4.md` |
| pass/fail closed set | `test_an_unrecognised_outcome_is_rejected_rather_than_stored` |
| unmeasured ≠ free | `test_unmeasured_cost_is_null_not_zero` |
| cost attribution | `test_cost_to_green_totals_the_whole_retry_chain_not_one_attempt` |
| confidence banding | `test_confidence_label_tracks_how_much_data_was_actually_measured` |
| interval math | `test_wilson_interval_matches_a_hand_checked_reference` |
| power / MDE | `test_minimum_detectable_effect_is_the_inverse_of_power` |
| safety guardrails | `test_safety_regression_is_detected_by_threshold_and_by_baseline` |
| utility regularizer | `test_efficiency_penalty_never_lets_a_win_offset_a_loss` |
| verification can fail | `test_a_persistently_denied_task_fails_the_run_instead_of_passing_it` |
| merge conflict handling | `test_a_conflicting_merge_is_refused_and_leaves_main_pristine` |
| learning gate | `test_an_unverified_run_produces_no_learning` |
| trace serialisation | `test_the_orchestration_result_is_serialisable_for_the_trace` |

---

## Everything (all AT4 areas)

```bash
uv run pytest -q tests/acceptance/
```

70 tests, ~4s.

## Adding a test to a suite

1. Write it in the area file (`tests/acceptance/test_1X_*.py`).
2. Decorate it with `@pytest.mark.acceptance_smoke` (fast + high-signal) or
   `@pytest.mark.acceptance_daily` (a new task category).
3. If it introduces a new category, add a row to `DAILY_CATEGORIES` in
   `tests/acceptance/suites/smoke.py` and to the table above.

## Rule for every test in here

**A test that still passes when the behaviour it claims to cover is deleted is
worse than no test.** Before adding one, delete the guard it targets and
confirm it goes red. `docs/acceptance/gaps-AT4.md` records the revert-tests
already performed.
