"""Hermetic tests for omniagentos.verify.report (pure model, no I/O)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omniagentos.verify.report import (
    BLOCKING_SEVERITIES,
    Finding,
    Severity,
    Verdict,
    VerificationError,
    VerificationReport,
)

_REPORT_PATH = Path(__file__).resolve().parents[2] / "omniagentos" / "verify" / "report.py"

_FORBIDDEN_PREFIXES = (
    "omniagentos.swarm",
    "omniagentos.db",
    "omniagentos.api",
    "omniagentos.orchestrator",
)


def _finding(
    severity: Severity | str = Severity.HIGH,
    *,
    file: str = "a.py",
    line: int | None = 1,
    summary: str = "bug",
    failure_scenario: str = "it breaks under load",
    id: str | None = None,
) -> Finding:
    return Finding(
        severity=severity,  # type: ignore[arg-type]
        file=file,
        line=line,
        summary=summary,
        failure_scenario=failure_scenario,
        id=id,
    )


def test_a_report_cannot_exist_without_an_artifact_hash() -> None:
    with pytest.raises(VerificationError, match="artifact_hash"):
        VerificationReport(artifact_hash="", verdict=Verdict.PASS)
    with pytest.raises(VerificationError, match="artifact_hash"):
        VerificationReport(artifact_hash="   ", verdict=Verdict.PASS)
    # Non-empty hash constructs cleanly.
    report = VerificationReport(artifact_hash="abc123", verdict=Verdict.PASS)
    assert report.artifact_hash == "abc123"


def test_a_pass_verdict_for_a_different_hash_is_stale_not_satisfying() -> None:
    report = VerificationReport(artifact_hash="hash-v1", verdict=Verdict.PASS)
    other = "hash-v2"
    assert report.verdict is Verdict.PASS
    assert report.is_stale_for(other) is True
    assert report.satisfies(other) is False
    assert report.status_for(other) == "stale"
    # Matching hash is satisfied, not stale.
    assert report.status_for("hash-v1") == "satisfied"
    assert report.satisfies("hash-v1") is True


def test_insufficient_evidence_never_satisfies_any_hash() -> None:
    h = "artifact-xyz"
    report = VerificationReport(
        artifact_hash=h,
        verdict=Verdict.INSUFFICIENT_EVIDENCE,
    )
    assert report.satisfies(h) is False
    assert report.satisfies("other-hash") is False
    assert report.status_for(h) == "insufficient_evidence"
    assert report.status_for("other-hash") == "insufficient_evidence"
    assert report.status_for("") == "insufficient_evidence"


def test_findings_are_ranked_most_severe_first_by_construction() -> None:
    # Deliberately scrambled: low, critical, medium, high, then two mediums
    # so equal-severity relative order can be checked.
    f_low = _finding(Severity.LOW, summary="low-issue", file="low.py")
    f_crit = _finding(Severity.CRITICAL, summary="crit-issue", file="crit.py")
    f_med_a = _finding(Severity.MEDIUM, summary="med-a", file="med_a.py")
    f_high = _finding(Severity.HIGH, summary="high-issue", file="high.py")
    f_med_b = _finding(Severity.MEDIUM, summary="med-b", file="med_b.py")
    report = VerificationReport(
        artifact_hash="h1",
        verdict=Verdict.FAIL,
        findings=(f_low, f_crit, f_med_a, f_high, f_med_b),
    )
    severities = [f.severity for f in report.findings]
    assert severities == [
        Severity.CRITICAL,
        Severity.HIGH,
        Severity.MEDIUM,
        Severity.MEDIUM,
        Severity.LOW,
    ]
    # Two equal-severity (MEDIUM) findings keep relative input order: med-a then med-b.
    mediums = [f for f in report.findings if f.severity is Severity.MEDIUM]
    assert [f.summary for f in mediums] == ["med-a", "med-b"]
    assert mediums[0].file == "med_a.py"
    assert mediums[1].file == "med_b.py"


def test_open_critical_or_high_findings_block_without_an_explicit_waiver() -> None:
    crit = _finding(Severity.CRITICAL, summary="auth bypass", id="F-crit")
    high = _finding(Severity.HIGH, summary="race", id="F-high")
    med = _finding(Severity.MEDIUM, summary="style", id="F-med")
    low = _finding(Severity.LOW, summary="nit", id="F-low")
    report = VerificationReport(
        artifact_hash="h1",
        verdict=Verdict.FAIL,
        findings=(crit, high, med, low),
    )
    assert BLOCKING_SEVERITIES == (Severity.CRITICAL, Severity.HIGH)
    open_blocking = report.blocking_findings()
    assert [f.key() for f in open_blocking] == ["F-crit", "F-high"]
    assert report.has_open_blocking_findings() is True
    # Medium/low do not block.
    only_soft = VerificationReport(
        artifact_hash="h1",
        verdict=Verdict.FAIL,
        findings=(med, low),
    )
    assert only_soft.blocking_findings() == ()
    assert only_soft.has_open_blocking_findings() is False


def test_a_waiver_names_a_finding_or_does_not_waive_it() -> None:
    f_a = _finding(Severity.CRITICAL, summary="a", id="FIND-A")
    f_b = _finding(Severity.HIGH, summary="b", id="FIND-B")
    f_c = _finding(Severity.HIGH, summary="c-no-id", file="c.py", line=9)
    report = VerificationReport(
        artifact_hash="h1",
        verdict=Verdict.FAIL,
        findings=(f_a, f_b, f_c),
    )
    # Exact key waiver for FIND-A only.
    remaining = report.blocking_findings(waivers=["FIND-A"])
    assert [f.key() for f in remaining] == ["FIND-B", f_c.key()]
    assert report.has_open_blocking_findings(waivers=["FIND-A"]) is True

    # Waiver naming an id no finding carries waives nothing.
    noop = report.blocking_findings(waivers=["DOES-NOT-EXIST"])
    assert [f.key() for f in noop] == ["FIND-A", "FIND-B", f_c.key()]

    # Derived key must match exactly to waive f_c.
    derived = f_c.key()
    assert derived == "high:c.py:9:c-no-id"
    after = report.blocking_findings(waivers=["FIND-A", "FIND-B", derived])
    assert after == ()
    assert report.has_open_blocking_findings(waivers=["FIND-A", "FIND-B", derived]) is False

    # Same severity, different finding: waiving FIND-B does not touch FIND-A.
    only_a = report.blocking_findings(waivers=["FIND-B", derived])
    assert [f.key() for f in only_a] == ["FIND-A"]


def test_lineage_fields_are_carried_verbatim() -> None:
    # Mixed case, whitespace, and non-canonical labels — never normalized.
    reviewer = "GPT-5.6-Sol"
    implementer = "  Claude-Opus  "
    report = VerificationReport(
        artifact_hash="h1",
        verdict=Verdict.PASS,
        reviewer_lineage=reviewer,
        implementer_lineage=implementer,
    )
    assert report.reviewer_lineage == reviewer
    assert report.implementer_lineage == implementer
    payload = report.to_dict()
    assert payload["reviewer_lineage"] == reviewer
    assert payload["implementer_lineage"] == implementer
    # Not lowercased, swapped, or defaulted.
    assert payload["reviewer_lineage"] != implementer
    assert payload["implementer_lineage"] != reviewer
    assert payload["reviewer_lineage"] == "GPT-5.6-Sol"
    assert payload["reviewer_lineage"] != "gpt-5.6-sol"
    assert payload["implementer_lineage"] == "  Claude-Opus  "
    assert payload["implementer_lineage"] != "Claude-Opus"


def test_the_verdict_vocabulary_has_no_error_member() -> None:
    values = {v.value for v in Verdict}
    assert "error" not in values
    assert len(Verdict) == 3
    assert values == {"pass", "fail", "insufficient_evidence"}
    with pytest.raises(VerificationError, match="error"):
        VerificationReport(artifact_hash="h1", verdict="error")  # type: ignore[arg-type]
    with pytest.raises(VerificationError):
        VerificationReport(artifact_hash="h1", verdict="confirm")  # type: ignore[arg-type]
    with pytest.raises(VerificationError):
        VerificationReport(artifact_hash="h1", verdict="deny")  # type: ignore[arg-type]


def test_medium_or_low_finding_does_not_block() -> None:
    report = VerificationReport(
        artifact_hash="h1",
        verdict=Verdict.FAIL,
        findings=(
            _finding(Severity.MEDIUM, summary="medium", id="M1"),
            _finding(Severity.LOW, summary="low", id="L1"),
        ),
    )
    assert report.blocking_findings() == ()
    assert report.has_open_blocking_findings() is False
    assert [f.severity for f in report.findings] == [Severity.MEDIUM, Severity.LOW]


def test_blank_current_hash_is_stale_fail_closed() -> None:
    report = VerificationReport(artifact_hash="real-hash", verdict=Verdict.PASS)
    assert report.is_stale_for("") is True
    assert report.is_stale_for("   ") is True
    assert report.satisfies("") is False
    assert report.status_for("") == "stale"
    # FAIL against blank is also stale (judged a different / unknown revision).
    failed = VerificationReport(artifact_hash="real-hash", verdict=Verdict.FAIL)
    assert failed.status_for("") == "stale"
    assert failed.status_for("other") == "stale"
    assert failed.status_for("real-hash") == "failed"


def test_finding_with_empty_failure_scenario_raises() -> None:
    with pytest.raises(VerificationError, match="failure_scenario"):
        Finding(
            severity=Severity.HIGH,
            file="x.py",
            line=1,
            summary="something wrong",
            failure_scenario="",
        )
    with pytest.raises(VerificationError, match="failure_scenario"):
        Finding(
            severity=Severity.HIGH,
            file="x.py",
            line=1,
            summary="something wrong",
            failure_scenario="   ",
        )
    with pytest.raises(VerificationError, match="summary"):
        Finding(
            severity=Severity.HIGH,
            file="x.py",
            line=1,
            summary="",
            failure_scenario="breaks",
        )


def test_severity_string_coercion_and_unknown() -> None:
    f = _finding("critical", summary="c")
    assert f.severity is Severity.CRITICAL
    assert f.severity.rank == 0
    assert Severity.HIGH.rank == 1
    assert Severity.MEDIUM.rank == 2
    assert Severity.LOW.rank == 3
    with pytest.raises(VerificationError, match="unknown severity"):
        _finding("catastrophic")


def test_finding_key_uses_id_when_set() -> None:
    with_id = _finding(Severity.HIGH, summary="s", id="WAIVE-1")
    assert with_id.key() == "WAIVE-1"
    without = _finding(Severity.HIGH, file="f.py", line=3, summary="s")
    assert without.key() == "high:f.py:3:s"


def test_finding_and_report_to_dict() -> None:
    f = _finding(Severity.CRITICAL, file="z.py", line=None, summary="s", id="Z")
    d = f.to_dict()
    assert d == {
        "severity": "critical",
        "file": "z.py",
        "line": None,
        "summary": "s",
        "failure_scenario": "it breaks under load",
        "id": "Z",
    }
    report = VerificationReport(
        artifact_hash="hh",
        verdict="pass",  # type: ignore[arg-type]
        findings=(f,),
        verifier="v1",
        evidence=("e1",),
        created_at="2026-01-01T00:00:00Z",
    )
    rd = report.to_dict()
    assert rd["artifact_hash"] == "hh"
    assert rd["verdict"] == "pass"
    assert rd["findings"] == [d]
    assert rd["verifier"] == "v1"
    assert rd["evidence"] == ["e1"]
    assert rd["created_at"] == "2026-01-01T00:00:00Z"


def test_module_imports_nothing_from_swarm_db_api_or_orchestrator() -> None:
    source = _REPORT_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_REPORT_PATH))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                imported.append(node.module)
            else:
                imported.append(".")
    for mod in imported:
        for forbidden in _FORBIDDEN_PREFIXES:
            assert not (
                mod == forbidden or mod.startswith(forbidden + ".")
            ), f"forbidden import {mod!r} (prefix {forbidden!r})"
    # Stdlib / typing only — no omniagentos.* at all for this pure module.
    for mod in imported:
        assert not mod.startswith("omniagentos"), f"unexpected omniagentos import: {mod!r}"
