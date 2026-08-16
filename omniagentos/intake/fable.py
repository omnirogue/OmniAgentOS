"""Fable model routing — the shared seam for running the Anthropic *Fable* model.

Every intake reasoning step (clarify, plan, project-route) is Fable at a chosen
reasoning EFFORT, run through the existing Claude CLI adapter (``--model fable
--effort <e>``). This module centralises that call so the effort/model policy lives
in exactly one place and each caller only picks its effort.

``run_fable_json`` never raises: an unavailable CLI or an unusable response returns
``None`` so callers fall back to their deterministic heuristics — intake stays
functional offline, exactly like the clarify loop always has.

When Fable is rate-limited or unavailable, falls back to a configurable chain.
The chain itself lives in ``omniagentos.intake.fallback`` and is now eight rungs
deep and cross-provider (fable → opus → sol → grok → gemini-api → kimi →
gemini-lite-api → openrouter, filtered to the CLIs/accounts this machine
actually has). ``OMNIAGENTOS_PLANNER_FALLBACKS`` still overrides it end to end:
the env value gates the chain here AND is honored rung-for-rung by
``run_with_fallback``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Literal

from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    BudgetSpec,
    HarnessType,
    ResultStatus,
    new_id,
)

LOG = logging.getLogger(__name__)

# The Fable model id passed to the Claude CLI adapter's --model. Overridable so an
# operator can pin a different model without touching code.
FABLE_MODEL = os.environ.get("OMNIAGENTOS_FABLE_MODEL", "fable")

# Planner fallback chain: env var controls which models to try in sequence.
# Format: "fable", "fable:opus", "fable:opus:sol", "sol", etc.
#
# Kept for backwards compatibility ONLY (it snapshots the environment at import
# time, which is exactly why it must not gate anything): the live decision reads
# the env on every call via _planner_fallback_names(). "fable" alone is the one
# value that means "legacy single-call behavior"; EVERY other value — including
# a single non-fable rung like "sol" — is an operator's explicit chain and is
# honored rung-for-rung by fallback._resolve_chain. Unset means the DEFAULT
# chain (fallback.DEFAULT_CHAIN, eight cross-provider rungs), not the literal
# below.
PLANNER_FALLBACKS = (
    os.environ.get("OMNIAGENTOS_PLANNER_FALLBACKS", "fable:opus:sol").lower().split(":")
)

#: The one override value that keeps the legacy single-Fable-call path.
LEGACY_SINGLE_CHAIN = ("fable",)

# Reasoning effort levels, cheapest -> deepest. Clarify uses "medium"; the planner
# uses "high" and escalates to "max" for high-complexity goals; the router uses
# "medium" (a light new-vs-existing judgement).
Effort = Literal["low", "medium", "high", "max"]


def _planner_fallback_names() -> tuple[str, ...]:
    """``OMNIAGENTOS_PLANNER_FALLBACKS`` as rung names, read at CALL time.

    Empty tuple when unset/blank. Parsed exactly like
    ``fallback._env_chain_names`` so the value that decides WHETHER the chain
    runs and the value the chain is BUILT from can never disagree.
    """
    raw = os.environ.get("OMNIAGENTOS_PLANNER_FALLBACKS", "").strip()
    if not raw:
        return ()
    return tuple(part.strip().lower() for part in raw.lower().split(":") if part.strip())


def _use_fallback_chain() -> bool:
    """True unless the operator explicitly pinned the legacy single-Fable path.

    Unset -> the DEFAULT chain. Any explicit value -> the chain resolver, so a
    one-rung override like ``OMNIAGENTOS_PLANNER_FALLBACKS=sol`` actually runs
    Sol instead of silently running the hard-coded legacy Fable call (which is
    what a length check did).
    """
    return _planner_fallback_names() != LEGACY_SINGLE_CHAIN


def run_fable_json(
    prompt: str,
    schema: dict[str, Any],
    *,
    model: str = FABLE_MODEL,
    effort: Effort | str = "medium",
    max_turns: int = 2,
    wall_ms: int = 120_000,
) -> dict[str, Any] | None:
    """Run ``prompt`` through Fable at ``effort`` and return the parsed JSON object.

    Returns ``None`` (never raises) when the CLI is unavailable or produced no
    structured output, so the caller can degrade to a heuristic. No API key is
    hardcoded — the subscription CLI carries its own auth.

    Falls back down the cross-provider chain when Fable is rate-limited or
    unavailable (``OMNIAGENTOS_PLANNER_FALLBACKS`` overrides it; unset means
    ``fallback.DEFAULT_CHAIN``). Only an explicit ``fable`` keeps the legacy
    single-call path below.
    """
    # Any chain other than the explicit legacy "fable" goes to the resolver,
    # which is the only thing that honors the operator's rung order.
    if _use_fallback_chain():
        from omniagentos.intake.fallback import run_with_fallback

        result = run_with_fallback(
            prompt, schema, model=model, effort=effort, max_turns=max_turns, wall_ms=wall_ms
        )
        if result is not None:
            return result
        # Fallback chain exhausted; return None for heuristic fallback
        return None

    # Legacy behavior: single Fable call, no fallback chain
    try:
        adapter = resolve_adapter(HarnessType.CLI_CLAUDE)
        agent_result: AgentResult = adapter.run(
            AgentInput(
                run_id=new_id("run"),
                task_id=new_id("tsk"),
                prompt=prompt,
                model=model,
                output_schema=schema,
                budget=BudgetSpec(max_turns=max_turns, wall_ms_max=wall_ms),
                # Effort rides on metadata; the Claude adapter turns it into --effort.
                metadata={"effort": str(effort)},
            )
        )
    except Exception:  # noqa: BLE001 -- an unavailable CLI must degrade, not crash intake.
        LOG.warning("Fable model unavailable; falling back to heuristic", exc_info=True)
        return None
    if agent_result.status != ResultStatus.OK or agent_result.output_json is None:
        LOG.warning(
            "Fable model returned no structured output (status=%s, error=%s); "
            "falling back to heuristic",
            agent_result.status,
            agent_result.error,
        )
        return None
    return agent_result.output_json


def resolve_adapter(harness: HarnessType) -> Any:
    """Resolve an adapter by harness type. Imported locally to avoid circular imports."""
    from omniagentos.adapters.registry import resolve_adapter as _resolve

    return _resolve(harness)


__all__ = ["Effort", "FABLE_MODEL", "PLANNER_FALLBACKS", "run_fable_json"]
