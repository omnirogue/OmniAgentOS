from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omniagentos.vaultgraph import build_graph, detect_communities, generate_mocs
from omniagentos.vaultgraph.search import _terms, global_search

# -- F6: Unicode-aware tokenization -------------------------------------------


def test_terms_tokenizes_non_latin() -> None:
    assert _terms("日本語") == ["日本語"]


def test_terms_keeps_accented_words_intact() -> None:
    # Previously produced the corrupted tokens ['caf', 'r', 'sum'].
    assert _terms("Café résumé") == ["café", "résumé"]


def test_terms_casefolds_unicode() -> None:
    assert _terms("CAFÉ") == _terms("café")


def test_global_search_matches_non_ascii_query(tmp_vault: Path) -> None:
    # Seed a MOC-like note with accented content and confirm it is searchable.
    graph = build_graph(tmp_vault)
    try:
        comms = detect_communities(graph, method="connected_components")
        generate_mocs(graph, tmp_vault, comms)
    finally:
        graph.close()
    moc = next((tmp_vault / "moc").glob("*.md"))
    moc.write_text(moc.read_text(encoding="utf-8") + "\n\nrésumé café naïve\n", encoding="utf-8")
    hits = global_search("résumé", tmp_vault)
    assert any("résumé" in h.snippet.lower() for h in hits)


# -- F8: global search must not follow symlinks out of the vault --------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_global_search_ignores_symlinked_moc(tmp_vault: Path, tmp_path: Path) -> None:
    graph = build_graph(tmp_vault)
    try:
        comms = detect_communities(graph, method="connected_components")
        generate_mocs(graph, tmp_vault, comms)
    finally:
        graph.close()

    outside = tmp_path / "external_secret.md"
    outside.write_text("# Leak\n\nSUPERSECRETTOKEN composting gardening\n", encoding="utf-8")
    (tmp_vault / "moc" / "leak.md").symlink_to(outside)

    hits = global_search("SUPERSECRETTOKEN", tmp_vault)
    assert all("SUPERSECRETTOKEN" not in h.snippet for h in hits)
