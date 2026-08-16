"""Acceptance tests for the TraceLab verdict-to-eval flywheel.

The decisive fixture has three source verdicts and one export.  Its denominator
is deliberately the unfiltered source count: computing the acceptance rate
after applying the gates would counterfeit a perfect result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.lab.contracts import EvalCase
from omniagentos.memlife import contracts as memlife_contracts
from omniagentos.tracelab import export
from tests.doctrine import TextReplace, counterfeit_test, revert_test


def _verdict(
    verdict_id: str,
    *,
    approved: bool | None = True,
    redaction_status: str | None = "passed",
    raw_input: object = None,
    rubric: str | None = "Judge the response against the expected result.",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "verdict_id": verdict_id,
        "experiment_id": f"exp-{verdict_id}",
        "eval_suite_id": "evs-flywheel",
        "case_input": {"instruction": f"redacted task {verdict_id}"},
        "expected": {"result": f"expected-{verdict_id}"},
        "invalidation_status": "valid",
        "panel_lineage_count": 2,
        "replicate_count": 3,
        "effective_n": 3,
        "agreement": 1.0,
        "mde": 0.1,
        "observed_effect": 0.4,
        "blind_presentation_seed": 17,
    }
    if approved is not None:
        record["is_human_approved"] = approved
    if redaction_status is not None:
        record["redaction_status"] = redaction_status
    if raw_input is not None:
        record["raw_input"] = raw_input
    if rubric is not None:
        record["rubric"] = rubric
        record["rubric_provenance"] = "human_authored"
    return record


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def test_three_verdict_fixture_exports_only_fully_gated_case(tmp_path: Path) -> None:
    """Decisive: absent approval and absent redaction each fail independently."""
    source = tmp_path / "verdicts.jsonl"
    destination = tmp_path / "eval-cases.jsonl"
    report_path = tmp_path / "report.json"
    _write_jsonl(
        source,
        [
            _verdict("approved"),
            _verdict("approval-absent", approved=None),
            _verdict("redaction-absent", redaction_status=None),
        ],
    )

    report = export.export_verdict_file(source, destination, report_path=report_path)
    rows = _read_jsonl(destination)

    assert report.input_count == 3
    assert report.exported_count == 1
    assert report.rejected_count == 2
    assert report.acceptance_rate == pytest.approx(1 / 3)
    assert [row["source_verdict_id"] for row in rows] == ["approved"]
    assert json.loads(report_path.read_text(encoding="utf-8"))["acceptance_rate"] == pytest.approx(
        1 / 3
    )

    # The improve/lab loop can consume the public fields without a translation.
    case = EvalCase.model_validate(rows[0])
    assert case.suite == "evs-flywheel"
    assert case.input == {"instruction": "redacted task approved"}
    assert case.expected == {"result": "expected-approved"}
    assert "raw_input" not in rows[0]


def test_gates_are_exact_and_fail_closed() -> None:
    assert export.is_human_approved({}) is False
    assert export.is_human_approved({"is_human_approved": "yes"}) is False
    assert export.is_human_approved({"is_human_approved": True}) is True

    assert export.redaction_passed({}) is False
    assert export.redaction_passed({"redaction_status": None}) is False
    assert export.redaction_passed({"redaction_status": "needs_review"}) is False
    assert export.redaction_passed({"redaction_status": "passed"}) is True

    gated = _verdict("trainable")
    assert export.is_trainable(gated) is True
    assert export.is_trainable({**gated, "raw_input": "secret prompt"}) is False
    assert export.is_trainable({**gated, "eval_case": {"raw_input": "nested secret"}}) is False
    assert export.is_trainable({**gated, "is_human_approved": False}) is False
    assert (
        export.is_trainable(
            {key: value for key, value in gated.items() if key != "redaction_status"}
        )
        is False
    )


def test_missing_input_is_nonzero_and_writes_no_empty_counterfeit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "does-not-exist.jsonl"
    destination = tmp_path / "eval-cases.jsonl"

    return_code = export.main(["--input", str(missing), "--out", str(destination)])

    captured = capsys.readouterr()
    assert return_code != 0, "a missing source must fail independently of the named exit code"
    assert return_code == export.INPUT_ERROR_EXIT_CODE
    assert "input: missing" in captured.err
    assert str(missing) in captured.err
    assert not destination.exists(), "missing input must not counterfeit a real empty export"


def test_empty_real_input_has_unknown_acceptance_rate(tmp_path: Path) -> None:
    source = tmp_path / "empty.jsonl"
    destination = tmp_path / "eval-cases.jsonl"
    source.write_text("", encoding="utf-8")

    report = export.export_verdict_file(source, destination)

    assert report.input_count == 0
    assert report.exported_count == 0
    assert report.acceptance_rate is None
    assert destination.read_text(encoding="utf-8") == ""
    persisted = json.loads(destination.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert persisted["acceptance_rate"] is None


def test_unparseable_input_is_never_an_empty_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "broken.jsonl"
    destination = tmp_path / "eval-cases.jsonl"
    source.write_text('{"is_human_approved": true\n', encoding="utf-8")

    return_code = export.main(["--input", str(source), "--out", str(destination)])

    captured = capsys.readouterr()
    assert return_code != 0
    assert "input: unparseable" in captured.err
    assert not destination.exists()


def test_synthesized_rubric_is_visibly_distinct_from_source_rubric(tmp_path: Path) -> None:
    source = tmp_path / "verdicts.jsonl"
    destination = tmp_path / "eval-cases.jsonl"
    _write_jsonl(
        source,
        [
            _verdict("authored"),
            _verdict("defaulted", rubric=None),
        ],
    )

    export.export_verdict_file(source, destination)
    by_id = {row["source_verdict_id"]: row for row in _read_jsonl(destination)}

    assert by_id["authored"]["rubric_provenance"] == "human_authored"
    assert by_id["defaulted"]["rubric_provenance"] == "synthesized_default"
    assert by_id["authored"]["rubric"] != by_id["defaulted"]["rubric"]


def test_memlife_contracts_are_imported_not_redefined() -> None:
    assert export.EpisodicEvent is memlife_contracts.EpisodicEvent
    assert export.Candidate is memlife_contracts.Candidate
    assert export.Decision is memlife_contracts.Decision
    assert export.Lesson is memlife_contracts.Lesson
    assert export.CycleStatus is memlife_contracts.CycleStatus
    assert export.CycleReport is memlife_contracts.CycleReport


def test_doctrine_counterfeits_and_revert_are_caught() -> None:
    """Executable registration for the named L8 counterfeits and revert."""
    target = Path(export.__file__).resolve()
    gate_node = (
        "tests/tracelab/test_export.py::test_three_verdict_fixture_exports_only_fully_gated_case"
    )

    approval = counterfeit_test(
        target=target,
        counterfeit=TextReplace(
            old='return record.get("is_human_approved", False) is True',
            new='return record.get("is_human_approved", True) is True',
        ),
        nodeid=gate_node,
    )
    assert approval.counterfeit_failure.failed
    assert approval.restored_pass is not None and approval.restored_pass.passed

    redaction = counterfeit_test(
        target=target,
        counterfeit=TextReplace(
            old='return record.get("redaction_status", "needs_review") == "passed"',
            new='return record.get("redaction_status", "passed") == "passed"',
        ),
        nodeid=gate_node,
    )
    assert redaction.counterfeit_failure.failed
    assert redaction.restored_pass is not None and redaction.restored_pass.passed

    missing = revert_test(
        target=target,
        mutation=TextReplace(
            old="INPUT_ERROR_EXIT_CODE = 2",
            new="INPUT_ERROR_EXIT_CODE = 0",
        ),
        nodeid=(
            "tests/tracelab/test_export.py::"
            "test_missing_input_is_nonzero_and_writes_no_empty_counterfeit"
        ),
    )
    assert missing.mutated_failure.failed
    assert missing.restored_pass.passed
