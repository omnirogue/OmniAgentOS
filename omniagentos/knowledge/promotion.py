"""Promotion gate — the ONLY place besides the operator API that may mint a PromotionGate.

This module exports gate(), which constructs a PromotionGate capability object for use
by the consolidator (the offline sleep-time process that may promote quarantined facts
to active status).

The REAL boundary is PostgreSQL role separation (knowledge_agent vs knowledge_admin).
This Python gate is defense-in-depth; see contracts.py docstring (F1 + R2 design review).
"""

from __future__ import annotations

from omniagentos.knowledge.contracts import _GATE_TOKEN, PromotionGate


def gate() -> PromotionGate:
    """Construct a PromotionGate for the consolidator.

    May ONLY be called by:
    - omniagentos.knowledge.consolidate.consolidate()
    - The operator-token-checked API route

    Returns:
        PromotionGate: A capability object authorized to call store.promote_fact/demote_fact.

    Raises:
        PromotionDenied: If called from anywhere else (defense-in-depth; should be
            unreachable since gate construction will fail at the API layer).
    """
    return PromotionGate(holder="consolidator", _token=_GATE_TOKEN)
