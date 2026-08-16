"""render_tournament_note (contracts/lab-interfaces.md §L08-labvault)."""

from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import NoteType
from omniagentos.lab.vault import render_tournament_note
from omniagentos.vault import parse_frontmatter, write_note

from .helpers import sample_elo, sample_matches, sample_tournament


def test_frontmatter_and_relpath() -> None:
    relpath, content = render_tournament_note(sample_tournament(), sample_matches(), sample_elo())
    fm = parse_frontmatter(content)
    assert fm.id == "tnm_test0000000000000001"
    assert fm.type == NoteType.TOURNAMENT
    assert fm.discipline == "coding"
    assert relpath == "tournaments/tnm_test0000000000000001.md"


def test_wikilinks_to_configs_leaderboard_discipline_and_home() -> None:
    _relpath, content = render_tournament_note(
        sample_tournament(), sample_matches(), sample_elo()
    )
    assert "[[srf_champion0000000001]]" in content
    assert "[[srf_challenger000001]]" in content
    assert "[[coding-orchestration]]" in content  # -> the leaderboard note for this subject
    assert "[[coding]]" in content
    assert "[[Home]]" in content


def test_configs_list_separates_each_config_on_its_own_line() -> None:
    _relpath, content = render_tournament_note(
        sample_tournament(), sample_matches(), sample_elo()
    )
    assert "- [[srf_champion0000000001]]" in content
    assert "- [[srf_challenger000001]] — **winner**" in content


def test_matches_table_shows_blind_judge_notes_verbatim() -> None:
    _relpath, content = render_tournament_note(
        sample_tournament(), sample_matches(), sample_elo()
    )
    assert (
        "Challenger's regression tests caught an edge case the champion missed." in content
    )
    assert "| yes |" in content  # blind: True


def test_elo_standings_sorted_by_rating_descending() -> None:
    _relpath, content = render_tournament_note(
        sample_tournament(), sample_matches(), sample_elo()
    )
    challenger_idx = content.index("| 1 | [[srf_challenger000001]] | 1012.0")
    champion_idx = content.index("| 2 | [[srf_champion0000000001]] | 988.0")
    assert challenger_idx < champion_idx


def test_partial_dict_does_not_crash() -> None:
    relpath, content = render_tournament_note({"id": "tnm_bare"}, [], [])
    assert relpath == "tournaments/tnm_bare.md"
    assert "_No configs recorded._" in content
    assert "_No matches recorded._" in content
    assert "_No elo standings recorded._" in content
    assert "[[Home]]" in content


def test_tolerates_pre_decoded_config_ids_list() -> None:
    tnm = sample_tournament(config_ids_json=None)
    tnm["config_ids"] = ["srf_a", "srf_b"]
    _relpath, content = render_tournament_note(tnm, [], [])
    assert "[[srf_a]]" in content
    assert "[[srf_b]]" in content


def test_full_round_trip_confined_and_resolving(vault_dir: Path) -> None:
    relpath, content = render_tournament_note(sample_tournament(), sample_matches(), sample_elo())
    abs_path = write_note(str(vault_dir), relpath, content, autocommit=False)
    assert Path(abs_path).is_relative_to(vault_dir)
    parse_frontmatter(Path(abs_path).read_text(encoding="utf-8"))
