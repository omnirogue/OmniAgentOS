"""omniagentos.vaultgraph — a derived graph view over the Obsidian vault.

The vault (``vault/*.md``) is a hand-built knowledge graph: notes are nodes,
`[[wiki-links]]` + frontmatter refs are edges. This package parses that into an
in-process SQLite adjacency graph and exposes, on top of it, the GraphRAG-style
primitives agents need:

    build_graph(vault_dir)                     -> VaultGraph
    detect_communities(graph)                  -> list[Community]
    generate_mocs(graph, vault_dir, comms)     -> list[relpath]   (Map-of-Content)
    local_search(graph, note, hops=N)          -> Neighborhood
    global_search(query, vault_dir)            -> list[GlobalHit]
    classify_fact(existing, incoming)          -> FactVerdict      (new-fact scaffold)
    propose_links(graph, note_id)              -> list[LinkSuggestion]

Nothing here writes to the main SQLite database — the vault is the source of
truth and the graph is rebuilt from it on demand.
"""

from __future__ import annotations

from omniagentos.vaultgraph.classify import classify_against_many, classify_fact
from omniagentos.vaultgraph.community import (
    connected_components,
    detect_communities,
    label_propagation,
)
from omniagentos.vaultgraph.contracts import (
    Community,
    Edge,
    FactClass,
    FactVerdict,
    GlobalHit,
    LinkSuggestion,
    Neighborhood,
    NeighborNode,
    Node,
)
from omniagentos.vaultgraph.graph import VaultGraph, build_graph, build_graph_from_notes
from omniagentos.vaultgraph.moc import generate_mocs, render_moc_note
from omniagentos.vaultgraph.search import global_search, local_search
from omniagentos.vaultgraph.suggest import (
    HeuristicLinkSuggester,
    LinkSuggester,
    LLMLinkSuggester,
    propose_links,
)

__all__ = [
    "VaultGraph",
    "build_graph",
    "build_graph_from_notes",
    "detect_communities",
    "connected_components",
    "label_propagation",
    "generate_mocs",
    "render_moc_note",
    "local_search",
    "global_search",
    "classify_fact",
    "classify_against_many",
    "propose_links",
    "HeuristicLinkSuggester",
    "LLMLinkSuggester",
    "LinkSuggester",
    "Community",
    "Edge",
    "FactClass",
    "FactVerdict",
    "GlobalHit",
    "LinkSuggestion",
    "Neighborhood",
    "NeighborNode",
    "Node",
]
