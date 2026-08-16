"""Memory / context-assembly configuration — typed accessors over the environment.

Kept tiny and dependency-free so the never-raising runner wiring can read it without
importing the heavier assembler/store modules.
"""

from __future__ import annotations

import os

ENV_ENABLED = "OMNIAGENTOS_MEMORY"
ENV_BUDGET_TOKENS = "OMNIAGENTOS_MEMORY_BUDGET_TOKENS"
ENV_MAX_NODE_TURNS = "OMNIAGENTOS_MEMORY_MAX_TURNS"
ENV_SCORED = "OMNIAGENTOS_MEMORY_SCORED"
ENV_LESSONS = "OMNIAGENTOS_MEMORY_LESSONS"
ENV_HYBRID = "OMNIAGENTOS_MEMORY_HYBRID"

DEFAULT_BUDGET_TOKENS = 1200
DEFAULT_MAX_NODE_TURNS = 12


def memory_enabled() -> bool:
    """Whether context assembly + conversation persistence run.

    Defaults ON (opt-out): the layer is a silent no-op whenever the frozen
    ``conversations`` table is absent, so enabling it before migration 031 lands is
    harmless. Set ``OMNIAGENTOS_MEMORY=0`` to disable it entirely.
    """
    return os.getenv(ENV_ENABLED, "1") != "0"


def budget_tokens() -> int:
    """Token budget for a rendered context block."""
    try:
        value = int(os.getenv(ENV_BUDGET_TOKENS, str(DEFAULT_BUDGET_TOKENS)))
    except (ValueError, TypeError):
        return DEFAULT_BUDGET_TOKENS
    return value if value > 0 else DEFAULT_BUDGET_TOKENS


def max_node_turns() -> int:
    """How many of a node's most-recent turns to consider for inclusion."""
    try:
        value = int(os.getenv(ENV_MAX_NODE_TURNS, str(DEFAULT_MAX_NODE_TURNS)))
    except (ValueError, TypeError):
        return DEFAULT_MAX_NODE_TURNS
    return value if value > 0 else DEFAULT_MAX_NODE_TURNS


def scored_enabled() -> bool:
    """Whether context packing ranks offered items by recency x importance x relevance
    (Generative Agents arXiv:2304.03442 style) instead of the fixed priority order.

    OFF by default (opt-in): set ``OMNIAGENTOS_MEMORY_SCORED=1`` to enable. Scored
    ranking only kicks in when a ``task_text`` is also supplied to ``assemble_context``;
    with no task text the fixed-priority order is preserved bit-for-bit.
    """
    return os.getenv(ENV_SCORED, "0") == "1"


def hybrid_enabled() -> bool:
    """Whether the hybrid context assembly upgrades run (memcert v2, 2026-08-13):
    a BM25 x recency-prior retrieval leg over older conversation turns
    ("## RELEVANT HISTORY"), temporal stamps on rendered turns, an abstention
    guard line in the envelope guidance, and budget-reserved section packing.

    OFF in a bare environment, ON on the launch path (``scripts/launch-env.sh``
    exports ``OMNIAGENTOS_MEMORY_HYBRID=1``) — the same default split as
    ``OMNIAGENTOS_KNOWLEDGE``. Every upgrade was pre-registered as a memcert
    hypothesis (H1/H2/H3, devtasks/memcert/RESULTS-2026-08-12.md) and is
    certified by the deterministic retrieval-sufficiency suite
    (tests/memcert/test_sufficiency.py) on every merge; flag off restores the
    v1 assembler's RENDERED BLOCK byte-for-byte (telemetry structures gain a
    zero ``history_hits`` field — the compatibility claim is prompt-block
    scope, codex-critic CR-008).
    """
    return os.getenv(ENV_HYBRID, "0") == "1"


def lessons_enabled() -> bool:
    """Whether metacog memory lessons are included in the context block.

    ON by default (opt-out): learned lessons from metacog are integrated into
    every agent lineage's prior-context block. Set ``OMNIAGENTOS_MEMORY_LESSONS=0``
    to disable lessons entirely, bypassing the memory recaller on every dispatch turn.
    """
    return os.getenv(ENV_LESSONS, "1") != "0"


__all__ = [
    "DEFAULT_BUDGET_TOKENS",
    "DEFAULT_MAX_NODE_TURNS",
    "budget_tokens",
    "hybrid_enabled",
    "lessons_enabled",
    "max_node_turns",
    "memory_enabled",
    "scored_enabled",
]
