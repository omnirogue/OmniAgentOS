"""Types and protocols for the W3 memory / context-assembly layer.

The memory layer turns the frozen ``conversations`` table (migration 031, owned by
W3-hierarchy) plus the Synapse knowledge graph into a single compact context block
that is prepended to an agent run so the operator never has to re-brief. This module
holds ONLY the shared vocabulary — the assembler (:mod:`omniagentos.memory.assemble`),
the concrete store (:mod:`omniagentos.memory.store`) and the never-raising runner
wiring (:mod:`omniagentos.memory.runner_hook`) depend on these types, never the
reverse.

Everything here is read against the FROZEN conversation model:

    conversations(id, scope_type 'project'|'task', scope_id, seq, role
                  'user'|'agent'|'system', content, model, created_at, meta_json)
    UNIQUE(scope_type, scope_id, seq)

so callers stub the :class:`ConversationReader` in tests and code against the reads.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, NamedTuple, Protocol

from pydantic import BaseModel, Field

# --- scope vocabulary -------------------------------------------------------

ScopeType = Literal["project", "task"]

# A knowledge recaller maps a query string + a top-k cap onto rendered fact lines.
# The default implementation (omniagentos.memory.recall_bridge) reuses the Synapse
# recall; tests inject a stub so assembly never touches the real graph.
KnowledgeRecaller = Callable[[str, int], list[str]]

# A memory recaller maps a query string + a top-k cap onto rendered metacog lesson
# lines (from metacog_memory_records). Same shape as KnowledgeRecaller so assembly
# can treat both as interchangeable callables; default lives in memory_bridge.
MemoryRecaller = Callable[[str, int], list[str]]


class ScopeRef(NamedTuple):
    """A single (scope_type, scope_id) address in the project/task hierarchy."""

    scope_type: ScopeType
    scope_id: str


class ConversationTurn(BaseModel):
    """One appended message on a node's conversation (frozen ``conversations`` row).

    ``seq`` is the per-scope monotonic order (UNIQUE(scope_type, scope_id, seq)); the
    reader returns turns in ascending ``seq`` (chronological) order.
    """

    seq: int
    role: Literal["user", "agent", "system"]
    content: str
    model: str | None = None
    created_at: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


# A history retriever maps a query string + a top-k cap onto OLDER conversation
# turns relevant to the query (the hybrid leg, omniagentos.memory.history). It
# returns full turns (not rendered lines) so the assembler can stamp them with
# their temporal metadata; the default is derived from the ConversationReader
# inside assemble_context when OMNIAGENTOS_MEMORY_HYBRID is on.
HistoryRetriever = Callable[[str, int], list[ConversationTurn]]


class AssembledContext(BaseModel):
    """The rendered context block plus the accounting a caller/telemetry needs.

    ``block`` is empty when nothing fit (no history, recall disabled, or the budget is
    smaller than the envelope). ``estimated_tokens`` is measured with the canonical
    :func:`omniagentos.contracts.estimate_tokens` and is always ``<= budget_tokens``.
    """

    block: str = ""
    scope_type: ScopeType
    scope_id: str
    node_turns: int = 0
    ancestor_summaries: int = 0
    recalls: int = 0
    history_hits: int = 0
    durable_ledger_entries: int = 0
    has_summary: bool = False
    estimated_tokens: int = 0
    budget_tokens: int = 0
    truncated: bool = False


class ConversationReader(Protocol):
    """The read seam the assembler codes against (stubbed in tests).

    A concrete implementation over the live SQLite store lives in
    :class:`omniagentos.memory.store.ConversationStore`; it degrades to empty results
    when the frozen ``conversations`` table is not yet present (pre-migration-031).
    """

    def recent_turns(self, scope_type: str, scope_id: str, limit: int) -> list[ConversationTurn]:
        """Up to ``limit`` most-recent turns for a node, in ascending-``seq`` order."""
        ...

    def resolve_ancestors(self, scope_type: str, scope_id: str) -> list[ScopeRef]:
        """The node's ancestor chain, ROOT first, immediate parent last (node excluded).

        A task's immediate parent is its project; a project's parents are the
        ``parent_project_id`` chain up to the top-level project.
        """
        ...

    def rolling_summary(self, scope_type: str, scope_id: str) -> str | None:
        """A compact standing summary for a node, or ``None`` if there is nothing to say."""
        ...


__all__ = [
    "AssembledContext",
    "ConversationReader",
    "ConversationTurn",
    "HistoryRetriever",
    "KnowledgeRecaller",
    "MemoryRecaller",
    "ScopeRef",
    "ScopeType",
]
