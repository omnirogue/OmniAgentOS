# AT3 gap report — areas 6, 8, 10, 12

Written alongside `tests/acceptance/test_06_decomposition.py`,
`test_08_gates.py`, `test_10_integration.py`, `test_12_fairness.py`.

Everything listed here is behaviour the acceptance criteria imply but the code
does **not** provide. Nothing below is faked into a pass; where a claim could
not be asserted, no test asserts it.

---

## Missing tests

Gaps that could not be closed because the **production behaviour does not
exist**. Writing a green test for any of these would be a false pass.

### Area 6 — decomposition

1. **No plan-level rejection of overlapping owned paths.** Overlap is
   *repaired* (`add_ownership_overlap_edges` serializes the pair with a new
   dependency edge, `planner.py:408`), never refused. `_check_disjoint_owned_paths`
   (`planner.py:648`) returns a bool that is written to a JSONL evidence row and
   read by nothing. A plan whose owner declared `["."]` for two workers is
   accepted. *Test that cannot exist yet: "build_plan raises on a genuinely
   unsafe decomposition."*
2. **`_check_disjoint_owned_paths` uses set equality, not `paths_overlap`.**
   `{"src"}` vs `{"src/a.py"}` is reported **disjoint** even though they nest.
   The acceptance test therefore checks disjointness with `paths_overlap`
   directly rather than trusting the helper. The helper is weaker than the
   property it is named for.
3. **`decide_fanout` / `decide_route` are not wired to the disjointness check.**
   `disjoint_dag_width` is passed in, but no caller refuses to widen fan-out when
   `_check_disjoint_owned_paths` is `False`. Maximal parallelism and safe
   parallelism are computed independently.
4. **No test can assert "team leads decompose correctly" as a distinct tier.**
   There is one planner. Sub-decomposition exists only as
   `request_subtasks` / `default_task_splitter`, which is a runtime split of a
   too-large task, not a second planning tier. The area-6 file therefore covers
   the single planner twice (project shape and dependency shape) and does not
   pretend a second tier exists.

### Area 8 — gates

5. **`blocking_failures()` and `evaluate_evidence()` have zero production
   callers.** `omniagentos/gates/engine.py:293` and `:239` are exercised only by
   tests. `default_verifier` constructs `GateSpec(command=..., timeout_s=600)`
   and branches on `res.ok` / `res.infra_error`; it never consults
   `spec.blocking`. **The `blocking` flag is decorative on the swarm path** —
   a gate declared `blocking=False` still stops the attempt.
6. **`assert_touched_modules_importable` (`scheduler.py:869`) is dead code.**
   It detects exactly the "edit to a module Python never loads" failure this
   suite exists to catch, and it is not called from anywhere in `omniagentos/`.
   The acceptance test exercises the function; it cannot assert the scheduler
   uses it, because it does not.
7. **`mechanical_suite_commands` has no production writer.** `default_verifier`
   reads the key (`scheduler.py:927`) but nothing in `omniagentos/` ever sets it,
   so multi-gate task contracts are test-only today.
8. **G5 fails OPEN when `GateService` itself raises.** `scheduler.py:5143-5161`
   keeps the mechanical verdict and emits `gate_degraded`. A gate service that
   is down therefore weakens, not blocks. Deliberate, but it means "gates
   execute" is not an invariant under gate-service failure.
9. **`scripts/benchmarks/configtest_gates.py` gates are not wired into the swarm
   or orchestrator.** `gate_build`, `gate_existing_suite_unmodified` and
   `gate_diff_scope` are importable and correct, but no runtime path calls them —
   in particular **`gate_existing_suite_unmodified` (the anti-sabotage gate that
   would have caught a worker editing the existing suite to go green) protects
   nothing today.**
10. **`gate_build` treats zero compilable candidates as a PASS** (documented at
    `configtest_gates.py:117`), in direct contrast to `omniagentos/verify`, which
    fails closed on the same input. Two gate packs, opposite vacuous-pass
    policies. No test can assert one consistent rule.
11. **The syntax gate ships off.** `OMNIAGENTOS_VERIFY_GATE` defaults to `off`
    (`omniagentos/verify/__init__.py:41`), and it is wired into
    `orchestrator/core.py` only — **never into `SwarmScheduler`**. The swarm's
    only gate is `verify_command` / detected suite.

### Area 10 — integration

12. **No end-to-end "integration task merges every worker branch" test is
    possible.** `LaneWorktrees.integrate` is gated behind
    `lanes._EXECUTOR_WIRING_COMPLETE = False`, so per-unit worktree merging is
    dark in production. The acceptance test drives `SubprocessWorktrees`
    directly (the real merge mechanism) and does not claim the lane wiring works.
13. **Conflict-preserving synthesis ships off.**
    `OMNIAGENTOS_FANIN_SYNTHESIS_MODE` defaults to `off`, so the default
    `SYNTHESIZE` path is still the legacy shallow merge that **silently drops the
    losing value on a key collision** (`adjudicate.py:123-126`). Both behaviours
    are asserted; only one is on.
14. **`MergeEscalationExhausted` has no handler.** In `enforce` mode it
    propagates out of `adjudicate` to whatever called it; there is no escalation
    queue, no human hand-off, no retry-with-larger-budget. "Escalation" is an
    unhandled exception.
15. **Artifacts are preserved but not *manifested*.** Salvage commits and
    `changed_paths_since` recover the bytes; nothing records a per-task artifact
    inventory that a final acceptance check could diff against the plan's
    `owned_paths`. "Final output matches acceptance criteria" is asserted at the
    fan-in score level (`min_score`, `require_agreement`), never against a task's
    own `acceptance` text.

### Area 12 — fairness

16. **There is no formation-vs-formation experiment harness at all.** The lab
    compares *surfaces* (prompts/genomes); `formation/etar.py` compares
    formation-vs-solo using analytic **priors**, not executions. The one real
    two-arm driver, `scripts/calibrate-l5-etar.py:130`, is not a fair comparison
    and does not run either arm. The acceptance file therefore asserts fairness
    on the three surfaces that do exist (planning contract, dispatch payload,
    blind judging) and states plainly that a formation A/B execution harness is
    absent.
17. **`scripts/calibrate-l5-etar.py:162` calls `select_formation_with_confidence(goal)`
    positionally against a keyword-only signature.** It raises `TypeError` on
    every call and is swallowed by a bare `except Exception` at line 165 — so the
    "formation" arm of the only calibration script has been running with an
    unselected formation. Not covered by a test here because the script is not
    importable as a module; flagged for repair.
18. **No assertion anywhere in `omniagentos/` that two arms share a seed, prompt,
    tool list, or budget.** Parity in `lab/campaign/__init__.py:169-189` is
    structural (the same expression is written twice) and would survive an edit
    to one arm. The acceptance file adds the missing parity assertions; the
    production code still has no runtime guard.
19. **`_snapshot_hash` (`lab/campaign/__init__.py:1114`) is asymmetric** — it
    takes `config` from the champion and `prompt` from the challenger. It is a
    provenance digest, not a parity check, and must not be read as one.
20. **`unlinkable_token()` is unseedable by design**, so a token-level
    reproducibility test is impossible (correctly). Only presentation ORDER is
    seeded. Noted so a future author does not "fix" it.

---

## Missing telemetry

Signals an operator would need to *audit* these four properties after a run, and
which are not recorded anywhere durable.

### Decomposition (area 6)

- **`disjoint_owned_paths` goes to a flag-gated JSONL file, not the DB.**
  `planner.py:1160-1196` writes `var/swarm/shadow_topology.jsonl` **relative to
  the process CWD**, and only when `OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE` or
  `OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE` is `shadow`/`enforce` (both default
  `off`). In a normal run the disjointness verdict is computed and discarded.
- **`_compute_disjoint_dag_width` result is never persisted.** Achieved
  parallelism (peak concurrent attempts) is likewise not recorded against the
  planned width, so "was work parallelized maximally?" cannot be answered from
  the ledger.
- **Ownership-overlap repairs are free-text only.** They land in
  `plan.assumptions` as prose (`"ownership overlap: task 'b' now depends on 'a'"`)
  with no structured counterpart, so the rate of overlap repair across runs is
  unqueryable.
- No record of **which planner emitted a plan** (model, effort, attempt number)
  next to the plan hash.

### Gates (area 8)

- **`default_verifier` records nothing.** It calls `run_gates` with `conn=None`
  (`scheduler.py:947-960`), so the swarm's own gate runs produce **no
  `gate_evidence` row, no output artifact, and no sha256** — the evidence store
  in `gates/engine.py:141-220` exists but the primary gate path bypasses it.
  Gate output survives only truncated to 4000 chars inside an attempt's `detail`.
- **No gate-level timing or pass/fail counters.** `GateResult.duration_ms` is
  computed and dropped. There is no per-command success rate, so a chronically
  failing gate is invisible.
- **`mechanical_retry_used` is a boolean latch with no timestamp** and is never
  reset, so the free-retry mechanism cannot be measured over time.
- **`gate_degraded` events are emitted but not counted.** A G5 service outage
  silently degrades every task in the window with no aggregate signal.
- **No telemetry distinguishes "gate failed" from "gate could not run"** at the
  run level. `infra_error` is folded into the same `review_denied` end reason as
  a genuine test failure.

### Integration (area 10)

- **Merge outcomes are not persisted.** `MergeOutcome`/`Integration` carry
  `status`, `sha`, `conflict_files`, `detail`; `lanes.integrate` logs a warning
  on conflict and returns. There is no conflict rate, no conflicted-path
  histogram, no record of which branches were left unmerged.
- **Salvage is invisible.** `salvage_commit` returning `None` after exhausting
  its retries logs at WARNING (`git.py:422`) and returns; nothing counts
  salvage attempts, successes, or `salvage_failed` worktrees left on disk.
- **Fan-in evidence is returned, not stored.** `fanin_multi_attempt_tasks`
  builds stamps and hands them to the summary writer; `needs_replan`,
  `conflicting_keys` and `escalated` are not queryable after the run.
- **No artifact manifest per task.** Nothing links a task's declared
  `owned_paths` to the paths it actually changed, so scope drift is only
  detectable live (`evaluate_post_attempt`), never retrospectively.

### Fairness (area 12)

- **The formation binding is recorded, the counterfactual is not.** A run stores
  which formation it used; nothing stores which formation it did *not* use, so
  no A/B can be reconstructed from history.
- **No per-arm input digest.** Neither the lab nor the swarm records a hash of
  (prompt, model, tools, budget, env) per arm. Confounds between two arms are
  therefore undetectable after the fact — the exact hole §2 of the acceptance
  file fills at test time only.
- **`arena_task_hash` is stored; the arms' own budgets are not.** A tournament
  can prove both configs saw the same task, but not that they were given the
  same budget.
- **The presentation-order coin flip is not recorded.** `_call_judge` appends
  `[presentation-order swapped]` to free-text notes; there is no boolean column,
  so position bias cannot be measured across matches.
- **`build_blind_pairs` returns the order seed but no caller persists it**, so a
  judging session's presentation order is not reproducible from the DB.
