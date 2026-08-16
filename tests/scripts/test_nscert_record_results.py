from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

import scripts.northstar_cert.record_results as recorder
from omniagentos.harnesses.release_gate import (
    ReleaseGateError,
    _northstar_phase_specs,
)
from omniagentos.lab.contracts import EvalCase, EvalSplit
from omniagentos.lab.db import LabStore
from omniagentos.pulse.store import PulseStore
from omniagentos.scheduler.gate_evidence import GateEvidenceStore
from scripts.northstar_cert.record_results import (
    NOT_EXECUTED_REASON,
    PYTEST_ERROR_REASON,
    VACUOUS_BINDING_REASON,
    CertificationError,
    CheckResult,
    CheckVerdict,
    JUnitOutcome,
    ManifestCheck,
    RunVerdict,
    WriterEvidence,
    compute_run_verdict,
    evaluate_checks,
    parse_junit,
    record_results,
    runnable_targets,
    vacuous_bindings,
    verdict_exit_code,
)
from scripts.northstar_cert.record_results import (
    main as record_results_main,
)


def _check(
    check_id: str,
    target: str,
    *,
    gate: bool = False,
    binding_type: str = "pytest",
    shared: bool | None = None,
    requires: list[str] | None = None,
    tier: str = "t1",
) -> dict[str, object]:
    binding: dict[str, object] = {"type": binding_type, "target": target}
    if shared is not None:
        binding["shared"] = shared
    return {
        "id": check_id,
        "capability": check_id.split("-")[1],
        "binding": binding,
        "tier": tier,
        "gate": gate,
        "requires": list(requires or []),
        "scope": "scenario",
        "provenance": ["unit-test"],
    }


def _write_manifest(tmp_path: Path, checks: list[dict[str, object]]) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "checks": checks}), encoding="utf-8")
    return path


def _write_junit(tmp_path: Path) -> Path:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="2", failures="1")
    ET.SubElement(
        suite,
        "testcase",
        classname="tests.example",
        file="tests/example.py",
        name="test_pass",
    )
    failed = ET.SubElement(
        suite,
        "testcase",
        classname="tests.example",
        file="tests/example.py",
        name="test_fail",
    )
    ET.SubElement(failed, "failure", message="candidate output differed")
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _manifest_check(
    *,
    gate: bool = True,
    check_id: str = "NSC-C01-ABSENT",
    target: str = "tests/example.py::test_absent",
    requires: tuple[str, ...] = (),
    shared: bool = False,
) -> ManifestCheck:
    return ManifestCheck(
        id=check_id,
        capability="C-01",
        binding_type="pytest",
        target=target,
        tier="t1",
        gate=gate,
        requires=requires,
        scope="scenario",
        provenance=("unit-test",),
        shared=shared,
    )


def test_record_results_writes_lab_pulse_and_signed_v3_receipt(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        [
            _check("NSC-C01-PASS", "tests/example.py::test_pass"),
            _check("NSC-C01-FAIL", "tests/example.py::test_fail", gate=True),
            _check("NSC-C01-MISSING", "tests/example.py::test_missing", gate=True),
        ],
    )
    db_path = tmp_path / "results.sqlite3"
    evidence_root = tmp_path / "evidence"
    summary = record_results(
        manifest_path=manifest,
        junit_path=_write_junit(tmp_path),
        tier="t1",
        run_id="run-one",
        db_path=db_path,
        evidence_root=evidence_root,
        repo_root=tmp_path,
    )

    assert summary.verdict is RunVerdict.FAILED
    assert [result.verdict for result in summary.results] == [
        CheckVerdict.PASS,
        CheckVerdict.FAIL,
        CheckVerdict.NOT_EVALUABLE,
    ]
    lab = LabStore(str(db_path))
    try:
        assert lab.list_eval_suites("northstar_cert")[0]["dataset_hash"]
        assert len(lab.eval_results("run-one")) == 3
        pulse = PulseStore(lab._store)
        assert pulse.latest("nsc.distance") == {
            "date": datetime.now(UTC).date().isoformat(),
            "value": pytest.approx(100 / 3),
        }
    finally:
        lab._store.close()

    receipt_store = GateEvidenceStore(evidence_root, create_key=False)
    receipt = receipt_store.load("northstar-cert", "run-one")
    assert receipt is not None and receipt_store.verify(receipt)
    assert receipt.schema == "omniagentos.gate-evidence.v3"
    assert (receipt.checks_passed, receipt.checks_failed, receipt.checks_skipped) == (1, 1, 1)
    assert json.loads(Path(summary.receipt_path).read_text())["routine_id"] == "northstar-cert"


def test_absent_junit_case_is_not_evaluable_and_gates_inconclusive(tmp_path: Path) -> None:
    results = evaluate_checks([_manifest_check()], [], repo_root=tmp_path)
    assert results[0].verdict is CheckVerdict.NOT_EVALUABLE
    assert results[0].reason == f"{NOT_EXECUTED_REASON}:tests/example.py::test_absent"
    assert compute_run_verdict(results, mission_closure=False) is RunVerdict.INCONCLUSIVE


def test_a_hard_gate_that_never_ran_is_named_in_the_summary(tmp_path: Path) -> None:
    """Defense in depth for R3-002: a silent deselection must be NAMEABLE.

    The verdict algebra is already right — an unexecuted hard gate is
    NOT_EVALUABLE and the run is INCONCLUSIVE — and it is deliberately left
    alone here. What was missing is the diagnosis: in a receipt, a hard gate
    removed by a default marker filter looks exactly like a renamed test or a
    genuinely absent writer, so the reader debugs the product instead of the
    runner. The ids go in the summary the cadence logs.
    """
    manifest = _write_manifest(
        tmp_path,
        [
            _check("NSC-C01-PASS", "tests/example.py::test_pass", gate=True),
            _check("NSC-C43-GONE", "tests/example.py::test_gone", gate=True),
            _check("NSC-C02-SOFT", "tests/example.py::test_soft_gone"),
            _check(
                "NSC-C03-MASKED",
                "tests/example.py::test_masked",
                gate=True,
                requires=["fs:nowhere/at/all"],
            ),
            _check("NSC-C04-DOC", "docs/whatever.md", gate=True, binding_type="doc_reference"),
        ],
    )
    summary = record_results(
        manifest_path=manifest,
        junit_path=_write_junit(tmp_path),
        tier="t1",
        run_id="run-deselected",
        db_path=tmp_path / "results.sqlite3",
        evidence_root=tmp_path / "evidence",
        repo_root=tmp_path,
    )

    # ONLY the hard gate whose carrier was expected to run and produced nothing.
    # A soft check, a requires-masked gate (precondition_missing, a REFUSAL the
    # run made on purpose) and a non-pytest binding are all different conditions.
    assert summary.deselected_gates == ("NSC-C43-GONE",)
    assert summary.to_dict()["deselected_gates"] == ["NSC-C43-GONE"]
    # Observability only: the verdict algebra is untouched.
    assert summary.verdict is RunVerdict.INCONCLUSIVE
    assert verdict_exit_code(summary.verdict) == 2


def test_a_run_that_executed_every_gate_names_nothing(tmp_path: Path) -> None:
    """The guard must stay quiet when nothing was deselected — a warning that
    fires on healthy runs is a warning nobody reads."""
    manifest = _write_manifest(
        tmp_path, [_check("NSC-C01-PASS", "tests/example.py::test_pass", gate=True)]
    )
    summary = record_results(
        manifest_path=manifest,
        junit_path=_write_junit(tmp_path),
        tier="t1",
        run_id="run-complete",
        db_path=tmp_path / "results.sqlite3",
        evidence_root=tmp_path / "evidence",
        repo_root=tmp_path,
    )
    assert summary.deselected_gates == ()
    assert summary.verdict is RunVerdict.MEASURED


def test_verdict_algebra_enforces_measured_cap_and_instrument_void() -> None:
    passed = CheckResult(_manifest_check(), CheckVerdict.PASS)
    assert compute_run_verdict([passed], mission_closure=False) is RunVerdict.MEASURED
    assert compute_run_verdict([passed], mission_closure=True) is RunVerdict.CERTIFIED
    assert (
        compute_run_verdict([passed], mission_closure=True, instrument_error=True)
        is RunVerdict.VOID
    )


def test_record_results_refuses_writes_to_live_runtime_db(tmp_path: Path) -> None:
    live_db = tmp_path / "var/runtime/state.sqlite3"
    with pytest.raises(CertificationError, match="live runtime DB is read-only"):
        record_results(
            manifest_path=tmp_path / "unused.yaml",
            junit_path=tmp_path / "unused.xml",
            tier="t1",
            run_id="live-write",
            db_path=live_db,
            evidence_root=tmp_path / "evidence",
            repo_root=tmp_path,
        )


# ---------------------------------------------------------------------------
# VOID(vacuous_binding) — one carrier may never stand in for many claims.
#
# These are keyed on the DEFECT SHAPE (how many checks bind one pytest target,
# and whether the sharing was declared), never on the anchor name, the target
# spelling, or the check ids that happened to carry it in the measured incident.
# ---------------------------------------------------------------------------


def _outcomes(tmp_path: Path) -> list[JUnitOutcome]:
    return parse_junit(_write_junit(tmp_path), repo_root=tmp_path)


@pytest.mark.parametrize(
    "target",
    ["tests/example.py::test_pass", "tests/example.py::test_fail"],
)
def test_three_checks_on_one_target_void_whatever_that_target_did(
    tmp_path: Path, target: str
) -> None:
    """The measured defect: one testcase rendering a wall of verdicts.

    Both a green and a red carrier are exercised — a vacuous binding may not be
    scored in EITHER direction, so this cannot be satisfied by "it only blocks
    the favorable side".
    """

    checks = [
        # ``shared: true`` everywhere: past two binders, declaring the sharing
        # does not make it measurable.
        _manifest_check(check_id=f"NSC-C01-{index:02d}", target=target, shared=True)
        for index in range(1, 4)
    ]
    results = evaluate_checks(checks, _outcomes(tmp_path), repo_root=tmp_path)

    assert [result.verdict for result in results] == [CheckVerdict.VOID] * 3
    for result in results:
        assert result.reason == f"{VACUOUS_BINDING_REASON}:target_bound_by_3_checks:{target}"
    assert compute_run_verdict(results, mission_closure=True) is RunVerdict.VOID


@pytest.mark.parametrize(
    ("first_shared", "second_shared", "expected"),
    [
        (False, False, CheckVerdict.VOID),
        (True, False, CheckVerdict.VOID),
        (False, True, CheckVerdict.VOID),
        (True, True, CheckVerdict.PASS),
    ],
)
def test_two_binders_are_only_admissible_when_both_declare_sharing(
    tmp_path: Path, first_shared: bool, second_shared: bool, expected: CheckVerdict
) -> None:
    checks = [
        _manifest_check(
            check_id="NSC-C01-01", target="tests/example.py::test_pass", shared=first_shared
        ),
        _manifest_check(
            check_id="NSC-C01-02", target="tests/example.py::test_pass", shared=second_shared
        ),
    ]
    results = evaluate_checks(checks, _outcomes(tmp_path), repo_root=tmp_path)

    assert [result.verdict for result in results] == [expected, expected]
    if expected is CheckVerdict.VOID:
        for result in results:
            assert result.reason.startswith(f"{VACUOUS_BINDING_REASON}:shared_not_declared:")


def test_vacuous_binding_outranks_failure_and_missing_preconditions(tmp_path: Path) -> None:
    """An instrument fault is never reported as a candidate defect."""

    checks = [
        _manifest_check(check_id="NSC-C01-01", target="tests/example.py::test_fail", gate=True),
        _manifest_check(
            check_id="NSC-C01-02",
            target="tests/example.py::test_fail",
            gate=True,
            requires=("writer:absent",),
        ),
    ]
    results = evaluate_checks(checks, _outcomes(tmp_path), repo_root=tmp_path)

    assert {result.verdict for result in results} == {CheckVerdict.VOID}
    assert not any("pytest_failure" in result.reason for result in results)
    assert not any("precondition_missing" in result.reason for result in results)
    # Gate math excludes VOID: the run is neither FAILED (a product verdict on a
    # broken instrument) nor INCONCLUSIVE (a merely-unmet precondition).
    assert compute_run_verdict(results, mission_closure=True) is RunVerdict.VOID


def test_manifest_wide_census_voids_a_target_shared_across_tiers(tmp_path: Path) -> None:
    """A tier slice with one binder still VOIDs when the manifest shares wider."""

    lone = [_manifest_check(check_id="NSC-C01-01", target="tests/example.py::test_pass")]
    graded = evaluate_checks(lone, _outcomes(tmp_path), repo_root=tmp_path)
    assert graded[0].verdict is CheckVerdict.PASS

    voided = evaluate_checks(
        lone,
        _outcomes(tmp_path),
        repo_root=tmp_path,
        vacuous_targets={"tests/example.py::test_pass": "target_bound_by_4_checks"},
    )
    assert voided[0].verdict is CheckVerdict.VOID


def test_vacuous_census_is_shape_keyed_and_scoped_to_pytest_carriers() -> None:
    unrelated = [
        _manifest_check(check_id="A", target="tests/alpha.py::test_one"),
        _manifest_check(check_id="B", target="tests/beta.py::test_two"),
    ]
    assert vacuous_bindings(unrelated) == {}

    # Any spelling, any ids: only the shape matters.
    for target in ("tests/zzz.py::TestX::test_deep", "packages/x/tests/t.py::test_y[param]"):
        group = [_manifest_check(check_id=f"X{index}", target=target) for index in range(1, 4)]
        assert vacuous_bindings(group) == {target: "target_bound_by_3_checks"}

    # Non-pytest bindings are adjudicated by their own writer rule, not this one.
    sentinels = [
        ManifestCheck(
            id=f"S{index}",
            capability="C-02",
            binding_type="sentinel",
            target="health-sentinel:context_packages",
            tier="t1",
            gate=False,
            requires=(),
            scope="estate",
            provenance=("unit-test",),
        )
        for index in range(3)
    ]
    assert vacuous_bindings(sentinels) == {}


def test_record_results_voids_a_merge_anchor_manifest_end_to_end(tmp_path: Path) -> None:
    """Reproduces the shipped defect through the whole recorder.

    Before the invariant, these three checks inherited one green carrier and the
    run reported MEASURED with three PASSes off a single testcase.
    """

    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "version: 1\n"
        "shared_defaults: &whatever_it_is_called\n"
        "  capability: C-01\n"
        "  binding: {type: pytest, target: tests/example.py::test_pass}\n"
        "  tier: t1\n"
        "  gate: false\n"
        "  requires: []\n"
        "  scope: scenario\n"
        "  provenance: [unit-test]\n"
        "checks:\n"
        "  - {<<: *whatever_it_is_called, id: NSC-C01-01}\n"
        "  - {<<: *whatever_it_is_called, id: NSC-C01-02, gate: true}\n"
        "  - {<<: *whatever_it_is_called, id: NSC-C01-03}\n",
        encoding="utf-8",
    )
    evidence_root = tmp_path / "evidence"
    db_path = tmp_path / "results.sqlite3"
    summary = record_results(
        manifest_path=manifest,
        junit_path=_write_junit(tmp_path),
        tier="t1",
        run_id="run-vacuous",
        db_path=db_path,
        evidence_root=evidence_root,
        repo_root=tmp_path,
    )

    assert summary.verdict is RunVerdict.VOID
    assert verdict_exit_code(summary.verdict) == 70
    assert {result.verdict for result in summary.results} == {CheckVerdict.VOID}
    # No score is published off unmeasurable checks — an empty denominator
    # publishes nothing rather than a coerced 0.0.
    assert summary.pulse == {"nsc.void_ratio": 1.0}

    receipt = GateEvidenceStore(evidence_root, create_key=False).load(
        "northstar-cert", "run-vacuous"
    )
    assert receipt is not None
    assert (receipt.checks_collected, receipt.checks_passed, receipt.checks_failed) == (3, 0, 0)
    assert receipt.checks_skipped == 0

    lab = LabStore(str(db_path))
    try:
        rows = lab.eval_results("run-vacuous")
        assert len(rows) == 3
        for row in rows:
            metrics = json.loads(row["metrics_json"])
            assert metrics["void"] == 1.0
            assert metrics["pass"] == 0.0 and metrics["fail"] == 0.0
            # Readers that only know the three-metric vocabulary must still see
            # "not a measurement" instead of silence.
            assert metrics["not_evaluable"] == 1.0
            assert not row["deterministic_passed"]
    finally:
        lab._store.close()


def test_record_results_accepts_declared_shared_bindings(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        [
            _check("NSC-C01-01", "tests/example.py::test_pass", shared=True),
            _check("NSC-C01-02", "tests/example.py::test_pass", shared=True),
        ],
    )
    summary = record_results(
        manifest_path=manifest,
        junit_path=_write_junit(tmp_path),
        tier="t1",
        run_id="run-shared",
        db_path=tmp_path / "results.sqlite3",
        evidence_root=tmp_path / "evidence",
        repo_root=tmp_path,
    )
    assert summary.verdict is RunVerdict.MEASURED
    assert {result.verdict for result in summary.results} == {CheckVerdict.PASS}
    assert summary.pulse["nsc.void_ratio"] == 0.0


def test_manifest_rejects_a_non_boolean_shared_flag(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        [_check("NSC-C01-01", "tests/example.py::test_pass", shared="yes")],  # type: ignore[arg-type]
    )
    with pytest.raises(CertificationError, match="invalid binding metadata"):
        record_results(
            manifest_path=manifest,
            junit_path=_write_junit(tmp_path),
            tier="t1",
            run_id="run-bad-shared",
            db_path=tmp_path / "results.sqlite3",
            evidence_root=tmp_path / "evidence",
            repo_root=tmp_path,
        )


# ---------------------------------------------------------------------------
# NOT_EVALUABLE(not_executed) — absence is never favorable.
# ---------------------------------------------------------------------------


def test_satisfied_mask_with_no_junit_case_is_not_executed_not_a_pass(tmp_path: Path) -> None:
    check = _manifest_check(check_id="NSC-C01-01", target="tests/example.py::test_never_ran")
    results = evaluate_checks(
        [check],
        _outcomes(tmp_path),
        available_requirements=frozenset(),
        repo_root=tmp_path,
    )
    assert results[0].verdict is CheckVerdict.NOT_EVALUABLE
    assert results[0].reason == f"{NOT_EXECUTED_REASON}:tests/example.py::test_never_ran"
    # A carrier that never ran cannot satisfy its gate.
    assert compute_run_verdict(results, mission_closure=True) is RunVerdict.INCONCLUSIVE


def test_unsatisfied_mask_still_reports_precondition_missing(tmp_path: Path) -> None:
    """The two absences stay distinguishable: blocked writer vs. silent no-show."""

    blocked = _manifest_check(
        check_id="NSC-C01-01",
        target="tests/example.py::test_never_ran",
        requires=("writer:NSG-005",),
    )
    results = evaluate_checks([blocked], _outcomes(tmp_path), repo_root=tmp_path)
    assert results[0].verdict is CheckVerdict.NOT_EVALUABLE
    assert results[0].reason == "precondition_missing:writer:NSG-005"

    satisfied = evaluate_checks(
        [blocked],
        _outcomes(tmp_path),
        available_requirements=frozenset({"writer:NSG-005"}),
        repo_root=tmp_path,
    )
    assert satisfied[0].reason == f"{NOT_EXECUTED_REASON}:tests/example.py::test_never_ran"


def test_bare_mask_deletion_without_genuine_writer_evidence_does_not_flip_pass(
    tmp_path: Path,
) -> None:
    """Deleting only ``requires: [writer:X]`` cannot improve certification."""

    token = "writer:not-really-landed"
    check_id = "NSC-C01-STICKY"
    masked = _manifest_check(
        check_id=check_id,
        target="tests/example.py::test_pass",
        requires=(token,),
    )
    gamed = _manifest_check(
        check_id=check_id,
        target=masked.target,
        requires=(),  # the entire attacker diff: delete the manifest mask
    )
    registry = {token: WriterEvidence(gates=(check_id,), witness=None)}
    outcomes = [JUnitOutcome(masked.target, "passed")]

    before = evaluate_checks([masked], outcomes, repo_root=tmp_path, writer_registry=registry)
    after = evaluate_checks([gamed], outcomes, repo_root=tmp_path, writer_registry=registry)

    assert before[0].verdict is CheckVerdict.NOT_EVALUABLE
    assert after[0].verdict is CheckVerdict.NOT_EVALUABLE
    assert after[0].reason == "no_writer_evidence:witness_absent:" + token
    # nsc.distance is PASS/total, so equality here is the direct metric parity
    # assertion: the manifest-only deletion buys no favorable movement.
    assert (
        recorder._pulse_values(before)["nsc.distance"]
        == recorder._pulse_values(after)["nsc.distance"]
    )


def test_genuinely_landed_writer_with_evidence_still_passes(tmp_path: Path) -> None:
    """A real AST witness plus a self-declared passing proof permits PASS."""

    token = "writer:genuine-fixture"
    check_id = "NSC-C01-GENUINE"
    (tmp_path / "feature.py").write_text("class GenuineWriter:\n    pass\n", encoding="utf-8")
    # A real production consumer so the witness is not a stub the realness guard
    # refuses (the whole point of "genuinely landed").
    (tmp_path / "omniagentos").mkdir()
    (tmp_path / "omniagentos" / "consumer.py").write_text(
        "from feature import GenuineWriter\n\n\ndef use() -> type:\n    return GenuineWriter\n",
        encoding="utf-8",
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from feature import GenuineWriter\n\n"
        f"@pytest.mark.nsc_writer_proof({token!r})\n"
        "def test_genuine_writer():\n    assert GenuineWriter.__name__ == 'GenuineWriter'\n",
        encoding="utf-8",
    )
    registry_path = tmp_path / "writers.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tokens": {
                    token: {
                        "gates": [check_id],
                        "witness": "feature.py::GenuineWriter",
                        "proof": "proof.py::test_genuine_writer",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    registry = recorder._load_writer_registry(
        registry_path,
        repo_root=tmp_path,
        manifest_targets={check_id: "tests/example.py::test_pass"},
    )
    check = _manifest_check(
        check_id=check_id,
        target="tests/example.py::test_pass",
        # Honest landing may remove the now-redundant presentation-layer mask;
        # the sticky registry, not this tuple, remains authoritative.
        requires=(),
    )
    outcomes = [
        JUnitOutcome(check.target, "passed"),
        JUnitOutcome("proof.py::test_genuine_writer", "passed"),
    ]

    result = evaluate_checks([check], outcomes, repo_root=tmp_path, writer_registry=registry)[0]
    assert result.verdict is CheckVerdict.PASS
    assert result.reason == ""


def test_bare_glue_mask_deletion_stays_sticky_and_genuine_arming_passes(
    tmp_path: Path,
) -> None:
    """Deleting glue:X is never worth more than leaving it visibly masked."""

    token = "glue:fold-legs-pending"
    check_id = "NSC-C01-GLUE"
    masked = _manifest_check(
        check_id=check_id,
        target="tests/example.py::test_pass",
        requires=(token,),
    )
    gamed = _manifest_check(check_id=check_id, target=masked.target, requires=())
    registry = {token: WriterEvidence(gates=(check_id,))}
    outcomes = [JUnitOutcome(masked.target, "passed")]

    before = evaluate_checks([masked], outcomes, repo_root=tmp_path, writer_registry=registry)
    after = evaluate_checks([gamed], outcomes, repo_root=tmp_path, writer_registry=registry)
    armed = evaluate_checks(
        [gamed],
        outcomes,
        available_requirements=frozenset({token}),
        repo_root=tmp_path,
        writer_registry=registry,
    )

    assert before[0].verdict is CheckVerdict.NOT_EVALUABLE
    assert after[0].verdict is CheckVerdict.NOT_EVALUABLE
    assert after[0].reason == f"precondition_missing:{token}"
    assert armed[0].verdict is CheckVerdict.PASS


def test_path_override_empties_registry_fails_closed_for_writer_mask(
    tmp_path: Path,
) -> None:
    """A non-canonical NSCERT_MANIFEST cannot silently turn writer gating off."""

    token = "writer:path-override"
    manifest = _write_manifest(
        tmp_path,
        [
            _check(
                "NSC-C01-OVERRIDE",
                "tests/example.py::test_pass",
                gate=True,
                requires=[token],
            )
        ],
    )
    summary = record_results(
        manifest_path=manifest,
        junit_path=_write_junit(tmp_path),
        tier="t1",
        run_id="run-path-override",
        db_path=tmp_path / "results.sqlite3",
        evidence_root=tmp_path / "evidence",
        repo_root=tmp_path,
        available_requirements=frozenset({token}),
    )

    assert summary.writer_gating_active is False
    assert summary.verdict is RunVerdict.INCONCLUSIVE
    assert summary.results[0].verdict is CheckVerdict.NOT_EVALUABLE
    assert summary.results[0].reason == recorder.WRITER_REGISTRY_UNAVAILABLE_REASON


def test_canonical_capability_override_without_mask_gating_voids_run(tmp_path: Path) -> None:
    """A copied canonical capability set cannot certify with gating disabled."""

    check = _check("NSC-C01-OVERRIDE", "tests/example.py::test_pass", gate=True)
    canonical = tmp_path / recorder.DEFAULT_MANIFEST
    canonical.parent.mkdir(parents=True)
    canonical.write_text(yaml.safe_dump({"version": 1, "checks": [check]}), encoding="utf-8")
    override = tmp_path / "override" / "manifest.yaml"
    override.parent.mkdir()
    override.write_text(yaml.safe_dump({"version": 1, "checks": [check]}), encoding="utf-8")

    summary = record_results(
        manifest_path=override,
        junit_path=_write_junit(tmp_path),
        tier="t1",
        run_id="run-canonical-capability-override",
        db_path=tmp_path / "results.sqlite3",
        evidence_root=tmp_path / "evidence",
        repo_root=tmp_path,
    )

    assert summary.writer_gating_active is False
    assert summary.results[0].verdict is CheckVerdict.PASS
    assert summary.verdict is RunVerdict.VOID


def test_empty_junit_renders_every_check_not_executed(tmp_path: Path) -> None:
    """A run that executed nothing reports nothing favorable."""

    manifest = _write_manifest(
        tmp_path,
        [
            _check("NSC-C01-01", "tests/example.py::test_one", gate=True),
            _check("NSC-C01-02", "tests/example.py::test_two"),
        ],
    )
    empty = tmp_path / "empty-junit.xml"
    ET.ElementTree(ET.Element("testsuites")).write(empty, encoding="utf-8", xml_declaration=True)
    summary = record_results(
        manifest_path=manifest,
        junit_path=empty,
        tier="t1",
        run_id="run-empty",
        db_path=tmp_path / "results.sqlite3",
        evidence_root=tmp_path / "evidence",
        repo_root=tmp_path,
    )
    assert summary.verdict is RunVerdict.INCONCLUSIVE
    assert {result.verdict for result in summary.results} == {CheckVerdict.NOT_EVALUABLE}
    assert all(result.reason.startswith(NOT_EXECUTED_REASON) for result in summary.results)
    assert summary.pulse["nsc.distance"] == 0.0
    assert summary.pulse["nsc.mechanics_refusal_ratio"] == 1.0


def test_northstar_release_gate_phase_sets_are_additive(tmp_path: Path) -> None:
    t1 = _northstar_phase_specs(tier="t1", repo_root=tmp_path)
    assert [phase.name for phase in t1] == ["northstar_t1"]
    assert t1[0].argv[-1] == "nscert-t1"
    with pytest.raises(ReleaseGateError, match="unknown North Star tier"):
        _northstar_phase_specs(tier="t3", repo_root=tmp_path)


def test_list_targets_excludes_masked_checks_and_dedupes(tmp_path: Path) -> None:
    """A masked or pending target must never reach pytest: one unresolvable
    node id aborts collection for the whole run and renders every check
    not_executed (measured live 2026-08-09)."""

    satisfied = tmp_path / "present.db"
    satisfied.write_text("seeded")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """
version: 1
checks:
  - {id: NSC-T01, capability: northstar-cert, tier: t1, gate: false, scope: scenario,
     provenance: [x], requires: [],
     binding: {type: pytest, target: tests/a.py::test_runs}}
  - {id: NSC-T02, capability: northstar-cert, tier: t1, gate: false, scope: scenario,
     provenance: [x], requires: ["writer:unbuilt-thing"],
     binding: {type: pytest, target: tests/b.py::pending_specific_carrier_nsc_t02}}
  - {id: NSC-T03, capability: northstar-cert, tier: t1, gate: false, scope: scenario,
     provenance: [x], requires: ["fs:present.db"],
     binding: {type: pytest, target: tests/c.py::test_needs_seeded_store}}
  - {id: NSC-T04, capability: northstar-cert, tier: t1, gate: false, scope: scenario,
     provenance: [x], requires: [],
     binding: {type: pytest, target: tests/a.py::test_runs, shared: true}}
  - {id: NSC-T05, capability: northstar-cert, tier: t2, gate: false, scope: scenario,
     provenance: [x], requires: [],
     binding: {type: pytest, target: tests/d.py::test_wrong_tier}}
  - {id: NSC-T06, capability: northstar-cert, tier: t1, gate: false, scope: scenario,
     provenance: [x], requires: [],
     binding: {type: sentinel, target: health-sentinel:not-pytest}}
"""
    )
    targets = runnable_targets(manifest, "t1", repo_root=tmp_path)
    assert targets == ["tests/a.py::test_runs", "tests/c.py::test_needs_seeded_store"]


def test_list_targets_cli_prints_runnable_targets_only(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """
version: 1
checks:
  - {id: NSC-T01, capability: northstar-cert, tier: t1, gate: false, scope: scenario,
     provenance: [x], requires: [],
     binding: {type: pytest, target: tests/a.py::test_runs}}
  - {id: NSC-T02, capability: northstar-cert, tier: t1, gate: false, scope: scenario,
     provenance: [x], requires: ["binding:needs-specific-carrier"],
     binding: {type: pytest, target: tests/b.py::pending_specific_carrier_nsc_t02}}
"""
    )
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = record_results_main(["--manifest", str(manifest), "--tier", "t1", "--list-targets"])
    assert code == 0
    assert buffer.getvalue().split() == ["tests/a.py::test_runs"]


# ---------------------------------------------------------------------------
# pytest <error> is an INSTRUMENT fault, not a product verdict.
# ---------------------------------------------------------------------------


def test_a_pytest_error_voids_the_check_and_the_run(tmp_path: Path) -> None:
    """A fixture/import/collection blow-up did not MEASURE the check.

    Rendering it NOT_EVALUABLE let a broken instrument look like a merely-unmet
    precondition and the run still reported MEASURED (exit 0) — the same
    process-boundary answer as a genuinely healthy non-certifying run.
    """
    check = _manifest_check(gate=False, target="tests/example.py::test_x")
    results = evaluate_checks(
        [check], [JUnitOutcome(check.target, "error", "fixture exploded")], repo_root=tmp_path
    )

    assert results[0].verdict is CheckVerdict.VOID
    assert results[0].reason == f"{PYTEST_ERROR_REASON}:fixture exploded"
    # ...and the existing any-VOID => run-VOID rule then does the rest.
    assert compute_run_verdict(results, mission_closure=True) is RunVerdict.VOID
    assert verdict_exit_code(compute_run_verdict(results, mission_closure=False)) == 70


def test_a_pytest_error_is_recorded_as_void_end_to_end(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        [
            _check("NSC-C01-OK", "tests/example.py::test_pass"),
            _check("NSC-C01-ERR", "tests/example.py::test_error", gate=True),
        ],
    )
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    ET.SubElement(
        suite, "testcase", classname="tests.example", file="tests/example.py", name="test_pass"
    )
    errored = ET.SubElement(
        suite, "testcase", classname="tests.example", file="tests/example.py", name="test_error"
    )
    ET.SubElement(errored, "error", message="ImportError: no module named nope")
    junit = tmp_path / "junit.xml"
    ET.ElementTree(root).write(junit, encoding="utf-8", xml_declaration=True)

    summary = record_results(
        manifest_path=manifest,
        junit_path=junit,
        tier="t1",
        run_id="run-error",
        db_path=tmp_path / "results.sqlite3",
        evidence_root=tmp_path / "evidence",
        repo_root=tmp_path,
    )

    assert summary.verdict is RunVerdict.VOID
    verdicts = {result.check.id: result.verdict for result in summary.results}
    assert verdicts == {"NSC-C01-OK": CheckVerdict.PASS, "NSC-C01-ERR": CheckVerdict.VOID}
    # The VOID check is spent against no budget: not passed, not failed, and NOT
    # folded into skipped where it would burn a real skip allowance.
    receipt = GateEvidenceStore(tmp_path / "evidence", create_key=False).load(
        "northstar-cert", "run-error"
    )
    assert receipt is not None
    assert (receipt.checks_collected, receipt.checks_passed) == (2, 1)
    assert (receipt.checks_failed, receipt.checks_skipped) == (0, 0)
    assert summary.pulse["nsc.void_ratio"] == 0.5


# ---------------------------------------------------------------------------
# launchd: requirements unmask themselves. writer: evidence is separately
# enforced through the sticky registry on the would-otherwise-PASS arm.
# ---------------------------------------------------------------------------


def test_a_loaded_launchd_label_unmasks_its_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A landed launchd mechanism must be OBSERVABLE by the scheduled run.

    Without this, a repaired prerequisite stays `precondition_missing` forever
    unless a human runs the recorder by hand with a matching flag.
    """
    probed: list[str] = []

    def probe(label: str) -> bool:
        probed.append(label)
        return label == "com.omniagentos.reflection-nightly"

    monkeypatch.setattr(recorder, "_launchd_label_loaded", probe)
    loaded = _manifest_check(
        check_id="NSC-C01-LOADED",
        target="tests/example.py::test_loaded",
        requires=("launchd:com.omniagentos.reflection-nightly",),
    )
    absent = _manifest_check(
        check_id="NSC-C01-ABSENT-JOB",
        target="tests/example.py::test_absent_job",
        requires=("launchd:com.omniagentos.never-installed",),
    )
    outcomes = [
        JUnitOutcome("tests/example.py::test_loaded", "passed"),
        JUnitOutcome("tests/example.py::test_absent_job", "passed"),
    ]

    results = evaluate_checks([loaded, absent], outcomes, repo_root=tmp_path)

    assert results[0].verdict is CheckVerdict.PASS
    assert results[1].verdict is CheckVerdict.NOT_EVALUABLE
    assert results[1].reason == "precondition_missing:launchd:com.omniagentos.never-installed"
    assert probed == [
        "com.omniagentos.reflection-nightly",
        "com.omniagentos.never-installed",
    ]


def test_only_launchd_tokens_are_probed_automatically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Requirement probing itself stays launchd-specific.

    Writer evidence is supplied through ``writer_registry`` and enforced at
    terminal PASS; it is not inferred merely from a requirement spelling.
    """
    monkeypatch.setattr(recorder, "_launchd_label_loaded", lambda label: True)
    check = _manifest_check(
        check_id="NSC-C01-WRITER",
        target="tests/example.py::test_pass",
        requires=("writer:landed-mechanism",),
    )
    results = evaluate_checks(
        [check], [JUnitOutcome("tests/example.py::test_pass", "passed")], repo_root=tmp_path
    )
    assert results[0].verdict is CheckVerdict.NOT_EVALUABLE
    assert results[0].reason == "precondition_missing:writer:landed-mechanism"


def test_an_unprobeable_launchd_label_stays_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every probe failure answers "not loaded": an unobservable prerequisite is
    not a satisfied one, and a missing launchctl must not grant a PASS."""
    monkeypatch.setattr(recorder, "_LAUNCHD_PROBE_CACHE", {})
    monkeypatch.setattr(recorder, "LAUNCHCTL", "/nonexistent/launchctl")
    assert recorder._launchd_label_loaded("com.omniagentos.anything") is False
    # Malformed labels are refused without shelling out at all.
    monkeypatch.setattr(recorder, "_LAUNCHD_PROBE_CACHE", {})
    assert recorder._launchd_label_loaded("gui/501/evil") is False
    assert recorder._launchd_label_loaded("") is False


def _approved_record(tmp_path: Path, labels: list[str]) -> Path:
    """A fixture standing in for configs/launchd-approved.yaml."""
    path = tmp_path / "launchd-approved.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "schema": "launchd-approved/v1",
                "entries": [
                    {"label": label, "reason": "fixture", "approved_on": "2026-08-09"}
                    for label in labels
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _use_approved_record(
    monkeypatch: pytest.MonkeyPatch, path: Path, probe: Callable[[str], bool]
) -> list[str]:
    """Point the resolver at *path*, install *probe*, and clear both caches."""
    monkeypatch.setattr(recorder, "_LAUNCHD_PROBE_CACHE", {})
    monkeypatch.setattr(recorder, "_LAUNCHD_APPROVED_CACHE", {})
    monkeypatch.setattr(recorder, "_approved_label_paths", lambda: (path,))
    probed: list[str] = []

    def recording_probe(label: str) -> bool:
        probed.append(label)
        return probe(label)  # type: ignore[operator]

    monkeypatch.setattr(recorder, "_probe_launchd_label", recording_probe)
    return probed


def test_a_short_manifest_token_resolves_to_the_approved_prefixed_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest writes `launchd:health-sentinel`; the machine runs
    `com.omniagentos.health-sentinel`.

    Probing the token literally can never observe the real job, and the False it
    returns is indistinguishable from a genuinely absent one — so a landed,
    approved, running mechanism stayed masked forever (R1-010).
    """
    record = _approved_record(
        tmp_path,
        ["com.omniagentos.health-sentinel", "com.omniagentos.provider-sentinel"],
    )
    probed = _use_approved_record(
        monkeypatch, record, lambda label: label == "com.omniagentos.health-sentinel"
    )

    assert recorder._launchd_label_loaded("health-sentinel") is True
    # The exact label still works, and resolution is exact-token-first.
    assert recorder._launchd_label_loaded("com.omniagentos.health-sentinel") is True
    assert probed[:2] == ["health-sentinel", "com.omniagentos.health-sentinel"]
    # A token nothing approved matches is still refused, not guessed at.
    assert recorder._launchd_label_loaded("never-installed") is False


def test_suffix_resolution_matches_whole_segments_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.<token>` — not `endswith(token)`.

    A loaded `com.omniagentos.xhealth-sentinel` is a DIFFERENT job; letting
    it satisfy `launchd:health-sentinel` would unmask a check on a prerequisite
    that is not there.
    """
    record = _approved_record(tmp_path, ["com.omniagentos.xhealth-sentinel"])
    probed = _use_approved_record(
        monkeypatch, record, lambda label: label == "com.omniagentos.xhealth-sentinel"
    )
    assert recorder._launchd_label_loaded("health-sentinel") is False
    assert probed == ["health-sentinel"]


def test_an_unreadable_approved_record_falls_back_to_exact_token_probing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-soft, and narrowly: no record means the pre-existing exact-token
    behaviour, never an invented label and never a granted PASS."""
    probed = _use_approved_record(
        monkeypatch,
        tmp_path / "does-not-exist.yaml",
        lambda label: label == "com.omniagentos.health-sentinel",
    )
    assert recorder._launchd_label_loaded("health-sentinel") is False
    assert probed == ["health-sentinel"]
    monkeypatch.setattr(recorder, "_LAUNCHD_PROBE_CACHE", {})
    assert recorder._launchd_label_loaded("com.omniagentos.health-sentinel") is True


def test_the_real_approved_record_carries_the_manifest_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin the resolution against the files in this checkout, not a fixture.

    The load-bearing assertion is on the resolver plus the approved-label record
    (both owned here). The manifest sweep that follows is a bonus invariant —
    every SHORT `launchd:` token it masks on must resolve to an approved label,
    or the mask can never lift by observation — and it is deliberately NOT
    guarded with `assert tokens`: the manifest is another lane's file, and a
    test of this resolver must not turn a manifest edit into a false red. Tokens
    that are already fully-qualified labels are probed exactly and may name a job
    this machine has not approved (it simply stays masked).
    """
    monkeypatch.setattr(recorder, "_LAUNCHD_APPROVED_CACHE", {})
    approved = set(recorder._approved_launchd_labels())
    assert "com.omniagentos.health-sentinel" in approved, "the real record must be readable"
    assert "com.omniagentos.health-sentinel" in recorder._launchd_label_candidates(
        "health-sentinel"
    )

    manifest = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "configs/northstar-cert/manifest.yaml").read_text()
    )
    tokens = {
        requirement[len(recorder.LAUNCHD_REQUIREMENT_PREFIX) :]
        for check in manifest["checks"]
        for requirement in check["requires"]
        if requirement.startswith(recorder.LAUNCHD_REQUIREMENT_PREFIX)
    }
    for token in sorted(tokens):
        if "." in token:
            continue
        candidates = recorder._launchd_label_candidates(token)
        assert any(candidate in approved for candidate in candidates), token


def test_masked_launchd_targets_are_also_excluded_from_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Selection and grading must use ONE availability answer: if they disagree,
    the run collects nodes it then refuses to grade."""
    monkeypatch.setattr(recorder, "_launchd_label_loaded", lambda label: label == "loaded-job")
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        """
version: 1
checks:
  - {id: NSC-L01, capability: northstar-cert, tier: t1, gate: false, scope: scenario,
     provenance: [x], requires: ["launchd:loaded-job"],
     binding: {type: pytest, target: tests/a.py::test_loaded}}
  - {id: NSC-L02, capability: northstar-cert, tier: t1, gate: false, scope: scenario,
     provenance: [x], requires: ["launchd:absent-job"],
     binding: {type: pytest, target: tests/b.py::test_absent}}
"""
    )
    assert runnable_targets(manifest, "t1", repo_root=tmp_path) == ["tests/a.py::test_loaded"]


# ---------------------------------------------------------------------------
# Runner parity: make and the launchd cadence are two carriers of ONE contract.
# ---------------------------------------------------------------------------

CADENCE = Path(__file__).resolve().parents[2] / "scripts/northstar-cert-cadence/t1_cadence.sh"
MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"


def _cadence_recipe() -> str:
    return CADENCE.read_text(encoding="utf-8")


def _make_recipe() -> str:
    text = MAKEFILE.read_text(encoding="utf-8")
    start = text.index("define nscert_run")
    return text[start : text.index("endef", start)]


def test_both_runners_select_targets_through_the_recorder() -> None:
    """The cadence used to reparse the manifest itself and hand pytest every t1
    binding, masked ones included — one unresolvable node id aborts COLLECTION
    for the whole run. Selection has exactly one implementation."""
    cadence = _cadence_recipe()
    assert "record_results.py" in cadence and "--list-targets" in cadence
    # Keyed on the SHAPE of the defect (a second manifest reader), not on the
    # spelling of the heredoc that carried it.
    assert "safe_load" not in cadence, "the cadence must not parse the manifest itself"
    assert "import yaml" not in cadence, "the cadence must not carry a second manifest reader"
    assert "--list-targets" in _make_recipe()


def _runner_pytest_markexpr(recipe: str) -> str:
    """The ``-m`` expression each runner hands its pytest invocation.

    Line continuations are folded first, so the parse does not depend on where a
    carrier happens to wrap.
    """
    folded = recipe.replace("\\\n", " ")
    invocation = next(
        (
            line
            for line in folded.splitlines()
            if "pytest" in line and "--junitxml" in line and not line.lstrip().startswith("#")
        ),
        "",
    )
    assert invocation, "runner has no pytest --junitxml invocation"
    match = re.search(r"(?<!\w)-m\s+[\"']([^\"']+)[\"']", invocation.split("pytest", 1)[1])
    assert match, f"runner does not override the default marker filter: {invocation.strip()}"
    return match.group(1)


def test_both_runners_run_an_explicitly_selected_node_despite_the_default_marker_filter(
    tmp_path: Path,
) -> None:
    """pyproject's addopts exclude `counterfeit_gate` from the DEFAULT selection.

    pytest applies `-m` to explicit node ids too, so the manifest-selected
    counterfeit-bound hard gates (C11-01, C43-01/02, C43-08) were silently
    dropped: pytest exited 0, the JUnit had no row for them, and the recorder
    rendered NOT_EVALUABLE(not_executed) — a certification hole wearing a
    mechanics refusal's clothes (R3-002).

    This does not assert a SPELLING. It reads whatever expression each runner
    actually passes and PROVES, with a real pytest run against the real
    pyproject, that a counterfeit-marked node id survives it.
    """
    cadence_expr = _runner_pytest_markexpr(_cadence_recipe())
    make_expr = _runner_pytest_markexpr(_make_recipe())
    assert cadence_expr == make_expr, "two carriers of one procedure have drifted"

    repo_root = Path(__file__).resolve().parents[2]
    junit = tmp_path / "markexpr-junit.xml"
    hard_gate = "tests/counterfeits/test_gate.py::test_manifest_loads_and_patches_exist"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-m",
            cadence_expr,
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            hard_gate,
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    names = [case.attrib.get("name", "") for case in ET.parse(junit).iter("testcase")]

    assert completed.returncode == 0, completed.stdout[-2000:]
    assert "test_manifest_loads_and_patches_exist" in names, (
        "the runner's marker expression still deselects a manifest-selected hard gate"
    )


def test_neither_runner_discards_a_command_status() -> None:
    for name, recipe in (("cadence", _cadence_recipe()), ("make", _make_recipe())):
        assert "|| true" not in recipe, f"{name} discards a status with || true"


def test_both_runners_forward_declared_requirements() -> None:
    for name, recipe in (("cadence", _cadence_recipe()), ("make", _make_recipe())):
        assert "--available-requirement" in recipe, f"{name} cannot accept satisfied requirements"
        assert "NSCERT_AVAILABLE_REQUIREMENTS" in recipe, f"{name} has no availability input"


def _fake_python(path: Path, record_rc: int) -> None:
    path.write_text(
        f"""#!/bin/sh
echo "$*" >> "$FAKE_CALL_LOG"
case "$*" in
  *--list-targets*) echo tests/x.py::test_x; exit 0 ;;
esac
case "$1" in
  *record_results.py) exit {record_rc} ;;
  *) exit 0 ;;
esac
"""
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run_cadence(tmp_path: Path, record_rc: int) -> tuple[int, str]:
    """Drive the REAL cadence script against a stub interpreter."""
    root = tmp_path / f"root-{record_rc}"
    (root / "scripts/northstar-cert-cadence").mkdir(parents=True)
    (root / "configs/northstar-cert").mkdir(parents=True)
    shutil.copy2(CADENCE, root / "scripts/northstar-cert-cadence/t1_cadence.sh")
    (root / "scripts/launch-env.sh").write_text("return 0\n")
    (root / "configs/northstar-cert/manifest.yaml").write_text("checks: []\n")
    calls = tmp_path / f"calls-{record_rc}"
    interpreter = tmp_path / f"python-{record_rc}"
    _fake_python(interpreter, record_rc)
    completed = subprocess.run(
        ["/bin/bash", str(root / "scripts/northstar-cert-cadence/t1_cadence.sh")],
        env=os.environ
        | {
            "NSCERT_VENV_PY": str(interpreter),
            "NSCERT_RESULTS_DIR": str(tmp_path / f"results-{record_rc}"),
            "NSCERT_RUN_LOG": str(tmp_path / f"run-{record_rc}.log"),
            "FAKE_CALL_LOG": str(calls),
        },
        capture_output=True,
        text=True,
    )
    return completed.returncode, (calls.read_text(encoding="utf-8") if calls.exists() else "")


@pytest.mark.parametrize("record_rc", [1, 2])
def test_cadence_emits_gaps_before_propagating_an_honest_verdict(
    tmp_path: Path, record_rc: int
) -> None:
    """FAILED (1) and INCONCLUSIVE (2) are the runs that MATTER to the loop.

    Under `set -e` the recorder's nonzero exit used to kill the script before
    gap emission, so the most important failing runs never fed the queue.
    """
    code, calls = _run_cadence(tmp_path, record_rc)
    assert "emit_gaps.py" in calls
    assert code == record_rc, "the runner's exit is the recorder's verdict"


def test_cadence_hands_pytest_the_marker_override_with_its_selected_nodes(
    tmp_path: Path,
) -> None:
    """The override must reach the REAL invocation, not just the source text.

    Driven through the same stub interpreter as the other cadence tests, so it
    observes the argv the script actually builds.
    """
    code, calls = _run_cadence(tmp_path, 0)
    invocation = next((line for line in calls.splitlines() if line.startswith("-m pytest")), "")
    assert invocation, f"the cadence never invoked pytest:\n{calls}"
    assert "tests/x.py::test_x" in invocation, "the recorder-selected node id must be run"
    assert _runner_pytest_markexpr(_cadence_recipe()) in invocation, (
        "the default marker exclusion is still in force for the cadence's explicit nodes"
    )
    assert code == 0


def test_cadence_refuses_to_emit_gaps_for_a_void_run(tmp_path: Path) -> None:
    """VOID means nothing was measured. Emitting gaps here would file instrument
    faults as candidate product defects."""
    code, calls = _run_cadence(tmp_path, 70)
    assert "emit_gaps.py" not in calls
    assert code == 70


def test_cadence_records_nothing_when_pytest_could_not_run(tmp_path: Path) -> None:
    root = tmp_path / "root-instrument"
    (root / "scripts/northstar-cert-cadence").mkdir(parents=True)
    (root / "configs/northstar-cert").mkdir(parents=True)
    shutil.copy2(CADENCE, root / "scripts/northstar-cert-cadence/t1_cadence.sh")
    (root / "scripts/launch-env.sh").write_text("return 0\n")
    (root / "configs/northstar-cert/manifest.yaml").write_text("checks: []\n")
    calls = tmp_path / "calls-instrument"
    interpreter = tmp_path / "python-instrument"
    interpreter.write_text(
        """#!/bin/sh
echo "$*" >> "$FAKE_CALL_LOG"
case "$*" in
  *--list-targets*) echo tests/x.py::test_x; exit 0 ;;
  *-m\\ pytest*) exit 4 ;;
  *) exit 0 ;;
esac
"""
    )
    interpreter.chmod(interpreter.stat().st_mode | stat.S_IXUSR)
    completed = subprocess.run(
        ["/bin/bash", str(root / "scripts/northstar-cert-cadence/t1_cadence.sh")],
        env=os.environ
        | {
            "NSCERT_VENV_PY": str(interpreter),
            "NSCERT_RESULTS_DIR": str(tmp_path / "results-instrument"),
            "NSCERT_RUN_LOG": str(tmp_path / "run-instrument.log"),
            "FAKE_CALL_LOG": str(calls),
        },
        capture_output=True,
        text=True,
    )
    text = calls.read_text(encoding="utf-8")
    assert completed.returncode == 4
    assert "--run-id" not in text, "a pytest that could not run must record no receipt"
    assert "emit_gaps.py" not in text


# --------------------------------------------------------------- suite identity


def _suite_manifest_check(check_id: str, *, target: str) -> ManifestCheck:
    return ManifestCheck(
        id=check_id,
        capability="C-01",
        binding_type="pytest",
        target=target,
        tier="t1",
        gate=False,
        requires=(),
        scope="scenario",
        provenance=("unit-test",),
        shared=False,
    )


def test_every_suite_version_owns_its_own_full_case_set(tmp_path: Path) -> None:
    """A suite version is immutable evidence, so eval-case identity is scoped to
    it. Keying cases on check.id alone let the first version that ran own every
    row forever: the bumped suite created NOTHING and `candidate_cases` returned
    an empty set for it, so a version bump silently produced a suite with zero
    cases (measured: v1=265, v2=0)."""
    store = LabStore(str(tmp_path / "results.sqlite3"))
    try:
        checks = [
            _suite_manifest_check(f"NSC-C01-{index:02d}", target=f"tests/x.py::test_{index}")
            for index in range(3)
        ]
        v1 = recorder._ensure_suite(store, version=1, manifest_digest="1" * 64)
        v2 = recorder._ensure_suite(store, version=2, manifest_digest="2" * 64)
        for suite in (v1, v2):
            for check in checks:
                recorder._ensure_case(store, suite, check)

        counts = dict(
            store._connection.execute(
                "SELECT suite_id, COUNT(*) FROM eval_cases GROUP BY suite_id"
            ).fetchall()
        )
        assert counts == {v1: len(checks), v2: len(checks)}
        assert len(store.candidate_cases(v1, "dev")) == len(checks)
        assert len(store.candidate_cases(v2, "dev")) == len(checks)
    finally:
        store._store.close()


def test_a_new_suite_version_may_redefine_a_check(tmp_path: Path) -> None:
    """The rollover failure mode: a v2 whose check metadata legitimately differs
    from v1 hit 'stored eval case disagrees with manifest' and could never
    record — permanently VOIDing that version."""
    store = LabStore(str(tmp_path / "results.sqlite3"))
    try:
        original = _suite_manifest_check("NSC-C01-01", target="tests/x.py::test_one")
        changed = _suite_manifest_check("NSC-C01-01", target="tests/x.py::test_one_v2")
        v1 = recorder._ensure_suite(store, version=1, manifest_digest="1" * 64)
        recorder._ensure_case(store, v1, original)
        v2 = recorder._ensure_suite(store, version=2, manifest_digest="2" * 64)
        recorder._ensure_case(store, v2, changed)  # must not raise

        stored = {
            row["suite_id"]: json.loads(row["input_json"])["binding"]["target"]
            for row in store._connection.execute(
                "SELECT suite_id, input_json FROM eval_cases"
            ).fetchall()
        }
        assert stored == {v1: "tests/x.py::test_one", v2: "tests/x.py::test_one_v2"}
        # ...and within ONE suite a changed definition is still a refusal.
        with pytest.raises(CertificationError, match="disagrees with manifest"):
            recorder._ensure_case(store, v2, _suite_manifest_check("NSC-C01-01", target="other"))
    finally:
        store._store.close()


def test_a_legacy_unscoped_case_row_is_adopted_not_duplicated(tmp_path: Path) -> None:
    """Rows written before ids were suite-scoped belong to the suite they are
    associated with; re-recording that suite must not mint a second row."""
    store = LabStore(str(tmp_path / "results.sqlite3"))
    try:
        check = _suite_manifest_check("NSC-C01-01", target="tests/x.py::test_one")
        v1 = recorder._ensure_suite(store, version=1, manifest_digest="1" * 64)
        store.add_eval_case(
            EvalCase(
                id=recorder._stable_id("evc_nsc", check.id),  # the pre-scoping identity
                suite=v1,
                split=EvalSplit.DEV,
                input=check.case_input(),
                rubric="North Star check verdict: PASS / FAIL / NOT_EVALUABLE",
            )
        )

        recorder._ensure_case(store, v1, check)
        assert store._connection.execute("SELECT COUNT(*) FROM eval_cases").fetchone()[0] == 1

        # ...and the legacy row is NOT mistaken for another suite's case.
        v2 = recorder._ensure_suite(store, version=2, manifest_digest="2" * 64)
        recorder._ensure_case(store, v2, check)
        counts = dict(
            store._connection.execute(
                "SELECT suite_id, COUNT(*) FROM eval_cases GROUP BY suite_id"
            ).fetchall()
        )
        assert counts == {v1: 1, v2: 1}
    finally:
        store._store.close()


def test_a_manifest_without_a_version_is_refused(tmp_path: Path) -> None:
    """`document.get("version", 1)` read a MISSING key as a healthy v1 — the
    favourable absence that let a content change ship unbumped and VOID the
    estate. An absent version is an unknown suite identity."""
    check = _check("NSC-C01-01", "tests/example.py::test_one")
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"checks": [check]}), encoding="utf-8")

    with pytest.raises(CertificationError, match="must declare an explicit integer version"):
        recorder._load_manifest(path, "t1")

    # an explicit version is still accepted, and still type-checked
    path.write_text(yaml.safe_dump({"version": 1, "checks": [check]}), encoding="utf-8")
    _checks, version, _digest, _vacuous = recorder._load_manifest(path, "t1")
    assert version == 1
    path.write_text(yaml.safe_dump({"version": "1", "checks": [check]}), encoding="utf-8")
    with pytest.raises(CertificationError, match="positive integer"):
        recorder._load_manifest(path, "t1")


def test_gap_metadata_is_read_from_the_runs_own_suite(tmp_path: Path) -> None:
    """Propagation guard for the suite-scoped case ids: once two suite versions
    both own a row for the same check_id, emit_gaps' case-metadata map is
    ambiguous unless it is scoped to the run's suite. That metadata supplies
    `capability`/`scope` to the gap's IDENTITY payload, so a v1 run described
    with v2's metadata mints a different finding id for the same breakage."""
    import scripts.northstar_cert.emit_gaps as emitter

    db_path = tmp_path / "results.sqlite3"
    store = LabStore(str(db_path))
    try:
        check = _suite_manifest_check("NSC-C01-01", target="tests/x.py::test_one")
        v1 = recorder._ensure_suite(store, version=1, manifest_digest="1" * 64)
        recorder._ensure_case(store, v1, check)
        v2 = recorder._ensure_suite(store, version=2, manifest_digest="2" * 64)
        recorder._ensure_case(
            store,
            v2,
            ManifestCheck(
                id=check.id,
                capability="C-99-RENAMED-IN-V2",
                binding_type="pytest",
                target=check.target,
                tier="t1",
                gate=False,
                requires=(),
                scope="scenario",
                provenance=("unit-test",),
                shared=False,
            ),
        )
        recorder._record_eval_result(
            store,
            run_id="nscert-t1-20260811T000000Z",
            suite_id=v1,
            version=1,
            result=CheckResult(
                check=check,
                verdict=CheckVerdict.FAIL,
                reason="pytest_failure:boom",
            ),
        )
    finally:
        store._store.close()

    decoded = emitter._read_evals(db_path, "nscert-t1-20260811T000000Z")

    assert len(decoded) == 1
    assert decoded[0].metadata["capability"] == "C-01", (
        "a v1 run was described with v2's case metadata"
    )


# ------------------------------------------------- per-(token, check) masking


# The lane's pilot pair, as wired in the real registry.
LANDED_CHECK = "NSC-C08-04"
SIBLING_CHECK = "NSC-C07-04"
CARRIER = "tests/swarm/test_worktrees.py::TestMerge::test_clean_merge_lands_no_ff_in_main"
SIBLING_CARRIER = "tests/swarm/test_worktrees.py::TestMerge::test_merge_of_unchanged_branch_is_noop"
WITNESS_SYMBOL = "omniagentos/swarm/worktrees.py::SubprocessSwarmWorktrees"
PROOF_NODE = (
    "tests/swarm/test_worktrees.py::TestMerge::"
    "test_lane_work_reaches_main_only_through_the_merge_commit"
)


def _fold_check(check_id: str, target: str) -> ManifestCheck:
    return ManifestCheck(
        id=check_id,
        capability="C-02",
        binding_type="pytest",
        target=target,
        tier="t1",
        gate=False,
        requires=("glue:fold-legs-pending",),
        scope="scenario",
        provenance=("unit-test",),
        shared=False,
    )


def _fold_registry(*, landed: bool) -> dict[str, recorder.MaskEvidence]:
    evidence = (
        {"witness": WITNESS_SYMBOL, "proof": PROOF_NODE}
        if landed
        else {"witness": None, "proof": None}
    )
    return {
        "glue:fold-legs-pending": recorder.MaskEvidence(
            (LANDED_CHECK, SIBLING_CHECK),
            pairs=(
                recorder.PairEvidence(LANDED_CHECK, **evidence),
                recorder.PairEvidence(SIBLING_CHECK),
            ),
        )
    }


def _fold_outcomes(*, proof_status: str = "passed") -> list[JUnitOutcome]:
    return [
        JUnitOutcome(CARRIER, "passed"),
        JUnitOutcome(SIBLING_CARRIER, "passed"),
        JUnitOutcome(PROOF_NODE, proof_status),
    ]


def _fold_verdicts(registry, outcomes, *, available=frozenset()):
    results = evaluate_checks(
        [
            _fold_check(LANDED_CHECK, CARRIER),
            _fold_check(SIBLING_CHECK, SIBLING_CARRIER),
        ],
        outcomes,
        repo_root=Path(__file__).resolve().parents[2],
        writer_registry=registry,
        available_requirements=available,
    )
    return {result.check.id: (result.verdict.value, result.reason) for result in results}


def test_an_unlanded_pair_masks_exactly_as_the_bare_gates_list_did() -> None:
    verdicts = _fold_verdicts(_fold_registry(landed=False), _fold_outcomes())
    assert verdicts[LANDED_CHECK] == (
        "NOT_EVALUABLE",
        "precondition_missing:glue:fold-legs-pending",
    )
    assert verdicts[SIBLING_CHECK] == (
        "NOT_EVALUABLE",
        "precondition_missing:glue:fold-legs-pending",
    )


def test_a_landed_pair_unmasks_its_own_check_only() -> None:
    verdicts = _fold_verdicts(_fold_registry(landed=True), _fold_outcomes())
    assert verdicts[LANDED_CHECK][0] == "PASS"
    assert verdicts[SIBLING_CHECK] == (
        "NOT_EVALUABLE",
        "precondition_missing:glue:fold-legs-pending",
    )


def test_a_landed_pair_whose_proof_failed_is_named_not_silently_masked() -> None:
    verdicts = _fold_verdicts(_fold_registry(landed=True), _fold_outcomes(proof_status="failure"))
    verdict, reason = verdicts[LANDED_CHECK]
    assert verdict == "NOT_EVALUABLE"
    assert reason == f"no_writer_evidence:proof_not_passing:glue:fold-legs-pending@{LANDED_CHECK}"


def test_operator_arming_stays_an_or_over_pair_evidence() -> None:
    """NSCERT_AVAILABLE_REQUIREMENTS is additive, never subtractive: arming the
    whole token still works, and a broken pair cannot cancel it."""
    armed = frozenset({"glue:fold-legs-pending"})
    both_masked = _fold_verdicts(_fold_registry(landed=False), _fold_outcomes(), available=armed)
    assert both_masked[LANDED_CHECK][0] == "PASS"
    assert both_masked[SIBLING_CHECK][0] == "PASS"
    broken = _fold_verdicts(
        _fold_registry(landed=True), _fold_outcomes(proof_status="failure"), available=armed
    )
    assert broken[LANDED_CHECK][0] == "PASS"


def test_the_pilot_pair_is_wired_to_real_landed_evidence() -> None:
    """The lane's acceptance artifact: NSC-C02-02's fold leg exists on disk, in
    the real registry, and binds a real witness to a real proof."""
    root = Path(__file__).resolve().parents[2]
    registry = recorder._load_writer_registry(
        root / "configs/northstar-cert/writers.yaml", repo_root=root
    )
    pair = registry["glue:fold-legs-pending"].pair_for("NSC-C08-04")
    assert pair is not None and pair.landed
    assert pair.witness == WITNESS_SYMBOL
    assert pair.proof == PROOF_NODE
    # ...and it is the ONLY landed pair for that token in this lane.
    landed = sorted(p.check_id for p in registry["glue:fold-legs-pending"].pairs if p.landed)
    assert landed == ["NSC-C08-04"]


def test_evaluate_checks_refuses_a_directly_constructed_stub_pair(tmp_path: Path) -> None:
    """Defense in depth for FIX (a): even a registry handed in already-constructed
    (never through the loader) cannot buy a PASS for a witness nothing in shipped
    code consumes. The witness resolves and the proof exercises it — the ONLY
    thing missing is a real production consumer, and that alone must refuse it."""
    (tmp_path / "omniagentos").mkdir()
    (tmp_path / "omniagentos" / "stub.py").write_text(
        "class SyntheticWriter:\n    value = 1\n", encoding="utf-8"
    )
    (tmp_path / "proof.py").write_text(
        "import pytest\n"
        "from omniagentos.stub import SyntheticWriter\n\n"
        "@pytest.mark.nsc_writer_proof('glue:x@NSC-C01-STUB')\n"
        "def test_stub():\n    assert SyntheticWriter.value == 1\n",
        encoding="utf-8",
    )
    recorder._PRODUCTION_TREE_CACHE.clear()
    check = _manifest_check(
        check_id="NSC-C01-STUB",
        target="tests/example.py::test_carrier",
        requires=("glue:x",),
    )
    registry = {
        "glue:x": recorder.MaskEvidence(
            ("NSC-C01-STUB",),
            pairs=(
                recorder.PairEvidence(
                    "NSC-C01-STUB",
                    witness="omniagentos/stub.py::SyntheticWriter",
                    proof="proof.py::test_stub",
                ),
            ),
        )
    }
    outcomes = [
        JUnitOutcome(check.target, "passed"),
        JUnitOutcome("proof.py::test_stub", "passed"),
    ]
    result = evaluate_checks([check], outcomes, repo_root=tmp_path, writer_registry=registry)[0]
    assert result.verdict is CheckVerdict.NOT_EVALUABLE
    assert result.reason == "no_writer_evidence:witness_not_production:glue:x@NSC-C01-STUB"
