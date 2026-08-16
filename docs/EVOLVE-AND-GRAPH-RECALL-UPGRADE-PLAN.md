# OmniAgentOS — Evaluation-Driven Optimization (Evolve) + Graph-Recall Upgrade Plan

**Date:** 2026-07-27
**Sources:** [WecoAI/weco-cli](https://github.com/WecoAI/weco-cli) (AIDE algorithm); "Graph Engineering" X article ([thread](https://x.com/sprytixl/status/2078778799064584535)) — see Source credibility below; `~/Desktop/Self_Optimizing_Research_and_Orchestration_Loop.docx` (Gap 3).
**Product:** OmniAgentOS only (never merge into legacy OmniAgentOS)
**Mode default:** `off` → `shadow` → `enforce` per the standard three-rung rollout.

## Source credibility (read first)

The X article's headline is **fabricated**: no "$3.1M Stanford and Anthropic study" exists, Microsoft positions GraphRAG as a *type* of RAG (not a replacement), and Anthropic never announced "graph engineering" (verified via public AI-research newsletters and blogs). The article is AI-generated engagement bait — **but it rips off four real systems**, which is what this plan compares against:

| Real system | The actual technique |
|---|---|
| Microsoft **GraphRAG** | LLM entity/relationship/claim extraction → Leiden community detection → bottom-up community summaries → map-reduce global search. Powerful but index cost is extreme (reported $33k / 331k tok per query at corpus scale). |
| Microsoft **LazyGraphRAG** | Cheap noun-phrase co-occurrence graph at index time (≈ vector-RAG cost); all LLM spend deferred to *query time*: best-first iterative deepening with an LLM relevance-test budget. ~700× lower query cost; matches GraphRAG global quality on their eval. |
| **HippoRAG 2** (OSU) | OpenIE S-P-O triples + passage nodes; **Personalized PageRank** seeded from query-matched nodes with node-specificity (idf-like) reset weights; recognition-memory LLM filter. ~1k tok/query; +7% associative memory over standard RAG. |
| Zep **Graphiti** | Bi-temporal facts (`valid_at`/`invalid_at`, invalidate-don't-delete), episode ingestion with provenance, contradiction handling at write time. |

Weco-cli is the real thing: an MIT-licensed CLI wrapping the **AIDE** algorithm (LLM-guided tree search over code solutions, driven by a user-supplied eval script and a numeric metric). RE-Bench results show it surpassing lower human-expert percentiles within hours and still improving at tens of hours. No hype-claim problem.

## What we already have (verified in-tree, 2026-07-27)

- **Graph memory is mostly done.** `omniagentos/knowledge/` (Synapse): typed edges, bi-temporal `invalid_at` + supersede-never-delete (Graphiti ✅), vector+FTS+2-hop spread fused by RRF with recency/trust/usefulness modulation, Hebbian `co_occurs` strengthening, contradiction edges blocking promotion, DB-role-separated quarantine. `omniagentos/vaultgraph/`: GraphRAG-style local N-hop BFS + global community/MOC search ✅.
- **Metric discipline is done.** `omniagentos/lab/`: `MetricSpec(direction=maximize|minimize)`, dev/held-out splits, protected expected store, blind judges, replicates, Elo tournaments, ablation mutations, champion/challenger promotion gates. Frozen benchmark corpus (21 fixtures) with oracle/noop control arms (`docs/benchmarks/PHASE2-CONTROL-BASELINE.md`).
- **Adjacent optimizers that are NOT it:** `lab/` = pairwise champion/challenger over *orchestration artifacts* (prompt/genome/model_assignment/routing_policy/review_rubric), not arbitrary code, not tree-structured. `agentless/` = best-of-N pass/fail repair, no continuous metric, no iteration across rounds. `swarm/optimize.py` = advisory only, never auto-applies. `csi/` frozen; `eval/counterfactual.py` dark; `novel/` = spec stub (deferred).
- **Isolation/execution:** `worktrees/git.py`, `gates/` fail-closed G0–G8, `llm/` + `budget/`, ledger, ARCHI.json regeneration.

## The two real gaps

### GAP 1 — No evaluation-driven tree search over code (the Weco gap) — HIGH IMPACT

Nothing in the tree does: *mutate code → run user eval command → parse numeric metric → branch/search over variants → keep iterating*. This is the single largest capability hole relative to both sources, and it is the one with direct empirical evidence of quality/performance gains (RE-Bench). It also subsumes prompt engineering (Weco's third example domain), which today goes through pairwise lab races only.

**Steal from AIDE:**
1. **Solution tree.** Node = `{code_snapshot, plan, analysis, metric_value, parent_id, status(draft|buggy|improvable|terminal)}`. Edge = mutation applied. Three operators only: **draft** (fresh solution from scratch), **debug** (repair a buggy node), **improve** (refine a working node toward the metric).
2. **Node selection.** Probabilistic: bias toward best-metric leaves (exploit), with a fixed exploration fraction for unvisited/draft capacity and a debug-probability for buggy nodes. This is what makes it a *tree* instead of hill-climbing — it recovers from local optima and preserves failed branches as negative knowledge.
3. **Eval contract.** User-supplied `--eval-command` prints `metric_name: value` to stdout/stderr; parse near end of output (tolerate truncation). `--goal max|min`, `--steps N`, `--eval-timeout`. Fail closed: no parseable metric → node marked buggy with the tail of output as the debugging signal.
4. **Long-horizon operation.** `resume <run-id>`, per-step code snapshots + exec logs (`step_N.py`, `outputs/step_N.out.txt`, `exec_output.jsonl` index), `results --top k`, `diff --step best`, `review` mode (human gate before apply), `--apply-change` only when green.
5. **Fire-and-forget tracking.** `weco observe`-style: metric logging calls that **always exit 0** so they never crash an agent loop. (We have ledger; add the exit-0 wrapper convention.)

**Build: `omniagentos/evolve/`**

| Spec need | Existing surface | Build |
|-----------|------------------|-------|
| Mutation LLM | `omniagentos/llm/` (provider adapters, budget) | `evolve/propose.py` — draft/debug/improve prompts with plan+analysis reflection per node |
| Isolated execution | `worktrees/git.py`, `gates/engine.py` | `evolve/sandbox.py` — one worktree per active branch; eval command runs under gate G3 tool allowlist; `--eval-timeout` enforced |
| Metric parsing | — | `evolve/metrics.py` — tolerant stdout/stderr parser; fail-closed on missing metric |
| Tree persistence | ledger + runs tables (SQLite) | `evolve/tree.py` + migration: `evolve_nodes`, `evolve_edges`, `evolve_runs`; snapshot files under `var/evolve/<run-id>/` |
| Selection policy | — | `evolve/select.py` — exploit/explore/debug probabilities (start 0.6/0.25/0.15, mirroring lab campaign portfolio) |
| Orchestration-artifact mode | `lab/` surfaces + `MetricSpec` + held-out store | New `SurfaceKind.CODE_EVOLVE`… or better: run evolve *against* lab surfaces where prompts/genomes are the "code" and lab eval suites are the eval command — tree search replaces pairwise races as an alternative `campaign` policy |
| Human gate | swarm review patterns, G5 | `review` mode: pending-approval nodes, revise/submit (parity with `weco run review/submit`) |
| Assistant ergonomics | `skills/` library | Vault skill pack `evolve` teaching assistants the end-to-end setup (the weco `setup claude-code` idea) |
| Observability | ARCHI.json, dashboard | Tree-viz endpoint `/api/evolve/<run>/tree`; ARCHI regen |

**Phases:**

| Phase | Deliverable | Gate (empirical) |
|-------|-------------|------------------|
| **0** | `evolve/` skeleton, migration, flag `OMNIAGENTOS_EVOLVE` (default `off`), metric parser + sandbox on one hello-world case (parity with weco `examples/hello-world`) | Parses metric; buggy node on garbage output; eval timeout kills |
| **1** | Tree search core (draft/debug/improve + selection) on **lab surfaces** (prompts, genomes) using lab eval suites as the eval command | On 3 frozen eval suites: best-node metric ≥ champion/challenger outcome at equal LLM spend; held-out check (no eval-set overfit); control arm = current pairwise campaign |
| **2** | General code mode: `--source/--sources`, worktree isolation, review/apply flow, resume, exec logs | Reproduce a weco-style speedup on a real repo hot path (target: ≥1.2× on an agreed benchmark with replicates ≥3) |
| **3** | Cross-run learning: mutation-strategy outcomes into `knowledge/` (which mutation types paid off per repo/task-shape), recalled into `propose.py` | Recall visibly changes proposal mix on repeat runs; precision@k of that recall not regressed |
| **4** | `evolve` skill pack + dashboard tree viz + `observe`-style exit-0 metric API | Skill-run completes an optimization end-to-end without human CLI flags |

**Why this order:** Phase 1 reuses lab's protected evals/judges so we get a controlled A/B against the existing pairwise policy *before* trusting it on arbitrary code. That is the empirical claim test: does tree search beat champion/challenger at equal spend on our own suites?

**Risks / rules:**
- Eval gaming: the metric *is* the objective — keep held-out evaluation and human promotion for anything safety-relevant (same rule as lab today).
- Cost: RE-Bench gains took tens of hours. `--steps`, `llm/budget.py` daily cap, and `routing/cascade.py` cheap-first apply from day one.
- Never auto-apply to `main`: apply-change only through the merge gate, same as swarm worktrees.
- Determinism note: weco/AIDE reruns vary; replicates ≥3 before any promotion (existing lab discipline).

### GAP 2 — Recall traversal is fixed 2-hop decay, not principled PPR (the HippoRAG/LazyGraphRAG gap) — MEDIUM-HIGH IMPACT

`knowledge/store.py:1034-1056` does a fixed 2-hop recursive CTE with `0.5` decay and `0.05` floor. The Horizon-3 doc explicitly deferred full PPR ("if ever needed"). HippoRAG 2's measured +7% associative-memory gain says it's needed — and LazyGraphRAG says the *spend should scale with query difficulty*, not be fixed.

**Build (small, contained):**

| # | Change | Where | Empirical check |
|---|--------|-------|-----------------|
| 2a | **Personalized PageRank leg** over the fact graph: seed = vector+FTS top-k query nodes, edge-type weights, node-specificity reset (rare entities weigh more). In-process over the small subgraph (per Horizon-3 note), output as a 4th RRF leg | `knowledge/recall.py`, `knowledge/store.py` | `knowledge/lab_eval.py` precision@k vs current 2-hop spread on a frozen recall fixture set; promote only if p@k improves at p95 latency ≤ +20% |
| 2b | **Budgeted deepening** (LazyGraphRAG lesson): easy queries keep current single-round-trip; only queries with low fused confidence escalate to a deeper/wider traversal (3-hop or PPR with larger subgraph) with a hard per-query budget | `knowledge/recall.py` | Cost per query (tokens/SQL time) flat on easy traffic; recall gain concentrated on multi-hop fixtures |
| 2c | **Synapse ↔ VaultGraph entity bridging**: alias table linking fact entities to vault note nodes so a recall can surface the related note (and vice versa) | `knowledge/`, `vaultgraph/` | Join rate on shared-entity fixtures; no p@k regression on either side |

**Explicitly not doing** (already done or bad economics): bi-temporal facts (✅ in `consolidate.py`), hybrid RRF (✅), Leiden community summaries over notes (✅ in vaultgraph), full GraphRAG-style LLM community summaries over *facts* (index cost unjustified at our scale — VaultGraph MOCs cover the note corpus), Neo4j/graph DB (Horizon-3 verdict stands: plain tables + CTEs win at 1–3 hops).

### GAP 3 — Research-cycle deltas (from `Self_Optimizing_Research_and_Orchestration_Loop.docx`) — MEDIUM IMPACT

The Desktop spec describes a DeepMind-style improvement engine: **hypothesis → one-variable controlled experiment → measure a battery of dimensions (quality, latency, cost, consensus, repair iterations, benchmarks) → 2–3 replicates → reject or promote-to-baseline → immediately generate small variations around the winner and keep optimizing until gains exhaust → repeat**, with a Learning Agent documenting every experiment and a secondary reviewer approving/rejecting proposals *with methodological feedback*.

**Code-verified 2026-07-27:** hypotheses and measurement vectors already exist — `lab/contracts.py:252` (required `hypothesis` field, auto-generated per explore policy, rendered to vault) and the scorecard vector `primary_delta / utility / cost_delta / complexity_delta / safety_regression / audit_flags` with CI-based reproducibility (`lab/campaign/__init__.py:375-495`). Two deltas genuinely remain (a third, loop closure, turned out so large it is its own top-priority item — see the Desktop gap review, G1):

| # | Delta | Existing surface to reuse | Build |
|---|-------|---------------------------|-------|
| 3a | **Falsification + verdict on hypotheses** — lab stores the hypothesis *text* but no falsification criteria or post-hoc verdict (`confirmed`/`rejected`/`inconclusive`), so rejected ideas can be re-proposed and streaks aren't auditable | `novel/mode.py` `NovelProblemSpec` (`falsifiers`/`baseline` fields, dormant); lab experiment tables | extend experiment schema with `falsifiers` + `verdict`; campaign proposer must check prior verdicts before re-proposing |
| 3b | **Guardrail thresholds + repair-iteration counter** — scorecard records cost/latency deltas but promotion has no hard guardrail block (no silent "quality up, cost doubled"); repair iterations are not a first-class scorecard field | existing scorecard fields; swarm scheduler counters | guardrail thresholds in the disposition gate; add `repair_iterations` to the scorecard |
| 3c | **Methodology reviewer agent** (the doc's "Pattern Approval Engine" reviewer) — reviews the *experiment design* before promotion: control present? eval leakage? metric noise? replicate count? Rejects with specific methodological fixes. Distinct from lab's blind judges, which score the *artifact*, not the *design* | lab promotion gate, G5 (refuses self-attested verification) | `lab/methodology_review.py` wired into the promotion path as a required check; veto = return-to-hypothesis with reasons |
| 3d | **Post-promotion variation campaign** — when a challenger is promoted, automatically queue a small-variation campaign (ablation-style single-trait mutations) around the new baseline until gains exhaust; *then* idle | `lab/tournament/ablation.py`, campaign portfolio | New campaign policy `exploit_champion` triggered on promotion events |

**Phasing:** 3a+3b land with evolve Phase 1 (cheap; make the Phase-1 A/B trustworthy); 3c lands before evolve Phase 2 (general-code mode must not promote on flawed experiments); 3d is a lab campaign-policy addition, independent of evolve.

## Not adopting from Weco (and why)

- **The hosted dashboard/credits/BYOK proxy** — we have dashboard + budget already; no external dependency.
- **`weco observe` as a separate product** — its value (exit-0 fire-and-forget logging) is one wrapper convention over our ledger, folded into Phase 4.
- **Their skill installer CLI** — concept only; we ship it as a native `skills/` pack.

## Measurement & rollout discipline (applies to every phase)

1. Flagged `off` → `shadow` (measure, no behavior change) → `enforce`, per FEATURE-FLAGS.md.
2. Every promotion claim needs: frozen fixture set, control arm, ≥3 replicates, `scripts/promotion_report.py` output attached.
3. ARCHI.json regenerated and committed per phase; STATUS.md updated from the live-tested SHA only.
4. Failure containment: evolve sandbox inherits G3 tool allowlist; eval commands are data, never shell-interpolated.

## TL;DR priority order

0. **P−1 — CLOSE THE LOOP FIRST (post-review addition, 2026-07-27):** code verification found the lab's promotion loop terminates in a store no runtime path reads — champion prompts/genomes/routing policies never reach prompt assembly or routing (zero callers of `get_champion`; prompts built inline at `swarm/spawn.py:779-793`). Wiring champion surfaces into runtime is a small diff that makes everything below real. Full evidence: `~/Desktop/OmniAgentOS-SelfOptimization-Gaps-20260727/README.md` (G1, G3, G4 also block this plan).
1. **P0 — `omniagentos/evolve/`**: metric-guided optimization loop, **greedy best-first MVP** (FML-bench: greedy ≈ tree search on most tasks; earn tree search via A/B), first against lab surfaces (controlled A/B), then general code. Biggest capability gap, strongest external evidence.
2. **P0.5 — research-cycle hardening (Gap 3)**: hypothesis falsification/verdicts + guardrail thresholds + methodology reviewer — cheap, and makes every other promotion claim trustworthy (evolve Phase 1 depends on 3a/3b).
3. **P1 — PPR recall leg + specificity weights** in Synapse (HippoRAG 2), measured by existing precision@k harness.
4. **P2 — budgeted query-time deepening** (LazyGraphRAG): spend on hard queries only; speed/cost win.
5. **P3 — Synapse↔VaultGraph entity bridge**; **P4 — evolve skill pack + tree viz**; **P5 — post-promotion variation campaign (3d)**.
