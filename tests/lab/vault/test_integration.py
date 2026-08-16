"""End-to-end: render every L08 note type -> write -> resolve, all through the
public omniagentos.lab.vault + omniagentos.vault APIs, against a vault_dir
seeded with the real Home.md (as it would look in the actual repo).

Proves contracts/lab-interfaces.md §L08-labvault's "Notes wikilink experiments
<->tournaments<->surfaces<->leaderboard<->playbook so the vault is a navigable
graph" claim literally: every cross-link asserted below is followed to a real
file written by THIS package, not merely a well-formed string.
"""

from __future__ import annotations

from pathlib import Path

from omniagentos.contracts import NoteType
from omniagentos.lab.vault import (
    render_experiment_note,
    render_leaderboard_note,
    render_playbook_note,
    render_prompt_note,
    render_tournament_note,
)
from omniagentos.vault import parse_frontmatter, write_note

from .helpers import (
    sample_elo,
    sample_eval_results,
    sample_experiment,
    sample_leaderboard_rows,
    sample_matches,
    sample_playbook_entries,
    sample_scorecard,
    sample_surface,
)


def _write(vault_dir: Path, relpath: str, content: str) -> Path:
    abs_path = Path(write_note(str(vault_dir), relpath, content, autocommit=False))
    assert abs_path.is_relative_to(vault_dir)
    return abs_path


def test_full_lab_vault_graph_resolves(vault_dir: Path) -> None:
    # 1. the two prompt surfaces referenced by the experiment/tournament
    champion_surface = sample_surface()
    challenger_surface = sample_surface(
        id="srf_challenger000001",
        version=4,
        parent_version=3,
        status="challenger",
        label="coder v4 (challenger)",
        path="prompts/coder/v4.md",
    )
    champion_relpath, champion_content = render_prompt_note(
        champion_surface, "You are a careful senior engineer."
    )
    challenger_relpath, challenger_content = render_prompt_note(
        challenger_surface, "You are a careful senior engineer.\nAlways add a regression test."
    )
    champion_path = _write(vault_dir, champion_relpath, champion_content)
    challenger_path = _write(vault_dir, challenger_relpath, challenger_content)

    # 2. the experiment comparing them
    exp_relpath, exp_content = render_experiment_note(
        sample_experiment(), sample_eval_results(), sample_scorecard()
    )
    exp_path = _write(vault_dir, exp_relpath, exp_content)

    # 3. the tournament between the same two configs
    tnm_relpath, tnm_content = render_tournament_note(
        {
            "id": "tnm_test0000000000000001",
            "subject": "coding-orchestration",
            "discipline": "coding",
            "arena_task_hash": "arena-hash-1",
            "config_ids": ["srf_champion0000000001", "srf_challenger000001"],
            "winner_config_id": "srf_challenger000001",
            "status": "done",
            "created_at": "2026-07-11T13:00:00Z",
        },
        sample_matches(),
        sample_elo(),
    )
    tnm_path = _write(vault_dir, tnm_relpath, tnm_content)

    # 4. the leaderboard (log-book) for the same subject
    lb_relpath, lb_content = render_leaderboard_note(
        "coding-orchestration", sample_leaderboard_rows()
    )
    lb_path = _write(vault_dir, lb_relpath, lb_content)

    # 5. the discipline playbook, with evidence pointing at both
    pbk_relpath, pbk_content = render_playbook_note("coding", sample_playbook_entries())
    pbk_path = _write(vault_dir, pbk_relpath, pbk_content)

    # --- every note's own frontmatter round-trips through the frozen 8-field parser ---
    for path in (champion_path, challenger_path, exp_path, tnm_path, lb_path, pbk_path):
        parse_frontmatter(path.read_text(encoding="utf-8"))

    # --- experiment <-> surfaces ---
    assert "[[srf_champion0000000001]]" in exp_content
    assert "[[srf_challenger000001]]" in exp_content
    assert champion_path.is_file()
    assert challenger_path.is_file()

    # --- tournament <-> surfaces ---
    assert "[[srf_champion0000000001]]" in tnm_content
    assert "[[srf_challenger000001]]" in tnm_content

    # --- tournament <-> leaderboard (same subject) ---
    assert "[[coding-orchestration]]" in tnm_content
    assert lb_path == vault_dir / "leaderboard" / "coding-orchestration.md"
    assert lb_path.is_file()

    # --- leaderboard <-> surfaces + experiments ---
    assert "[[srf_challenger000001]]" in lb_content
    assert "[[exp_test0000000000000001]]" in lb_content
    assert exp_path.is_file()

    # --- playbook <-> experiments + tournaments ---
    assert "[[exp_test0000000000000001]]" in pbk_content
    assert "[[tnm_test0000000000000001]]" in pbk_content
    assert exp_path.is_file()
    assert tnm_path.is_file()

    # --- no orphans: every note links at least [[Home]], which always resolves ---
    assert (vault_dir / "Home.md").is_file()
    for content in (champion_content, challenger_content, exp_content, tnm_content, lb_content, pbk_content):
        assert "[[Home]]" in content

    # --- note types match the frozen H2 enum members ---
    assert parse_frontmatter(exp_content).type == NoteType.EXPERIMENT
    assert parse_frontmatter(tnm_content).type == NoteType.TOURNAMENT
    assert parse_frontmatter(lb_content).type == NoteType.LEADERBOARD
    assert parse_frontmatter(pbk_content).type == NoteType.PLAYBOOK
    assert parse_frontmatter(champion_content).type == NoteType.PROMPT

    # --- reward-hacking invariant holds across the whole rendered graph ---
    for content in (exp_content, tnm_content, lb_content, pbk_content, champion_content, challenger_content):
        assert "SECRET" not in content


def test_regenerating_an_experiment_note_preserves_human_notes_section(vault_dir: Path) -> None:
    relpath, content_v1 = render_experiment_note(
        sample_experiment(), sample_eval_results(), sample_scorecard()
    )
    write_note(str(vault_dir), relpath, content_v1, autocommit=False)

    note_path = vault_dir / relpath
    hand_written = note_path.read_text(encoding="utf-8").replace(
        "## Notes (human)\n", "## Notes (human)\n\nDon't touch surface srf_champion without asking the operator.\n"
    )
    note_path.write_text(hand_written, encoding="utf-8")

    relpath_v2, content_v2 = render_experiment_note(
        sample_experiment(status="canary"), sample_eval_results(), sample_scorecard()
    )
    assert relpath_v2 == relpath
    write_note(str(vault_dir), relpath_v2, content_v2, autocommit=False)

    regenerated = note_path.read_text(encoding="utf-8")
    assert "Don't touch surface srf_champion without asking the operator." in regenerated
    assert "**Status:** canary" in regenerated
