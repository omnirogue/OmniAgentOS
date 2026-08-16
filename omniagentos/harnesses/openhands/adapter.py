"""OpenHands SDK adapter (harness id ``openhands``).

Wraps `openhands-sdk` (pinned in `pyproject.toml`'s `harness` extra, see
`docs/research/harnesses.md` for the empirical recon this file is built from)
behind the `AgentAdapter` protocol (`omniagentos/contracts.py`).

Two rules drive every method here:

1. `openhands` is imported LAZILY, inside methods only. Importing this module
   (`omniagentos.harnesses.openhands.adapter`) must never fail just because the
   `harness` extra isn't installed -- `health()` reports that as
   `healthy=False` with the import error in `detail` instead of raising.
2. `run()` never fakes a live run and never touches the network without a
   usable provider API key. OpenHands routes every model call through
   litellm, so "usable" means one of the well-known provider API key env vars
   (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...) is set and non-empty. Without
   one, `run()` returns a `status=error` `AgentResult` before even attempting
   to import the SDK -- `health()`'s `capabilities["live_runs"]` reports this
   honestly ahead of time.
"""

from __future__ import annotations

import os
import tempfile
import time
from importlib import metadata
from types import ModuleType
from typing import Any

from pydantic import SecretStr

from omniagentos.budget.policy import session_budget_cap
from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    HarnessProfile,
    HarnessType,
    HealthStatus,
    ResultStatus,
    estimate_tokens,
)
from omniagentos.harnesses.envhash import env_hash

# ---------------------------------------------------------------------------
# Provider API key detection (the ONLY thing that gates live_runs / run()).
#
# litellm resolves the provider from the model string and reads credentials
# from well-known env vars. Each entry pairs the env var with a cheap/fast
# default model string for that provider, used only when AgentInput.model is
# not given (docs/research/harnesses.md: "live LLM calls need provider API
# keys"). Order is also the detection priority when multiple keys are set.
# ---------------------------------------------------------------------------
_PROVIDER_KEYS: tuple[tuple[str, str], ...] = (
    ("ANTHROPIC_API_KEY", "anthropic/claude-3-5-haiku-20241022"),
    ("OPENAI_API_KEY", "openai/gpt-4o-mini"),
    ("AZURE_API_KEY", "azure/gpt-4o-mini"),
    ("GEMINI_API_KEY", "gemini/gemini-1.5-flash"),
    ("GOOGLE_API_KEY", "gemini/gemini-1.5-flash"),
    ("OPENROUTER_API_KEY", "openrouter/anthropic/claude-3-5-haiku"),
    ("XAI_API_KEY", "xai/grok-2-latest"),
    ("GROQ_API_KEY", "groq/llama-3.1-8b-instant"),
    ("MISTRAL_API_KEY", "mistral/mistral-small-latest"),
    ("DEEPSEEK_API_KEY", "deepseek/deepseek-chat"),
    ("FIREWORKS_API_KEY", "fireworks_ai/llama-v3p1-8b-instruct"),
    ("TOGETHERAI_API_KEY", "together_ai/meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo"),
)
_PROVIDER_KEY_ENV_VARS: tuple[str, ...] = tuple(name for name, _ in _PROVIDER_KEYS)
_DEFAULT_MODEL_BY_KEY_ENV: dict[str, str] = dict(_PROVIDER_KEYS)

_NO_KEY_ERROR = "openhands: no provider API key; live runs unavailable"


def _detect_provider_key_env() -> str | None:
    """Return the first provider API key env var that is set and non-empty,
    or None. This single check is what `health()`'s `live_runs` capability
    and `run()`'s no-key error path both key off of -- never claim a key
    exists that isn't actually there."""
    for name in _PROVIDER_KEY_ENV_VARS:
        if os.environ.get(name, "").strip():
            return name
    return None


def _elapsed_ms(started: float) -> int:
    return max(1, int((time.monotonic() - started) * 1000))


def _sdk_version() -> str:
    """Installed openhands-sdk version via importlib.metadata. This reads
    installed-distribution metadata only -- it does NOT import the
    `openhands` package, so it is always safe to call at module import time,
    extra installed or not."""
    try:
        return metadata.version("openhands-sdk")
    except metadata.PackageNotFoundError:
        return "0.0.0"


def _import_sdk() -> ModuleType:
    """Lazily import `openhands.sdk`, suppressing its startup banner. Left to
    raise unchanged (ImportError when the `harness` extra is absent, or
    whatever else the SDK's own module-level code might raise) -- callers
    decide how to report that.

    ``litellm`` (an `openhands.sdk` transitive import) fetches its model cost
    map from the network at IMPORT time unless told not to -- a plain,
    key-less `health()` call would otherwise reach out to the network before
    this adapter ever gets to its own no-key check. `setdefault` so a caller
    that wants the live remote map can still opt back in explicitly."""
    os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    import openhands.sdk as sdk

    return sdk


def _last_agent_text(conversation: Any, sdk: ModuleType) -> str:
    """Walk the conversation's events (newest first) for the last agent
    message and return its text content joined by newlines. Empty string if
    the agent never produced a text message (e.g. tool-only run)."""
    events = list(conversation.state.events)
    for event in reversed(events):
        if isinstance(event, sdk.MessageEvent) and event.source == "agent":
            parts = [item.text for item in event.llm_message.content if getattr(item, "text", None)]
            if parts:
                return "\n".join(parts)
    return ""


def _usage_from_conversation(conversation: Any, wall_ms: int, prompt: str) -> AgentUsage:
    """Map openhands' own litellm-backed accounting (conversation_stats) to
    AgentUsage. estimated=False + source="imported" whenever the SDK actually
    recorded a completion; falls back to our char/4 estimator only if it
    somehow recorded none (shouldn't happen after a successful run())."""
    metrics = conversation.conversation_stats.get_combined_metrics()
    token_usage = metrics.accumulated_token_usage
    if token_usage is not None and metrics.token_usages:
        return AgentUsage(
            wall_ms=wall_ms,
            turns=len(metrics.token_usages),
            input_tokens=token_usage.prompt_tokens,
            output_tokens=token_usage.completion_tokens,
            cost_usd=metrics.accumulated_cost,
            estimated=False,
            source="imported",
        )
    return AgentUsage(
        wall_ms=wall_ms,
        turns=None,
        input_tokens=estimate_tokens(prompt),
        output_tokens=None,
        cost_usd=None,
        estimated=True,
        source="estimator",
    )


class OpenHandsAdapter:
    """AgentAdapter over openhands-sdk's LocalWorkspace + Agent/LLM/Conversation
    surface. Registered as exactly
    `omniagentos.harnesses.openhands.adapter:OpenHandsAdapter`
    (contracts/interfaces.md Section "p04 -- omniagentos.adapters")."""

    name = "openhands"
    # Read once at import time via importlib.metadata (no `openhands` import
    # involved -- see _sdk_version docstring), so this is always populated
    # correctly whether or not the extra is installed.
    version = _sdk_version()

    def profile(self) -> HarnessProfile:
        """HarnessProfile for this adapter, meant to be recorded on every run
        (contracts.HarnessProfile / RunManifest.harness)."""
        return HarnessProfile(
            harness=HarnessType.OPENHANDS,
            version=self.version,
            env_hash=env_hash(),
        )

    def health(self) -> HealthStatus:
        try:
            sdk = _import_sdk()
        except Exception as exc:  # ImportError (extra absent) or any load-time failure
            return HealthStatus(
                healthy=False,
                detail=f"openhands: import failed: {type(exc).__name__}: {exc}",
                capabilities={"live_runs": False},
            )

        try:
            with tempfile.TemporaryDirectory(prefix="omniagentos-openhands-health-") as tmp:
                sdk.LocalWorkspace(working_dir=tmp)
        except Exception as exc:
            return HealthStatus(
                healthy=False,
                detail=f"openhands: LocalWorkspace instantiation failed: {type(exc).__name__}: {exc}",
                capabilities={"live_runs": False},
            )

        key_env = _detect_provider_key_env()
        if key_env is None:
            return HealthStatus(
                healthy=True,
                detail=(
                    "openhands-sdk installed & importable; LocalWorkspace instantiable; "
                    "no provider API key found in env (checked "
                    f"{', '.join(_PROVIDER_KEY_ENV_VARS)}) so live runs are unavailable"
                ),
                capabilities={"live_runs": False},
            )
        return HealthStatus(
            healthy=True,
            detail=f"openhands-sdk installed & importable; live runs enabled via {key_env}",
            capabilities={"live_runs": True},
        )

    def run(self, input: AgentInput) -> AgentResult:
        started = time.monotonic()

        # Gate FIRST, before any import: no key means no network, guaranteed.
        key_env = _detect_provider_key_env()
        if key_env is None:
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=AgentUsage(wall_ms=_elapsed_ms(started), estimated=True, source="estimator"),
                error=_NO_KEY_ERROR,
            )

        try:
            sdk = _import_sdk()
        except Exception as exc:
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=AgentUsage(wall_ms=_elapsed_ms(started), estimated=True, source="estimator"),
                error=f"openhands: import failed: {type(exc).__name__}: {exc}",
            )

        working_dir = input.working_dir or os.getcwd()
        model = input.model or _DEFAULT_MODEL_BY_KEY_ENV.get(key_env, "gpt-5.5")

        try:
            llm = sdk.LLM(
                model=model,
                api_key=SecretStr(os.environ[key_env]),
                usage_id=input.run_id,
            )
            # Minimal flow (docs/research/harnesses.md's enumerated import
            # surface: Agent, LocalConversation, LLM, LocalWorkspace): no
            # tools wired up. openhands-sdk alone (without the separate
            # `openhands-tools` ecosystem package, which isn't part of our
            # pinned `harness` extra) ships no pre-registered Tool
            # implementations, so naming e.g. "TerminalTool" here would only
            # be safe if something else registered it first -- out of scope
            # for this minimal adapter.
            agent = sdk.Agent(llm=llm, tools=[])
            workspace = sdk.LocalWorkspace(working_dir=working_dir)

            # LocalConversation directly (not the `Conversation` factory --
            # empirically confirmed against the real 1.35.0 install that its
            # __new__ does NOT accept max_budget_per_run even though
            # LocalConversation.__init__ does).
            conversation_kwargs: dict[str, Any] = {"visualizer": None}
            if input.budget.max_turns:
                conversation_kwargs["max_iteration_per_run"] = input.budget.max_turns
            # Advisory by default: an in-conversation cost cap aborts the run
            # INSIDE OpenHands, which is the same hard blocker the CLI's
            # --max-budget-usd was (omniagentos.budget.policy).
            harness_cost_cap = session_budget_cap(input.budget.cost_usd_max)
            if harness_cost_cap is not None:
                conversation_kwargs["max_budget_per_run"] = harness_cost_cap

            conversation = sdk.LocalConversation(
                agent=agent, workspace=workspace, **conversation_kwargs
            )
            conversation.send_message(input.prompt)
            conversation.run()
        except Exception as exc:
            return AgentResult(
                status=ResultStatus.ERROR,
                usage=AgentUsage(wall_ms=_elapsed_ms(started), estimated=True, source="estimator"),
                error=f"openhands: run failed: {type(exc).__name__}: {exc}",
            )

        wall_ms = _elapsed_ms(started)
        state_id = conversation.state.id
        return AgentResult(
            status=ResultStatus.OK,
            output_text=_last_agent_text(conversation, sdk),
            session_ref=str(state_id) if state_id else None,
            usage=_usage_from_conversation(conversation, wall_ms, input.prompt),
        )

    def cancel(self, session_ref: str) -> bool:
        # run() executes synchronously to completion inside a single call and
        # keeps no cross-call registry of live LocalConversation objects (no
        # background thread/process survives a run() call to cancel), so
        # there is nothing to act on out-of-band. Best-effort per the
        # AgentAdapter contract: report unsupported rather than pretend.
        return False
