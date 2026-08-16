# ADR-004: Subscription CLIs as first provider adapters

**Status:** accepted · 2026-07-11 · Blueprint §5 (agent runtimes), §9

## Decision
The first three AgentAdapter implementations wrap the CLIs already authenticated on
this machine, via subprocess (empirical surfaces: docs/research/cli-adapters.md):

| Adapter | Command shape | Usage reporting → AgentUsage flags |
|---|---|---|
| cli-claude | `claude -p <prompt> --output-format json --model <m>` | cost + tokens + turns EXACT (`estimated=false, source=cli-report`) from `total_cost_usd`, `usage.*`, `num_turns`; `session_id` → session_ref |
| cli-codex | `codex exec --json -m <m> -c model_reasoning_effort='"<e>"' --sandbox <s> --skip-git-repo-check -` (stdin) | tokens EXACT from `turn.completed.usage`; cost ESTIMATED (`source=mixed`); `thread_id` → session_ref; resume: `codex exec resume <id>` |
| cli-grok | `grok -p <prompt> --output-format json --sandbox <s> --cwd <dir>` | NO usage reported → tokens/cost ESTIMATED (`estimate_tokens`, `source=estimator`); `sessionId` → session_ref |

Common adapter behavior: explicit timeout (budget.wall_ms_max, default 300s) with
process kill + `status=timeout`; exit code ≠ 0 → `status=error` with stderr tail in
`error`; structured output = schema embedded in prompt + strict JSON parse + ONE
repair reprompt (then `status=error`); working_dir honored via subprocess cwd
(claude) / `-C` (codex) / `--cwd` (grok); logs (raw stdout/stderr) to
`var/logs/<run_id>/<adapter>.log` (log_path in result).

## Why CLIs, not APIs
Zero marginal cost on existing subscriptions; already authenticated; the
AgentAdapter boundary keeps everything above provider-neutral. Direct API adapters
are Horizon 5 (ADR to follow); nothing above the adapter layer may depend on CLI
quirks.

## Known costs
Process-spawn latency (~2.5s/probe measured), inexact usage on codex/grok
(estimates flagged — G1 criterion B9), CLI version drift (adapter `version` pins
the probed CLI version; health() re-probes `--version`).
