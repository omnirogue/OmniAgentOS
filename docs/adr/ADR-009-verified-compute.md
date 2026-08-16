# ADR-009: Verified compute — spend tokens only where a verifier converts them to accuracy

**Status:** accepted · 2026-07-22

## Decision

Adopt "verified compute" as the organizing principle for how OmniAgentOS spends
reasoning budget, and wire five research-backed mechanisms into the product paths,
each OFF by default behind an env flag (all flags unset ⇒ byte-identical to today):

1. **Agentless localization as the CHEAP-lane executor** (`OMNIAGENTOS_AGENTLESS=1`).
   `AgentlessOrCheapRunner` replaces turn-by-turn repo wandering with one localization
   pass + N sampled candidate patches, each applied and tested in a THROWAWAY scratch
   worktree; the project's own test suite picks the winner, which is then applied to the
   real tree. It engages only when the working dir is a git repo AND a verifier command
   (`OMNIAGENTOS_AGENTLESS_TEST_CMD`) is configured; otherwise it delegates to the normal
   cheap session runner. Agentless (2407.01489); compute-optimal sampling (2408.03314);
   repeated sampling (2407.21787).

2. **Verification-gated tier escalation** (`OMNIAGENTOS_CASCADE=1`). The Orchestrator's
   `_execute_task` runs the CHEAP tier first and escalates one rung
   (CHEAP → FUSION → FUSION_ULTRA, capped) ONLY on a reviewer DENY or an
   objectively-errored attempt — never on an LLM's unverified opinion. Even a
   priority=fast task earns one escalation. Every attempt appends a win/loss JSONL trace
   that `omniagentos.routing.learn` mines for the START tier of future tasks of the same
   class. FrugalGPT (2305.05176); RouteLLM (2406.18665).

3. **Reflexion on gate failure** (`OMNIAGENTOS_REFLEXION=1`). When a gate fails, the
   corrective retry's feedback is enriched with a one-paragraph reflection over the
   failure evidence (reviewer feedback/error text + the tail of the prior attempt's
   output), prepended above the raw reviewer feedback, so the escalated tier does not
   repeat the same mistake blind. Reflexion (2303.11366).

4. **Compress-before-cap** (`OMNIAGENTOS_COMPRESS=basic`). In context assembly, noisy
   repeated log/traceback lines are compressed BEFORE the per-item char cap, so a 50-line
   repeat collapses to a marker and frees budget for more distinct items. LLMLingua-2
   (2403.12968).

5. **Scored memory packing** (`OMNIAGENTOS_MEMORY_SCORED=1`). Context items are ranked by
   recency × importance × relevance-to-the-task instead of a fixed priority order, so a
   task-relevant older turn can outrank an irrelevant recent one. Generative Agents
   (2304.03442); ordering follows Lost-in-the-Middle (2307.03172); MemGPT (2310.08560)
   for the standing-memory framing.

## Why

Two failure modes bracket naive routing. Expensive-first burns frontier budget on tasks
a cheap tier could solve; cheap-first-with-no-verification silently ships cheap-tier
failures. The missing middle is an OBJECTIVE verifier — a test/build harness, not another
model's judgement — that decides pass/fail and gates escalation. Once a deterministic
verifier exists, three things compound: sampling N cheap candidates and testing them
(Agentless) beats one expensive agentic crawl on both cost and accuracy; escalation
becomes evidence-driven rather than guessed; and the recorded pass/fail history lets a
pure-arithmetic learner pick a smarter start tier over time. Localization beating agentic
wandering, and compute paying off only where a verifier banks it, is the through-line.

Context spend obeys the same principle. Lost-in-the-Middle shows tokens in the middle of
a long prompt are nearly wasted, so we would rather pack FEWER, more relevant items than
more noise: compress the noise first, then rank by task relevance.

## Consequences

- With every flag unset, behavior is byte-identical and the full existing test suite
  stays green; each mechanism is independently toggleable.
- `test_cmd` is operator-authored and runs with the same trust class as a project's
  runner validate steps. Agentless verifies candidates ONLY in throwaway scratch
  worktrees and applies to the real tree exclusively AFTER a candidate has objectively
  passed — an objectively-verified no-fix returns an error that (under the cascade)
  escalates to FUSION with the failure evidence attached.
- The Reflexion store is a SEPARATE JSONL root from `selfimprove`'s PASSED-only skills/
  constraints capture and MUST stay that way: a reflection exists *because* verification
  failed, and letting an unverified failure narrative into the verified-only capture
  surfaces would poison every future run that reuses them.
- Cascade traces and the reflexion store are gitignored runtime scratch under `var/`
  (`var/routing/cascade/traces.jsonl`, `var/reflexion/`), both injectable for tests.
- Routers/mode weights across Fusion and OmniAgentOS must keep tier semantics in sync
  with the cascade ladder.
- An escalated (or learner-bumped) tier drops the operator's lane/model pins so the
  escalated tier's own defaults take over. Note that `FUSION → FUSION_ULTRA` currently
  changes only the tier LABEL carried on the request and its trace rows — both tiers
  resolve to the same `FusionSessionRunner` (same `FABLE_MODEL`, same spawn), so today
  the distinction is observability/labeling, not a different runtime path. Honest gap:
  a genuinely heavier ultrabuild spawn is future work.

## Deferred (honest gaps)

- **LLMLingua-2 backend unbenchmarked.** The `llmlingua2` compress mode is present but
  optional and unmeasured on our workloads; only the deterministic `basic` mode is wired
  into memory assembly.
- **MemGPT-style paging deferred.** Scored packing ranks a single offered set; true
  virtual-context paging / eviction across a long-running session is not built.
- **Threshold learner needs real traces.** `recommend_start_tier` is live but conservative
  (Wilson lower bound, min-sample floor); it must accumulate real traces before it should
  be allowed to move a task's start tier in production.
- **RouteLLM-style learned router is future work.** The current learner is arithmetic over
  recorded outcomes, not a trained router; a learned pre-dispatch router is a later step.
