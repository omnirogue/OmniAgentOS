"""render_leaderboard_note (contracts/lab-interfaces.md §L08-labvault) — "the
human-readable log-book". Acceptance: "a leaderboard note shows ranked
orchestrations + judge notes"."""

from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import NoteType
from omniagentos.lab.vault import render_leaderboard_note
from omniagentos.vault import parse_frontmatter, write_note

from .helpers import sample_leaderboard_rows


def test_frontmatter_and_relpath() -> None:
    relpath, content = render_leaderboard_note("coding-orchestration", sample_leaderboard_rows())
    fm = parse_frontmatter(content)
    assert fm.id == "coding-orchestration"
    assert fm.type == NoteType.LEADERBOARD
    assert fm.discipline == "coding"  # common across both sample rows
    assert relpath == "leaderboard/coding-orchestration.md"


def test_ranked_orchestrations_table_sorted_by_rank_regardless_of_input_order() -> None:
    rows = list(reversed(sample_leaderboard_rows()))  # deliberately out of order
    _relpath, content = render_leaderboard_note("coding-orchestration", rows)
    assert "## Ranked orchestrations" in content
    rank1_idx = content.index("| 1 | [[srf_challenger000001]]")
    rank2_idx = content.index("| 2 | [[srf_champion0000000001]]")
    assert rank1_idx < rank2_idx
    assert "Adds a regression-test-writing stage after the fix." in content


def test_judge_notes_section_present_and_distinct_from_table() -> None:
    _relpath, content = render_leaderboard_note("coding-orchestration", sample_leaderboard_rows())
    assert "## Judge notes" in content
    assert "### #1 — [[srf_challenger000001]]" in content
    assert "Consistently catches edge cases; slightly higher cost." in content
    assert "### #2 — [[srf_champion0000000001]]" in content
    assert "Reliable but occasionally misses edge cases." in content


def test_wikilinks_to_configs_and_source_experiments() -> None:
    _relpath, content = render_leaderboard_note("coding-orchestration", sample_leaderboard_rows())
    assert "[[srf_challenger000001]]" in content
    assert "[[srf_champion0000000001]]" in content
    assert "[[exp_test0000000000000001]]" in content
    assert "[[Home]]" in content


def test_empty_rows_still_resolves_via_home_no_orphan() -> None:
    relpath, content = render_leaderboard_note("empty-subject", [])
    assert relpath == "leaderboard/empty-subject.md"
    assert "_No ranked orchestrations recorded yet._" in content
    assert "_No judge notes recorded yet._" in content
    assert "[[Home]]" in content
    fm = parse_frontmatter(content)
    assert fm.discipline is None


def test_full_round_trip_confined_and_resolving(vault_dir: Path) -> None:
    relpath, content = render_leaderboard_note("coding-orchestration", sample_leaderboard_rows())
    abs_path = write_note(str(vault_dir), relpath, content, autocommit=False)
    assert Path(abs_path).is_relative_to(vault_dir)
    parse_frontmatter(Path(abs_path).read_text(encoding="utf-8"))
