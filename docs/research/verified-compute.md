# Verified Compute — research → implementation map

One page mapping each research thread to what was built, where it lives, how to turn it
on, and what remains deferred. Thesis: spend compute only where an OBJECTIVE verifier
converts it into accuracy; localization beats agentic wandering. See
`docs/adr/ADR-009-verified-compute.md` for the decision record.

Everything below is OFF by default. With all flags unset, behavior is byte-identical to
the pre-upgrade product and the full test suite stays green.

---

## 1. Agentless localization (arXiv 2407.01489) + compute-optimal sampling (2408.03314) + repeated sampling (2407.21787)

- **Built:** `AgentlessOrCheapRunner`, a drop-in CHEAP-lane executor that runs one
  localization pass, samples N candidate patches against a single byte-stable prompt,
  applies + tests each in a throwaway scratch worktree, and selects the winner by the
  project's own test suite. The selected diff is applied to the real working tree only
  after it has passed; no verified fix returns an objective error carrying per-candidate
  test evidence.
- **Files:** `omniagentos/orchestrator/agentless_runner.py` (runner + wiring in
  `omniagentos/orchestrator/core.py::_runner_for`); pipeline in `omniagentos/agentless/`
  (P1).
- **Enable:** `OMNIAGENTOS_AGENTLESS=1` plus a verifier `OMNIAGENTOS_AGENTLESS_TEST_CMD`;
  optional `OMNIAGENTOS_AGENTLESS_N` (default 4), `OMNIAGENTOS_AGENTLESS_ADAPTER`
  (default `cli-claude`). Engages only inside a git working dir.
- **Deferred:** engaged only when a verifier is configured; non-git or verifier-less runs
  transparently delegate to the normal cheap session runner.

## 2. Verification-gated cascade (FrugalGPT 2305.05176) + learned start tier (RouteLLM 2406.18665)

- **Built:** the Orchestrator retry loop escalates one tier rung ONLY on a reviewer DENY
  or an objectively-errored attempt, and records a win/loss JSONL trace per attempt that a
  pure-arithmetic learner mines for the start tier of future same-class tasks.
- **Files:** `omniagentos/orchestrator/core.py::_execute_task`; trace/enable helpers in
  `omniagentos/routing/cascade.py` (`cascade_enabled`, `record_trace`); learner in
  `omniagentos/routing/learn.py` (P2).
- **Enable:** `OMNIAGENTOS_CASCADE=1`. Trace path defaults to
  `var/routing/cascade/traces.jsonl`, injectable via `Orchestrator(cascade_trace_path=...)`.
- **Deferred:** the learner is conservative (Wilson lower bound + min-sample floor) and
  needs real traces before it should move a production start tier; a trained RouteLLM-style
  router is future work.

## 3. Reflexion on failure (arXiv 2303.11366)

- **Built:** on a gate failure, the corrective retry's feedback is a one-paragraph
  reflection over the failure evidence (reviewer feedback/error + prior output tail),
  prepended above the raw reviewer feedback, persisted to a SEPARATE JSONL store.
- **Files:** `omniagentos/orchestrator/core.py::_corrective_feedback` /
  `_reflect` / `_persist_reflection_safe`; builder in
  `omniagentos/selfimprove/reflexion.py` (P2).
- **Enable:** `OMNIAGENTOS_REFLEXION=1`. Store defaults to `var/reflexion/`, injectable via
  `Orchestrator(reflexion_store_dir=..., reflector=...)`. Template mode (adapter=None) by
  default — no LLM call.
- **Deferred:** an LLM-authored reflection (adapter-backed) is supported by the builder but
  not wired into the orchestrator path; template mode only.

## 4. Compress-before-cap (LLMLingua-2 2403.12968)

- **Built:** context assembly compresses noisy repeated log/traceback lines BEFORE the
  per-item char cap, so a repeated block collapses to a marker and frees budget for more
  distinct items. Diffs/patches are never compressed.
- **Files:** `omniagentos/memory/assemble.py::_maybe_compress` (inside `_sanitize`);
  compressor in `omniagentos/promptshape/compress.py` (P1).
- **Enable:** `OMNIAGENTOS_COMPRESS=basic` (deterministic). `llmlingua2` mode exists but is
  optional/lazy and falls back to `basic` on any failure.
- **Deferred:** the `llmlingua2` third-party backend is unbenchmarked on our workloads.

## 5. Scored memory packing (Generative Agents 2304.03442) + ordering (Lost-in-the-Middle 2307.03172) + standing memory (MemGPT 2310.08560)

- **Built:** context items are ranked by recency × importance × relevance (Jaccard overlap
  with the task text) instead of the fixed priority order, so a task-relevant older turn
  can outrank an irrelevant recent one; same budget, same truncated flag, same return type.
- **Files:** `omniagentos/memory/assemble.py::_score_and_rank`; flag in
  `omniagentos/memory/config.py::scored_enabled`; threaded via
  `omniagentos/memory/runner_hook.py::safe_memory_block(task_text=...)` and the runner call
  site in `omniagentos/runner/core.py`.
- **Enable:** `OMNIAGENTOS_MEMORY_SCORED=1` AND a `task_text`. `task_text=None` preserves the
  fixed-priority order bit-for-bit.
- **Deferred:** recency uses a rank-position proxy (only conversation rows carry a
  timestamp); MemGPT-style virtual-context paging/eviction across a session is not built.

---

## Security notes

- `test_cmd` is operator-authored — the same trust class as a project's runner validate
  steps. Agentless verifies candidates only in throwaway scratch worktrees and applies to
  the real tree exclusively after a candidate objectively passes.
- The Reflexion store (`var/reflexion/`) is SEPARATE from `selfimprove`'s PASSED-only skills/
  constraints capture and must stay that way — a reflection exists because verification
  failed, and must never leak into the verified-only capture surfaces.
- Cascade traces and the reflexion store are gitignored runtime scratch under `var/`.
