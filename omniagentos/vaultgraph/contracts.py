"""Value types for the vault graph engine (nodes, edges, communities, search
results, fact classification, link suggestions).

These are plain dataclasses / enums — NOT frozen pydantic contracts. The vault
markdown (notes + `[[wiki-links]]` + YAML frontmatter) is the single source of
truth; the graph is a derived, in-process view that is rebuilt from the vault on
demand, so nothing here is persisted to the main SQLite database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

# Edge kinds. Wiki-links are the hand-built knowledge graph; frontmatter refs
# (supersedes / discipline / source_run) are the structured edges.
EDGE_WIKILINK = "wikilink"
EDGE_FRONTMATTER = "frontmatter"

# Where generated Map-of-Content notes live, and the marker every generated MOC
# carries. Defined here (not in ``moc``) so the parser can exclude derived notes
# from graph construction without importing ``moc`` (which would be circular).
MOC_DIR = "moc"
MOC_MARKER = "<!-- omniagentos:vaultgraph:moc -->"


@dataclass(frozen=True, slots=True)
class Node:
    """A vault note (or an unresolved `[[link]]` target).

    ``resolved`` is False for a link target that has no backing ``*.md`` file
    (a dangling link) — those still participate in the graph as connector nodes
    (e.g. hub pages like ``[[model-intelligence]]``) but have no ``relpath``.
    """

    id: str
    title: str
    ntype: str
    relpath: str | None = None
    resolved: bool = True


@dataclass(frozen=True, slots=True)
class Edge:
    src: str
    dst: str
    kind: str  # EDGE_WIKILINK or "frontmatter:<field>"
    alias: str | None = None


@dataclass(frozen=True, slots=True)
class NeighborNode:
    """A node reached from the search center, with its hop distance."""

    node: Node
    distance: int


@dataclass(frozen=True, slots=True)
class Neighborhood:
    """Result of ``local_search`` — the N-hop neighborhood around a center note."""

    center: str
    hops: int
    nodes: list[NeighborNode] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class Community:
    """A detected cluster of notes and its representative ("hub") members.

    ``id`` is the stable Map-of-Content slug (``moc-<representative>``); the MOC
    note is written to ``moc/<id>.md``.
    """

    id: str
    members: list[str]
    hubs: list[str]

    @property
    def size(self) -> int:
        return len(self.members)


@dataclass(frozen=True, slots=True)
class GlobalHit:
    """A ranked Map-of-Content match from ``global_search``."""

    community_id: str
    relpath: str
    title: str
    score: float
    snippet: str


class FactClass(StrEnum):
    """Outcome of classifying an incoming fact against an existing one."""

    NEW = "new"
    DUPLICATE = "duplicate"
    CONTRADICTION = "contradiction"
    UPDATE = "update"


@dataclass(frozen=True, slots=True)
class FactVerdict:
    """The classification plus a human-readable rationale. ``CONTRADICTION`` and
    ``UPDATE`` must never be applied silently — the caller decides."""

    label: FactClass
    reason: str
    overlap: float


@dataclass(frozen=True, slots=True)
class LinkSuggestion:
    """A proposed `[[wiki-link]]` for an unlinked entity mention, awaiting human
    approval. ``suggested_markdown`` is the exact `[[target|mention]]` a reviewer
    would insert in place of ``mention``."""

    source_note: str
    target_note: str
    mention: str
    confidence: float
    reason: str
    suggested_markdown: str
