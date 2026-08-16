"""FAST DISPATCH: a three-gate classifier that routes simple briefs onto the
fast session-spawn path in milliseconds instead of paying for an inline Fable
planning pass at intake.

Behind the ``OMNIAGENTOS_FAST_DISPATCH`` flag (default OFF = byte-identical
behavior). See :mod:`omniagentos.dispatch.gate` for the decision logic and
:mod:`omniagentos.dispatch.log` for the (best-effort) decision telemetry.
"""

from __future__ import annotations

from omniagentos.dispatch.gate import GateDecision, decide
from omniagentos.dispatch.log import record_decision
from omniagentos.dispatch.providers import (
    allowed_providers_from_params,
    assert_dispatch_provider,
    filter_dispatch_candidates,
)

__all__ = [
    "GateDecision",
    "allowed_providers_from_params",
    "assert_dispatch_provider",
    "decide",
    "filter_dispatch_candidates",
    "record_decision",
]
