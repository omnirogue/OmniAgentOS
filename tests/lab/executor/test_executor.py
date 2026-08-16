from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from omniagentos.lab.contracts import (
    Budgets,
    EvalCase,
    EvalSplit,
    EvalSuite,
    GenomeRole,
    GenomeSpec,
    GenomeStage,
    MetricSpec,
    SurfaceKind,
)
from omniagentos.lab.db.store import LabStore
from omniagentos.lab.executor import execute_genome, run_surface_over_cases
from omniagentos.mock_adapter import MockAdapter


@pytest.fixture
def store(tmp_path: Path) -> LabStore:
    lab_store = LabStore(str(tmp_path / "lab.db"))
    suite = EvalSuite(
        id="evs_executor",
        discipline="test",
        metrics=[MetricSpec(name="quality", role="primary")],
    )
    lab_store.create_eval_suite(suite)
    for case_id in ("evc_one", "evc_two"):
        lab_store.add_eval_case(
            EvalCase(
                id=case_id,
                suite=suite.id,
                split=EvalSplit.DEV,
                input={"question": f"question for {case_id}"},
                expected={"answer": "candidate-visible dev answer"},
                rubric="Be accurate.",
            )
        )
    return lab_store


@pytest.fixture(autouse=True)
def isolated_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OMNIAGENTOS_VAULT_DIR", str(tmp_path / "vault"))
    monkeypatch.setenv("OMNIAGENTOS_WORKSPACE_DIR", str(tmp_path / "runs"))


def _genome() -> GenomeSpec:
    return GenomeSpec(
        id="srf_genome",
        roles=[
            GenomeRole(name="writer", agent="mock"),
            GenomeRole(name="arbiter", agent="mock"),
        ],
        flow=[
            GenomeStage(stage="draft", kind="generate", role="writer"),
            GenomeStage(
                stage="decision",
                kind="judge",
                role="arbiter",
                inputs_from=["draft"],
            ),
        ],
        judge={"blind": True, "dimensions": ["quality"]},
    )


def test_prompt_surface_runs_every_candidate_case_through_h1_runner(
    store: LabStore,
) -> None:
    result = run_surface_over_cases(
        store,
        {
            "id": "srf_prompt",
            "kind": SurfaceKind.PROMPT.value,
            "content": "Answer carefully and concisely.",
        },
        "evs_executor",
        EvalSplit.DEV.value,
        Budgets(tokens=10_000),
        dry_run=True,
    )

    assert set(result["outputs"]) == {"evc_one", "evc_two"}
    assert all(
        output["text"].startswith("dry-run prompt:") for output in result["outputs"].values()
    )
    assert len(result["manifests"]) == 2
    for run_id in result["manifests"]:
        run = store._store.get_run(run_id)
        assert run is not None
        assert run["state"] == "completed"
        assert run["manifest_path"]


def test_two_role_generate_judge_genome_runs_end_to_end(store: LabStore) -> None:
    result = run_surface_over_cases(
        store,
        {
            "id": "srf_genome",
            "kind": SurfaceKind.ORCHESTRATION_GENOME.value,
            "content": _genome().model_dump(mode="json"),
        },
        "evs_executor",
        EvalSplit.DEV.value,
        Budgets(tokens=10_000),
        dry_run=True,
    )

    assert set(result["outputs"]) == {"evc_one", "evc_two"}
    assert {output["text"] for output in result["outputs"].values()} == {"dry-run judge:decision"}
    assert len(result["manifests"]) == 2
    steps = store._store.get_steps(result["manifests"][0])
    assert [step["name"] for step in steps] == ["draft", "decision"]


def test_execute_genome_returns_final_stage_output() -> None:
    output = execute_genome(_genome(), {"question": "test"}, Budgets(), dry_run=True)
    assert output == {"text": "dry-run judge:decision", "json": None}


def test_sensitive_server_values_are_absent_during_child_adapter_call(
    store: LabStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected_path = "/secret/var/eval_protected.db"
    operator_token = "server-only-operator-token"
    monkeypatch.setenv("OMNIAGENTOS_EVAL_PROTECTED", protected_path)
    monkeypatch.setenv("OMNIAGENTOS_OPERATOR_TOKEN", operator_token)
    original = MockAdapter.run
    observations: list[dict[str, Any]] = []

    def inspect_environment(self: MockAdapter, input: Any) -> Any:
        observations.append(
            {
                "protected": os.environ.get("OMNIAGENTOS_EVAL_PROTECTED"),
                "operator_token": os.environ.get("OMNIAGENTOS_OPERATOR_TOKEN"),
                "metadata": json.dumps(input.metadata, sort_keys=True),
            }
        )
        return original(self, input)

    monkeypatch.setattr(MockAdapter, "run", inspect_environment)
    run_surface_over_cases(
        store,
        {
            "id": "srf_prompt",
            "kind": SurfaceKind.PROMPT.value,
            "content": "Do not inspect hidden evaluator data.",
        },
        "evs_executor",
        EvalSplit.DEV.value,
        Budgets(),
        env_scrub=True,
        dry_run=True,
    )

    assert observations
    assert all(item["protected"] is None for item in observations)
    assert all(item["operator_token"] is None for item in observations)
    assert all(protected_path not in item["metadata"] for item in observations)
    assert all(operator_token not in item["metadata"] for item in observations)
    assert os.environ["OMNIAGENTOS_EVAL_PROTECTED"] == protected_path
    assert os.environ["OMNIAGENTOS_OPERATOR_TOKEN"] == operator_token


def test_manifest_finalization_failure_is_not_reported_as_success(
    store: LabStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_manifest(*args: Any, **kwargs: Any) -> str:
        raise OSError("ledger unavailable")

    monkeypatch.setattr("omniagentos.ledger.append_manifest", fail_manifest)

    with pytest.raises(RuntimeError, match="manifest finalization failed"):
        run_surface_over_cases(
            store,
            {
                "id": "srf_prompt",
                "kind": SurfaceKind.PROMPT.value,
                "content": "Answer carefully.",
            },
            "evs_executor",
            EvalSplit.DEV.value,
            Budgets(),
            dry_run=True,
        )
