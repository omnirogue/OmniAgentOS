from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.lab import surfaces
from omniagentos.lab.contracts import EvalSplit, SurfaceKind
from omniagentos.lab.db import LabStore
from omniagentos.lab.eval.grader import ProtectedGrader
from omniagentos.lab.seed import DEMO_DISCIPLINE, DEMO_SUITE_ID, ensure_demo_seeded


def test_demo_seed_is_idempotent_and_runnable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(surfaces, "_repository_root", lambda: tmp_path)
    store = LabStore(":memory:")
    grader = ProtectedGrader(":memory:")

    ensure_demo_seeded(store, grader)
    first_surface_ids = {row["id"] for row in store.list_surfaces(DEMO_DISCIPLINE)}
    ensure_demo_seeded(store, grader)

    assert {row["id"] for row in store.list_surfaces(DEMO_DISCIPLINE)} == first_surface_ids
    assert len(first_surface_ids) == 2
    assert store.get_champion(DEMO_DISCIPLINE, SurfaceKind.PROMPT.value) is not None
    assert store.get_champion(DEMO_DISCIPLINE, SurfaceKind.ORCHESTRATION_GENOME.value) is not None
    assert len(store.candidate_cases(DEMO_SUITE_ID, EvalSplit.DEV.value)) == 2
    held_out = store.candidate_cases(DEMO_SUITE_ID, EvalSplit.HELD_OUT.value)
    assert len(held_out) == 2
    result = grader.score_outputs(
        DEMO_SUITE_ID,
        EvalSplit.HELD_OUT,
        "champion",
        {case.id: {"text": "wrong"} for case in held_out},
    )
    assert result.metrics["accuracy"] == 0.0
