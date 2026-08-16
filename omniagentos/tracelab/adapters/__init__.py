"""Adapter registry: each adapter sniffs files it understands and yields
normalized :class:`~omniagentos.tracelab.events.Trace` streams."""

from __future__ import annotations

from omniagentos.tracelab.adapters.base import TraceAdapter
from omniagentos.tracelab.adapters.claude_code import ADAPTER as CLAUDE_CODE
from omniagentos.tracelab.adapters.conversations import ADAPTER as CONVERSATIONS
from omniagentos.tracelab.adapters.openhands import ADAPTER as OPENHANDS
from omniagentos.tracelab.adapters.own_ledger import ADAPTER as OWN_LEDGER
from omniagentos.tracelab.adapters.pi_session import ADAPTER as PI_SESSION
from omniagentos.tracelab.adapters.swe_agent import ADAPTER as SWE_AGENT

#: Sniff order matters: most-specific formats first, generic ones last.
ADAPTERS: tuple[TraceAdapter, ...] = (
    OWN_LEDGER,
    CLAUDE_CODE,
    PI_SESSION,
    SWE_AGENT,
    OPENHANDS,
    CONVERSATIONS,
)

__all__ = ["ADAPTERS", "TraceAdapter"]
