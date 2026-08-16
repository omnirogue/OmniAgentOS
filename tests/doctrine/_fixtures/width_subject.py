"""Antichain width stand-in for the fan-out / allocation-simulator case.

Real production analogue: ``omniagentos/allocation/simulator.py`` once used a
root-layer reading (O-11 shape) that collapsed single-root/wide-body DAGs to
``worker_count=1``. The realistic counterfeit is ``max(worker_count, 2)``, which
satisfies "wide ⇒ >= 2" while breaking strict chains.
"""

from __future__ import annotations

from collections import defaultdict, deque


def antichain_width(edges: list[tuple[str, str]], nodes: list[str]) -> int:
    """Return the size of the largest antichain level (Kahn layering width)."""
    preds: dict[str, set[str]] = {n: set() for n in nodes}
    succs: dict[str, set[str]] = defaultdict(set)
    for a, b in edges:
        # a -> b means a precedes b
        succs[a].add(b)
        preds[b].add(a)

    indeg = {n: len(preds[n]) for n in nodes}
    q = deque([n for n in nodes if indeg[n] == 0])
    levels: dict[int, int] = defaultdict(int)
    depth: dict[str, int] = {n: 0 for n in nodes}

    while q:
        # process one wave
        wave = list(q)
        q.clear()
        for n in wave:
            levels[depth[n]] += 1
            for m in succs[n]:
                indeg[m] -= 1
                if indeg[m] == 0:
                    depth[m] = depth[n] + 1
                    q.append(m)

    return max(levels.values()) if levels else 1


def worker_count(edges: list[tuple[str, str]], nodes: list[str]) -> int:
    """Simulated allocation worker_count from DAG width."""
    return antichain_width(edges, nodes)
