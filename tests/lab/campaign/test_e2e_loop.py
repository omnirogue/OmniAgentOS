from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.lab import surfaces
from omniagentos.lab.campaign import run_experiment
from omniagentos.lab.contracts import (
    Budgets,
    Disposition,
    EvalCase,
    EvalSplit,
    EvalSuite,
    Experiment,
    MetricSpec,
    Surface,
    SurfaceKind,
)
from omniagentos.lab.db import LabStore
from omniagentos.lab.eval.evaluator import ProtectedEvaluator
from omniagentos.lab.eval.grader import ProtectedGrader
from omniagentos.mock_adapter import MockAdapter


@pytest.fixture
def real_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LabStore, ProtectedEvaluator, str]:
    import omniagentos.lab.executor as executor

    monkeypatch.setattr(surfaces, "_repository_root", lambda: tmp_path)
    monkeypatch.setattr(executor, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(tmp_path / "runner-vault"))
    monkeypatch.setenv("OMNIAGENTOS_WORKSPACE_DIR", str(tmp_path / "runs"))

    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite = EvalSuite(
        id="evs_e2e",
        discipline="e2e",
        metrics=[MetricSpec(name="accuracy", role="primary")],
        dataset_hash="e2e-v1",
    )
    store.create_eval_suite(suite)
    for split in (EvalSplit.DEV, EvalSplit.HELD_OUT):
        for number in range(1, 5):
            case_id = f"evc_{split.value}_{number}"
            expected = {"value": f"answer-{number}"}
            store.add_eval_case(
                EvalCase(
                    id=case_id,
                    suite=suite.id,
                    split=split,
                    input={"number": number},
                    expected=expected if split == EvalSplit.DEV else None,
                    rubric="Return the exact answer.",
                )
            )
            grader.put_expected(case_id, expected)

    original_run = MockAdapter.run

    def prompt_sensitive_run(self: MockAdapter, agent_input: Any) -> Any:
        executor = agent_input.metadata["executor"]
        prompt = str(executor["system_prompt"])
        number = int(executor["case_input"]["number"])
        correct_through = 3 if "WINNING" in prompt else (1 if "LOSING" in prompt else 2)
        reply = f"answer-{number}" if number <= correct_through else "incorrect"
        metadata = dict(agent_input.metadata)
        metadata["mock"] = {"reply": reply}
        return original_run(self, agent_input.model_copy(update={"metadata": metadata}))

    monkeypatch.setattr(MockAdapter, "run", prompt_sensitive_run)
    return store, ProtectedEvaluator(store, grader), suite.id


def _create_experiment(store: LabStore, suite_id: str, challenger_prompt: str, exp_id: str) -> str:
    champion_entry = store.get_champion("e2e", SurfaceKind.PROMPT.value)
    if champion_entry is None:
        champion = surfaces.version_prompt(store, "e2e", "worker", "BASELINE")
        surfaces.seed_champion(store, "e2e", SurfaceKind.PROMPT, champion.id)
    else:
        champion = Surface.model_validate(store.get_surface(str(champion_entry["surface_id"])))
    challenger = surfaces.version_prompt(
        store,
        "e2e",
        "worker",
        challenger_prompt,
        parent_version=champion.version,
    )
    experiment = Experiment(
        id=exp_id,
        hypothesis="real integrated scoring changes the disposition",
        discipline="e2e",
        mutable_surface_kind=SurfaceKind.PROMPT,
        champion_surface_id=champion.id,
        challenger_surface_id=challenger.id,
        eval_suite_id=suite_id,
        dataset_hash="e2e-v1",
        primary_metric="accuracy",
        budgets=Budgets(replicates=2),
    )
    store.create_experiment(experiment)
    return experiment.id


def test_real_loop_promotes_a_genuinely_better_challenger(
    real_loop: tuple[LabStore, ProtectedEvaluator, str],
) -> None:
    store, evaluator, suite_id = real_loop
    exp_id = _create_experiment(store, suite_id, "WINNING", "exp_e2e_promote")

    assert run_experiment(store, evaluator, exp_id, dry_run=True) == Disposition.PROMOTE

    results = store.eval_results(exp_id)
    assert len(results) == 8
    assert {(row["arm"], row["split"]) for row in results} == {
        ("champion", EvalSplit.DEV.value),
        ("challenger", EvalSplit.DEV.value),
        ("champion", EvalSplit.HELD_OUT.value),
        ("challenger", EvalSplit.HELD_OUT.value),
    }
    persisted = store.get_experiment(exp_id)
    assert persisted is not None
    scorecard = json.loads(persisted["scorecard_json"])
    assert scorecard["audit_flags"] == []
    assert scorecard["primary_delta"] == pytest.approx(0.25)


def test_real_loop_rejects_a_worse_challenger(
    real_loop: tuple[LabStore, ProtectedEvaluator, str],
) -> None:
    store, evaluator, suite_id = real_loop
    exp_id = _create_experiment(store, suite_id, "LOSING", "exp_e2e_reject")

    assert run_experiment(store, evaluator, exp_id, dry_run=True) == Disposition.REJECT
    results = store.eval_results(exp_id)
    assert len(results) == 4
    assert {row["split"] for row in results} == {EvalSplit.DEV.value}


def test_real_genome_blind_policy_persists_experiment_judgments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.lab.executor as executor

    monkeypatch.setattr(surfaces, "_repository_root", lambda: tmp_path)
    monkeypatch.setattr(executor, "_repo_root", lambda: tmp_path)
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("OMNIAGENTOS_WORKSPACE_DIR", str(tmp_path / "runs"))
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")
    suite = EvalSuite(
        id="evs_blind",
        discipline="blind-e2e",
        metrics=[MetricSpec(name="accuracy", role="primary")],
    )
    store.create_eval_suite(suite)
    case = EvalCase(
        id="evc_blind_dev",
        suite=suite.id,
        split=EvalSplit.DEV,
        input={"question": "answer"},
        expected={"value": "dry-run generate:answer"},
        rubric="exact",
    )
    store.add_eval_case(case)
    grader.put_expected(case.id, case.expected or {})
    genome = {
        "archetype": "solo",
        "roles": [{"name": "worker", "agent": "mock"}],
        "flow": [{"stage": "answer", "kind": "generate", "role": "worker"}],
        "judge": {"blind": True, "panel": ["judge-a"]},
    }
    champion = surfaces.version_genome(store, suite.discipline, genome)
    challenger = surfaces.version_genome(store, suite.discipline, genome, parent_version=1)
    surfaces.seed_champion(
        store,
        suite.discipline,
        SurfaceKind.ORCHESTRATION_GENOME,
        champion.id,
    )
    experiment = Experiment(
        id="exp_blind_e2e",
        hypothesis="blind judging is auditable",
        discipline=suite.discipline,
        mutable_surface_kind=SurfaceKind.ORCHESTRATION_GENOME,
        champion_surface_id=champion.id,
        challenger_surface_id=challenger.id,
        eval_suite_id=suite.id,
        primary_metric="accuracy",
    )
    store.create_experiment(experiment)

    disposition = run_experiment(
        store,
        ProtectedEvaluator(store, grader),
        experiment.id,
        dry_run=True,
    )

    # Blind judgments are currently scored by the offline heuristic stand-in,
    # so the campaign flags them and the disposition must be HUMAN_REVIEW —
    # never a silent numeric verdict on manufactured unanimous scores.
    assert disposition == Disposition.HUMAN_REVIEW
    decided = store.get_experiment(experiment.id)
    assert decided is not None
    stored_card = json.loads(decided["scorecard_json"] or "{}")
    assert "blind-judging-used-offline-heuristic-judge" in stored_card.get("audit_flags", [])
    records = store.judge_records(experiment.id)
    assert len(records) == 2
    assert {row["arm"] for row in records} == {"champion", "challenger"}
    assert {row["judge_lineage"] for row in records} == {"judge-a"}
