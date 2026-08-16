from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from omniagentos.lab.contracts import (
    Budgets,
    ChampionEntry,
    Elo,
    EvalCase,
    EvalResult,
    EvalSplit,
    EvalSuite,
    Experiment,
    ExperimentStatus,
    JudgeRecord,
    LeaderboardEntry,
    MatchResult,
    MetricSpec,
    PlaybookEntry,
    Scorecard,
    Surface,
    SurfaceKind,
    SurfaceStatus,
    Tournament,
)
from omniagentos.lab.db import ChampionCASMismatch, LabStore


def test_surfaces_and_same_h1_database_round_trip() -> None:
    store = LabStore(":memory:")
    prompt = Surface(
        id="srf_prompt",
        kind=SurfaceKind.PROMPT,
        discipline="coding",
        path="vault/prompts/coder/v1.md",
        content_hash="hash-1",
        label="first",
    )
    rubric = Surface(
        id="srf_rubric",
        kind=SurfaceKind.REVIEW_RUBRIC,
        discipline="coding",
        path="vault/prompts/reviewer/v1.md",
        content_hash="hash-2",
    )

    store.create_surface(prompt)
    store.create_surface(rubric)

    assert store.get_surface(prompt.id) == {
        "id": "srf_prompt",
        "kind": "prompt",
        "discipline": "coding",
        "version": 1,
        "path": "vault/prompts/coder/v1.md",
        "content_hash": "hash-1",
        "parent_version": None,
        "status": "draft",
        "label": "first",
        "safety_relevant": 0,
        "created_at": prompt.created_at,
    }
    assert [row["id"] for row in store.list_surfaces("coding", "prompt")] == [prompt.id]
    assert {row["id"] for row in store.list_surfaces("coding")} == {prompt.id, rubric.id}
    assert store.get_surface(rubric.id)["safety_relevant"] == 1  # type: ignore[index]

    store.set_surface_status(prompt.id, SurfaceStatus.CHAMPION)
    assert store.get_surface(prompt.id)["status"] == "champion"  # type: ignore[index]
    assert store._connection.execute("SELECT 1 FROM tasks").fetchall() == []


def test_champion_cas_and_history() -> None:
    store = LabStore(":memory:")
    seed = ChampionEntry(
        discipline="coding",
        surface_kind=SurfaceKind.PROMPT,
        surface_id="srf_seed",
        surface_version=1,
    )
    store.set_champion(seed)
    current = store.get_champion("coding", "prompt")
    assert current is not None
    assert current["surface_id"] == "srf_seed"
    assert current["cas_version"] == 1

    promoted = ChampionEntry(
        discipline="coding",
        surface_kind=SurfaceKind.PROMPT,
        surface_id="srf_next",
        surface_version=2,
        promoted_from_experiment="exp_1",
        rollback_to_surface_id="srf_seed",
        cas_version=1,
    )
    store.set_champion(promoted)

    with pytest.raises(ChampionCASMismatch, match="stale champion CAS"):
        store.set_champion(promoted.model_copy(update={"surface_id": "srf_stale"}))

    current = store.get_champion("coding", "prompt")
    assert current is not None
    assert current["surface_id"] == "srf_next"
    assert current["cas_version"] == 2
    history = store.champion_history("coding", "prompt")
    assert [row["surface_id"] for row in history] == ["srf_seed", "srf_next"]
    assert [row["event"] for row in history] == ["promoted", "promoted"]


def test_two_concurrent_champion_writers_preserve_one_pointer(tmp_path: Path) -> None:
    db_path = str(tmp_path / "lab.db")
    seed_store = LabStore(db_path)
    seed_store.set_champion(
        ChampionEntry(
            discipline="coding",
            surface_kind=SurfaceKind.PROMPT,
            surface_id="srf_seed",
            surface_version=1,
        )
    )
    stores = [LabStore(db_path), LabStore(db_path)]
    barrier = Barrier(2)

    def write(index: int) -> str:
        entry = ChampionEntry(
            discipline="coding",
            surface_kind=SurfaceKind.PROMPT,
            surface_id=f"srf_writer_{index}",
            surface_version=2,
            rollback_to_surface_id="srf_seed",
            cas_version=1,
        )
        barrier.wait()
        try:
            stores[index].set_champion(entry)
        except ChampionCASMismatch:
            return "stale"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(write, range(2)))

    assert sorted(outcomes) == ["stale", "won"]
    current = seed_store.get_champion("coding", "prompt")
    assert current is not None
    assert current["surface_id"] in {"srf_writer_0", "srf_writer_1"}
    assert current["cas_version"] == 2
    assert len(seed_store.champion_history("coding", "prompt")) == 2


def test_experiment_and_eval_round_trip() -> None:
    store = LabStore(":memory:")
    suite = EvalSuite(
        id="evs_1",
        discipline="coding",
        metrics=[MetricSpec(name="quality", role="primary")],
        dataset_hash="dataset-1",
    )
    store.create_eval_suite(suite)
    experiment = Experiment(
        id="exp_1",
        hypothesis="Shorter prompts improve quality",
        discipline="coding",
        mutable_surface_kind=SurfaceKind.PROMPT,
        champion_surface_id="srf_a",
        challenger_surface_id="srf_b",
        eval_suite_id=suite.id,
        budgets=Budgets(tokens=1000),
    )
    store.create_experiment(experiment)

    row = store.get_experiment(experiment.id)
    assert row is not None
    assert json.loads(row["budgets_json"])["tokens"] == 1000
    assert json.loads(row["promotion_json"])["primary_delta_min"] == 0.03
    store.update_experiment(
        experiment.id,
        {
            "status": ExperimentStatus.EVALUATING,
            "scorecard": Scorecard(primary_delta=0.1),
        },
    )
    row = store.get_experiment(experiment.id)
    assert row is not None
    assert row["status"] == "evaluating"
    assert json.loads(row["scorecard_json"])["primary_delta"] == 0.1
    assert [item["id"] for item in store.list_experiments("coding", "evaluating")] == [
        experiment.id
    ]
    assert store.list_experiments("other") == []

    suite_row = store.get_eval_suite(suite.id)
    assert suite_row is not None
    assert json.loads(suite_row["metrics_json"])[0]["name"] == "quality"

    dev = EvalCase(
        id="evc_dev",
        suite=suite.id,
        split=EvalSplit.DEV,
        input={"question": "2+2"},
        expected={"answer": 4},
        rubric="exact",
    )
    held_out_secret = {"answer": "NEVER-LEAK-THIS"}
    held_out = EvalCase(
        id="evc_held",
        suite=suite.id,
        split=EvalSplit.HELD_OUT,
        input={"question": "secret"},
        expected=held_out_secret,
        rubric="protected",
    )
    store.add_eval_case(dev)
    store.add_eval_case(held_out)

    held_row = store._connection.execute(
        "SELECT expected_json FROM eval_cases WHERE id = ?", (held_out.id,)
    ).fetchone()
    assert held_row["expected_json"] is None
    cases = store.candidate_cases(suite.id, "held_out")
    assert len(cases) == 1
    assert cases[0].input == {"question": "secret"}
    assert "expected" not in type(cases[0]).model_fields
    assert "NEVER-LEAK-THIS" not in cases[0].model_dump_json()
    assert cases[0].model_dump() != held_out_secret
    assert json.loads(
        store._connection.execute(
            "SELECT expected_json FROM eval_cases WHERE id = ?", (dev.id,)
        ).fetchone()["expected_json"]
    ) == {"answer": 4}

    result = EvalResult(
        id="evr_1",
        experiment_id=experiment.id,
        arm="challenger",
        suite_id=suite.id,
        suite_version=1,
        split=EvalSplit.DEV,
        metrics={"quality": 0.9},
        per_case={dev.id: {"quality": 0.9}},
    )
    store.record_eval_result(result)
    results = store.eval_results(experiment.id)
    assert len(results) == 1
    assert json.loads(results[0]["metrics_json"]) == {"quality": 0.9}

    judge = JudgeRecord(
        id="jdg_1",
        experiment_id=experiment.id,
        case_id=dev.id,
        arm="challenger",
        judge_lineage="judge-v1",
        blind_token="opaque",
        score=0.9,
        notes="good",
    )
    store.record_judge(judge)
    judge_row = store._connection.execute(
        "SELECT * FROM judge_records WHERE id = ?", (judge.id,)
    ).fetchone()
    assert judge_row["blind_token"] == "opaque"
    assert judge_row["created_at"]
    assert [row["id"] for row in store.judge_records(experiment.id)] == [judge.id]


def test_tournament_elo_playbook_and_leaderboard_round_trip() -> None:
    store = LabStore(":memory:")
    tournament = Tournament(
        id="tnm_1",
        subject="python",
        discipline="coding",
        arena_task_hash="arena-hash",
        config_ids=["srf_a", "srf_b"],
    )
    store.create_tournament(tournament)
    store.update_tournament(
        tournament.id,
        {"winner_config_id": "srf_b", "status": "done", "config_ids": ["srf_b"]},
    )
    tournament_row = store._connection.execute(
        "SELECT * FROM tournaments WHERE id = ?", (tournament.id,)
    ).fetchone()
    assert tournament_row["winner_config_id"] == "srf_b"
    assert json.loads(tournament_row["config_ids_json"]) == ["srf_b"]

    match = MatchResult(
        id="mch_1",
        tournament_id=tournament.id,
        config_a="srf_a",
        config_b="srf_b",
        winner="srf_b",
        score_b=1.0,
        judge_notes="B was clearer",
    )
    store.record_match(match)
    match_row = store._connection.execute(
        "SELECT * FROM matches WHERE id = ?", (match.id,)
    ).fetchone()
    assert match_row["winner"] == "srf_b"
    assert match_row["blind"] == 1

    elo = Elo(subject="python", config_id="srf_b", rating=1012, matches=1, wins=1)
    store.upsert_elo(elo)
    assert store.get_elo("python", "srf_b")["rating"] == 1012  # type: ignore[index]
    store.upsert_elo(elo.model_copy(update={"rating": 1024, "matches": 2}))
    assert store.get_elo("python", "srf_b")["rating"] == 1024  # type: ignore[index]

    first = LeaderboardEntry(
        subject="python",
        discipline="coding",
        rank=1,
        config_id="srf_b",
        elo=1024,
        summary="panel",
        source_experiments=["exp_1"],
    )
    second = LeaderboardEntry(
        subject="python",
        discipline="coding",
        rank=2,
        config_id="srf_a",
        elo=1000,
        summary="solo",
    )
    store.upsert_leaderboard_entry(second)
    store.upsert_leaderboard_entry(first)
    store.upsert_leaderboard_entry(first.model_copy(update={"elo": 1030}))
    board = store.leaderboard("python")
    assert [row["rank"] for row in board] == [1, 2]
    assert board[0]["elo"] == 1030
    assert json.loads(board[0]["source_experiments_json"]) == ["exp_1"]

    playbook = PlaybookEntry(
        id="pbk_1",
        trait="Use a blind panel",
        discipline="coding",
        evidence_experiments=["exp_1"],
        evidence_tournaments=[tournament.id],
        confidence="high",
        elo_support=24,
    )
    store.add_playbook_entry(playbook)
    rows = store.list_playbook("coding")
    assert len(rows) == 1
    assert json.loads(rows[0]["evidence_tournaments_json"]) == [tournament.id]
    assert store.list_playbook("writing") == []


def test_update_methods_reject_unknown_columns() -> None:
    store = LabStore(":memory:")
    with pytest.raises(ValueError, match="unknown columns"):
        store.update_experiment("exp_missing", {"not_a_column": "value"})
    with pytest.raises(ValueError, match="unknown columns"):
        store.update_tournament("tnm_missing", {"not_a_column": "value"})
