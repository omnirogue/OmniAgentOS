from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.vaultgraph import (
    HeuristicLinkSuggester,
    LLMLinkSuggester,
    VaultGraph,
    propose_links,
)
from omniagentos.vaultgraph.contracts import Node


def test_heuristic_suggests_unlinked_mention(graph: VaultGraph, fixture_vault: Path) -> None:
    suggestions = propose_links(graph, "hub", vault_dir=fixture_vault)
    targets = {s.target_note for s in suggestions}
    assert "capability-speed" in targets  # "Speed" appears unlinked in hub.md
    # already-linked model-a / model-b must NOT be suggested again
    assert "model-a" not in targets
    assert "model-b" not in targets


def test_heuristic_skips_text_already_inside_a_link() -> None:
    suggester = HeuristicLinkSuggester()
    candidates = [Node(id="model-a", title="Model A", ntype="source")]
    # 'model-a' only appears inside an existing wikilink -> no suggestion
    out = suggester.suggest("hub", "See [[model-a]] here.", candidates)
    assert out == []


def test_llm_suggester_is_a_stub_until_wired() -> None:
    with pytest.raises(NotImplementedError):
        LLMLinkSuggester().suggest("hub", "text", [])


def test_llm_suggester_parses_injected_backend() -> None:
    captured: dict[str, str] = {}

    def fake_llm(prompt: str) -> str:
        captured["prompt"] = prompt
        return "capability-speed\tSpeed\nunknown-id\tnope"

    suggester = LLMLinkSuggester(llm_fn=fake_llm)
    candidates = [Node(id="capability-speed", title="Speed", ntype="source")]
    out = suggester.suggest("hub", "This note is about Speed.", candidates)
    assert len(out) == 1
    assert out[0].target_note == "capability-speed"
    assert out[0].suggested_markdown == "[[capability-speed|Speed]]"
    assert "CANDIDATES:" in captured["prompt"]


def test_propose_links_accepts_explicit_text(graph: VaultGraph) -> None:
    out = propose_links(graph, "hub", note_text="A note mentioning Speed plainly.")
    assert any(s.target_note == "capability-speed" for s in out)


# -- F10: LLM suggester must ground every mention in the source note ----------


def test_llm_suggester_rejects_hallucinated_mention() -> None:
    def fake_llm(_: str) -> str:
        return "target\tphrase that is not present"

    candidates = [Node(id="target", title="Target", ntype="source")]
    out = LLMLinkSuggester(llm_fn=fake_llm).suggest("src", "actual body", candidates)
    assert out == []


def test_llm_suggester_prefers_unlinked_occurrence() -> None:
    # "Speed" occurs first inside a link, then again unlinked — suggest the latter.
    def fake_llm(_: str) -> str:
        return "capability-speed\tSpeed"

    candidates = [Node(id="capability-speed", title="Speed", ntype="source")]
    text = "Already [[capability-speed|Speed]] here; Speed appears again unlinked."
    out = LLMLinkSuggester(llm_fn=fake_llm).suggest("src", text, candidates)
    assert len(out) == 1
    start = text.index("Speed", text.index("again") - 20)
    assert out[0].suggested_markdown == "[[capability-speed|Speed]]"
    # the mention must map to the unlinked occurrence, not the one inside the link
    assert text[start : start + len("Speed")] == out[0].mention


def test_llm_suggester_skips_mention_only_inside_link() -> None:
    def fake_llm(_: str) -> str:
        return "capability-speed\tSpeed"

    candidates = [Node(id="capability-speed", title="Speed", ntype="source")]
    out = LLMLinkSuggester(llm_fn=fake_llm).suggest(
        "src", "Only [[capability-speed|Speed]] appears here.", candidates
    )
    assert out == []


def test_llm_suggester_deduplicates_repeated_rows() -> None:
    def fake_llm(_: str) -> str:
        return "capability-speed\tSpeed\ncapability-speed\tspeed"

    candidates = [Node(id="capability-speed", title="Speed", ntype="source")]
    out = LLMLinkSuggester(llm_fn=fake_llm).suggest(
        "src", "Speed matters for this note.", candidates
    )
    assert len(out) == 1
