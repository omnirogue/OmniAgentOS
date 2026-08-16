"""Rank a repo's symbols by importance — the repo-map's core.

The algorithm follows Aider's repo-map (Paul Gauthier, github.com/Aider-AI/aider):
extract tags → build a graph where a file that REFERENCES a symbol points at the file
that DEFINES it → run (personalized) PageRank so the most depended-upon files/symbols
float to the top → distribute each file's rank across the symbols it defines, weighted
by how often each is referenced. Personalization biases the whole ranking toward the
files/terms the current TASK mentions, so the map is task-aware, not generic.

Dependency-free: PageRank is a ~40-line power iteration (no networkx).
"""

from __future__ import annotations

from collections import defaultdict

from omniagentos.repomap.tags import Definition, FileTags

# A reference only creates a graph edge when it names a DISTINCTIVE symbol. A name
# defined in more than a few files (``get``/``list``/``row``/``path`` — every DAL has
# them) carries no locality signal and just inflates unrelated files; ignore it. This
# replaces tree-sitter's precise def/ref typing that a stdlib-ast + attr-name approach
# can't get, and is what turns a noisy symbol soup into a sharp map.
_MAX_DEFINERS = 3
# A name referenced more than this many times across the whole repo is a ubiquitous
# attribute/method (``path``, ``get``, ``self``, ``text``) — defined in few files yet
# used everywhere — so it carries no locality signal. Self-tuning; complements the
# hardcoded stoplist.
_MAX_TOTAL_REFS = 50
_STOP_NAMES: frozenset[str] = frozenset(
    {
        "__init__",
        "__enter__",
        "__exit__",
        "__repr__",
        "__str__",
        "__eq__",
        "__hash__",
        "__call__",
        "__len__",
        "__iter__",
        "__next__",
        "__post_init__",
        "setUp",
        "tearDown",
        "main",
        "run",
        "close",
        "get",
        "set",
        "list",
        "add",
    }
)


def common_names(all_tags: list[FileTags], threshold: int = _MAX_TOTAL_REFS) -> frozenset[str]:
    """Names referenced too often across the repo to be a useful locality signal."""
    totals: dict[str, int] = defaultdict(int)
    for file_tags in all_tags:
        for name, count in file_tags.references.items():
            totals[name] += count
    return frozenset(name for name, total in totals.items() if total > threshold)


def _distinct_definers(
    name: str, index: dict[str, set[str]], self_path: str, common: frozenset[str]
) -> tuple[str, ...]:
    """Files that define ``name`` — but only if ``name`` is DISTINCTIVE: not too short,
    not a stopword, not repo-ubiquitous, and defined in only a few files. Excludes
    ``self_path``. Ambiguous/common names create no graph edge (that's the whole trick
    that turns a stdlib-ast symbol soup into a sharp map)."""
    if len(name) <= 2 or name in _STOP_NAMES or name in common:
        return ()
    definers = index.get(name)
    if not definers or len(definers) > _MAX_DEFINERS:
        return ()
    return tuple(definer for definer in definers if definer != self_path)


def build_index(all_tags: list[FileTags]) -> dict[str, set[str]]:
    """``symbol name -> {files defining it}``.

    Methods are indexed under BOTH their qualified name (``Class.method``) and their
    bare final component (``method``), so a plain cross-file reference still resolves."""
    index: dict[str, set[str]] = defaultdict(set)
    for file_tags in all_tags:
        for definition in file_tags.definitions:
            index[definition.name].add(file_tags.rel_path)
            base = definition.name.rsplit(".", 1)[-1]
            index[base].add(file_tags.rel_path)
    return index


def build_graph(
    all_tags: list[FileTags], index: dict[str, set[str]], common: frozenset[str]
) -> tuple[set[str], dict[str, dict[str, float]]]:
    """File dependency graph: ``out_edges[referencer][definer] += weight``.

    Only DISTINCTIVE references create edges (see ``_distinct_definers``); a reference
    to a symbol defined in N files splits its weight across those N; self-references
    are ignored."""
    out_edges: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    nodes: set[str] = set()
    for file_tags in all_tags:
        nodes.add(file_tags.rel_path)
        for name, count in file_tags.references.items():
            targets = _distinct_definers(name, index, file_tags.rel_path, common)
            if not targets:
                continue
            weight = count / len(targets)
            for definer in targets:
                out_edges[file_tags.rel_path][definer] += weight
                nodes.add(definer)
    return nodes, out_edges


def pagerank(
    nodes: set[str],
    out_edges: dict[str, dict[str, float]],
    personalization: dict[str, float] | None = None,
    damping: float = 0.85,
    iterations: int = 60,
    tolerance: float = 1e-8,
) -> dict[str, float]:
    """Personalized PageRank via power iteration. Rank flows referencer → definer, so
    the most depended-upon files score highest. ``personalization`` is the teleport
    distribution (task-relevant files); ``None`` = uniform."""
    node_list = list(nodes)
    total = len(node_list)
    if total == 0:
        return {}

    if personalization:
        mass = sum(max(0.0, personalization.get(node, 0.0)) for node in node_list)
        if mass <= 0:
            teleport = {node: 1.0 / total for node in node_list}
        else:
            teleport = {node: max(0.0, personalization.get(node, 0.0)) / mass for node in node_list}
    else:
        teleport = {node: 1.0 / total for node in node_list}

    out_weight = {node: sum(out_edges.get(node, {}).values()) for node in node_list}
    in_edges: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for src, dsts in out_edges.items():
        for dst, weight in dsts.items():
            in_edges[dst].append((src, weight))

    rank = {node: 1.0 / total for node in node_list}
    for _ in range(iterations):
        dangling = sum(rank[node] for node in node_list if out_weight[node] == 0.0)
        updated: dict[str, float] = {}
        for node in node_list:
            incoming = 0.0
            for src, weight in in_edges.get(node, ()):
                src_out = out_weight[src]
                if src_out > 0.0:
                    incoming += rank[src] * (weight / src_out)
            updated[node] = (1.0 - damping) * teleport[node] + damping * (
                incoming + dangling * teleport[node]
            )
        norm = sum(updated.values()) or 1.0
        updated = {node: value / norm for node, value in updated.items()}
        if max(abs(updated[node] - rank[node]) for node in node_list) < tolerance:
            rank = updated
            break
        rank = updated
    return rank


def personalization_vector(
    all_tags: list[FileTags],
    focus_files: list[str] | None,
    focus_terms: list[str] | None,
) -> dict[str, float] | None:
    """Bias the ranking toward the current task: the files it names, and the files that
    DEFINE any symbol/term it mentions. ``None`` when the task named nothing → generic map."""
    wanted_files = [f.strip() for f in (focus_files or []) if f.strip()]
    wanted_terms = {t.strip().lower() for t in (focus_terms or []) if t.strip()}
    if not wanted_files and not wanted_terms:
        return None
    vector: dict[str, float] = {}
    for file_tags in all_tags:
        weight = 0.0
        rel = file_tags.rel_path
        if any(rel == f or rel.endswith("/" + f) or f in rel for f in wanted_files):
            weight += 10.0
        if wanted_terms:
            for definition in file_tags.definitions:
                base = definition.name.rsplit(".", 1)[-1].lower()
                if base in wanted_terms or definition.name.lower() in wanted_terms:
                    weight += 5.0
        if weight > 0.0:
            vector[rel] = weight
    return vector or None


def rank_definitions(
    all_tags: list[FileTags],
    index: dict[str, set[str]],
    file_rank: dict[str, float],
    common: frozenset[str],
) -> list[Definition]:
    """Every definition, ordered most-important first. File PageRank is primary (a
    symbol in a central file matters); inbound distinctive cross-file references are the
    secondary boost; generic names are down-weighted so classes/named functions lead."""
    inbound: dict[tuple[str, str], float] = defaultdict(float)
    for file_tags in all_tags:
        for name, count in file_tags.references.items():
            targets = _distinct_definers(name, index, file_tags.rel_path, common)
            if not targets:
                continue
            share = count / len(targets)
            for definer in targets:
                inbound[(definer, name)] += share

    ranked: list[tuple[float, Definition]] = []
    for file_tags in all_tags:
        base_rank = file_rank.get(file_tags.rel_path, 0.0)
        for definition in file_tags.definitions:
            base = definition.name.rsplit(".", 1)[-1]
            inbound_weight = inbound.get((file_tags.rel_path, definition.name), 0.0) + inbound.get(
                (file_tags.rel_path, base), 0.0
            )
            # File importance is primary (a symbol in a central file matters); inbound
            # cross-file references are the secondary boost. A generic name (a common
            # method like ``row``/``process``) is down-weighted so distinctive symbols
            # -- classes, named functions -- surface first within a file.
            penalty = 0.15 if base in _STOP_NAMES or len(base) <= 3 else 1.0
            ranked.append((base_rank * (inbound_weight + 1.0) * penalty, definition))

    ranked.sort(key=lambda item: item[0], reverse=True)
    return [definition for _, definition in ranked]
