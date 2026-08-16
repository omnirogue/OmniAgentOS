from __future__ import annotations

from pathlib import Path

from omniagentos.vaultgraph import (
    VaultGraph,
    build_graph,
    detect_communities,
    generate_mocs,
    global_search,
)
from omniagentos.vaultgraph.community import connected_components, label_propagation


def _cluster_of(clusters: list[list[str]], member: str) -> list[str]:
    return next(c for c in clusters if member in c)


def test_connected_components_separates_disjoint_topics(graph: VaultGraph) -> None:
    comps = connected_components(graph.adjacency())
    garden = _cluster_of(comps, "gardening")
    assert set(garden) == {"gardening", "composting"}
    assert "model-a" not in garden


def test_label_propagation_groups_garden_together(graph: VaultGraph) -> None:
    clusters = label_propagation(graph.adjacency())
    garden = _cluster_of(clusters, "gardening")
    assert "composting" in garden
    assert "model-a" not in garden


def test_label_propagation_is_deterministic(graph: VaultGraph) -> None:
    a = label_propagation(graph.adjacency())
    b = label_propagation(graph.adjacency())
    assert a == b


def test_detect_communities_drops_singletons(graph: VaultGraph) -> None:
    comms = detect_communities(graph, method="connected_components", min_size=2)
    assert all(c.size >= 2 for c in comms)
    ids = {c.id for c in comms}
    assert any(cid.startswith("moc-") for cid in ids)
    for community in comms:
        assert community.hubs
        assert set(community.hubs) <= set(community.members)


def test_generate_mocs_writes_notes_and_preserves_human_section(tmp_vault: Path) -> None:
    graph = build_graph(tmp_vault)
    try:
        comms = detect_communities(graph, method="connected_components")
        written = generate_mocs(graph, tmp_vault, comms)
    finally:
        graph.close()

    assert written
    for relpath in written:
        note = (tmp_vault / relpath).read_text(encoding="utf-8")
        assert note.startswith("---\n")  # valid frontmatter
        assert "## Members" in note
        assert "omniagentos:vaultgraph:moc" in note

    # Human edit survives regeneration.
    target = tmp_vault / written[0]
    edited = target.read_text(encoding="utf-8").replace(
        "## Notes (human)\n", "## Notes (human)\n\nHUMAN INSIGHT HERE\n"
    )
    target.write_text(edited, encoding="utf-8")

    graph2 = build_graph(tmp_vault)
    try:
        comms2 = detect_communities(graph2, method="connected_components")
        generate_mocs(graph2, tmp_vault, comms2)
    finally:
        graph2.close()
    assert "HUMAN INSIGHT HERE" in target.read_text(encoding="utf-8")


def test_global_search_ranks_mocs(tmp_vault: Path) -> None:
    graph = build_graph(tmp_vault)
    try:
        comms = detect_communities(graph, method="connected_components")
        generate_mocs(graph, tmp_vault, comms)
    finally:
        graph.close()

    hits = global_search("composting gardening", tmp_vault)
    assert hits
    assert "compost" in hits[0].snippet.lower() or "garden" in hits[0].title.lower()


def test_global_search_empty_without_mocs(tmp_vault: Path) -> None:
    assert global_search("anything", tmp_vault) == []


# -- F3: MOC generation is idempotent; generated notes never re-enter graph ---


def _cycle(vault: Path) -> tuple[list[str], list[str]]:
    """Run one generate-then-rebuild cycle; return (written paths, node ids)."""
    graph = build_graph(vault)
    try:
        comms = detect_communities(graph, method="connected_components")
        written = generate_mocs(graph, vault, comms)
    finally:
        graph.close()
    rebuilt = build_graph(vault)
    try:
        nodes = sorted(n.id for n in rebuilt.nodes())
    finally:
        rebuilt.close()
    return sorted(written), nodes


def test_moc_generation_is_idempotent_across_two_cycles(tmp_vault: Path) -> None:
    written1, nodes1 = _cycle(tmp_vault)
    written2, nodes2 = _cycle(tmp_vault)
    assert written1 == written2  # identical paths
    assert nodes1 == nodes2  # identical nodes


def test_generated_mocs_are_not_ingested_as_source_nodes(tmp_vault: Path) -> None:
    graph = build_graph(tmp_vault)
    try:
        comms = detect_communities(graph, method="connected_components")
        generate_mocs(graph, tmp_vault, comms)
    finally:
        graph.close()

    rebuilt = build_graph(tmp_vault)
    try:
        # No node should be a generated MOC (e.g. a "moc-moc-..." recursion).
        assert not any(n.id.startswith("moc-") for n in rebuilt.nodes())
    finally:
        rebuilt.close()
