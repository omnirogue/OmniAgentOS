from __future__ import annotations

import sys
from pathlib import Path

import pytest

from omniagentos.vaultgraph.parser import (
    extract_wikilinks,
    parse_note,
    slugify_path,
    slugify_target,
    walk_vault,
)


def test_slugify_strips_path_anchor_and_case() -> None:
    assert slugify_target("capabilities/Web-Research#Facts") == "web-research"
    assert slugify_target("note^block-ref") == "note"
    assert slugify_target("  Model-A  ") == "model-a"


def test_extract_wikilinks_handles_alias_and_escaped_pipe() -> None:
    text = "See [[hub]] and [[capability-speed|Speed]] and [[coding\\|Coding]] in a table."
    refs = extract_wikilinks(text)
    targets = [(r.target, r.alias) for r in refs]
    assert ("hub", None) in targets
    assert ("capability-speed", "Speed") in targets
    assert ("coding", "Coding") in targets


def test_parse_note_uses_frontmatter_id_and_first_heading() -> None:
    content = "---\nid: model-a\ntype: source\n---\n\n# source: Model A\n\nBest at [[capability-speed|Speed]].\n"
    note = parse_note("models/model-a.md", content)
    assert note.id == "model-a"
    assert note.ntype == "source"
    assert note.title == "source: Model A"
    assert any(r.target == "capability-speed" for r in note.refs)


def test_parse_note_frontmatter_supersedes_becomes_edge() -> None:
    content = "---\nid: model-a-v2\ntype: source\nsupersedes: model-a\n---\n\n# v2\n"
    note = parse_note("models/model-a-v2.md", content)
    ref = next(r for r in note.refs if r.kind.startswith("frontmatter"))
    assert ref.kind == "frontmatter:supersedes"
    assert ref.target == "model-a"


def test_parse_note_missing_frontmatter_falls_back_to_stem() -> None:
    note = parse_note("orphan.md", "# Orphan\n\nNo frontmatter here [[hub]].\n")
    assert note.id == "orphan"
    assert note.title == "Orphan"


def test_walk_vault_reads_every_note(fixture_vault) -> None:  # type: ignore[no-untyped-def]
    notes = walk_vault(fixture_vault)
    ids = {n.id for n in notes}
    assert {"home", "hub", "model-a", "model-b", "capability-speed", "gardening"} <= ids


# -- F9: non-rendered regions must not produce edges --------------------------


def test_code_fence_and_comment_links_are_not_edges() -> None:
    content = (
        "# Doc\n\nReal [[hub]] link.\n\n"
        "```md\n[[not-a-real-edge]]\n```\n\n"
        "<!-- [[also-not-an-edge]] -->\n\n"
        "Inline `[[nope]]` code and a table [[coding\\|Coding]].\n"
    )
    targets = {r.target for r in extract_wikilinks(content)}
    assert "hub" in targets
    assert "coding" in targets  # a real link outside code still counts
    assert "not-a-real-edge" not in targets
    assert "also-not-an-edge" not in targets
    assert "nope" not in targets


def test_unterminated_code_fence_masks_to_end() -> None:
    content = "Real [[hub]].\n```\n[[hidden]]\nno closing fence"
    targets = {r.target for r in extract_wikilinks(content)}
    assert targets == {"hub"}


# -- F6: Unicode normalization of identity ------------------------------------


def test_slugify_normalizes_unicode_nfc_and_case() -> None:
    nfc = "Café"  # composed
    nfd = "Café"  # decomposed
    assert slugify_target(nfc) == slugify_target(nfd)
    assert slugify_target("日本語") == "日本語"


def test_slugify_path_preserves_folders() -> None:
    assert slugify_path("One/Shared#Heading") == "one/shared"


# -- F5: malformed / pathological frontmatter is quarantined, never fatal -----


def test_deeply_nested_frontmatter_does_not_crash() -> None:
    payload = "---\nx: " + ("[" * 1500) + ("]" * 1500) + "\n---\n# Deep\n[[hub]]\n"
    note = parse_note("deep.md", payload)  # must not raise RecursionError
    assert note.id == "deep"
    assert any(r.target == "hub" for r in note.refs)


def test_missing_closing_delimiter_frontmatter_is_lenient() -> None:
    note = parse_note("bad.md", "---\nid: x\ntype: source\n# no closing\n[[hub]]\n")
    # No valid frontmatter block -> id falls back to stem, body links still found.
    assert note.id == "bad"
    assert any(r.target == "hub" for r in note.refs)


def test_alias_heavy_frontmatter_is_indexed() -> None:
    note = parse_note("m.md", "---\nid: m\naliases: [Model One, M1, Primary Model]\n---\n# M\n")
    assert set(note.aliases) == {"model one", "m1", "primary model"}


def test_walk_vault_skips_malformed_note_but_keeps_others(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text("---\nid: good\n---\n# Good\n", encoding="utf-8")
    (tmp_path / "deep.md").write_text(
        "---\nx: " + ("[" * 1500) + ("]" * 1500) + "\n---\n# Deep\n", encoding="utf-8"
    )
    ids = {n.id for n in walk_vault(tmp_path)}
    assert "good" in ids  # the walk did not abort on the pathological note


# -- F8: symlink traversal must not escape the vault --------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_walk_vault_ignores_symlink_to_outside_file(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "real.md").write_text("---\nid: real\n---\n# Real\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("# TOPSECRET\n\n[[hub]]\n", encoding="utf-8")
    (vault / "leak.md").symlink_to(outside)

    notes = walk_vault(vault)
    ids = {n.id for n in notes}
    assert "real" in ids
    assert "leak" not in ids  # symlinked note is not ingested
    assert all("TOPSECRET" not in n.title for n in notes)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_walk_vault_ignores_symlinked_directory(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "real.md").write_text("---\nid: real\n---\n# Real\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    (external / "evil.md").write_text("# Evil\n\n[[hub]]\n", encoding="utf-8")
    (vault / "linked").symlink_to(external, target_is_directory=True)

    ids = {n.id for n in walk_vault(vault)}
    assert "real" in ids
    assert "evil" not in ids
