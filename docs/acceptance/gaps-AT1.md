# AT1 gap report — areas 1, 2, 3, 16

Scope: `tests/acceptance/test_01_hierarchy.py` (agent creation & hierarchy),
`test_02_contracts.py` (work contract injection), `test_03_context.py`
(context & memory injection), `test_16_config_validation.py`
(configuration validation / preflight).

Every gap below is also encoded as a `@pytest.mark.xfail(strict=True)` in the
suite, so the day the behavior lands the test flips to XPASS and fails the run —
the gap cannot be closed silently, and it cannot be forgotten.

---

## Missing tests

Things area 1/2/3/16 should be able to assert but currently cannot, and exactly
why.

### AT-01 — hierarchy

| Gap | Why it cannot be tested today |
| --- | --- |
| **A worker's runtime role (`worker` / `integrator` / `reviewer`) is correct at spawn.** | The classification is inline in `SwarmScheduler._execute_task` (`omniagentos/swarm/scheduler.py` ~:3866), not a function. It is only observable as the `role` field of an `ACTION_WORKER_SPAWNED` event, which requires driving a full threaded scheduler run. There is no pure `worker_role_for(swarm_json)` seam to call. Covered indirectly at provision time (`formation_role` on the board card) instead. |
| **An org unit cannot become its own ancestor.** | No cycle guard exists. `org_units.parent_id` has no `CHECK`, no trigger, and `SqliteReliabilityStore` has no `update_org_unit` at all — the only way to create a cycle is raw SQL. `ProjectStore.set_parent` (`omniagentos/projects/store.py`) walks the ancestor chain and raises `ProjectError`; the org tree has no equivalent. → strict xfail `test_org_unit_cannot_become_its_own_parent`. |
| **Orphan *detection*.** | Insert-time orphaning is prevented by the FK (`PRAGMA foreign_keys=ON`, verified by revert-test). But nothing sweeps for agents whose department was later retired: `list_org_units()` defaults to `status="active"`, so a retired parent silently disappears from listings while the agent row keeps pointing at it. There is no repair pass and no API to call. |
| **An unregistered agent cannot claim work.** | `CollabStore.claim_task` (`omniagentos/collab/store.py:325`) validates only `(id, status, claim_version)`. `board_tasks.claimed_by` has no FK to `agents(id)` and no application check, so a never-spawned agent id can own a board task. → strict xfail `test_an_unregistered_agent_cannot_claim_work`. |
| **`org_units.kind='team'` layer.** | The CHECK constraint and `GET /api/org/tree` both support teams, but nothing in the repo ever creates one, so there is no team-leader hierarchy to assert. |

### AT-02 — work contracts

| Gap | Why it cannot be tested today |
| --- | --- |
| **No agent may work without a valid contract.** *(the headline requirement)* | Two separate holes. (1) `contract_bridge.build_task_contract_from_swarm` **manufactures** a contract for a task that has none: `objective` falls back to `f"swarm task {task_id}"` and a placeholder `deliver` criterion is synthesized, so validation can never fire. (2) `build_worker_brief` renders `Acceptance: (none recorded)` / `Verify: (none)` and hands the prompt to the adapter anyway. Nothing between the planner and the spawn refuses to launch. → two strict xfails. The `TaskContract` model itself *does* validate correctly (7 passing tests) — it is simply never given a chance to reject. |
| **The verify command actually runs and gates completion.** | TESTING.md's "Swarm Verification Rule" is enforced in the scheduler's mechanical gate over a live attempt/session. Not reachable without driving a full run. |
| **Budgets in the contract bound the real attempt.** | `contract_bridge` always constructs `Budgets()` — every field `None`. No swarm path ever populates `max_tokens` / `max_cost_usd` / `max_wall_seconds` / `max_tool_calls`, so there is no non-trivial value to assert. |

### AT-03 — context & memory

| Gap | Why it cannot be tested today |
| --- | --- |
| **The real retrieval result reaches the prompt.** | `worker_context_block` calls `MetacogService.compile_context` against the live DB. Populating it end-to-end (artifacts + memories + skills, with embeddings) is a metacog-suite concern. The *empty-corpus* case is tested for real (returns `""`, injects nothing); the *populated* case injects through a `monkeypatch` of the seam so the production fencing/truncation is what is actually asserted. |
~~| **`role_pack` context reaches a worker.** | `promptshape.rolepack.role_pack` has **zero production callers** — only the re-export in `omniagentos/promptshape/__init__.py`. `spawn.py` and `build_worker_brief` never read `vault/prompts/`. The packs are tested as artifacts (they load, compose, and refuse traversal), but not as delivered context, because they are not delivered. |~~
  **CLOSED 2026-07-27 (R2, commit 2e29b841):** `role_pack` now has production callers —
  `spawn.py::_apply_role_pack`, invoked at both `_build_prompt` returns (first-attempt and
  relay) behind `OMNIAGENTOS_ROLE_PACK_MODE` (default `off`). The import is function-local,
  so a naive `grep role_pack omniagentos/` at module scope still misses it — that is what
  made this line outlive its truth and mislead a downstream plan.
| **`vault/prompts/roles/` drift.** | `roles/executor.md` and `roles/verifier.md` exist on disk but are absent from `JOB_ROLES`; nothing reconciles the two. Currently satisfiable in one direction only. |
| **CORAL skill/playbook context.** | `swarm/worktrees.py` CORAL hub provisioning is gated off by default (`OMNIAGENTOS_CORAL_CONTEXT_MODE=off`) and needs a provisioned worktree + shared root. Belongs to a worktree-area test, not AT-03. |

### AT-16 — preflight

| Gap | Why it cannot be tested today |
| --- | --- |
| **A single preflight gate that blocks a run.** *(headline)* | There is none. `omniagentos/routing/fleet_preflight.py` is the only readiness check in the repo and its own docstring says *"Wired nowhere on purpose"*. It covers limits + file descriptors only, never raises, and no `make` target or CLI entry point invokes it. Nothing verifies models + prompts + tools + keys + worktrees + MCP + limits + benchmark inputs as one gate. → strict xfail `test_a_single_preflight_gate_blocks_a_run_with_a_missing_prerequisite`. |
| **Model availability.** | No `configs/models.yaml`, no "is this model id known" function. A typo'd model in `configs/swarm.yaml` `router.lane_floors` is absorbed at **route** time by `SwarmRouter._apply_lane_floors` with a `LOG.warning` and dropped — discovered mid-run. → strict xfail. |
| **Formation planner/reviewer resolvability.** | `configs/formations.yaml` sets `planner: sol` and `reviewer: fable`; neither resolves through `router.lineage_providers` *or* `default_model_lineage_index`. They are fusion-agent aliases with no provider binding, so a typo there is undetectable. (Implementers *are* checked and all resolve — that test passes.) → strict xfail. |
| **API key shape.** | Non-empty is the only check anywhere (`accounts.service.add_account`). A truncated or wrong-provider key is accepted and fails at the first live call. Presence *is* testable and is tested via the adapters' configuration-only `health()` probe. → strict xfail on shape. |
| **MCP server connectivity.** | No Python code checks MCP registration or reachability. The only doctor is `tools/install-tools.sh` (bash, invoked by nothing in `omniagentos/`), and `tools/README.md` points at a `tools/mcp_tool.py` that does not exist. The manifest's *existence and shape* are tested; connectivity is a strict xfail. |
| **`make bench` — live defect.** | `make bench` runs `--tasks devtasks`, and `load_tasks()` reads **every** `.json/.yaml/.yml` in the directory. `devtasks/phase1-unified-plan.json` is a plan document, not a task, so the command dies with `ValueError: task file .../phase1-unified-plan.json missing required field 'id'`. The individual `task_*.yaml` files are all well-formed (tested and passing). → strict xfail `test_the_devtasks_benchmark_corpus_loads_as_make_bench_would_load_it`. |
| **Live provider reachability.** | `ProviderSessionRunner.provider_doctor` spawns each CLI twice per account with a real prompt. Correctly out of scope for a hermetic suite; belongs behind `-m live`. |

---

## Missing telemetry

What would have to exist for the untestable things above to become testable.

### Seams (a pure function or an explicit entry point)

1. **`omniagentos/swarm/preflight.py` with `assert_ready(...) -> PreflightReport`.** One aggregate gate raising a single error that *names every* missing prerequisite (not the first). `fleet_preflight`'s `Ceiling` / `warnings` / `binding` shape is the right model to reuse — it is already well designed and well tested; it just needs the other seven checks and a caller. Invoke it from `intake.service` run admission and from a `make preflight` target.
2. **`worker_role_for(swarm_json) -> str`** extracted from `SwarmScheduler._execute_task`. Today the leader/worker/reviewer decision is only observable through a threaded run.
3. **`assert_models_available(config) -> None`** in `swarm/router.py`, resolving every model named in `lane_floors`, `category_pins`, `extra_candidates` and `configs/formations.yaml` (`implementers`, `reviewer`, `planner`) against the lineage index — at load time, not route time.
4. **`api_key_shape_ok(provider, key) -> bool`** (prefix + length + charset, value never logged) in `accounts.service`, called from `add_account`.
5. **`toolplane/mcp_preflight.check_servers() -> list[ServerStatus]`** — a Python handshake per declared server, replacing the bash-only doctor.
6. **An org-tree validator** — `validate_org_tree(store) -> list[str]` reporting dangling `org_unit_id`, retired parents with live children, and `parent_id` cycles; plus a `CHECK (parent_id IS NULL OR parent_id <> id)` on `org_units`.
7. **A contract admission guard** at spawn: refuse a `SpawnRequest` whose task has no acceptance criteria **and** no verify command, instead of `build_task_contract_from_swarm` synthesizing a placeholder.
8. **A task-file glob in `bench.runner.load_tasks`** (`task_*.y[a]ml`) or an explicit manifest, so a non-task document in `devtasks/` cannot break `make bench`.

### Fields / rows / log lines

| Signal | Where | Why |
| --- | --- | --- |
| `swarm_attempts.contract_id` + `contract_hash` at **open**, not just in `swarm_json` | swarm DAL | Lets a test assert "no attempt row exists without a contract" as a DB invariant, rather than inferring it from a prompt string. |
| `preflight_report_json` on `swarm_runs` | migration | The run records what was verified before it started; a post-hoc test can then assert the gate actually ran, and an operator can see *why* a run was admitted. |
| A `context_injected` event carrying `{memory_ids, artifact_ids, skill_slugs, bytes, truncated}` | `build_worker_brief` | Today the retrieval result is invisible: there is no way to assert that *the relevant* memory reached an agent and unrelated memory did not, only that *some* fenced block did. |
| `board_tasks.claimed_by` → `FOREIGN KEY REFERENCES agents(id)` | migration | Converts "an unregistered agent claimed work" from an untestable policy into an enforced invariant. |
| `agents.org_unit_id` index + a `retired_parent` view | migration | Makes the orphan sweep cheap enough to run as a preflight check. |
| `role` on the `ACTION_WORKER_SPAWNED` event promoted to a column on `swarm_attempts` | swarm DAL | The role is currently only in an event payload, so asserting "the integration task ran as the reviewer" needs event-log archaeology. |
