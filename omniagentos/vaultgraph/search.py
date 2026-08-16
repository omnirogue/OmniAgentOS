"""Two retrieval primitives for agents (the GraphRAG local/global split):

* ``local_search(graph, note, hops=N)`` — the N-hop neighbourhood of a note,
  for grounded, entity-centric context.
* ``global_search(query, vault_dir)`` — a keyword ranking over the generated
  Map-of-Content summaries, for broad "what does the vault know about X" queries.
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from pathlib import Path

from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.vaultgraph.contracts import (
    MOC_DIR,
    Edge,
    GlobalHit,
    Neighborhood,
    NeighborNode,
)
from omniagentos.vaultgraph.graph import VaultGraph
from omniagentos.vaultgraph.parser import slugify_target

# Unicode-aware word tokenizer: letters/digits of any script, keeping intra-word
# combining marks so accented and non-Latin terms are searchable (F6). ``_``
# excluded so it splits snake_case; anchored numbers like ``3.5`` stay whole.
_WORD_RE = re.compile(r"[^\W_]+(?:\.[^\W_]+)*", re.UNICODE)
_STOPWORDS = frozenset(
    "the a an and or of to in on for with is are be at by from as this that it".split()
)


def local_search(graph: VaultGraph, note: str, hops: int = 1) -> Neighborhood:
    """Return the ``hops``-hop undirected neighbourhood around ``note``.

    ``note`` is matched leniently (slugified), so callers can pass a raw note
    name, id, or `[[link]]` target. Nodes come back tagged with hop distance;
    edges are only those internal to the returned node set.
    """
    center = slugify_target(note)
    if graph.get_node(center) is None:
        return Neighborhood(center=center, hops=hops, nodes=[], edges=[])

    distance: dict[str, int] = {center: 0}
    queue: deque[str] = deque([center])
    while queue:
        current = queue.popleft()
        if distance[current] >= hops:
            continue
        for neighbor in graph.neighbors(current):
            if neighbor not in distance:
                distance[neighbor] = distance[current] + 1
                queue.append(neighbor)

    nodes: list[NeighborNode] = []
    for node_id in sorted(distance, key=lambda n: (distance[n], n)):
        node = graph.get_node(node_id)
        if node is not None:
            nodes.append(NeighborNode(node=node, distance=distance[node_id]))

    inside = set(distance)
    # Only the induced neighbourhood's edges are queried — never the full edge
    # table — so a small local search stays cheap on a huge vault (F7).
    edges: list[Edge] = graph.induced_edges(inside)
    return Neighborhood(center=center, hops=hops, nodes=nodes, edges=edges)


def _terms(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFC", text).casefold()
    return [t for t in _WORD_RE.findall(normalized) if t not in _STOPWORDS]


def _snippet(content: str, query_terms: set[str], *, width: int = 200) -> str:
    lowered = content.lower()
    for term in query_terms:
        idx = lowered.find(term)
        if idx >= 0:
            start = max(0, idx - width // 2)
            return " ".join(content[start : start + width].split())
    return " ".join(content[:width].split())


def global_search(query: str, vault_dir: str | Path, *, limit: int = 5) -> list[GlobalHit]:
    """Rank the generated MOC summaries by keyword overlap with ``query``.

    A simple, dependency-free TF scorer over ``<vault_dir>/moc/*.md``. Returns
    an empty list when no MOCs have been generated yet.
    """
    query_terms = set(_terms(query))
    if not query_terms:
        return []

    moc_root = Path(vault_dir) / MOC_DIR
    if not moc_root.is_dir():
        return []

    root_resolved = Path(vault_dir).resolve()
    hits: list[GlobalHit] = []
    for path in sorted(moc_root.glob("*.md")):
        # Never follow a symlink whose target escapes the vault (F8).
        if path.is_symlink():
            continue
        try:
            if inode_relative_parts_anchored(path.resolve(), root_resolved) is None:
                continue
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        doc_terms = _terms(content)
        if not doc_terms:
            continue
        counts = {term: doc_terms.count(term) for term in query_terms}
        raw = sum(counts.values())
        if raw == 0:
            continue
        coverage = sum(1 for term in query_terms if counts[term]) / len(query_terms)
        score = round(raw / len(doc_terms) * (1 + coverage), 6)
        title = next(
            (ln[2:].strip() for ln in content.splitlines() if ln.startswith("# ")),
            path.stem,
        )
        hits.append(
            GlobalHit(
                community_id=path.stem,
                relpath=str(path.relative_to(vault_dir)),
                title=title,
                score=score,
                snippet=_snippet(content, query_terms),
            )
        )

    hits.sort(key=lambda h: (-h.score, h.community_id))
    return hits[:limit]
