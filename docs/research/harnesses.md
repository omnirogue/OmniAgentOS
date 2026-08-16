# Harness recon (empirical, 2026-07-11) — OpenHands SDK 1.35.0 + mini-swe-agent 2.4.5

Verified by installing both into a scratch venv on this Mac (uv, arm64). Basis for
p10-openhands and p11-bench. Pin exactly these versions (pyproject `harness` extra).

## Verdicts that shape the design

- **No Docker required locally for either.** OpenHands `LocalWorkspace` and
  mini-swe-agent `LocalEnvironment` both execute via direct `subprocess` on the host.
- **Both route models through litellm** → live LLM calls need provider API keys
  (`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` …). Subscription CLIs are NOT litellm
  backends.
- **mini-swe-agent's `Model` is a duck-typed Protocol** (`query(messages) -> dict`,
  `format_message(...)`, `format_observation_messages(...)`) → a CLI-shim model that
  shells to `claude -p --output-format json` per query satisfies B1 with NO API key.
  p11 MUST read the installed `minisweagent/models/litellm_model.py` and
  `minisweagent/agents/default.py` to conform the shim's message/return dict shape
  (2.4.5 expects OpenAI-style responses; FormatError after 3 bad formats).

## Import surfaces

| Package | Import | Key classes |
|---|---|---|
| openhands-sdk | `openhands.sdk` | `agent.Agent`, `conversation.LocalConversation`, `llm.LLM`, `llm.LLMRegistry`, `workspace.LocalWorkspace` |
| mini-swe-agent | `minisweagent` | `agents.default.DefaultAgent`, `models.litellm_model.LitellmModel`, `environments.local.LocalEnvironment`, `package_dir` |

## mini-swe-agent minimal run (from shipped `minisweagent/run/hello_world.py`)

```python
import yaml
from pathlib import Path
from minisweagent import package_dir
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment

config = yaml.safe_load((Path(package_dir) / "config" / "default.yaml").read_text())
agent = DefaultAgent(<Model instance>, LocalEnvironment(cwd=workdir), **config["agent"])
result = agent.run("<task>")   # -> {"exit_status": str, "submission": str, ...}
```

AgentConfig knobs: `step_limit`, `cost_limit` (USD), `wall_time_limit_seconds`,
`max_consecutive_format_errors`, `output_path` (trajectory file). Cost tracking can
fail for unknown models → set `MSWEA_COST_TRACKING=ignore_errors` for the CLI shim
(usage then comes from our own AgentUsage accounting, estimated=True where needed).

## OpenHands minimal local surface

```python
from openhands.sdk.workspace import LocalWorkspace
with LocalWorkspace(working_dir=workdir) as ws:
    ws.execute_command("echo hi", timeout=30.0)
# Agent/LocalConversation/LLM compose for full runs; LLM model string is litellm
# format e.g. "anthropic/claude-haiku-..." and needs the provider key.
```

`OPENHANDS_SUPPRESS_BANNER=1` silences startup. Adapter `health()` must report
`capabilities={"live_runs": <key present>}` honestly — install/env-hash/workspace
smoke is testable without keys; full agent runs are key-gated.

## Env-hash recipe (both harnesses)

sha256 over: python version + sorted `pip freeze`-style resolved versions of the
harness extra + harness package version + platform. Implemented once in
`omniagentos/harnesses` and recorded on every run (contracts.HarnessProfile.env_hash).

## Gotchas

- litellm pins: openhands `>=1.84.1`; mini `>=1.75.5,!=1.82.7,!=1.82.8` — resolver
  handled it (1.91.2). Don't add our own litellm pin.
- Scout venv ran Python 3.14.3 fine, but repo pins 3.12 (`.python-version`) —
  openhands-ai (the app, NOT installed) caps <3.14; the SDK doesn't.
- mini-swe CLI entry point is `mini` (typer), config merge via repeated `-c`.
