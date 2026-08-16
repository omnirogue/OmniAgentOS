from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import (
    AgentUsage,
    HarnessProfile,
    HarnessType,
    RunManifest,
    RunState,
)
from omniagentos.lab.contracts import (
    ChampionEntry,
    Elo,
    Experiment,
    MatchResult,
    PlaybookEntry,
    Surface,
    SurfaceKind,
    Tournament,
)
from omniagentos.lab.curator.curate import DEFAULT_TOP_N, curate
from omniagentos.lab.db import LabStore


def _seed_store() -> LabStore:
    """A realistic in-memory lab db: one subject (`headline-copy`, discipline
    `copywriting`) with two orchestration genomes, a tournament + match with
    judge notes, elo ratings, one decided experiment, and one playbook entry."""
    store = LabStore(":memory:")
    store.create_surface(
        Surface(
            id="srf_pipeline",
            kind=SurfaceKind.ORCHESTRATION_GENOME,
            discipline="copywriting",
            path="configs/genomes/srf_pipeline.json",
            content_hash="hash-pipeline",
            label="Pipeline v2",
        )
    )
    store.create_surface(
        Surface(
            id="srf_panel",
            kind=SurfaceKind.ORCHESTRATION_GENOME,
            discipline="copywriting",
            path="configs/genomes/srf_panel.json",
            content_hash="hash-panel",
            label="Blind panel v1",
        )
    )
    store.create_tournament(
        Tournament(
            id="tnm_1",
            subject="headline-copy",
            discipline="copywriting",
            arena_task_hash="arena-hash-1",
            config_ids=["srf_pipeline", "srf_panel"],
            status="done",
            winner_config_id="srf_panel",
        )
    )
    store.record_match(
        MatchResult(
            id="mch_1",
            tournament_id="tnm_1",
            config_a="srf_pipeline",
            config_b="srf_panel",
            winner="srf_panel",
            score_a=0.0,
            score_b=1.0,
            judge_notes="Panel's headline was punchier and scored higher on clarity.",
        )
    )
    store.upsert_elo(
        Elo(
            subject="headline-copy",
            config_id="srf_panel",
            rating=1032,
            matches=3,
            wins=3,
            losses=0,
            draws=0,
        )
    )
    store.upsert_elo(
        Elo(
            subject="headline-copy",
            config_id="srf_pipeline",
            rating=980,
            matches=3,
            wins=0,
            losses=3,
            draws=0,
        )
    )
    store.create_experiment(
        Experiment(
            id="exp_1",
            hypothesis="A blind judging panel beats a single pipeline",
            discipline="copywriting",
            mutable_surface_kind=SurfaceKind.ORCHESTRATION_GENOME,
            champion_surface_id="srf_pipeline",
            challenger_surface_id="srf_panel",
            eval_suite_id="evs_1",
            status="decided",
            disposition="promote",
        )
    )
    store.add_playbook_entry(
        PlaybookEntry(
            id="pbk_1",
            trait="Blind panel judging beats a single reviewer",
            discipline="copywriting",
            evidence_experiments=["exp_1"],
            evidence_tournaments=["tnm_1"],
            confidence="high",
            elo_support=52,
        )
    )
    return store


def _manifest(run_id: str, discipline: str | None) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        task_id=f"task-{run_id}",
        discipline=discipline,
        state=RunState.COMPLETED,
        harness=HarnessProfile(harness=HarnessType.MOCK),
        usage=AgentUsage(wall_ms=10, input_tokens=5, output_tokens=5, cost_usd=0.01),
    )


def _patch_ledger(
    monkeypatch: pytest.MonkeyPatch, manifests: list[RunManifest] | None = None
) -> None:
    monkeypatch.setattr(
        "omniagentos.ledger.read_manifests",
        lambda ledger_dir, limit=100: list(manifests or []),
        raising=False,
    )


def _forbid_l08_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("dry-run must not render or write vault notes via L08")

    monkeypatch.setattr("omniagentos.lab.vault.render_leaderboard_note", _boom, raising=False)
    monkeypatch.setattr("omniagentos.lab.vault.render_playbook_note", _boom, raising=False)
    monkeypatch.setattr("omniagentos.vault.write_note", _boom, raising=False)


def test_dry_run_recomputes_leaderboard_and_judge_notes_digest_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _seed_store()
    _patch_ledger(
        monkeypatch, [_manifest("run-1", "copywriting"), _manifest("run-2", "copywriting")]
    )
    _forbid_l08_writes(monkeypatch)

    def _no_champion_write(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("curate() must never call store.set_champion (HD-006)")

    monkeypatch.setattr(store, "set_champion", _no_champion_write)

    vault_dir = tmp_path / "vault"
    summary = curate(store, str(tmp_path / "ledger"), str(vault_dir), dry_run=True)

    assert summary["dry_run"] is True
    assert summary["subjects"] == ["headline-copy"]

    rows = summary["leaderboard"]["headline-copy"]
    assert [row["config_id"] for row in rows] == ["srf_panel", "srf_pipeline"]
    assert [row["rank"] for row in rows] == [1, 2]
    assert rows[0]["elo"] == 1032.0
    assert "Panel's headline was punchier" in rows[0]["judge_notes"]
    assert rows[0]["source_experiments"] == ["exp_1"]
    assert rows[0]["is_new"] is True
    assert rows[0]["previous_rank"] is None

    digest = summary["judge_notes_digest"]["headline-copy"]
    assert digest[0]["config_id"] == "srf_panel"
    assert "punchier" in digest[0]["notes"]

    assert summary["tournaments"]["headline-copy"][0]["id"] == "tnm_1"

    assert summary["playbook"]["count"] == 1
    assert summary["playbook"]["by_discipline"] == {"copywriting": 1}

    assert summary["runs"]["count"] == 2
    assert summary["runs"]["by_discipline"] == {"copywriting": 2}

    assert summary["experiments"]["count"] == 1
    assert summary["experiments"]["by_disposition"] == {"promote": 1}

    # RECOMMEND only: no champion is seeded yet for this discipline, so the
    # curator surfaces the gap without acting on it (HD-006).
    assert any("RECOMMEND" in r for r in summary["recommendations"])
    assert any("srf_panel" in r for r in summary["recommendations"])

    assert summary["notes_written"] == []
    assert not vault_dir.exists()

    # Nothing was persisted to the store either.
    assert store.leaderboard("headline-copy") == []


def test_non_dry_run_persists_leaderboard_rows_and_writes_notes_via_monkeypatched_l08(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _seed_store()
    _patch_ledger(monkeypatch)
    written: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "omniagentos.lab.vault.render_leaderboard_note",
        lambda subject, rows: (f"leaderboard/{subject}.md", f"# {subject}\n{len(rows)} rows"),
        raising=False,
    )
    monkeypatch.setattr(
        "omniagentos.lab.vault.render_playbook_note",
        lambda discipline, entries: (
            f"playbook/{discipline}.md",
            f"# {discipline}\n{len(entries)} entries",
        ),
        raising=False,
    )

    def _write_note(vault_dir: str, relpath: str, content: str) -> str:
        written.append((vault_dir, relpath, content))
        return relpath

    monkeypatch.setattr("omniagentos.vault.write_note", _write_note, raising=False)

    summary = curate(store, str(tmp_path / "ledger"), "vault", dry_run=False)

    assert summary["notes_written"] == ["leaderboard/headline-copy.md", "playbook/copywriting.md"]
    assert [entry[1] for entry in written] == [
        "leaderboard/headline-copy.md",
        "playbook/copywriting.md",
    ]
    assert all(entry[0] == "vault" for entry in written)

    persisted = store.leaderboard("headline-copy")
    assert [row["config_id"] for row in persisted] == ["srf_panel", "srf_pipeline"]
    assert persisted[0]["updated_by"] == "curator"


def test_recommend_promotion_is_silent_when_the_champion_already_leads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _seed_store()
    store.set_champion(
        ChampionEntry(
            discipline="copywriting",
            surface_kind=SurfaceKind.ORCHESTRATION_GENOME,
            surface_id="srf_panel",
            surface_version=1,
        )
    )
    _patch_ledger(monkeypatch)

    summary = curate(store, str(tmp_path / "ledger"), str(tmp_path / "vault"), dry_run=True)

    assert summary["recommendations"] == []


def test_recommend_promotion_flags_a_stale_champion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _seed_store()
    store.set_champion(
        ChampionEntry(
            discipline="copywriting",
            surface_kind=SurfaceKind.ORCHESTRATION_GENOME,
            surface_id="srf_pipeline",
            surface_version=1,
        )
    )
    _patch_ledger(monkeypatch)

    summary = curate(store, str(tmp_path / "ledger"), str(tmp_path / "vault"), dry_run=True)

    assert any("outranks current champion" in r for r in summary["recommendations"])
    assert any("srf_pipeline" in r for r in summary["recommendations"])


def test_leaderboard_is_capped_at_default_top_n(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LabStore(":memory:")
    store.create_tournament(
        Tournament(id="tnm_1", subject="s1", discipline="d1", arena_task_hash="h", config_ids=[])
    )
    for index in range(DEFAULT_TOP_N + 5):
        store.upsert_elo(Elo(subject="s1", config_id=f"cfg_{index:02d}", rating=1000 + index))
    _patch_ledger(monkeypatch)

    summary = curate(store, str(tmp_path / "ledger"), str(tmp_path / "vault"), dry_run=True)

    rows = summary["leaderboard"]["s1"]
    assert len(rows) == DEFAULT_TOP_N
    assert rows[0]["config_id"] == f"cfg_{DEFAULT_TOP_N + 4:02d}"  # highest rating


def test_curate_on_a_completely_empty_store_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LabStore(":memory:")
    _patch_ledger(monkeypatch)

    summary = curate(store, str(tmp_path / "ledger"), str(tmp_path / "vault"), dry_run=True)

    assert summary == {
        "dry_run": True,
        "generated_at": summary["generated_at"],
        "subjects": [],
        "runs": {
            "count": 0,
            "by_state": {},
            "by_discipline": {},
            "total_est_cost_usd": 0.0,
            "total_est_tokens": 0,
        },
        "experiments": {"count": 0, "by_status": {}, "by_disposition": {}, "by_discipline": {}},
        "playbook": {"count": 0, "by_discipline": {}, "by_confidence": {}},
        "leaderboard": {},
        "judge_notes_digest": {},
        "tournaments": {},
        "recommendations": [],
        "vault_context": [],
        "notes_written": [],
    }


def test_successive_non_dry_runs_track_rank_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LabStore(":memory:")
    store.create_tournament(
        Tournament(
            id="tnm_1", subject="s1", discipline="d1", arena_task_hash="h", config_ids=["a", "b"]
        )
    )
    store.upsert_elo(Elo(subject="s1", config_id="a", rating=1010))
    store.upsert_elo(Elo(subject="s1", config_id="b", rating=990))
    _patch_ledger(monkeypatch)
    monkeypatch.setattr(
        "omniagentos.lab.vault.render_leaderboard_note",
        lambda subject, rows: (f"leaderboard/{subject}.md", "x"),
        raising=False,
    )
    monkeypatch.setattr(
        "omniagentos.lab.vault.render_playbook_note",
        lambda discipline, entries: (f"playbook/{discipline}.md", "x"),
        raising=False,
    )
    monkeypatch.setattr(
        "omniagentos.vault.write_note",
        lambda vault_dir, relpath, content: relpath,
        raising=False,
    )

    first = curate(store, str(tmp_path / "ledger"), str(tmp_path / "vault"), dry_run=False)
    first_rows = first["leaderboard"]["s1"]
    assert [row["config_id"] for row in first_rows] == ["a", "b"]
    assert [row["is_new"] for row in first_rows] == [True, True]

    store.upsert_elo(
        Elo(subject="s1", config_id="b", rating=1050, matches=1, wins=1)
    )  # b overtakes a

    second = curate(store, str(tmp_path / "ledger"), str(tmp_path / "vault"), dry_run=False)
    second_rows = second["leaderboard"]["s1"]
    assert [row["config_id"] for row in second_rows] == ["b", "a"]
    assert second_rows[0]["is_new"] is False
    assert second_rows[0]["previous_rank"] == 2
    assert second_rows[1]["previous_rank"] == 1


def test_non_dry_curate_removes_stale_leaderboard_tail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = LabStore(":memory:")
    store.create_tournament(
        Tournament(id="tnm_shrink", subject="s1", discipline="d1", arena_task_hash="h")
    )
    for index in range(5):
        store.upsert_elo(Elo(subject="s1", config_id=f"cfg_{index}", rating=1000 + index))
    _patch_ledger(monkeypatch)
    monkeypatch.setattr(
        "omniagentos.lab.vault.render_leaderboard_note",
        lambda subject, rows: (f"leaderboard/{subject}.md", "x"),
    )
    monkeypatch.setattr("omniagentos.vault.write_note", lambda *args: "written")

    curate(store, str(tmp_path / "ledger"), str(tmp_path / "vault"), dry_run=False)
    assert len(store.leaderboard("s1")) == 5

    store._connection.execute(  # noqa: SLF001 - controlled shrink fixture.
        "DELETE FROM elo_ratings WHERE subject = ? AND config_id IN (?, ?, ?)",
        ("s1", "cfg_0", "cfg_1", "cfg_2"),
    )
    curate(store, str(tmp_path / "ledger"), str(tmp_path / "vault"), dry_run=False)

    assert [row["config_id"] for row in store.leaderboard("s1")] == ["cfg_4", "cfg_3"]


def test_curate_surfaces_existing_vault_notes_as_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omniagentos.contracts import NoteType, VaultFrontmatter
    from omniagentos.vault import render_frontmatter

    vault_dir = tmp_path / "vault"
    note_dir = vault_dir / "tournaments"
    note_dir.mkdir(parents=True)
    frontmatter = VaultFrontmatter(id="tnm_1", type=NoteType.TOURNAMENT, discipline="copywriting")
    (note_dir / "tnm_1.md").write_text(render_frontmatter(frontmatter) + "\n# tournament: tnm_1\n")
    (vault_dir / "README.md").write_text("not a vault note")

    store = LabStore(":memory:")
    _patch_ledger(monkeypatch)

    summary = curate(store, str(tmp_path / "ledger"), str(vault_dir), dry_run=True)

    assert summary["vault_context"] == [
        {
            "path": "tournaments/tnm_1.md",
            "id": "tnm_1",
            "type": "tournament",
            "discipline": "copywriting",
        }
    ]
