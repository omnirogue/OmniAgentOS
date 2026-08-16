"""render_prompt_note (contracts/lab-interfaces.md §L08-labvault)."""

from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import NoteType
from omniagentos.lab.vault import render_prompt_note
from omniagentos.lab.vault.paths import prompt_note_relpath
from omniagentos.vault import parse_frontmatter, write_note

from .helpers import sample_surface

PROMPT_TEXT = "You are a careful senior engineer. Fix bugs with minimal diffs."


def test_frontmatter_and_relpath() -> None:
    relpath, content = render_prompt_note(sample_surface(), PROMPT_TEXT)
    fm = parse_frontmatter(content)
    assert fm.id == "srf_champion0000000001"
    assert fm.type == NoteType.PROMPT
    assert fm.discipline == "coding"
    assert fm.status == "active"  # surface status "champion" -> note "active"
    assert relpath == "prompts/coding/srf_champion0000000001.md"


def test_relpath_never_collides_with_l03_raw_content_path() -> None:
    surface = sample_surface(path="prompts/coder/v3.md")
    relpath, _content = render_prompt_note(surface, PROMPT_TEXT)
    assert relpath != surface["path"]


def test_content_is_embedded_verbatim() -> None:
    _relpath, content = render_prompt_note(sample_surface(), PROMPT_TEXT)
    assert PROMPT_TEXT in content
    assert "## Content" in content


def test_metadata_and_wikilinks() -> None:
    _relpath, content = render_prompt_note(sample_surface(), PROMPT_TEXT)
    assert "**Kind:** prompt" in content
    assert "**Version:** 3 (parent v2)" in content
    assert "`sha256:abc123`" in content
    assert "[[coding]]" in content
    assert "[[Home]]" in content


def test_archived_surface_maps_to_superseded_note_status() -> None:
    _relpath, content = render_prompt_note(sample_surface(status="archived"), PROMPT_TEXT)
    fm = parse_frontmatter(content)
    assert fm.status == "superseded"


def test_draft_surface_maps_to_draft_note_status() -> None:
    _relpath, content = render_prompt_note(sample_surface(status="draft"), PROMPT_TEXT)
    fm = parse_frontmatter(content)
    assert fm.status == "draft"


def test_challenger_surface_maps_to_active_note_status() -> None:
    _relpath, content = render_prompt_note(sample_surface(status="challenger"), PROMPT_TEXT)
    fm = parse_frontmatter(content)
    assert fm.status == "active"


def test_empty_content_does_not_crash() -> None:
    relpath, content = render_prompt_note(sample_surface(), "")
    assert relpath
    assert "_empty prompt content_" in content


def test_partial_dict_does_not_crash() -> None:
    relpath, content = render_prompt_note({"id": "srf_bare"}, "hello")
    assert relpath == prompt_note_relpath("unknown", "srf_bare")
    assert "hello" in content


def test_full_round_trip_confined_and_resolving(vault_dir: Path) -> None:
    relpath, content = render_prompt_note(sample_surface(), PROMPT_TEXT)
    abs_path = write_note(str(vault_dir), relpath, content, autocommit=False)
    assert Path(abs_path).is_relative_to(vault_dir)
    parse_frontmatter(Path(abs_path).read_text(encoding="utf-8"))
