# AT-17 gap report — control-plane invariants and barriers

Written alongside `tests/acceptance/test_17_control_plane.py`.

Everything listed here is behaviour the §5.3 / §4.3 / §10.3 acceptance claims
imply but the code does **not** provide. Every ⛔ test is a strict xfail that
exercises real importable symbols and reaches a genuine assertion that is false
today. When the owning package lands, the xfail *fails* (strict) and the marker
is removed.

Progress metric: `uv run python -m tests.acceptance.suites.at17_progress`
(pytest collection, not `grep -c` — see that module's docstring and commit
`f33b669`).

---

## Invariants (§5.3)

### 1. `test_a_task_cannot_become_ready_without_approved_contract_and_ready_context`

* **Missing seam**: a readiness predicate that binds contract approval and
  context readiness to task eligibility.
* **Today**: `SwarmDal.eligible_tasks` (`dal.py:770`) keys only on dependency
  board status (`p.status <> 'done'`). An open task with empty acceptance /
  verify_command is still eligible.
* **Assertion style**: **behavioural** — seed an open task with empty contract
  fields; assert it is not eligible (fails today because it is).
* **Flips at**: W3.1.

### 2. `test_verified_requires_a_report_bound_to_the_exact_artifact_hash`

* **Missing seam**: a verification report bound to the exact artifact hash
  before a task may enter verified/done and release dependents.
* **Today**: marking a parent `done` with no hash-bound report is enough for
  `eligible_tasks` to release the child.
* **Assertion style**: **behavioural** — mark parent done with no report; assert
  the child is not eligible (fails today because it is).
* **Flips at**: W2.2 + W3.1.

### 3. `test_a_stale_verification_hash_blocks_integration`

* **Missing seam**: re-validation of a stored verification digest against the
  current worktree HEAD before integration.
* **Today**: `_integration_ready` ignores digests entirely.
* **Assertion style**: **symbol existence** — asserts
  `scheduler.assert_verification_hash_fresh` is not None (no such symbol today).
* **Flips at**: W2.2 + W3.1.

### 4. `test_acceptance_is_blocked_while_a_critical_or_high_finding_is_open`

* **Missing seam**: an acceptance barrier that refuses terminal acceptance while
  a critical or high finding remains open.
* **Today**: no `acceptance_blocked_by_open_findings` on dal or scheduler.
* **Assertion style**: **symbol existence** — looks up that name on
  `omniagentos.swarm.dal` then `scheduler`.
* **Flips at**: W2.5.

### 5. `test_every_state_transition_writes_a_sequenced_trace_event`

* **Missing seam**: every board/task state transition writes a sequenced trace
  event with a monotonic sequence number.
* **Today**: `CollabStore.update_board_task` is a silent `UPDATE`; no transition
  row appears in `dal.events_for_run`.
* **Assertion style**: **behavioural** — flip open→done; assert a sequenced
  transition event was appended (fails today — none are).
* **Flips at**: W2.6.

---

## Barriers (§4.3)

### 6. `test_dependency_release_requires_hash_bound_verification_not_board_status`

* **Missing seam**: identity binding — the review verdict must be bound to an
  artifact hash before dependency release.
* **Today's truth (not the gap the prose might suggest)**: release keys on
  `dal.py:770`'s strict `done`, and `done` requires a review confirm at
  `scheduler.py:5223`. **The gap is identity, not review.**
* **Assertion style**: **behavioural** — mark parent done with no
  `artifact_hash` / `verification_hash`; assert child is not eligible.
* **Flips at**: W3.1.

### 7. `test_a_dangling_dependency_edge_does_not_satisfy_the_integration_gate`

* **Missing seam**: dangling dependency edges must fail closed (never count as
  satisfied).
* **Today's defect**: `scheduler.py:3035` —
  `statuses = [status_by_id.get(dep, "done") for dep in deps]` defaults a
  missing dep id to `"done"`. The SQL eligibility JOIN also drops edges whose
  parent row is absent, so `NOT EXISTS (... p.status <> 'done')` is vacuously
  true.
* **Assertion style**: **behavioural** — inject a dep on a never-created task
  id; assert the reconstructed :3035 default is not `"done"` (fails today
  because it is).
* **Flips at**: W3.1.

### 8. `test_the_blocked_exemption_is_scoped_to_the_integration_task_only` — ✅ green

Not a gap. Pins `_integration_ready` (`scheduler.py:3012-3038`): an ordinary
task with a blocked dep is never eligible; the integration task with mixed
terminal deps is ready via the integration-only path. Guards W3.1 against
over-correction.

### 9. `test_a_workstream_is_ready_for_global_merge_only_on_integration_pass`

* **Missing seam**: `workstream_ready_for_global_merge` (or equivalent) that
  requires an integration-task pass before global merge.
* **Assertion style**: **symbol existence** on `omniagentos.swarm.scheduler`.
* **Flips at**: W3.1.

### 10. `test_acceptance_barrier_requires_post_merge_verification_and_trace_audit`

* **Missing seam**: `acceptance_barrier` requiring post-merge verification and a
  trace audit before terminal acceptance.
* **Assertion style**: **symbol existence** on `omniagentos.swarm.scheduler`.
* **Flips at**: W2.5.

### 11. `test_terminal_disposition_dispatches_the_learning_agent`

* **Missing seam**: `dispatch_learning_agent` on terminal disposition.
* **Today**: `orchestrator.learn` is fire-and-forget session curation, not a
  disposition-driven learning agent.
* **Assertion style**: **symbol existence** on scheduler then
  `orchestrator.learn`.
* **Flips at**: W5.1.

---

## Pass rules (§10.3) and posture

### 12. `test_scheduler_review_infrastructure_failure_never_confirms` — ✅ green

Not a gap. Pins `scheduler.py:5205-5221`: two reviewer infrastructure errors →
blocked, no retry consumed, never auto-confirm (via real
`CrossLineageSwarmReviewer` + failing adapter).

### 13. `test_orchestrator_review_failure_does_not_confirm` — ✅ CLOSED (H2)

* **Missing seam**: fail-closed orchestrator review (never auto-confirm on infra
  failure / unparseable / unrecognised verdict).
* **Was — three sites**, not one:
  1. adapter raises → `verdict="confirm"`
  2. unparseable output → `verdict="confirm"`
  3. unrecognised verdict string coerced to `"confirm"`
* **Now**: all three route through `review._infrastructure_error` and return
  `verdict="error"` — the same three-valued shape
  `swarm.scheduler.SwarmReviewOutcome` already used. `orchestrator/core.py`
  retries the reviewer exactly once, then returns
  `TaskOutcome(status="blocked_on_review")`; the executor is not re-spawned, so
  a reviewer outage never consumes the task's retry budget, and
  `_aggregate_status` cannot report the run as `done`.
* **Assertion style**: **behavioural** — call `CrossLineageReviewer` with a
  raising adapter and `_parse_verdict` with bad payloads; assert none confirm.
* **Flipped at**: phase-0 hardening (H2). Strict xfail marker removed; the test
  is now a live regression guard. Loop-level contract:
  `tests/orchestrator/test_review_fails_closed.py`. Counterfeit
  `cf-reviewer-fails-open` drives a revert mechanically.

### 14. `test_an_incomplete_trace_yields_inconclusive_and_fails_closed`

* **Missing seam**: an audit *runner* that loads real `events`-table rows into a
  `TraceSegment` and persists an `AuditReport`.
* **Today**: pure posture already exists in `omniagentos/audit/trace.py` (W1.4)
  and correctly yields INCONCLUSIVE / fails closed on incomplete evidence. The
  remaining gap is the runner — nothing reads DB rows into a segment and
  persists a report.
* **Assertion style**: **symbol existence** — looks for
  `run_trace_audit_over_events` on `omniagentos.audit`, `audit.trace`, or
  `audit.runner`. **Not** asserted against the pure rules (those would pass and
  hide the gap).
* **Flips at**: W2.3.

### 15. `test_verdicts_record_reviewer_and_implementer_lineage_distinct_for_high_risk`

* **Missing seam**: `reviewer_lineage` and `implementer_lineage` fields on
  verdict types so high-risk can prove cross-lineage review.
* **Today**: `ReviewVerdict` has `verdict`/`feedback`/`reviewer`;
  `SwarmReviewOutcome` has `verdict`/`feedback`/`reviewer` — no lineage pair.
* **Assertion style**: **behavioural on the type surface** — assert both fields
  exist on `ReviewVerdict` or `SwarmReviewOutcome` dataclass fields.
* **Flips at**: W2.2.

---

## Role machinery

### 16. `test_the_role_vocabulary_matches_prompts_on_disk_from_the_acceptance_view` — ✅ green

Not a gap. `JOB_ROLES` (7 entries) matches `vault/prompts/roles/*.md` (7 files).

### 17. `test_a_planner_stamped_job_role_survives_to_the_dispatched_prompt`

* **Missing seam**: planner-stamped `JobRole` values surviving all the way to the
  dispatched prompt (full 7-role vocabulary, role pack injected).
* **Today's truth** (spec prose was stale on two counts):
  1. `job_role_from_swarm_json` (`roles.py:19-36`) collapses the **7**-member
     `JobRole` enum to **3** outcomes (INTEGRATOR when `integration` truthy,
     REVIEWER when complexity in `{review, verify}`, else IMPLEMENTER). There is
     no `roles.py:44-61`.
  2. `role_pack` is **not** zero production callers — `spawn.py:1008
     _apply_role_pack` calls it at `:1032`. But it is gated by
     `OMNIAGENTOS_ROLE_PACK_MODE` (`spawn.py:110`), and `parse_role_pack_mode`
     (`spawn.py:95-107`) resolves absent/misspelled/non-string values to `off`
     — so the seam exists and is **dark by default**.
* **Assertion style**: **behavioural** — stamp complexity/role `tester`; assert
  `job_role_from_swarm_json` returns `JobRole.TESTER` (fails today —
  collapses to IMPLEMENTER); also assert role_pack mode is not `off` by default.
* **Flips at**: W3.3.
