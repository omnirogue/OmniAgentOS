"""``VaultGraph`` — an in-process SQLite adjacency store over the vault.

No external graph database and no heavy dependency: the graph lives in an
in-memory ``sqlite3`` connection (``:memory:`` by default) so it is queryable
with plain SQL while staying rebuildable from the vault at any time. Edges are
stored as parsed (directed src -> dst); neighbourhood/community queries treat
the graph as undirected, which matches how a reader traverses `[[wiki-links]]`.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from pathlib import Path

from omniagentos.vaultgraph.contracts import Edge, Node
from omniagentos.vaultgraph.parser import LinkRef, ParsedNote, walk_vault

_SCHEMA = """
CREATE TABLE nodes (
    id       TEXT PRIMARY KEY,
    title    TEXT NOT NULL,
    ntype    TEXT NOT NULL,
    relpath  TEXT,
    resolved INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE edges (
    src   TEXT NOT NULL,
    dst   TEXT NOT NULL,
    kind  TEXT NOT NULL,
    alias TEXT,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX idx_edges_src ON edges(src);
CREATE INDEX idx_edges_dst ON edges(dst);
"""


class VaultGraph:
    """A queryable node/edge view of the vault, backed by in-process SQLite."""

    def __init__(self, database: str = ":memory:") -> None:
        self._conn = sqlite3.connect(database)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # Non-fatal identity problems surfaced during construction (colliding
        # basenames, ambiguous links). Reported, never silently merged (F4).
        self.diagnostics: list[str] = []

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> VaultGraph:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- construction --------------------------------------------------------
    def _add_node(self, node: Node) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO nodes (id, title, ntype, relpath, resolved) "
            "VALUES (?, ?, ?, ?, ?)",
            (node.id, node.title, node.ntype, node.relpath, int(node.resolved)),
        )

    def _add_edge(self, edge: Edge) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO edges (src, dst, kind, alias) VALUES (?, ?, ?, ?)",
            (edge.src, edge.dst, edge.kind, edge.alias),
        )

    def _ensure_dangling(self, node_id: str) -> None:
        """Create a placeholder node for a link target that has no backing file."""
        self._conn.execute(
            "INSERT OR IGNORE INTO nodes (id, title, ntype, relpath, resolved) "
            "VALUES (?, ?, ?, ?, 0)",
            (node_id, node_id, "dangling", None),
        )

    # -- reads ---------------------------------------------------------------
    def _row_to_node(self, row: sqlite3.Row) -> Node:
        return Node(
            id=row["id"],
            title=row["title"],
            ntype=row["ntype"],
            relpath=row["relpath"],
            resolved=bool(row["resolved"]),
        )

    def get_node(self, node_id: str) -> Node | None:
        row = self._conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return self._row_to_node(row) if row else None

    def nodes(self) -> list[Node]:
        rows = self._conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()
        return [self._row_to_node(row) for row in rows]

    def edges(self) -> list[Edge]:
        rows = self._conn.execute("SELECT * FROM edges ORDER BY src, dst, kind").fetchall()
        return [Edge(src=r["src"], dst=r["dst"], kind=r["kind"], alias=r["alias"]) for r in rows]

    def induced_edges(self, node_ids: set[str]) -> list[Edge]:
        """Edges wholly inside ``node_ids`` — queried in SQLite so a local search
        never materializes the full edge table (F7). Returns [] for an empty set."""
        if not node_ids:
            return []
        ids = sorted(node_ids)
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT * FROM edges WHERE src IN ({placeholders}) AND dst IN ({placeholders}) "
            "ORDER BY src, dst, kind",
            (*ids, *ids),
        ).fetchall()
        return [Edge(src=r["src"], dst=r["dst"], kind=r["kind"], alias=r["alias"]) for r in rows]

    def neighbors(self, node_id: str) -> list[str]:
        """Undirected 1-hop neighbours (both `[[link]]` directions), sorted."""
        rows = self._conn.execute(
            "SELECT dst AS other FROM edges WHERE src = ? "
            "UNION SELECT src AS other FROM edges WHERE dst = ? ORDER BY other",
            (node_id, node_id),
        ).fetchall()
        return [r["other"] for r in rows if r["other"] != node_id]

    def adjacency(self) -> dict[str, set[str]]:
        """Full undirected adjacency map — used by community detection."""
        adj: dict[str, set[str]] = defaultdict(set)
        for row in self._conn.execute("SELECT id FROM nodes").fetchall():
            adj.setdefault(row["id"], set())
        for row in self._conn.execute("SELECT src, dst FROM edges").fetchall():
            if row["src"] == row["dst"]:
                continue
            adj[row["src"]].add(row["dst"])
            adj[row["dst"]].add(row["src"])
        return adj

    def degree(self, node_id: str) -> int:
        return len(self.neighbors(node_id))

    def stats(self) -> dict[str, int]:
        n = self._conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
        resolved = self._conn.execute(
            "SELECT COUNT(*) AS c FROM nodes WHERE resolved = 1"
        ).fetchone()["c"]
        e = self._conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]
        return {"nodes": n, "resolved": resolved, "dangling": n - resolved, "edges": e}


def _assign_canonical_ids(
    notes: list[ParsedNote], graph: VaultGraph
) -> list[tuple[ParsedNote, str]]:
    """Give every note a unique canonical id.

    The preferred key is the frontmatter id (or file stem). When two notes would
    claim the same key, both fall back to their path-qualified id so distinct
    notes are never merged by ``INSERT OR REPLACE`` — the collision is reported
    on ``graph.diagnostics`` (F4)."""
    from collections import Counter

    counts = Counter(note.id for note in notes)
    assigned: list[tuple[ParsedNote, str]] = []
    used: set[str] = set()
    for note in notes:
        if counts[note.id] > 1:
            canonical = note.path_slug or note.id
            graph.diagnostics.append(
                f"identity collision on {note.id!r}: {note.relpath} keyed as {canonical!r}"
            )
        else:
            canonical = note.id
        if canonical in used:
            graph.diagnostics.append(
                f"duplicate canonical id {canonical!r} for {note.relpath}; keeping first"
            )
            continue
        used.add(canonical)
        assigned.append((note, canonical))
    return assigned


class _Resolver:
    """Resolves a link target to a canonical node id via explicit indexes.

    Priority mirrors Obsidian: an explicit path wins, then a unique basename,
    then a unique alias, then a unique frontmatter id. Ambiguous matches are
    reported and left dangling rather than silently collapsed (F4)."""

    def __init__(self, assigned: list[tuple[ParsedNote, str]]) -> None:
        self.by_path: dict[str, str] = {}
        self.by_basename: dict[str, set[str]] = {}
        self.by_alias: dict[str, set[str]] = {}
        self.by_fmid: dict[str, set[str]] = {}
        for note, canonical in assigned:
            self.by_path[note.path_slug] = canonical
            self.by_basename.setdefault(note.stem_slug, set()).add(canonical)
            if note.frontmatter_id:
                self.by_fmid.setdefault(note.frontmatter_id, set()).add(canonical)
            for alias in note.aliases:
                self.by_alias.setdefault(alias, set()).add(canonical)

    def resolve(self, ref: LinkRef) -> tuple[str | None, str | None]:
        """Return ``(canonical_id, diagnostic)``; ``canonical_id`` is None when the
        target is dangling or ambiguous."""
        if ref.path_hint is not None:
            hit = self.by_path.get(ref.path_hint)
            if hit is not None:
                return hit, None
            return None, None  # a path-qualified link that names nothing is dangling
        for index in (self.by_basename, self.by_alias, self.by_fmid):
            matches = index.get(ref.target)
            if not matches:
                continue
            if len(matches) == 1:
                return next(iter(matches)), None
            return None, (f"ambiguous link {ref.target!r} matches {sorted(matches)}; left dangling")
        return None, None


def build_graph_from_notes(notes: list[ParsedNote], *, database: str = ":memory:") -> VaultGraph:
    """Assemble a VaultGraph from already-parsed notes.

    Nodes carry a unique canonical id; links resolve through basename / alias /
    frontmatter-id / path indexes so a filename-addressed `[[link]]` reaches a
    note whose id differs from its filename, and same-named notes in different
    folders stay distinct (F4)."""
    graph = VaultGraph(database)
    assigned = _assign_canonical_ids(notes, graph)
    resolver = _Resolver(assigned)

    for note, canonical in assigned:
        graph._add_node(
            Node(id=canonical, title=note.title, ntype=note.ntype, relpath=note.relpath)
        )

    for note, canonical in assigned:
        seen: set[tuple[str, str]] = set()
        for ref in note.refs:
            dst, diagnostic = resolver.resolve(ref)
            if diagnostic is not None:
                graph.diagnostics.append(f"{note.relpath}: {diagnostic}")
            if dst is None:
                dst = ref.target  # dangling / ambiguous target keyed by its slug
                if dst != canonical and graph.get_node(dst) is None:
                    graph._ensure_dangling(dst)
            if dst == canonical or (dst, ref.kind) in seen:
                continue
            seen.add((dst, ref.kind))
            graph._add_edge(Edge(src=canonical, dst=dst, kind=ref.kind, alias=ref.alias))
    graph._conn.commit()
    return graph


def build_graph(vault_dir: str | Path, *, database: str = ":memory:") -> VaultGraph:
    """Walk ``vault_dir`` and build the graph. This is the main entry point."""
    return build_graph_from_notes(walk_vault(vault_dir), database=database)
