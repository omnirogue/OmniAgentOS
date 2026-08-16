from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.contracts import (
    AgentUsage,
    HarnessProfile,
    HarnessType,
    NoteType,
    RunManifest,
    RunState,
    VaultFrontmatter,
)
from omniagentos.lab.curator import rollup
from omniagentos.vault import render_frontmatter


def _manifest(
    run_id: str, discipline: str | None, state: RunState = RunState.COMPLETED
) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        task_id=f"t-{run_id}",
        discipline=discipline,
        state=state,
        harness=HarnessProfile(harness=HarnessType.MOCK),
        usage=AgentUsage(wall_ms=5, input_tokens=1, output_tokens=1, cost_usd=0.1),
    )


def test_summarize_runs_groups_by_state_and_discipline_and_sums_usage() -> None:
    manifests = [
        _manifest("a", "copywriting"),
        _manifest("b", "copywriting", RunState.FAILED),
        _manifest("c", None),
    ]
    result = rollup.summarize_runs(manifests)
    assert result["count"] == 3
    assert result["by_state"] == {"completed": 2, "failed": 1}
    assert result["by_discipline"] == {"(none)": 1, "copywriting": 2}
    assert result["total_est_cost_usd"] == pytest.approx(0.3)
    assert result["total_est_tokens"] == 6


def test_summarize_runs_of_an_empty_ledger() -> None:
    assert rollup.summarize_runs([]) == {
        "count": 0,
        "by_state": {},
        "by_discipline": {},
        "total_est_cost_usd": 0.0,
        "total_est_tokens": 0,
    }


def test_summarize_experiments_groups_by_status_disposition_discipline() -> None:
    experiments = [
        {"status": "decided", "disposition": "promote", "discipline": "copywriting"},
        {"status": "proposed", "disposition": None, "discipline": "copywriting"},
        {"status": "decided", "disposition": "reject", "discipline": "coding"},
    ]
    result = rollup.summarize_experiments(experiments)
    assert result["count"] == 3
    assert result["by_status"] == {"decided": 2, "proposed": 1}
    assert result["by_disposition"] == {"promote": 1, "reject": 1}
    assert result["by_discipline"] == {"coding": 1, "copywriting": 2}


def test_summarize_playbook_groups_by_discipline_and_confidence() -> None:
    entries = [
        {"discipline": "copywriting", "confidence": "high"},
        {"discipline": "copywriting", "confidence": "medium"},
    ]
    assert rollup.summarize_playbook(entries) == {
        "count": 2,
        "by_discipline": {"copywriting": 2},
        "by_confidence": {"high": 1, "medium": 1},
    }


def test_scan_vault_context_returns_empty_for_a_missing_dir(tmp_path: Path) -> None:
    assert rollup.scan_vault_context(str(tmp_path / "does-not-exist")) == []


def test_scan_vault_context_finds_typed_notes_and_skips_the_rest(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    (vault_dir / "tournaments").mkdir(parents=True)
    tournament_fm = VaultFrontmatter(id="tnm_1", type=NoteType.TOURNAMENT, discipline="copywriting")
    (vault_dir / "tournaments" / "tnm_1.md").write_text(
        render_frontmatter(tournament_fm) + "\n# tournament: tnm_1\n"
    )
    (vault_dir / "README.md").write_text("no frontmatter here at all")
    (vault_dir / "disciplines").mkdir()
    discipline_fm = VaultFrontmatter(id="copywriting", type=NoteType.DISCIPLINE)
    (vault_dir / "disciplines" / "copywriting.md").write_text(
        render_frontmatter(discipline_fm) + "\nbody\n"
    )

    found = rollup.scan_vault_context(str(vault_dir))

    assert found == [
        {
            "path": "tournaments/tnm_1.md",
            "id": "tnm_1",
            "type": "tournament",
            "discipline": "copywriting",
        }
    ]


def test_scan_vault_context_respects_limit(tmp_path: Path) -> None:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    for index in range(5):
        fm = VaultFrontmatter(id=f"exp_{index}", type=NoteType.EXPERIMENT)
        (vault_dir / f"exp_{index}.md").write_text(render_frontmatter(fm) + "\nbody\n")

    assert len(rollup.scan_vault_context(str(vault_dir), limit=2)) == 2
