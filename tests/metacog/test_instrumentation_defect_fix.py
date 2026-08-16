"""Regression coverage for honest metacog progress instrumentation."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.metacog.service import MetacogService
from omniagentos.metacog.store import MetacogStore


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetacogService:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "artifacts"))
    return MetacogService(store=MetacogStore(str(tmp_path / "metacog.db")))


def test_unmeasured_progress_is_neutral_and_does_not_compound_stalls(
    svc: MetacogService,
) -> None:
    states = [
        svc.evaluate(
            task_id="manual-card",
            criteria_total=0,
            criteria_passed=0,
            progress_measured=False,
        )
        for _ in range(3)
    ]

    for state in states:
        assert state.progress_measured is False
        assert state.stall_count == 0
        assert "no_progress_cycles" not in state.reason_codes
        assert state.next_control_action == "continue"

    snapshots = svc.store.list_snapshots("manual-card")
    assert all(snap["state"]["progress_measured"] is False for snap in snapshots)

    # Neutral snapshots are ignored when a later, measured observation starts
    # its ordinary stall accounting.
    measured = svc.evaluate(
        task_id="manual-card",
        criteria_total=3,
        criteria_passed=0,
        previous_progress=0.0,
    )
    assert measured.progress_measured is True
    assert measured.stall_count == 1
    assert "no_progress_cycles" not in measured.reason_codes


def test_measured_zero_over_zero_is_rejected(svc: MetacogService) -> None:
    with pytest.raises(ValueError, match="measured progress requires"):
        svc.evaluate(
            task_id="invalid-instrumentation",
            criteria_total=0,
            criteria_passed=0,
            progress_measured=True,
        )
