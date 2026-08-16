"""B0 arm — single bare CLI call (no loop, no tools).

Resolves the cli-claude adapter and runs AgentInput once. Does not modify
usage, retry, or metadata.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from omniagentos.contracts import AgentAdapter, AgentInput, AgentResult, HarnessType


def run_b0_arm(input: AgentInput) -> AgentResult:
    """Single-shot B0: resolve cli-claude and call adapter.run(input)."""
    adapter = _resolve_cli_claude()
    return adapter.run(input)


def _resolve_cli_claude() -> AgentAdapter:
    """Lazy import of resolve_adapter to avoid circular deps / missing p04."""
    resolve = _import_resolve_adapter()
    return cast(AgentAdapter, resolve(HarnessType.CLI_CLAUDE))


def _import_resolve_adapter() -> Callable[[HarnessType], Any]:
    try:
        from omniagentos.adapters import resolve_adapter  # type: ignore[attr-defined]

        return cast(Callable[[HarnessType], Any], resolve_adapter)
    except (ImportError, AttributeError):
        pass
    try:
        from omniagentos.adapters.registry import (  # type: ignore[import-untyped,import-not-found]
            resolve_adapter,
        )

        return cast(Callable[[HarnessType], Any], resolve_adapter)
    except ImportError as e:
        raise ImportError(
            "resolve_adapter not available. Implement omniagentos.adapters.registry "
            "(p04) or pass a monkeypatched resolve_adapter in tests."
        ) from e
