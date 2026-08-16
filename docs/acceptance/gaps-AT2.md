# Gaps — AT2 (areas 4, 5, 7, 9)

Findings from writing `tests/acceptance/test_04_tools.py`, `test_05_limits.py`,
`test_07_execution.py`, `test_09_failure_recovery.py` and
`tests/acceptance/failure_injection/`.

Everything below is something the suite **could not prove**, not something it
chose to skip. Where behaviour is absent the test is a `strict=True` xfail or a
precise skip, never a silent pass.

---

## Missing tests

### AT2-05-A — An omitted budget cap defaults to UNBOUNDED
*Status: `@pytest.mark.xfail(strict=True)` in
`test_05_limits.py::TestBudgetKernelEnforcesEveryDimension::test_missing_cap_is_a_hard_failure_not_unbounded`.*

`BudgetSpec`'s four limit fields (`wall_ms_max`, `tokens_max`, `cost_usd_max`,
`max_turns`) all default to `None`, and `omniagentos/budget/__init__.py::check`
skips any dimension whose cap is `None`. A spec that simply *forgot* a token
limit therefore permits unlimited usage, and nothing distinguishes "deliberately
uncapped" from "misconfigured". The acceptance requirement is that a missing
limit is a hard failure; production defaults to unbounded.

**Fix shape:** a `BudgetSpec.require_complete()` (or a strict constructor used on
the agent-spawn path) that refuses a spec with any `None` dimension, so omission
fails loudly at configuration time rather than silently at runtime.

### AT2-05-B — TaskContract token / tool-call / wall budgets are prose, not limits
`omniagentos/taskcontract/models.py::Budgets` carries `max_tokens`,
`max_cost_usd`, `max_wall_seconds`, `max_tool_calls`. A repo-wide grep for those
names finds exactly **one** consumer: `omniagentos/swarm/spawn.py:243-250`, which
renders them into the TASK.md markdown handed to the agent. There is no
enforcement site. A per-task token limit is currently a *request to the model*,
not a bound on it.

Untestable as an enforced limit, so no test was written for it beyond the kernel
tests on `budget.check` (which the swarm path does not call — see AT2-05-C).

### AT2-05-C — The swarm scheduler enforces cost only, and never via the kernel
`omniagentos/swarm/scheduler.py:2496` compares `run.cost_usd >= budget_usd_max`
inline. It does not call `omniagentos.budget.check`, so the tokens / wall / turns
dimensions have **no swarm-side enforcement at all**, and the two paths disagree
on the boundary: `budget.check` breaches on `used > cap`, the scheduler on
`used >= cap`. Both behaviours are pinned by tests; the divergence itself is the
gap.

### AT2-05-D — Cost enforcement is opt-in; the default posture stops nothing
`omniagentos/budget/policy.py::blocks()` returns `block` only when
`OMNIAGENTOS_BUDGET_ENFORCEMENT` is exactly `block`; otherwise a breach is
recorded and work continues. This is a deliberate, documented owner decision
(2026-07-24), and both postures are pinned in
`test_05_limits.py::TestCostLimit`. It is listed here because the AT-05
requirement ("every agent must have a cost limit") is satisfied only in the
opt-in mode — in the shipped default nothing mechanical stops a runaway spend.

### AT2-05-E — The idle-timeout reaper is not exercised
`test_05_limits.py::TestIdleTimeout` proves every `SpawnRequest` carries a
positive `idle_minutes` derived from its tier. It cannot prove that an idle
session is actually *killed*, because the enforcement lives in the sessions A2
reaper, outside the swarm scheduler and outside the injected fakes. The
propagation is tested; the kill is not.

### AT2-04-A — CapabilityManifest resource limits also default to unbounded
`max_read_bytes` and `max_subprocess_seconds` default to `None`. `_limit_read`
returns early on `None`, and `_run_test` passes `timeout=None` straight to
`subprocess.run` — i.e. a manifest that omits the subprocess timeout grants an
*unbounded* subprocess. Same shape as AT2-05-A, on the tool plane.

### AT2-04-B — "Never serialized into a prompt" is proven one layer early
`test_04_tools.py` proves `compute_exposure` places ungranted and over-risk
capabilities in `hidden`, and that `enforce_argv_patch` only ever narrows. It
does **not** prove the end of that chain — that no hidden tool name or schema
reaches the model — because prompt assembly is a separate surface. A test that
renders a real prompt for an identity and asserts the hidden names are absent
from the bytes would close this.

### AT2-09-A — `FakeWorktrees` does not model `git branch -d`
`tests/swarm/scheduler_fakes.py::FakeWorktrees.delete_run_branches` deletes every
matching branch unconditionally. Production uses `git branch -d` (merged-only)
precisely so a branch still carrying salvaged or conflicted work survives
terminal cleanup. An assertion written against the fake therefore *fails* while
production is correct — which is how this was found.

Worked around by `inject_real_merge_conflict()` in
`tests/acceptance/failure_injection/`, which drives real git; the survival claim
is asserted there and in `test_07_execution.py`. **The fake should be fixed** to
refuse unmerged branches, or a future test will "fix" the wrong side.

### AT2-09-B — Provider rate-limit classification is not exercised end to end
The injected `rate_limit` fault sets `swarm_outcome = "rate_limited"` directly on
the session row. The real path classifies free-text CLI output, and there are
**two classifiers that disagree**:
`omniagentos/routing/account_pool.py::classify_outcome` matches the bare
substring `"429"` (so `geminiChat.js:429:12` in a stack trace classifies as rate
limited), while `omniagentos/longhaul/limits.py::classify_limit_text` guards it
with `_BARE_NUMERIC_RE`. Neither is covered here; a corpus test over real CLI
transcripts belongs in the routing suite.

### AT2-09-C — The routing cascade ladder is untested by this suite
`configs/cascade.yaml` defines the escalation ladder (gemini x2 -> grok-4.5 ->
sol -> fable) consumed by `omniagentos/routing/cascade.py::run_cascade`. That is
a *model* escalation ladder, distinct from the swarm *tier* ladder
(`TIER_LADDER`) these areas own, so it is deliberately out of scope — flagged so
it is not assumed covered. Note for whoever picks it up: `run_cascade` never
raises, and its default `start_tier` is **learned from the trace file**, so a
deterministic test must pass `start_tier=0` and a `tmp_path` trace.

### AT2-07-A — Sandbox confinement skips where Seatbelt is unavailable
`test_07_execution.py::TestSandboxConfinement` asserts `wrap_command` returns
argv unchanged when it cannot confine, and asserts the real profile only when
`wrap_available()` is true. On a host without Seatbelt the second test skips
rather than faking a pass. CI on Linux will therefore not cover the profile.

---

## Missing telemetry

### T1 — Limit counters live in a JSON blob, not columns
`retries`, `timeout_count`, `rate_limit_requeues` and `mechanical_retry_used` are
keys inside `board_tasks.swarm_json`. Every test here reads them through
`SwarmDal.get_swarm_json`. As columns on `swarm_attempts` (or a
`swarm_task_limits` view) they would be queryable across runs — "how often does
the retry cap fire, and on which task shapes" is currently unanswerable without
parsing every blob.

### T2 — No signal when a limit is APPROACHED
Only the terminal event is observable: `task_blocked` with reason `retry_cap`,
`rate_limit_flapping`, `split_failed`. Nothing is emitted at
`retries == cap - 1`. An operator cannot see a run degrading, only one that has
already stopped. A `limit_pressure` event carrying `{dimension, used, cap}` would
make the tests in AT-05 assertable *before* the stop, and make dashboards useful.

### T3 — A budget breach does not record WHICH dimension broke
`budget.check` produces a precise reason string (`"tokens 200001 > cap 200000"`),
but the swarm path never calls it; its `task_blocked` reason is the fixed literal
`"budget cap reached"`, and `run_completed` carries only the booleans
`budget_overshot` / `budget_exhausted`. The dimension, the used value and the cap
are all absent from swarm telemetry.

### T4 — Session reaps are not attributed to a cause
`idle_minutes` and `budget_usd_max` both ride on the spawn request, and the
reaper acts on them, but nothing records *why* a session died. `world.kills` in
the fakes has the same shape as production: a list of session ids. Without a
`killed_reason` (idle / budget / timeout / operator) on the sessions row, AT2-05-E
cannot be closed even after the reaper is reachable from a test.

### T5 — No counter for "a cap fired"
There is no metric distinguishing a healthy run from one that completed only
because a bound stopped a runaway. `run_completed` reports `partial`, which
conflates every blocking reason. A per-reason tally on the run summary would let
the acceptance suite assert *system-level* safety, not just per-task safety.

### T6 — A routed merge conflict has no durable link to its resolver
`swarm_json.merge_conflict`, `conflict_files` and the `merge_conflict` event
record that a conflict happened, and the branch survives (proven with real git).
But the association "branch X must be resolved by integration task Y" exists only
as free text inside the integration task's `feedback` list. A durable
`conflicted_branch -> resolver_task_id` row would make "no conflict was silently
dropped" assertable across a run.

### T7 — Exposure decisions record counts, not membership
`omniagentos/toolplane/exposure.py::log_exposure_decision` deliberately writes
`n_hidden` / `n_deferred` rather than the lists, to avoid putting an identity's
catalog shape in the ledger. That is a defensible trade, but it means **an
after-the-fact audit of "was tool T ever exposed to identity I" is impossible**.
A separate access-controlled audit sink, or a salted digest of the hidden set,
would make it answerable without publishing the membership.

### T8 — Two of the three dispatch denials are unrecorded
`omniagentos/toolplane/tools.py::dispatch` denies in three ways. Only the
`not_allowed` path records a decision (via `GateService().g3_tool` inside
`_allow`). `unknown_capability` and `missing_holder_generation` both raise
*before* that call, so a probe for a non-existent tool, or a call with no
identity binding, leaves **no trace anywhere** — precisely the two shapes an
audit would most want to see.
