"""Model Intelligence — live model-capability registry, vault knowledge graph,
and the optional Grok-routed orchestrator brain.

Pipeline (daily, launchd `com.omniagentos.modelintel`):
  sources.fetch_all()  -> live leaderboard/pricing rows (deterministic HTTP):
                          Aider polyglot, OpenRouter pricing+latency,
                          Artificial Analysis quality/speed/cost (AA_API_KEY),
                          SWE-bench-Live lite-split leaderboard
  research.sweep()     -> Grok 4.5 agentic web-search sweep (fills the gaps)
  registry.build()     -> var/modelintel/registry.json + ~/.claude/fusion/
                          model-intel.json + ~/.claude/fusion/model-rankings.json
                          (codingScore/toolUseScore/costScore refresh only)
  vault_notes.render() -> vault/models|capabilities|benchmarks/sources knowledge graph

Routing (on demand, optional — mechanical route-task.py stays the default):
  router.route()       -> Grok 4.5 (reasoning_effort=low) picks the agent for a
                          task from the registry digest; deterministic fallback.
"""
