"""Declarative per-node retry (P5) with an explicit transient-fault vocabulary.

P5's result: transient faults need no graph logic at all — a ``RetryPolicy``
with an exception filter is enough. What LangGraph gives no help with is
*deciding* which exceptions are transient, and that decision is safety-critical:
retrying a permission error is a policy bypass attempt in a loop, and retrying
an ``EffectStateUnknown`` is exactly the duplicate side effect P7 measured.

So the filter is an ALLOWLIST of faults known to be retryable. Everything else —
including every ``LoopError`` that means "denied", "not approved", or "state
unknown" — propagates on the first attempt.
"""

from __future__ import annotations

import inspect
from typing import Any

from omniagentos.llm.budget import LLMTransportError
from omniagentos_loops.contracts import TransientLoopError

try:  # httpx is present in the loops venv; keep the import non-fatal anyway
    import httpx

    _HTTP_TRANSIENT: tuple[type[Exception], ...] = (httpx.TransportError,)
except ImportError:  # pragma: no cover
    _HTTP_TRANSIENT = ()


#: The complete set of faults a loop node may be retried on.
TRANSIENT_EXCEPTIONS: tuple[type[Exception], ...] = (
    TransientLoopError,
    LLMTransportError,
    ConnectionError,
    TimeoutError,
    *_HTTP_TRANSIENT,
)


def retry_kwarg() -> str:
    """``add_node`` renamed ``retry`` -> ``retry_policy`` across the 1.x line.

    PROTOTYPE_RESULTS unresolved-risk #4: detect it instead of pinning hope to a
    version string.
    """
    from langgraph.graph import StateGraph

    params = inspect.signature(StateGraph.add_node).parameters
    return "retry_policy" if "retry_policy" in params else "retry"


def default_retry_policy(max_attempts: int = 3, initial_interval: float = 0.5) -> Any:
    """RetryPolicy for an ordinary loop node (network-ish work)."""
    from langgraph.types import RetryPolicy

    return RetryPolicy(
        max_attempts=max_attempts,
        initial_interval=initial_interval,
        retry_on=TRANSIENT_EXCEPTIONS,
    )


def retry_kwargs(max_attempts: int = 3, initial_interval: float = 0.5) -> dict[str, Any]:
    """``**retry_kwargs()`` — ready to splat into ``StateGraph.add_node``."""
    if max_attempts <= 1:
        return {}
    return {retry_kwarg(): default_retry_policy(max_attempts, initial_interval)}


__all__ = [
    "TRANSIENT_EXCEPTIONS",
    "default_retry_policy",
    "retry_kwarg",
    "retry_kwargs",
]
