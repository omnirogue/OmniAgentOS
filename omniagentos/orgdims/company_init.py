"""AI engineering company organization helper logic, migrated to orgdims.

This module hosts the small pieces every other module in the package shares:
the `adapter_fn` calling convention, a tolerant JSON-from-LLM parser, and a
slug helper for vault note paths. Nothing here talks to the DB directly.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from typing import Any

from omniagentos.contracts import AgentInput, AgentResult, BudgetSpec, ResultStatus, new_id

logger = logging.getLogger(__name__)

AdapterFn = Callable[..., AgentResult]

DEFAULT_BUDGET: dict[str, int] = {"tokens_max": 6000, "wall_ms_max": 120_000}


def default_adapter_fn(
    harness: str,
    prompt: str,
    *,
    output_schema: dict[str, Any] | None = None,
    budget: dict[str, Any] | None = None,
) -> AgentResult:
    """Production `adapter_fn`: resolve *harness* and call it directly."""
    from omniagentos.adapters.registry import resolve_adapter

    spec = dict(budget or DEFAULT_BUDGET)
    adapter = resolve_adapter(harness)
    agent_input = AgentInput(
        run_id=new_id("run"),
        task_id=new_id("tsk"),
        prompt=prompt,
        output_schema=output_schema,
        budget=BudgetSpec(
            tokens_max=spec.get("tokens_max"),
            wall_ms_max=spec.get("wall_ms_max"),
            cost_usd_max=spec.get("cost_usd_max"),
            max_turns=spec.get("max_turns"),
        ),
        metadata={"source": "company"},
    )
    return adapter.run(agent_input)


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_maybe(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction from LLM output."""
    if not text or not text.strip():
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
        text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError):
        pass
    match = _JSON_BLOCK_RE.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except (json.JSONDecodeError, ValueError):
        logger.warning("company: could not parse JSON from adapter output (%d chars)", len(text))
        return None


def adapter_text(result: AgentResult) -> str:
    """Best-effort text extraction from an `AgentResult`."""
    if result.status != ResultStatus.OK:
        return ""
    if result.output_json is not None:
        return json.dumps(result.output_json)
    return result.output_text or ""


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Lowercase, hyphen-joined slug for vault note filenames."""
    slug = _SLUG_RE.sub("-", name.strip().lower()).strip("-")
    return slug or "unnamed"
