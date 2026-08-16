"""Context-assembly ("never re-brief") layer for OmniAgentOS.

Turns the frozen ``conversations`` table plus the Synapse knowledge graph into a single
compact, budget-bounded context block that is prepended to an agent run for a project or
task, so the operator never has to re-explain context every prompt.

Public surface:

* :func:`omniagentos.memory.assemble.assemble_context` — build the block for a node.
* :class:`omniagentos.memory.store.ConversationStore` — live conversation reads/appends.
* :mod:`omniagentos.memory.runner_hook` — never-raising wiring for the dispatch path.
* :func:`omniagentos.memory.recall_bridge.default_knowledge_recaller` — Synapse recaller.
* :func:`omniagentos.memory.memory_bridge.default_memory_recaller` — metacog lessons recaller.
"""

from __future__ import annotations

from omniagentos.memory.assemble import assemble_context
from omniagentos.memory.contracts import (
    AssembledContext,
    ConversationReader,
    ConversationTurn,
    KnowledgeRecaller,
    MemoryRecaller,
    ScopeRef,
)

__all__ = [
    "AssembledContext",
    "ConversationReader",
    "ConversationTurn",
    "KnowledgeRecaller",
    "MemoryRecaller",
    "ScopeRef",
    "assemble_context",
]
