"""Community detection over the vault graph — the GraphRAG "community" step.

Two light, dependency-free methods:

* ``connected_components`` — the robust default when the graph is fragmented.
* ``label_propagation`` — a *deterministic* variant of the classic algorithm
  that finds finer clusters inside a single large component (the real vault is
  one big component wired through hub notes like ``[[model-intelligence]]``, so
  plain connected components would return one useless blob).

Determinism matters for reproducible MOC notes: nodes are processed in sorted
order and every tie is broken by the lexicographically smallest label, so a
given vault always yields the same communities.
"""

from __future__ import annotations

from collections import Counter

from omniagentos.vaultgraph.contracts import Community
from omniagentos.vaultgraph.graph import VaultGraph


def connected_components(adjacency: dict[str, set[str]]) -> list[list[str]]:
    """Undirected connected components, each returned sorted, outer list sorted
    by (descending size, first member) for stable ordering."""
    seen: set[str] = set()
    components: list[list[str]] = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        stack = [start]
        seen.add(start)
        component: list[str] = []
        while stack:
            node = stack.pop()
            component.append(node)
            for neighbor in adjacency[node]:
                if neighbor not in seen:
                    seen.add(neighbor)
                    stack.append(neighbor)
        components.append(sorted(component))
    components.sort(key=lambda c: (-len(c), c[0]))
    return components


def label_propagation(adjacency: dict[str, set[str]], *, max_iter: int = 100) -> list[list[str]]:
    """Deterministic asynchronous label propagation.

    Each node adopts the most frequent label among its neighbours; ties (and the
    all-singleton start) break toward the smallest label id. Isolated nodes keep
    their own label. Returns clusters sorted like ``connected_components``.
    """
    nodes = sorted(adjacency)
    labels: dict[str, str] = {node: node for node in nodes}

    for _ in range(max_iter):
        changed = False
        for node in nodes:
            neighbors = adjacency[node]
            if not neighbors:
                continue
            counts = Counter(labels[n] for n in neighbors)
            best = max(counts, key=lambda label: (counts[label], _neg_key(label)))
            if labels[node] != best:
                labels[node] = best
                changed = True
        if not changed:
            break

    clusters: dict[str, list[str]] = {}
    for node in nodes:
        clusters.setdefault(labels[node], []).append(node)
    result = [sorted(members) for members in clusters.values()]
    result.sort(key=lambda c: (-len(c), c[0]))
    return result


def _neg_key(label: str) -> tuple[int, ...]:
    """Sort key that makes ``max`` prefer the lexicographically smallest label."""
    return tuple(-ord(ch) for ch in label)


def _hubs(graph: VaultGraph, members: list[str], *, top: int = 3) -> list[str]:
    """Highest-degree members (tie-break: smallest id) — the cluster's anchors."""
    ranked = sorted(members, key=lambda m: (-graph.degree(m), m))
    return ranked[:top]


def _community_id(graph: VaultGraph, members: list[str]) -> str:
    """Stable MOC slug for a cluster: ``moc-<highest-degree resolved member>``."""
    resolved = [m for m in members if _is_resolved(graph, m)] or members
    representative = sorted(resolved, key=lambda m: (-graph.degree(m), m))[0]
    return f"moc-{representative}"


def _is_resolved(graph: VaultGraph, node_id: str) -> bool:
    node = graph.get_node(node_id)
    return bool(node and node.resolved)


def detect_communities(
    graph: VaultGraph, *, method: str = "label_propagation", min_size: int = 2
) -> list[Community]:
    """Detect communities and wrap them as :class:`Community` (with hub members).

    Clusters smaller than ``min_size`` are dropped — a lone note is not a
    "Map of Content" worth summarizing.
    """
    adjacency = graph.adjacency()
    if method == "connected_components":
        clusters = connected_components(adjacency)
    elif method == "label_propagation":
        clusters = label_propagation(adjacency)
    else:  # pragma: no cover - guarded by CLI choices
        raise ValueError(f"unknown community method: {method!r}")

    communities: list[Community] = []
    for members in clusters:
        if len(members) < min_size:
            continue
        communities.append(
            Community(
                id=_community_id(graph, members),
                members=members,
                hubs=_hubs(graph, members),
            )
        )
    return communities
