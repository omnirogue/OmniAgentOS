# ADR-006: Model Intelligence — vault knowledge graph + optional Grok-routed orchestration

**Status:** accepted · 2026-07-12

## Decision
A new subsystem `omniagentos/modelintel/` maintains a **daily-refreshed model
capability registry** and exposes an **optional LLM router**:

- **Registry** (`var/modelintel/registry.json`): curated priors from
  `configs/modelintel.yaml` merged with live evidence — the Aider polyglot board
  and OpenRouter pricing/context via deterministic fetchers, plus SWE-bench
  Verified / Terminal-Bench / LMArena numbers via one **Grok 4.5 agentic
  web-search sweep** (xAI Agent Tools API, `/v1/responses` + `web_search`; the
  old Live Search `search_parameters` is deprecated). Percent benchmarks blend
  into per-domain scores; Elo boards rank but never blend; every score carries
  a `basis` provenance list. Sources degrade last-known-good, never empty.
- **Vault knowledge graph**: `vault/models/`, `vault/capabilities/`,
  `vault/benchmarks/sources/`, hub `vault/sources/model-intelligence.md` — all
  through p05's `write_note` (frozen frontmatter, human-section preservation),
  densely wikilinked so Obsidian's graph shows the
  model↔capability↔benchmark triangle and "who's best at what" is one hop from
  [[Home]]. Notes are views; the registry JSON stays the machine truth
  (vault contract forbids embedding it).
- **Router** (`python -m omniagentos.modelintel route`): Grok 4.5 at
  `reasoning_effort=low` (~3s, pennies) picks a Fusion agent from the compact
  digest `~/.claude/fusion/model-intel.json`. The LLM proposes, the rankings
  file disposes: picks are validated against `model-rankings.json`
  availability/role, effort is clamped to the agent's `maxReasoning`, and ANY
  failure falls back to a deterministic port of route-task.py's fit-score —
  the verdict then says `router: "mechanical-fallback"` with the reason.
- **Fusion integration** (strictly additive): `route-task.py --llm-task "…"`
  tries the Grok router and falls back to the unchanged mechanical path;
  without the flag, behavior is byte-identical to before. `FUSION_LLM_ROUTER=0`
  hard-disables.
- **Schedule**: launchd `com.omniagentos.modelintel` (07:15 daily,
  `scripts/scheduler/install-modelintel.sh`, same pinned-interpreter pattern as
  the morning report). `XAI_API_KEY` resolves from the environment or
  `~/.config/omni/connections.env` because launchd starts with a minimal env.

## Why
Model quality moves weekly; a hand-edited rankings file goes stale silently and
routing folklore ("sol is best") never gets re-checked. Pinning the evidence to
dated, sourced benchmark rows in a git-versioned vault makes the routing
opinion auditable and self-updating, while the two-layer design (LLM proposes /
mechanical disposes, fallback always available) means the orchestrator gains
judgment without gaining a new availability dependency: if xAI is down, routing
still works exactly as it did before this ADR.

## Consequences
- Grok's research numbers land with `source: grok-research` and
  `confidence: medium` on notes; treat surprising values as leads, not gospel —
  the deterministic fetchers and dated provenance exist precisely so a bad
  sweep is visible and re-derivable from `var/modelintel/raw/<date>/`.
- `configs/modelintel.yaml` is the single place to add models, aliases,
  domains, or blend weights; alias matching strips config suffixes
  (`-thinking`, `-high`) so leaderboard name variants converge.
- The mode weights in `router.py` FALLBACK_WEIGHTS mirror route-task.py by
  hand; if one changes, change both.

## Update 2026-07-21 (W7 — benchmark feeds)
Two more deterministic fetchers joined `sources.fetch_all()`, both optional
and both degrading per-source (never zeroing a model out):
- **Artificial Analysis** (`aa-coding-index`, `/api/v2/language/models`,
  `x-api-key: AA_API_KEY`) — an independently-measured coding-quality index,
  live tokens/sec + time-to-first-token, and $/Mtok pricing. With no
  `AA_API_KEY` set the fetch is skipped entirely (`ok=False`, no HTTP call) —
  a missing paid-API key is not a broken source.
- **SWE-bench-Live** (lite split, JSONL leaderboard) — a continuously
  refreshed, real-submission cross-check/override on the Grok-research-sourced
  `swe-bench-verified` figure; `domain_blend` weight shifted from
  swe-bench-verified toward it and `aa-coding-index` (same per-domain totals).
- **OpenRouter** gained a best-effort per-model endpoint-latency cross-check
  (`/api/v1/models/{id}/endpoints`); often null (sparse traffic data on the
  public API) but wired for whenever it populates.
- Models with no `fusion_agents` (most of the reference-only roster) now get
  a `speed` fallback from Artificial Analysis tokens/sec instead of being
  permanently pinned to their static prior.
- `registry.build()` also refreshes `~/.claude/fusion/model-rankings.json` in
  place (`codingScore`/`toolUseScore`/`costScore` only, keyed off
  `fusion_agents`) — availability/latency/host stay owned by
  `refresh-rankings.sh`, a live CLI probe this module never runs. No-op if
  that file doesn't exist yet.
