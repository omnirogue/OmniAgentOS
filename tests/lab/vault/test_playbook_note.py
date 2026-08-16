"""render_playbook_note (contracts/lab-interfaces.md §L08-labvault)."""

from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import NoteType
from omniagentos.lab.vault import render_playbook_note
from omniagentos.vault import parse_frontmatter, write_note

from .helpers import sample_playbook_entries


def test_frontmatter_and_relpath() -> None:
    relpath, content = render_playbook_note("coding", sample_playbook_entries())
    fm = parse_frontmatter(content)
    assert fm.id == "coding"
    assert fm.type == NoteType.PLAYBOOK
    assert fm.discipline == "coding"
    assert relpath == "playbook/coding.md"


def test_validated_traits_sort_before_provisional() -> None:
    _relpath, content = render_playbook_note("coding", sample_playbook_entries())
    validated_idx = content.index("Add a regression-test-writing stage after bugfixes")
    provisional_idx = content.index("Prefer smaller diffs when scores tie")
    assert validated_idx < provisional_idx


def test_wikilinks_to_evidence_experiments_and_tournaments() -> None:
    _relpath, content = render_playbook_note("coding", sample_playbook_entries())
    assert "[[exp_test0000000000000001]]" in content
    assert "[[tnm_test0000000000000001]]" in content
    assert "[[Home]]" in content


def test_entry_with_no_evidence_renders_none_not_a_crash() -> None:
    _relpath, content = render_playbook_note("coding", sample_playbook_entries())
    assert "Prefer smaller diffs when scores tie" in content
    # the provisional/low-confidence entry has no evidence ids at all
    lines = [line for line in content.splitlines() if "Prefer smaller diffs" in line]
    assert lines and "_none_" in lines[0]


def test_empty_entries_still_resolves_via_home_no_orphan() -> None:
    relpath, content = render_playbook_note("empty-discipline", [])
    assert relpath == "playbook/empty-discipline.md"
    assert "_No validated traits recorded yet._" in content
    assert "[[Home]]" in content


def test_full_round_trip_confined_and_resolving(vault_dir: Path) -> None:
    relpath, content = render_playbook_note("coding", sample_playbook_entries())
    abs_path = write_note(str(vault_dir), relpath, content, autocommit=False)
    assert Path(abs_path).is_relative_to(vault_dir)
    parse_frontmatter(Path(abs_path).read_text(encoding="utf-8"))
