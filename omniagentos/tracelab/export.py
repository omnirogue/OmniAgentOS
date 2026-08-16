"""Fail-closed export of approved lab verdicts into public eval cases.

The source is an explicit JSON or JSONL handoff rather than a direct database
query because approval/redaction attestations are not columns in migration 082.
Each source row may carry the migration's ``lab_verdict_provenance`` fields plus
an eval-case payload.  The resulting JSONL rows retain the public
``lab.contracts.EvalCase`` shape so the improvement loop can ingest them.

Three gates are independent and exact:

* ``is_human_approved`` accepts only the literal boolean ``True``;
* ``redaction_passed`` accepts only ``redaction_status == "passed"`` and treats
  omission as ``needs_review``;
* ``is_trainable`` requires both gates and rejects any non-empty raw input.

Missing and unparseable sources are input errors, not empty corpora.  A real,
successfully read empty corpus is representable and reports a null acceptance
rate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from omniagentos.lab.contracts import EvalCase
from omniagentos.memlife.contracts import (
    Candidate,
    CycleReport,
    CycleStatus,
    Decision,
    EpisodicEvent,
    Lesson,
)

INPUT_ERROR_EXIT_CODE = 2
EXPORT_SCHEMA_VERSION = 1
DEFAULT_RUBRIC = (
    "Assess whether the response correctly satisfies the redacted case input "
    "and contains no sensitive or raw source material."
)

_PROVENANCE_FIELDS = (
    "verdict_id",
    "experiment_id",
    "panel_composition",
    "panel_composition_json",
    "panel_lineage_count",
    "replicate_count",
    "effective_n",
    "agreement",
    "mde",
    "observed_effect",
    "invalidation_status",
    "blind_presentation_seed",
    "created_at",
)


class ExportInputError(ValueError):
    """The source could not be read as a verdict corpus."""

    def __init__(self, status: Literal["missing", "unparseable"], path: Path, detail: str = ""):
        self.status = status
        self.path = path
        self.detail = detail
        message = f"input: {status}: {path}"
        if detail:
            message += f" ({detail})"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ExportReport:
    """Auditable summary whose rate always uses the complete input corpus."""

    input_path: str
    output_path: str
    report_path: str
    input_status: Literal["ok"]
    input_count: int
    eligible_count: int
    exported_count: int
    rejected_count: int
    acceptance_rate: float | None
    gate_failure_counts: dict[str, int]
    invalid_case_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_human_approved(record: Mapping[str, Any]) -> bool:
    """Return true only for an explicit boolean approval attestation."""
    return record.get("is_human_approved", False) is True


def redaction_passed(record: Mapping[str, Any]) -> bool:
    """Return true only for the explicit passing redaction status."""
    return record.get("redaction_status", "needs_review") == "passed"


def _has_raw_input(record: Mapping[str, Any]) -> bool:
    """Whether a record carries raw material rather than a redacted case.

    ``None`` and an empty string contain no raw bytes.  Containers and other
    types are rejected even when falsey: an unparseable raw-input carrier must
    not turn into favourable evidence.
    """

    def contains(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if key == "raw_input" and nested is not None and nested != "":
                    return True
                if contains(nested):
                    return True
        elif isinstance(value, list):
            return any(contains(item) for item in value)
        return False

    return contains(record)


def is_trainable(record: Mapping[str, Any]) -> bool:
    """Apply all three independent export gates."""
    return is_human_approved(record) and redaction_passed(record) and not _has_raw_input(record)


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ExportInputError("missing", path)
    if not path.is_file():
        raise ExportInputError("unparseable", path, "not a file")

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ExportInputError("unparseable", path, str(exc)) from exc
    if not text.strip():
        return []

    try:
        if path.suffix.lower() == ".json":
            payload = json.loads(text)
            if isinstance(payload, dict):
                payload = payload.get("verdicts")
            if not isinstance(payload, list):
                raise ValueError("JSON input must be a list or an object with a verdicts list")
            records: Sequence[Any] = payload
        else:
            parsed: list[Any] = []
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    parsed.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
            records = parsed
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExportInputError("unparseable", path, str(exc)) from exc

    normalized: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ExportInputError(
                "unparseable",
                path,
                f"record {index} must be an object, got {type(record).__name__}",
            )
        normalized.append(record)
    return normalized


def _first(mapping: Mapping[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _stable_case_id(record: Mapping[str, Any], case_payload: Mapping[str, Any]) -> str:
    supplied = _first(case_payload, ("id", "case_id"))
    if isinstance(supplied, str) and supplied.strip():
        return supplied.strip()
    source_id = _first(record, ("verdict_id", "experiment_id", "run_id"))
    identity = (
        str(source_id)
        if source_id is not None
        else json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    )
    return f"evc_lab_{hashlib.sha256(identity.encode()).hexdigest()[:20]}"


def _source_provenance(record: Mapping[str, Any]) -> dict[str, Any]:
    nested = record.get("provenance")
    provenance = nested if isinstance(nested, Mapping) else {}
    result: dict[str, Any] = {}
    for field in _PROVENANCE_FIELDS:
        value = record[field] if field in record else provenance.get(field)
        result[field] = value
    invalidation_status = result["invalidation_status"]
    result["status"] = (
        invalidation_status if invalidation_status in {"valid", "invalidated"} else "unknown"
    )
    return result


def _build_eval_case(record: Mapping[str, Any]) -> dict[str, Any] | None:
    nested = record.get("eval_case")
    case_payload: Mapping[str, Any] = nested if isinstance(nested, Mapping) else record

    suite = _first(case_payload, ("suite", "suite_id", "eval_suite_id"))
    if suite is None:
        suite = _first(record, ("eval_suite_id", "suite_id", "suite"))
    if not isinstance(suite, str) or not suite.strip():
        return None

    case_input = _first(case_payload, ("input", "case_input", "redacted_input"))
    if case_input is None:
        case_input = _first(record, ("case_input", "redacted_input"))
    if not isinstance(case_input, dict):
        return None

    expected = _first(case_payload, ("expected", "expected_output"))
    if expected is None:
        expected = _first(record, ("expected", "expected_output"))
    if expected is not None and not isinstance(expected, dict):
        return None

    split = _first(case_payload, ("split",))
    if split is None:
        split = _first(record, ("split",))
    if split is None:
        split = "dev"
    if split not in {"dev", "held_out"}:
        return None
    # Never carry a held-out answer into a candidate-readable export.
    if split == "held_out" and expected is not None:
        return None

    rubric_value = _first(case_payload, ("rubric",))
    if rubric_value is None:
        rubric_value = _first(record, ("rubric",))
    if isinstance(rubric_value, str) and rubric_value.strip():
        rubric = rubric_value.strip()
        source_rubric_provenance = _first(case_payload, ("rubric_provenance",))
        if source_rubric_provenance is None:
            source_rubric_provenance = _first(record, ("rubric_provenance",))
        rubric_provenance = (
            source_rubric_provenance
            if source_rubric_provenance
            in {"human_authored", "synthesized_default", "source_record_unknown"}
            else "source_record_unknown"
        )
    elif rubric_value is None or rubric_value == "":
        rubric = DEFAULT_RUBRIC
        rubric_provenance = "synthesized_default"
    else:
        return None

    weight = _first(case_payload, ("weight",))
    if weight is None:
        weight = _first(record, ("weight",))
    if weight is None:
        weight = 1.0
    if isinstance(weight, bool) or not isinstance(weight, int | float) or weight <= 0:
        return None

    public = {
        "id": _stable_case_id(record, case_payload),
        "suite": suite.strip(),
        "split": split,
        "input": case_input,
        "expected": expected,
        "rubric": rubric,
        "weight": float(weight),
    }
    try:
        validated = EvalCase.model_validate(public).model_dump(mode="json")
    except ValueError:
        return None

    validated.update(
        {
            "export_schema_version": EXPORT_SCHEMA_VERSION,
            "rubric_provenance": rubric_provenance,
            "source_verdict_id": record.get("verdict_id"),
            "source_experiment_id": record.get("experiment_id"),
            "source_provenance": _source_provenance(record),
            "gate_evidence": {
                "is_human_approved": True,
                "redaction_status": "passed",
                "raw_input_present": False,
            },
        }
    )
    return validated


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
    temporary.replace(path)


def _output_paths(output: Path, report_path: Path | None) -> tuple[Path, Path]:
    if output.suffix.lower() == ".jsonl":
        eval_cases_path = output
        default_report = output.with_suffix(".report.json")
    else:
        eval_cases_path = output / "eval-cases.jsonl"
        default_report = output / "export-report.json"
    return eval_cases_path, report_path or default_report


def export_verdict_file(
    input_path: Path,
    output_path: Path,
    *,
    report_path: Path | None = None,
) -> ExportReport:
    """Read verdicts, apply the gates, and write eval JSONL plus a report."""
    records = _load_records(input_path)
    eval_cases_path, resolved_report_path = _output_paths(output_path, report_path)

    exported: list[dict[str, Any]] = []
    eligible_count = 0
    invalid_case_count = 0
    gate_failures = {
        "approval_absent_or_false": 0,
        "redaction_not_passed": 0,
        "raw_input_present": 0,
    }

    for record in records:
        approved = is_human_approved(record)
        redacted = redaction_passed(record)
        raw_input_present = _has_raw_input(record)
        if not approved:
            gate_failures["approval_absent_or_false"] += 1
        if not redacted:
            gate_failures["redaction_not_passed"] += 1
        if raw_input_present:
            gate_failures["raw_input_present"] += 1
        if not is_trainable(record):
            continue

        eligible_count += 1
        eval_case = _build_eval_case(record)
        if eval_case is None:
            invalid_case_count += 1
            continue
        exported.append(eval_case)

    input_count = len(records)
    exported_count = len(exported)
    acceptance_rate = exported_count / input_count if input_count else None
    report = ExportReport(
        input_path=str(input_path),
        output_path=str(eval_cases_path),
        report_path=str(resolved_report_path),
        input_status="ok",
        input_count=input_count,
        eligible_count=eligible_count,
        exported_count=exported_count,
        rejected_count=input_count - exported_count,
        acceptance_rate=acceptance_rate,
        gate_failure_counts=gate_failures,
        invalid_case_count=invalid_case_count,
    )

    jsonl = "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n" for row in exported
    )
    _atomic_write_text(eval_cases_path, jsonl)
    _atomic_write_text(
        resolved_report_path,
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.tracelab.export",
        description="Export approved, redacted lab verdicts as eval-case JSONL.",
    )
    parser.add_argument("--input", required=True, type=Path, help="source JSON or JSONL verdicts")
    parser.add_argument(
        "--out",
        "--output",
        required=True,
        dest="output",
        type=Path,
        help="destination JSONL path or artifact directory",
    )
    parser.add_argument("--report", type=Path, default=None, help="optional report JSON path")
    args = parser.parse_args(argv)

    try:
        report = export_verdict_file(args.input, args.output, report_path=args.report)
    except ExportInputError as exc:
        print(str(exc), file=sys.stderr)
        return INPUT_ERROR_EXIT_CODE

    print(json.dumps(report.to_dict(), sort_keys=True))
    return 0


__all__ = [
    "Candidate",
    "CycleReport",
    "CycleStatus",
    "Decision",
    "EpisodicEvent",
    "ExportInputError",
    "ExportReport",
    "Lesson",
    "export_verdict_file",
    "is_human_approved",
    "is_trainable",
    "main",
    "redaction_passed",
]


if __name__ == "__main__":
    raise SystemExit(main())
