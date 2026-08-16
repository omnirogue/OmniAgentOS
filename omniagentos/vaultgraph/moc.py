"""Generate a "Map of Content" (MOC) summary note per community — the
GraphRAG *community-report* equivalent, expressed as a vault note.

Each MOC is a derived view (``status: draft`` so it visibly invites review) and
is written through ``omniagentos.vault.write_note``, which preserves any
``## Notes (human)`` section on regeneration. MOCs reuse ``NoteType.SOURCE``
(the same type ``Home.md`` uses) so no frozen contract enum has to change.
"""

from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import NoteType, VaultFrontmatter, utc_now_iso
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.vault import render_frontmatter, write_note
from omniagentos.vaultgraph.community import Community
from omniagentos.vaultgraph.contracts import MOC_DIR, MOC_MARKER
from omniagentos.vaultgraph.graph import VaultGraph

__all__ = [
    "MOC_DIR",
    "MOC_MARKER",
    "moc_relpath",
    "render_moc_note",
    "generate_mocs",
]


def moc_relpath(community: Community) -> str:
    return f"{MOC_DIR}/{community.id}.md"


def _link(graph: VaultGraph, node_id: str) -> str:
    node = graph.get_node(node_id)
    if node is None:
        return f"[[{node_id}]]"
    if node.title and node.title.lower() != node_id.lower():
        return f"[[{node_id}|{node.title}]]"
    return f"[[{node_id}]]"


def _representative_title(graph: VaultGraph, community: Community) -> str:
    rep_id = community.id.removeprefix("moc-")
    node = graph.get_node(rep_id)
    return node.title if node and node.title else rep_id


def render_moc_note(
    graph: VaultGraph, community: Community, *, created: str | None = None
) -> tuple[str, str]:
    """Render ``(relpath, content)`` for a community's MOC note."""
    fm = VaultFrontmatter(
        id=community.id,
        type=NoteType.SOURCE,
        discipline=None,
        created=created or utc_now_iso(),
        source_run=None,
        confidence=None,
        status="draft",
        supersedes=None,
    )

    title = _representative_title(graph, community)
    resolved = [m for m in community.members if _is_resolved(graph, m)]
    lines: list[str] = [
        f"# Map of Content: {title}",
        "",
        MOC_MARKER,
        f"_Generated cluster summary — {len(community.members)} notes "
        f"({len(resolved)} resolved). Derived view; edit only the human section below._",
        "",
        "## Hubs",
        "",
    ]
    lines += [f"- {_link(graph, hub)}" for hub in community.hubs] or ["- _(none)_"]
    lines += ["", "## Members", ""]
    lines += [f"- {_link(graph, member)}" for member in community.members]
    lines += ["", "## Notes (human)", ""]

    body = "\n".join(lines) + "\n"
    return moc_relpath(community), render_frontmatter(fm) + "\n" + body


def _is_resolved(graph: VaultGraph, node_id: str) -> bool:
    node = graph.get_node(node_id)
    return bool(node and node.resolved)


def generate_mocs(
    graph: VaultGraph,
    vault_dir: str | Path,
    communities: list[Community],
    *,
    autocommit: bool | None = None,
    created: str | None = None,
) -> list[str]:
    """Write one MOC note per community into ``<vault_dir>/moc/``.

    Returns the list of relative paths written. Existing human sections are
    preserved by ``write_note``.
    """
    written: list[str] = []
    for community in communities:
        relpath, content = render_moc_note(graph, community, created=created)
        write_note(str(vault_dir), relpath, content, autocommit=autocommit)
        written.append(relpath)
    _cleanup_obsolete_mocs(vault_dir, keep=set(written))
    return written


def _cleanup_obsolete_mocs(vault_dir: str | Path, *, keep: set[str]) -> None:
    """Remove previously generated MOC notes that no current community produced,
    so a re-clustered vault does not accumulate stale summaries (F3).

    Only files carrying the generation marker are touched — a human-authored
    note under ``moc/`` is never deleted — and symlinks are refused so cleanup
    cannot be aimed outside the vault."""
    moc_root = Path(vault_dir) / MOC_DIR
    if not moc_root.is_dir():
        return
    root_resolved = Path(vault_dir).resolve()
    for path in moc_root.glob("*.md"):
        rel = f"{MOC_DIR}/{path.name}"
        if rel in keep or path.is_symlink():
            continue
        try:
            if inode_relative_parts_anchored(path.resolve(), root_resolved) is None:
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if MOC_MARKER in text:
            path.unlink()
