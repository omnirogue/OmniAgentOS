"""Tests for the archi-morning system-map diagram (`omniagentos.archdocs.diagram`).

The diagram is a STATIC hand-curated map regenerated verbatim each morning, so
the tests pin its well-formedness rather than its exact content: valid mermaid
shape (starts with `flowchart`, balanced brackets), a legibility node-count
bound, referential integrity (every edge endpoint is a declared node or
subgraph, every subgraph member is a declared node, no id collisions), and the
two output files written by `write_system_map` (raw .mmd + fenced .md).
All filesystem checks use tmp_path — never the real repo.
"""

from __future__ import annotations

from pathlib import Path

from omniagentos.archdocs.diagram import (
    EDGES,
    NODES,
    SUBGRAPHS,
    build_markdown,
    build_mermaid,
    write_system_map,
)

# ---------------------------------------------------------------------------
# Mermaid well-formedness
# ---------------------------------------------------------------------------


def test_mermaid_starts_with_flowchart():
    assert build_mermaid().startswith("flowchart ")


def test_mermaid_brackets_balanced():
    text = build_mermaid()
    for open_ch, close_ch in ("[]", "()", "{}"):
        assert text.count(open_ch) == text.count(close_ch), (
            f"unbalanced {open_ch}{close_ch} in generated mermaid"
        )


def test_mermaid_quotes_balanced_and_labels_clean():
    # Node labels are emitted inside double quotes; brackets/quotes/pipes in a
    # label would break both mermaid parsing and the bracket-balance check.
    assert build_mermaid().count('"') % 2 == 0
    for node_id, label in NODES.items():
        assert not set('[](){}"|') & set(label), (
            f"node {node_id!r} label contains mermaid-breaking characters: {label!r}"
        )


def test_every_subgraph_declared_before_edges():
    # Subgraph blocks must be closed (one `end` per `subgraph`) or every
    # following line is swallowed into the last block.
    lines = build_mermaid().splitlines()
    assert sum(1 for ln in lines if ln.strip().startswith("subgraph ")) == len(SUBGRAPHS)
    assert sum(1 for ln in lines if ln.strip() == "end") == len(SUBGRAPHS)


# ---------------------------------------------------------------------------
# Node-count bound (legibility) + referential integrity
# ---------------------------------------------------------------------------


def test_node_count_bound():
    # The plan bound: enough to be a real map, few enough to render legibly.
    assert 20 <= len(NODES) <= 35


def test_edge_endpoints_are_declared():
    valid_ids = set(NODES) | {sg_id for sg_id, _, _ in SUBGRAPHS}
    for src, dst, style, _label in EDGES:
        assert src in valid_ids, f"edge source {src!r} is not a declared node/subgraph"
        assert dst in valid_ids, f"edge target {dst!r} is not a declared node/subgraph"
        assert style in ("solid", "dashed")


def test_subgraph_members_are_declared_nodes_and_ids_unique():
    seen: set[str] = set()
    for sg_id, _title, members in SUBGRAPHS:
        assert sg_id not in NODES, f"subgraph id {sg_id!r} collides with a node id"
        for node_id in members:
            assert node_id in NODES, f"subgraph {sg_id!r} member {node_id!r} undeclared"
            assert node_id not in seen, f"node {node_id!r} appears in two subgraphs"
            seen.add(node_id)


def test_every_node_rendered_exactly_once():
    text = build_mermaid()
    for node_id, label in NODES.items():
        assert text.count(f'{node_id}["{label}"]') == 1


# ---------------------------------------------------------------------------
# File outputs
# ---------------------------------------------------------------------------


def test_write_system_map_outputs(tmp_path: Path):
    result = write_system_map(tmp_path)

    mmd = tmp_path / "docs" / "architecture" / "system-map.mmd"
    md = tmp_path / "docs" / "architecture" / "system-map.md"
    assert mmd.exists() and md.exists()
    assert result["mmd"] == str(mmd) and result["md"] == str(md)
    assert int(result["nodes"]) == len(NODES)
    assert int(result["edges"]) == len(EDGES)

    assert mmd.read_text(encoding="utf-8") == build_mermaid()

    md_text = md.read_text(encoding="utf-8")
    assert md_text == build_markdown()
    assert "```mermaid\n" in md_text
    assert build_mermaid() in md_text  # the fenced block embeds the diagram verbatim
    # One-paragraph legend before the fence, and the fence is closed.
    assert md_text.index("regenerated verbatim") < md_text.index("```mermaid")
    assert md_text.rstrip().endswith("```")
