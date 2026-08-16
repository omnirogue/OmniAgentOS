"""Guards for the gap -> loop-queue finding adapter.

Every test builds its own scratch queue under ``tmp_path``. The real queue at
``var/loopqueue`` is never touched, and ``test_default_queue_is_never_written_by_tests``
below is the mechanical statement of that promise.
"""

from __future__ import annotations

import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
import yaml

import scripts.northstar_cert.file_gap_findings as filer
from scripts.northstar_cert.canonical import content_identity
from scripts.northstar_cert.emit_gaps import _SCHEMA_LIVE, emit_gaps
from scripts.northstar_cert.file_gap_findings import (
    DEFAULT_QUEUE,
    LIVE_ENV_FLAG,
    LIVE_SCHEMA,
    GapFilingError,
    GapFilingNotArmed,
    build_finding,
    file_gap_findings,
    main,
)
from scripts.northstar_cert.record_results import record_results

LEDGER_EVENT_SCHEMA = (
    Path.home() / "Work" / "Ops" / "ThreeLoops" / "schema" / "ledger-event.schema.json"
)

# The envelope schema lives in ThreeLoops today and moves to pipeline/schema/
# with the convergence migration (Convergence-Brief-2026-08-09) — accept either.
_SCHEMA_CANDIDATES = (
    Path("pipeline/schema/envelope.schema.json"),
    Path.home() / "Work" / "Ops" / "ThreeLoops" / "schema" / "envelope.schema.json",
)
THREELOOPS_SCHEMA = next(
    (p for p in _SCHEMA_CANDIDATES if p.is_file()), _SCHEMA_CANDIDATES[-1]
)


# --------------------------------------------------------------------------- fixtures


def _queue(tmp_path: Path) -> Path:
    queue = tmp_path / "loopqueue"
    for name in ("findings", "rejected", "parked", "proposals", "claims"):
        (queue / name).mkdir(parents=True, exist_ok=True)
    return queue


def _gap(**overrides: Any) -> dict[str, Any]:
    """A gap artifact in exactly the shape ``emit_gaps`` writes."""
    identity = {
        "check_id": "NSC-C14-FAIL",
        "capability": "C-14",
        "project": "estate",
        "scope": "scenario",
        "verdict": "FAIL",
        "reason_class": "pytest_failure",
    }
    identity.update(overrides.pop("identity_payload", {}))
    gap = {
        "schema": "omniagentos.northstar-gap.v1",
        "dry_run": False,
        "gap_id": "nsgap-abc123",
        # The REAL emitter signature over this identity. A placeholder here would
        # be a forgery the filer now refuses — which is the point of R1-011.
        "signature": content_identity(identity),
        "identity_payload": identity,
        "check_id": identity["check_id"],
        "capability": identity["capability"],
        "project": identity["project"],
        "scenario": "tests/example.py::test_fail",
        "expected": "PASS",
        "actual": "FAIL(pytest_failure:wrong result)",
        "evidence_refs": [
            {
                "run_id": "run-one",
                "receipt": "var/gate-evidence/records/northstar-cert/run-one.json",
                "bundle": "var/northstar-cert/runs/run-one",
            }
        ],
        "severity": "high",
        "hard_gate": False,
        "frequency": 1,
        "cause_class": "broken",
        "recommended_next_step": "review evidence for NSC-C14-FAIL before planning a fix",
        "first_seen_run": "run-one",
        "latest_run": "run-one",
        "observed_runs": ["run-one"],
    }
    gap.update(overrides)
    return gap


def _write_gaps(tmp_path: Path, *gaps: dict[str, Any]) -> Path:
    directory = tmp_path / "gaps"
    directory.mkdir(parents=True, exist_ok=True)
    for index, gap in enumerate(gaps):
        (directory / f"gap-{index}.json").write_text(json.dumps(gap, indent=2), encoding="utf-8")
    return directory


def _arm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LIVE_ENV_FLAG, "1")


def _ledger_lines(queue: Path) -> list[dict[str, Any]]:
    ledger = queue / "ledger.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]


def _assert_valid_envelope(envelope: dict[str, Any], *, kind: str = "finding") -> None:
    """Validate against the real ThreeLoops schema when it is readable here."""
    try:
        schema = json.loads(THREELOOPS_SCHEMA.read_text(encoding="utf-8"))
    except OSError:
        schema = None
    if schema is not None:
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(envelope, schema)
        return
    for key in ("id", "kind", "title", "created_at", "producer", "payload"):
        assert key in envelope, f"envelope missing required field {key}"
    assert envelope["kind"] == kind
    required = ("symptom",) if kind == "finding" else ("area", "observation", "why_not_a_fix")
    for field in required:
        assert envelope["payload"][field]
    assert envelope["producer"]["role"] in {"planner", "reviewer", "implementer", "external"}
    assert 1 <= len(envelope["title"]) <= 200


# --------------------------------------------------------------------------- identity


def test_finding_id_is_the_sha256_of_the_payload_alone() -> None:
    envelope = build_finding(_gap())
    assert envelope["id"] == content_identity(envelope["payload"])
    assert envelope["id"].startswith("sha256:")
    assert len(envelope["id"]) == len("sha256:") + 64


def test_identity_ignores_everything_that_changes_between_runs() -> None:
    """The dedup contract: two runs observing one breakage mint ONE id.

    If a run id, a receipt path, a timestamp or a frequency counter ever leaks
    into the payload, this fails — and in production it would instead flood the
    queue with one finding per day, silently.
    """
    first = build_finding(_gap(), created_at="2026-08-09T06:10:00Z")
    second = build_finding(
        _gap(
            frequency=17,
            first_seen_run="run-one",
            latest_run="run-seventeen",
            observed_runs=[f"run-{index}" for index in range(17)],
            evidence_refs=[
                {
                    "run_id": "run-seventeen",
                    "receipt": "var/gate-evidence/records/northstar-cert/run-seventeen.json",
                    "bundle": "var/northstar-cert/runs/run-seventeen",
                }
            ],
            actual="FAIL(pytest_failure:a different message)",
        ),
        created_at="2026-08-26T06:10:00Z",
    )
    assert first["id"] == second["id"]
    assert first["payload"] == second["payload"]
    # ...and the run-specific data is still carried, just not hashed.
    assert second["northstar_cert"]["frequency"] == 17
    assert second["evidence"][0]["claim"].endswith("for NSC-C14-FAIL")


def test_a_different_check_is_a_different_finding() -> None:
    other = _gap(identity_payload={"check_id": "NSC-C14-ABSENT"}, check_id="NSC-C14-ABSENT")
    assert build_finding(_gap())["id"] != build_finding(other)["id"]


@pytest.mark.parametrize(
    ("verdict", "hard_gate", "expected"),
    [
        ("FAIL", True, 1),
        ("NOT_EVALUABLE", True, 1),
        ("FAIL", False, 2),
        ("NOT_EVALUABLE", False, 3),
    ],
)
def test_priority_mapping(verdict: str, hard_gate: bool, expected: int) -> None:
    gap = _gap(identity_payload={"verdict": verdict}, hard_gate=hard_gate)
    assert build_finding(gap)["priority"] == expected


# --------------------------------------------------------------------------- filing


def test_live_filing_writes_a_valid_envelope_and_one_ledger_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())

    result = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert result.filed and result.would_file == []
    written = list((queue / "findings").glob("*.json"))
    assert len(written) == 1
    envelope = json.loads(written[0].read_text(encoding="utf-8"))
    _assert_valid_envelope(envelope)
    assert written[0].name == envelope["id"].replace(":", "_") + ".json"
    assert envelope["producer"] == {"role": "external", "actor": "northstar-cert"}
    assert envelope["title"].startswith("northstar-cert: NSC-C14-FAIL")
    assert oct(written[0].stat().st_mode)[-3:] == "644"

    events = _ledger_lines(queue)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "found"
    assert event["role"] == "external"
    assert event["actor"] == "northstar-cert"
    assert event["id"] == envelope["id"]
    assert event["ts"].endswith("Z")
    assert event["detail"]["check_id"] == "NSC-C14-FAIL"


def test_refiling_an_identical_gap_is_skipped_and_writes_no_second_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    first = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    artifact = queue / "findings" / (first.filed[0].replace(":", "_") + ".json")
    before = artifact.read_bytes()

    second = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert second.filed == []
    assert second.skipped_existing == 1
    assert artifact.read_bytes() == before
    assert len(_ledger_lines(queue)) == 1


@pytest.mark.parametrize("terminal", ["parked", "rejected"])
def test_a_terminal_marker_skips_the_gap_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    """A parked or rejected gap that is re-detected every morning must not
    error and must not walk back into findings/."""
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    identifier = build_finding(_gap())["id"]
    marker = queue / terminal / (identifier.replace(":", "_") + ".json")
    marker.write_text(json.dumps({"id": identifier, "reason": "human decision"}), encoding="utf-8")

    result = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert result.filed == []
    assert result.skipped_existing == 1
    assert list((queue / "findings").glob("*.json")) == []
    assert _ledger_lines(queue) == []


def test_a_resolved_gap_is_not_filed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap(resolved_run="run-nine", resolved_at="2026-08-09T06:10:00Z"))

    result = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert result.skipped_resolved == 1
    assert result.filed == []
    assert list((queue / "findings").glob("*.json")) == []


def test_dry_run_reports_and_writes_nothing(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())

    result = file_gap_findings(gaps_dir=gaps, queue=queue, live=False)

    assert result.would_file and result.filed == []
    assert list((queue / "findings").glob("*.json")) == []
    assert not (queue / "ledger.jsonl").exists()


def test_live_filing_refuses_a_dry_run_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing live filing at the dry-run corpus is a wiring error, and a
    wiring error must not read as 'no gaps today'."""
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap(dry_run=True))

    with pytest.raises(GapFilingError, match="dry-run artifact"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=True)


def test_a_gap_missing_identity_fields_is_refused(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    broken = _gap()
    del broken["identity_payload"]["scope"]
    gaps = _write_gaps(tmp_path, broken)

    with pytest.raises(GapFilingError, match="identity_payload is missing scope"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=False)


# --------------------------------------------------------------------------- fail-closed


def test_missing_queue_is_refused_in_both_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)  # armed on purpose: the refusal below is about the QUEUE
    gaps = _write_gaps(tmp_path, _gap())
    for live in (False, True):
        with pytest.raises(GapFilingError, match="queue root is not a directory"):
            file_gap_findings(gaps_dir=gaps, queue=tmp_path / "absent", live=live)


def test_a_queue_without_parked_is_refused(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    (queue / "parked").rmdir()
    gaps = _write_gaps(tmp_path, _gap())
    with pytest.raises(GapFilingError, match="missing parked/"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=False)


def test_an_unwritable_findings_dir_is_refused_rather_than_degraded(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    findings = queue / "findings"
    findings.chmod(0o500)
    try:
        gaps = _write_gaps(tmp_path, _gap())
        with pytest.raises(GapFilingError, match="findings/ is not writable"):
            file_gap_findings(gaps_dir=gaps, queue=queue, live=False)
    finally:
        findings.chmod(0o755)


# --------------------------------------------------------------------------- CLI


def test_live_is_refused_without_the_environment_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(LIVE_ENV_FLAG, raising=False)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())

    code = main(["--gaps-dir", str(gaps), "--queue", str(queue), "--live"])

    assert code == 2
    assert LIVE_ENV_FLAG in json.loads(capsys.readouterr().out)["error"]
    assert list((queue / "findings").glob("*.json")) == []
    assert not (queue / "ledger.jsonl").exists()


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_only_an_exact_1_arms_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(LIVE_ENV_FLAG, value)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    assert main(["--gaps-dir", str(gaps), "--queue", str(queue), "--live"]) == 2
    assert list((queue / "findings").glob("*.json")) == []


def test_cli_dry_run_is_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _arm(monkeypatch)  # armed, but no --live: still a dry run
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())

    code = main(["--gaps-dir", str(gaps), "--queue", str(queue)])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["live"] is False
    assert len(report["would_file"]) == 1
    assert report["filed"] == []
    assert list((queue / "findings").glob("*.json")) == []


def test_cli_reports_exit_2_for_an_unusable_queue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    gaps = _write_gaps(tmp_path, _gap())
    code = main(["--gaps-dir", str(gaps), "--queue", str(tmp_path / "nope")])
    assert code == 2
    assert "queue root is not a directory" in json.loads(capsys.readouterr().out)["error"]


def test_the_write_boundary_itself_needs_both_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`live=True` from Python, with no environment key, must REFUSE.

    The CLI check is not the boundary — this function is. A two-key arming any
    in-process caller can satisfy with one key is a one-key arming.
    """
    monkeypatch.delenv(LIVE_ENV_FLAG, raising=False)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())

    with pytest.raises(GapFilingNotArmed, match=LIVE_ENV_FLAG):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert list((queue / "findings").glob("*.json")) == []
    assert not (queue / "ledger.jsonl").exists()
    # ...and the refusal is a GapFilingError, so the CLI still exits 2.
    assert issubclass(GapFilingNotArmed, GapFilingError)


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_the_write_boundary_accepts_only_an_exact_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(LIVE_ENV_FLAG, value)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    with pytest.raises(GapFilingNotArmed):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    assert list((queue / "findings").glob("*.json")) == []


# --------------------------------------------------------------------------- gap schema


@pytest.mark.parametrize("field", ["schema", "dry_run", "signature"])
def test_a_gap_that_omits_its_mode_or_provenance_is_refused(tmp_path: Path, field: str) -> None:
    """A missing `dry_run` used to read as live (None is falsy). Absence of the
    mode is now a refusal, not a favourable default."""
    queue = _queue(tmp_path)
    broken = _gap()
    del broken[field]
    gaps = _write_gaps(tmp_path, broken)

    with pytest.raises(GapFilingError, match=f"is missing {field}"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=False)


def test_a_forged_gap_with_no_schema_mode_or_receipt_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole malformed shape at once: no schema, no mode, no signature, no
    receipt. Armed and live — and it still writes nothing."""
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    forged = {
        "identity_payload": {
            "check_id": "NSC-X",
            "capability": "C-X",
            "project": "estate",
            "scope": "scenario",
            "verdict": "FAIL",
            "reason_class": "pytest_failure",
        },
        "check_id": "NSC-X",
        "cause_class": "broken",
        "severity": "high",
        "hard_gate": False,
        "actual": "FAIL(forged)",
        "evidence_refs": [{"run_id": "forged"}],
    }
    gaps = _write_gaps(tmp_path, forged)

    with pytest.raises(GapFilingError, match="is missing"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert list((queue / "findings").glob("*.json")) == []
    assert not (queue / "ledger.jsonl").exists()


def test_a_non_boolean_mode_is_refused(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap(dry_run="false"))
    with pytest.raises(GapFilingError, match="non-boolean dry_run"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=False)


def test_live_filing_refuses_a_foreign_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap(schema="something.else.v9"))
    with pytest.raises(GapFilingError, match="not the live gap schema"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    assert list((queue / "findings").glob("*.json")) == []


@pytest.mark.parametrize("live", [False, True])
def test_a_forged_all_zero_signature_never_reaches_the_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: bool
) -> None:
    """A `signature` checked only for being a non-empty string is not a signature.

    An armed live filing accepted `sha256:000…0` and published the finding
    (R1-011). The signature is RECOMPUTED from `identity_payload` — in dry run
    too, so the preview and the live pass agree about what is admissible.
    """
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap(signature="sha256:" + "0" * 64))

    with pytest.raises(GapFilingError, match="does not match its identity_payload"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=live)

    assert list((queue / "findings").glob("*.json")) == []
    assert not (queue / "ledger.jsonl").exists()


def test_a_top_level_identity_that_contradicts_the_payload_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The finding is built from `identity_payload`; the gap's own `check_id`
    is what a human reads. A file where they disagree describes one check under
    the name of another, and it is a forgery whatever the signature says."""
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    for field, value in (
        ("check_id", "NSC-CONFLICT"),
        ("capability", "C-OTHER"),
        ("project", "not-estate"),
    ):
        gaps = _write_gaps(tmp_path, _gap(**{field: value}))
        with pytest.raises(GapFilingError, match=f"top-level {field}"):
            file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    assert list((queue / "findings").glob("*.json")) == []
    assert not (queue / "ledger.jsonl").exists()


def test_a_signature_forged_to_match_a_tampered_identity_still_names_that_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recomputation is not a proof of ORIGIN — nothing here is signed with a
    key — so state exactly what it buys: an artifact whose signature, identity
    payload and top-level fields cannot disagree with each other.

    A forger who rewrites all of them consistently gets a finding about the
    check they named, which is the same thing the emitter would have produced;
    what they can no longer do is smuggle one check's evidence under another
    check's name.
    """
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    tampered = _gap(identity_payload={"check_id": "NSC-OTHER"}, check_id="NSC-OTHER")
    gaps = _write_gaps(tmp_path, tampered)

    result = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    envelope = json.loads(
        (queue / "findings" / (result.filed[0].replace(":", "_") + ".json")).read_text(
            encoding="utf-8"
        )
    )
    assert envelope["payload"]["check_id"] == "NSC-OTHER"
    assert envelope["northstar_cert"]["gap_signature"] == tampered["signature"]


def test_the_live_schema_constant_matches_the_emitter() -> None:
    """Two carriers of one constant — pinned equal so they cannot drift."""
    assert LIVE_SCHEMA == _SCHEMA_LIVE


def test_evidence_says_whether_the_cited_receipt_is_actually_there(tmp_path: Path) -> None:
    """`verified_by: reading` must not stand over a receipt nobody read."""
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}", encoding="utf-8")
    present = build_finding(
        _gap(evidence_refs=[{"run_id": "run-one", "receipt": str(receipt), "bundle": "b"}])
    )
    assert present["evidence"][0]["result"] == "receipt-present"
    assert present["evidence"][0]["recorded"].startswith("FAIL(")

    absent = build_finding(
        _gap(evidence_refs=[{"run_id": "run-one", "receipt": str(tmp_path / "gone.json")}])
    )
    assert absent["evidence"][0]["result"] == "receipt-missing"
    # Never dropped: an evidence entry that vanishes is how an uncheckable
    # citation passes for a verified one.
    assert len(absent["evidence"]) == 1

    unreferenced = build_finding(_gap(evidence_refs=[{"run_id": "run-one"}]))
    assert unreferenced["evidence"][0]["result"] == "receipt-missing"
    assert "receipt" not in unreferenced["evidence"][0]


# --------------------------------------------------------------------------- ledger repair


def test_an_orphaned_artifact_gets_its_missing_found_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publish succeeded, ledger append failed: the retry must REPAIR, not skip.

    Skipping reports the favourable-looking `skipped_existing=1` while the
    ledger-derived view of the queue never sees the item at all.
    """
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("simulated ledger full")

    monkeypatch.setattr(filer, "_append_ledger", explode)
    with pytest.raises(OSError, match="simulated ledger full"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    monkeypatch.undo()
    _arm(monkeypatch)

    orphan = list((queue / "findings").glob("*.json"))
    assert len(orphan) == 1 and _ledger_lines(queue) == []

    repaired = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert repaired.skipped_existing == 1
    assert repaired.ledger_repaired == 1
    events = _ledger_lines(queue)
    assert [event["event"] for event in events] == ["found"]
    assert events[0]["detail"]["repaired"] is True
    assert events[0]["id"] == json.loads(orphan[0].read_text(encoding="utf-8"))["id"]

    # The repair is idempotent: a third pass finds the event and appends nothing.
    third = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    assert third.ledger_repaired == 0
    assert len(_ledger_lines(queue)) == 1


# --------------------------------------------------------------------------- resolution


def test_resolution_asks_the_owning_loop_to_close_and_never_terminalizes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PASS raises an INQUIRY; it does not mint `completed` (R1-009 ruling).

    `completed` is the contract's terminal event for work that was verified AND
    APPLIED (CONTRACT.md §5). This producer read a certification receipt — it
    applied nothing — so claiming the terminal event both overstates what it did
    and lets an external process close another loop's (possibly parked) item.
    The resolved evidence stays on the gap artifact; the queue gets a question.
    """
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    filed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True).filed[0]

    resolved_gap = _gap(resolved_run="run-nine", resolved_at="2026-08-09T06:10:00Z")
    _write_gaps(tmp_path, resolved_gap)
    artifact_before = (gaps / "gap-0.json").read_bytes()
    closed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert closed.skipped_resolved == 1 and closed.inquiries_filed == 1
    events = _ledger_lines(queue)
    assert [event["event"] for event in events] == ["found", "inquired"]
    # The finding itself is untouched and un-terminalized.
    assert (queue / "findings" / (filed.replace(":", "_") + ".json")).exists()
    assert not any(event["event"] in {"merged", "completed", "rejected"} for event in events)
    # The resolved evidence is PRESERVED, not consumed.
    assert (gaps / "gap-0.json").read_bytes() == artifact_before

    published = list((queue / "inquiries").glob("*.json"))
    assert len(published) == 1
    inquiry = json.loads(published[0].read_text(encoding="utf-8"))
    _assert_valid_envelope(inquiry, kind="inquiry")
    assert inquiry["id"] == events[1]["id"] == content_identity(inquiry["payload"])
    assert inquiry["payload"]["finding_id"] == filed
    assert inquiry["payload"]["resolved_run"] == "run-nine"
    # The location is provenance, never identity — see the dedup test below.
    assert "queue_state" not in inquiry["payload"]
    assert inquiry["northstar_cert"]["queue_state"] == "findings"
    assert events[1]["detail"]["queue_state"] == "findings"
    assert filed in inquiry["payload"]["question"]
    assert "run-nine" in inquiry["payload"]["question"]
    assert events[1]["detail"]["finding_id"] == filed

    # Exactly one question per resolution, however many times the gap is re-read.
    again = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    assert again.inquiries_filed == 0
    assert len(_ledger_lines(queue)) == 2
    assert len(list((queue / "inquiries").glob("*.json"))) == 1


def test_a_resolution_reaches_a_parked_occurrence_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parked work waits on a HUMAN, so it is exactly the case that must be asked
    rather than closed."""
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    filed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True).filed[0]
    name = filed.replace(":", "_") + ".json"
    (queue / "findings" / name).rename(queue / "parked" / name)

    _write_gaps(tmp_path, _gap(resolved_run="run-nine", resolved_at="2026-08-09T06:10:00Z"))
    closed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert closed.inquiries_filed == 1
    assert (queue / "parked" / name).exists()
    inquiry = json.loads(next((queue / "inquiries").glob("*.json")).read_text(encoding="utf-8"))
    assert inquiry["northstar_cert"]["queue_state"] == "parked"


def test_a_rejected_occurrence_is_not_asked_about(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is nothing to reconcile about an occurrence the loop refused."""
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    filed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True).filed[0]
    name = filed.replace(":", "_") + ".json"
    (queue / "findings" / name).rename(queue / "rejected" / name)

    _write_gaps(tmp_path, _gap(resolved_run="run-nine", resolved_at="2026-08-09T06:10:00Z"))
    closed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert closed.skipped_resolved == 1 and closed.inquiries_filed == 0
    assert not (queue / "inquiries").exists() or not list((queue / "inquiries").glob("*.json"))


def test_a_resolution_of_an_unqueued_gap_asks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No finding on the desk, no question to ask — and still no queue write."""
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap(resolved_run="run-nine", resolved_at="2026-08-09T06:10:00Z"))

    closed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert closed.skipped_resolved == 1 and closed.inquiries_filed == 0
    assert _ledger_lines(queue) == []


def test_a_question_already_asked_is_not_asked_again_after_the_loop_files_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifact is the arbiter, but the ledger is a second suppressor.

    Once the owning loop has picked the inquiry up and moved the artifact
    somewhere this filer does not look, the ledger is the only remaining record
    that the question was asked — and asking it again every morning is how a
    queue becomes unreadable.
    """
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    _write_gaps(tmp_path, _gap(resolved_run="run-nine", resolved_at="2026-08-09T06:10:00Z"))
    assert file_gap_findings(gaps_dir=gaps, queue=queue, live=True).inquiries_filed == 1

    for published in (queue / "inquiries").glob("*.json"):
        published.unlink()

    again = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert again.inquiries_filed == 0
    assert list((queue / "inquiries").glob("*.json")) == []
    assert [event["event"] for event in _ledger_lines(queue)] == ["found", "inquired"]


def test_an_orphaned_inquiry_gets_its_missing_inquired_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inquiry path publishes then appends, so it needs the finding path's
    repair-on-read (R1-009 residue).

    A crash between the two writes leaves the question ON DISK with nothing in
    the ledger. Reading the artifact alone as "already asked" froze that state
    forever: the caller got the favourable-looking `inquiries_filed=0`, and every
    ledger-derived view of the queue was permanently missing the question.
    """
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    filed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True).filed[0]
    _write_gaps(tmp_path, _gap(resolved_run="run-nine", resolved_at="2026-08-09T06:10:00Z"))

    real_append = filer._append_ledger

    def explode_on_inquiry(queue_path: Path, event: dict[str, Any]) -> None:
        if event.get("event") == "inquired":
            raise OSError("simulated ledger full")
        real_append(queue_path, event)

    monkeypatch.setattr(filer, "_append_ledger", explode_on_inquiry)
    with pytest.raises(OSError, match="simulated ledger full"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    monkeypatch.undo()
    _arm(monkeypatch)

    orphan = list((queue / "inquiries").glob("*.json"))
    assert len(orphan) == 1
    assert [event["event"] for event in _ledger_lines(queue)] == ["found"]

    repaired = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    # A repair is NOT a new question: no second artifact, and its own counter.
    assert repaired.inquiries_filed == 0
    assert repaired.inquiries_ledger_repaired == 1
    assert len(list((queue / "inquiries").glob("*.json"))) == 1
    events = _ledger_lines(queue)
    assert [event["event"] for event in events] == ["found", "inquired"]
    assert events[1]["id"] == json.loads(orphan[0].read_text(encoding="utf-8"))["id"]
    assert events[1]["detail"]["repaired"] is True
    assert events[1]["detail"]["finding_id"] == filed

    # Idempotent: a third pass finds the event and appends nothing.
    third = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    assert third.inquiries_ledger_repaired == 0 and third.inquiries_filed == 0
    assert len(_ledger_lines(queue)) == 2


def test_moving_the_finding_between_queue_directories_asks_no_second_question(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One resolution is ONE question, wherever the occurrence is sitting.

    `queue_state` used to participate in the hashed inquiry payload, so a human
    parking a still-open finding overnight changed the inquiry's identity and the
    next morning's pass minted a second question about the same resolution
    (observed `inquiry_count=2`). The location is provenance, not identity.
    """
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    filed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True).filed[0]
    _write_gaps(tmp_path, _gap(resolved_run="run-nine", resolved_at="2026-08-09T06:10:00Z"))
    assert file_gap_findings(gaps_dir=gaps, queue=queue, live=True).inquiries_filed == 1
    asked = json.loads(next((queue / "inquiries").glob("*.json")).read_text(encoding="utf-8"))

    name = filed.replace(":", "_") + ".json"
    (queue / "findings" / name).rename(queue / "parked" / name)
    again = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert again.inquiries_filed == 0 and again.inquiries_ledger_repaired == 0
    published = list((queue / "inquiries").glob("*.json"))
    assert len(published) == 1
    assert json.loads(published[0].read_text(encoding="utf-8"))["id"] == asked["id"]
    assert [event["event"] for event in _ledger_lines(queue)] == ["found", "inquired"]
    # The identity is the resolution's, so it survives the move byte-for-byte.
    assert content_identity(asked["payload"]) == asked["id"]


def test_a_dry_run_asks_no_question(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _arm(monkeypatch)  # armed, but the call is not live
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    _write_gaps(tmp_path, _gap(resolved_run="run-nine", resolved_at="2026-08-09T06:10:00Z"))

    dry = file_gap_findings(gaps_dir=gaps, queue=queue, live=False)

    assert dry.skipped_resolved == 1 and dry.inquiries_filed == 0
    assert list((queue / "inquiries").glob("*.json")) == []
    assert [event["event"] for event in _ledger_lines(queue)] == ["found"]


def test_a_regression_after_parking_reopens_work_under_a_new_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """resolve -> park -> regress: the recurrence must become actionable again.

    Parking never decays, so the ORIGINAL id must stay parked. A regression is
    new information, so it files under a new identity that names the old one.
    """
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    original = file_gap_findings(gaps_dir=gaps, queue=queue, live=True).filed[0]

    # The loop parks the occurrence, then the emitter stamps it resolved.
    name = original.replace(":", "_") + ".json"
    (queue / "findings" / name).rename(queue / "parked" / name)
    _write_gaps(tmp_path, _gap(resolved_run="run-two", resolved_at="2026-08-09T06:10:00Z"))
    file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    # ...and then it breaks again: the emitter demotes the stamp to a regression.
    _write_gaps(
        tmp_path,
        _gap(
            regressions=[
                {
                    "resolved_run": "run-two",
                    "resolved_at": "2026-08-09T06:10:00Z",
                    "regressed_run": "run-three",
                    "regressed_at": "2026-08-10T06:10:00Z",
                }
            ]
        ),
    )
    regressed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert regressed.regressions_filed == 1
    assert len(regressed.filed) == 1
    reopened = regressed.filed[0]
    assert reopened != original
    # The parked occurrence is untouched; the new one cites it.
    assert (queue / "parked" / name).exists()
    envelope = json.loads(
        (queue / "findings" / (reopened.replace(":", "_") + ".json")).read_text(encoding="utf-8")
    )
    _assert_valid_envelope(envelope)
    assert envelope["payload"]["regression_of"] == original
    assert envelope["payload"]["regression_run"] == "run-three"
    assert "REGRESSED" in envelope["payload"]["symptom"]
    assert envelope["id"] == content_identity(envelope["payload"])

    # A second pass over the same regression is dedup, not a second envelope.
    assert file_gap_findings(gaps_dir=gaps, queue=queue, live=True).filed == []
    assert len(list((queue / "findings").glob("*.json"))) == 1


def test_the_same_regression_read_twice_is_dedup_not_a_second_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    original = file_gap_findings(gaps_dir=gaps, queue=queue, live=True).filed[0]
    name = original.replace(":", "_") + ".json"
    (queue / "findings" / name).rename(queue / "rejected" / name)
    regression = _gap(regressions=[{"resolved_run": "run-two", "regressed_run": "run-three"}])
    _write_gaps(tmp_path, regression)
    assert file_gap_findings(gaps_dir=gaps, queue=queue, live=True).regressions_filed == 1

    # The identity is stable across the daily runs in between: re-reading the
    # SAME regressions list files nothing and appends nothing.
    repeat = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    assert repeat.filed == [] and repeat.skipped_existing == 1
    assert len(list((queue / "findings").glob("*.json"))) == 1
    assert len(_ledger_lines(queue)) == 2


def test_a_second_regression_cycle_files_against_the_latest_chain_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The R1-009 chain defect: the SECOND recurrence must not silently freeze.

    Keying re-entry on the original identity meant the first regression (still
    open in findings/) matched every later cycle, so a genuine second
    resolve→regress was reported as `skipped_existing` and disappeared. A new
    cycle is new information — the check was observed PASSING and then broke
    again — so it files under a new identity naming the occurrence it regressed
    from, which is the LATEST chain member, not the original.
    """
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    original = file_gap_findings(gaps_dir=gaps, queue=queue, live=True).filed[0]
    name = original.replace(":", "_") + ".json"
    (queue / "findings" / name).rename(queue / "parked" / name)

    first_cycle = [{"resolved_run": "run-two", "regressed_run": "run-three"}]
    _write_gaps(tmp_path, _gap(regressions=first_cycle))
    first = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    assert len(first.filed) == 1
    reg_one = first.filed[0]

    # It passes again — the still-open regression gets a closure QUESTION...
    _write_gaps(
        tmp_path,
        _gap(regressions=first_cycle, resolved_run="run-four", resolved_at="2026-08-10T06:10:00Z"),
    )
    closed = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    assert closed.inquiries_filed == 1
    inquiry = json.loads(next((queue / "inquiries").glob("*.json")).read_text(encoding="utf-8"))
    assert inquiry["payload"]["finding_id"] == reg_one, "the ACTIVE occurrence, not the original"

    # ...and then it breaks again. That is a new occurrence, and it must land.
    _write_gaps(
        tmp_path,
        _gap(
            regressions=[
                *first_cycle,
                {"resolved_run": "run-four", "regressed_run": "run-five"},
            ]
        ),
    )
    second = file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    assert len(second.filed) == 1 and second.regressions_filed == 1
    reg_two = second.filed[0]
    assert reg_two not in {original, reg_one}
    envelope = json.loads(
        (queue / "findings" / (reg_two.replace(":", "_") + ".json")).read_text(encoding="utf-8")
    )
    _assert_valid_envelope(envelope)
    # The chain: each regression names its PREDECESSOR, not always the original.
    assert envelope["payload"]["regression_of"] == reg_one
    assert envelope["payload"]["regression_run"] == "run-five"
    assert envelope["id"] == content_identity(envelope["payload"])
    # And the cycle is still dedup-stable on a re-read.
    assert file_gap_findings(gaps_dir=gaps, queue=queue, live=True).filed == []


def test_a_regressions_list_with_no_run_id_is_refused(tmp_path: Path) -> None:
    queue = _queue(tmp_path)
    identifier = build_finding(_gap())["id"]
    marker = queue / "parked" / (identifier.replace(":", "_") + ".json")
    marker.write_text(json.dumps({"id": identifier}), encoding="utf-8")
    gaps = _write_gaps(tmp_path, _gap(regressions=[{"resolved_run": "run-two"}]))
    with pytest.raises(GapFilingError, match="no regressed_run"):
        file_gap_findings(gaps_dir=gaps, queue=queue, live=False)


def test_every_ledger_event_validates_against_the_contract_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger event vocabulary is a CLOSED enum (CONTRACT.md §5). This is the
    mechanical guard against this adapter inventing an event kind."""
    if not LEDGER_EVENT_SCHEMA.is_file():
        pytest.skip("ThreeLoops ledger-event schema is not readable here")
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(LEDGER_EVENT_SCHEMA.read_text(encoding="utf-8"))
    _arm(monkeypatch)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    file_gap_findings(gaps_dir=gaps, queue=queue, live=True)
    _write_gaps(tmp_path, _gap(resolved_run="run-two", resolved_at="2026-08-09T06:10:00Z"))
    file_gap_findings(gaps_dir=gaps, queue=queue, live=True)

    events = _ledger_lines(queue)
    assert {event["event"] for event in events} == {"found", "inquired"}
    for event in events:
        jsonschema.validate(event, schema)


def test_default_queue_is_never_written_by_tests() -> None:
    """The default is the REAL queue; every test above passes its own."""
    assert DEFAULT_QUEUE == Path("/Users/youruser/OmniAgentOS/var/loopqueue")


# --------------------------------------------------------------------------- end to end


def _manifest(tmp_path: Path) -> Path:
    base = {
        "capability": "C-14",
        "tier": "t1",
        "gate": False,
        "requires": [],
        "scope": "scenario",
        "provenance": ["unit-test"],
    }
    checks = [
        {
            **base,
            "id": "NSC-C14-PASS",
            "binding": {"type": "pytest", "target": "tests/example.py::test_pass"},
        },
        {
            **base,
            "id": "NSC-C14-FAIL",
            "binding": {"type": "pytest", "target": "tests/example.py::test_fail"},
        },
        {
            **base,
            "id": "NSC-C14-ABSENT",
            "gate": True,
            "binding": {"type": "pytest", "target": "tests/example.py::test_absent"},
        },
    ]
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "checks": checks}), encoding="utf-8")
    return path


def _junit(tmp_path: Path) -> Path:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    ET.SubElement(
        suite, "testcase", file="tests/example.py", classname="tests.example", name="test_pass"
    )
    failed = ET.SubElement(
        suite, "testcase", file="tests/example.py", classname="tests.example", name="test_fail"
    )
    ET.SubElement(failed, "failure", message="wrong result")
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def test_the_real_emitter_output_files_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two adapters must agree on the artifact shape, not just in fixtures."""
    _arm(monkeypatch)
    db = tmp_path / "results.sqlite3"
    evidence = tmp_path / "evidence"
    record_results(
        manifest_path=_manifest(tmp_path),
        junit_path=_junit(tmp_path),
        tier="t1",
        run_id="run-one",
        db_path=db,
        evidence_root=evidence,
        repo_root=tmp_path,
    )
    gaps_dir = tmp_path / "gaps"
    emit_gaps(
        db_path=db,
        run_id="run-one",
        output_dir=gaps_dir,
        evidence_root=evidence,
        bundle_path=tmp_path / "bundles/run-one",
        dry_run=False,
    )
    queue = _queue(tmp_path)

    result = file_gap_findings(gaps_dir=gaps_dir, queue=queue, live=True)

    assert len(result.filed) == 2
    envelopes = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((queue / "findings").glob("*.json"))
    ]
    for envelope in envelopes:
        _assert_valid_envelope(envelope)
    by_check = {envelope["payload"]["check_id"]: envelope for envelope in envelopes}
    assert set(by_check) == {"NSC-C14-FAIL", "NSC-C14-ABSENT"}
    # NSC-C14-ABSENT is a hard gate with no writer evidence: fix-priority.
    assert by_check["NSC-C14-ABSENT"]["priority"] == 1
    assert by_check["NSC-C14-ABSENT"]["payload"]["cause_class"] == "missing-wiring"
    assert by_check["NSC-C14-FAIL"]["priority"] == 2
    assert len(_ledger_lines(queue)) == 2
    # A second identical certification run adds no queue noise.
    assert file_gap_findings(gaps_dir=gaps_dir, queue=queue, live=True).skipped_existing == 2
    assert len(_ledger_lines(queue)) == 2


def test_filing_never_reaches_for_the_ambient_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`file_gap_findings` itself takes the queue as an argument; the env key
    gates only the CLI. This pins that the library call cannot be armed by
    environment alone."""
    monkeypatch.delenv(LIVE_ENV_FLAG, raising=False)
    queue = _queue(tmp_path)
    gaps = _write_gaps(tmp_path, _gap())
    assert file_gap_findings(gaps_dir=gaps, queue=queue, live=False).filed == []
    assert os.environ.get(LIVE_ENV_FLAG) is None
