from __future__ import annotations

from omniagentos.vaultgraph import VaultGraph
from omniagentos.vaultgraph.graph import build_graph_from_notes
from omniagentos.vaultgraph.parser import parse_note
from omniagentos.vaultgraph.search import local_search


def test_build_graph_registers_file_nodes(graph: VaultGraph) -> None:
    node = graph.get_node("model-a")
    assert node is not None
    assert node.resolved is True
    assert node.relpath == "models/model-a.md"


def test_dangling_link_becomes_unresolved_node(graph: VaultGraph) -> None:
    node = graph.get_node("missing-benchmark")
    assert node is not None
    assert node.resolved is False
    assert node.relpath is None
    stats = graph.stats()
    assert stats["dangling"] >= 1


def test_supersedes_frontmatter_edge_exists(graph: VaultGraph) -> None:
    edge = next(e for e in graph.edges() if e.src == "model-a-v2" and e.dst == "model-a")
    assert edge.kind == "frontmatter:supersedes"


def test_neighbors_are_undirected(graph: VaultGraph) -> None:
    # capability-speed links to the models; the models also link back to it.
    assert "capability-speed" in graph.neighbors("model-a")
    assert "model-a" in graph.neighbors("capability-speed")


def test_local_search_one_hop(graph: VaultGraph) -> None:
    hood = local_search(graph, "model-a", hops=1)
    ids = {n.node.id for n in hood.nodes}
    assert "model-a" in ids
    assert "hub" in ids
    assert "capability-speed" in ids
    # gardening is in another cluster, not within one hop
    assert "gardening" not in ids
    assert all(n.distance <= 1 for n in hood.nodes)


def test_local_search_two_hops_reaches_further(graph: VaultGraph) -> None:
    one = {n.node.id for n in local_search(graph, "model-a", hops=1).nodes}
    two = {n.node.id for n in local_search(graph, "model-a", hops=2).nodes}
    assert one < two  # strictly larger neighborhood
    assert "model-b" in two  # model-a -> capability-speed -> model-b


def test_local_search_lenient_matching(graph: VaultGraph) -> None:
    assert local_search(graph, "Model-A", hops=1).nodes  # slugified match
    assert local_search(graph, "does-not-exist", hops=1).nodes == []


# -- F4: node identity resolves filenames and never merges distinct notes -----


def test_filename_link_resolves_to_note_with_other_id() -> None:
    # A note whose id differs from its filename must still be reachable by a
    # `[[Filename]]` link — not left as a dangling `fancy name` node.
    fancy = parse_note("Fancy Name.md", "---\nid: internal-id\n---\n# Fancy\n")
    other = parse_note("other.md", "# Other\n\nSee [[Fancy Name]].\n")
    graph = build_graph_from_notes([fancy, other])
    try:
        assert {n.id for n in graph.nodes()} == {"internal-id", "other"}
        assert graph.get_node("fancy name") is None  # no dangling duplicate
        assert ("other", "internal-id") in {(e.src, e.dst) for e in graph.edges()}
    finally:
        graph.close()


def test_alias_link_resolves() -> None:
    target = parse_note("m.md", "---\nid: m\naliases: [Primary Model]\n---\n# M\n")
    other = parse_note("o.md", "# O\n\nRefers to [[Primary Model]].\n")
    graph = build_graph_from_notes([target, other])
    try:
        assert ("o", "m") in {(e.src, e.dst) for e in graph.edges()}
    finally:
        graph.close()


def test_same_basename_different_folders_stay_distinct() -> None:
    a = parse_note("one/shared.md", "# One\n\nmeta-one [[alpha]]\n")
    b = parse_note("two/shared.md", "# Two\n\nmeta-two [[beta]]\n")
    graph = build_graph_from_notes([a, b])
    try:
        ids = {n.id for n in graph.nodes()}
        assert {"one/shared", "two/shared"} <= ids  # both survive, not merged
        # each keeps its own metadata + edges
        assert graph.get_node("one/shared").title == "One"  # type: ignore[union-attr]
        assert graph.get_node("two/shared").title == "Two"  # type: ignore[union-attr]
        assert any("collision" in d for d in graph.diagnostics)
    finally:
        graph.close()


def test_path_qualified_link_resolves_before_basename() -> None:
    a = parse_note("one/shared.md", "# One\n")
    b = parse_note("two/shared.md", "# Two\n")
    linker = parse_note("l.md", "# L\n\nSee [[two/shared]].\n")
    graph = build_graph_from_notes([a, b, linker])
    try:
        assert ("l", "two/shared") in {(e.src, e.dst) for e in graph.edges()}
    finally:
        graph.close()


def test_ambiguous_basename_link_is_reported_not_merged() -> None:
    a = parse_note("one/shared.md", "# One\n")
    b = parse_note("two/shared.md", "# Two\n")
    linker = parse_note("l.md", "# L\n\nSee [[shared]].\n")
    graph = build_graph_from_notes([a, b, linker])
    try:
        assert any("ambiguous" in d for d in graph.diagnostics)
    finally:
        graph.close()


# -- F6: NFD link and NFC file collapse to one node ---------------------------


def test_nfd_link_and_nfc_file_are_one_node() -> None:
    cafe = parse_note("Café.md", "---\nid: Café\n---\n# Cafe\n")  # NFC filename+id
    other = parse_note("o.md", "# O\n\nSee [[Café]].\n")  # NFD link target
    graph = build_graph_from_notes([cafe, other])
    try:
        ids = {n.id for n in graph.nodes()}
        assert ids == {"café", "o"}  # single resolved café node, no dangling twin
        assert graph.get_node("café").resolved is True  # type: ignore[union-attr]
    finally:
        graph.close()


# -- F7: local search stays inside the neighborhood ---------------------------


def test_local_search_does_not_scan_full_edge_table(graph: VaultGraph) -> None:
    calls = {"n": 0}
    original = graph.edges

    def tripwire():  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return original()

    graph.edges = tripwire  # type: ignore[method-assign]
    try:
        hood = local_search(graph, "model-a", hops=1)
        assert hood.nodes
        assert calls["n"] == 0  # induced_edges used, not the whole table
    finally:
        graph.edges = original  # type: ignore[method-assign]


def test_local_search_hops_zero_returns_only_center(graph: VaultGraph) -> None:
    hood = local_search(graph, "model-a", hops=0)
    assert [n.node.id for n in hood.nodes] == ["model-a"]


def test_induced_edges_only_returns_internal_edges(graph: VaultGraph) -> None:
    subset = {"model-a", "capability-speed"}
    edges = graph.induced_edges(subset)
    assert edges
    assert all(e.src in subset and e.dst in subset for e in edges)
    assert graph.induced_edges(set()) == []
