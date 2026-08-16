"""gate_from_status / gate_from_status_json (omniagentos/selfimprove/gate.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.selfimprove.gate import gate_from_status, gate_from_status_json
from omniagentos.selfimprove.models import GateStatus

from .helpers import write_status_json


def test_state_done_maps_to_passed() -> None:
    gate = gate_from_status({"state": "done", "package": "w4-skillcapture"})
    assert gate.status is GateStatus.PASSED
    assert gate.passed is True
    assert gate.source_run_id == "w4-skillcapture"


@pytest.mark.parametrize("state", ["partial", "failed"])
def test_state_partial_or_failed_maps_to_failed(state: str) -> None:
    gate = gate_from_status({"state": state})
    assert gate.status is GateStatus.FAILED
    assert gate.passed is False


def test_missing_or_unknown_state_maps_to_pending() -> None:
    assert gate_from_status({}).status is GateStatus.PENDING
    assert gate_from_status({"state": "queued"}).status is GateStatus.PENDING


def test_state_running_maps_to_pending() -> None:
    gate = gate_from_status({"state": "running"})
    assert gate.status is GateStatus.PENDING
    assert gate.passed is False


def test_explicit_validated_false_overrides_done_state() -> None:
    gate = gate_from_status({"state": "done", "validated": False})
    assert gate.status is GateStatus.FAILED


def test_explicit_validated_true_does_not_downgrade_done_state() -> None:
    gate = gate_from_status({"state": "done", "validated": True})
    assert gate.status is GateStatus.PASSED


@pytest.mark.parametrize("bad_validated", [0, 1, "false", "False", "", [], {}, None])
def test_malformed_validated_type_is_rejected_rather_than_fail_open(bad_validated: object) -> None:
    # F2: `0 is False` and `"false" is False` are both False in Python — a
    # naive `data.get("validated") is False` check lets a tampered/buggy
    # status.json with a non-boolean `validated` slip through and still
    # authorize a PASSED gate. Malformed `validated` must be rejected
    # outright, never silently treated as truthy.
    with pytest.raises(ValueError, match="boolean"):
        gate_from_status({"state": "done", "validated": bad_validated})


def test_evidence_summarizes_commits_and_validation() -> None:
    gate = gate_from_status(
        {
            "state": "done",
            "commits": [{"hash": "a"}, {"hash": "b"}],
            "validation": {"pytest": "5 passed", "ruff": "clean"},
        }
    )
    assert gate.evidence is not None
    assert "state=done" in gate.evidence
    assert "2 commit(s)" in gate.evidence
    assert "pytest=5 passed" in gate.evidence


def test_gate_from_status_json_reads_real_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "session-1"
    write_status_json(run_dir, state="done")

    gate = gate_from_status_json(run_dir)

    assert gate.status is GateStatus.PASSED


def test_gate_from_status_json_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        gate_from_status_json(tmp_path / "no-such-session")


def test_gate_from_status_json_non_object_raises(tmp_path: Path) -> None:
    run_dir = tmp_path / "session-2"
    run_dir.mkdir()
    (run_dir / "status.json").write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(ValueError, match="JSON object"):
        gate_from_status_json(run_dir)
