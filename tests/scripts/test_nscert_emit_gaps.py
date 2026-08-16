from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

import scripts.northstar_cert.emit_gaps as emit_gaps_module
from scripts.northstar_cert.emit_gaps import (
    DEFAULT_LIVE_OUTPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    LIVE_ENV_FLAG,
    CheckVerdict,
    FailedEval,
    GapEmissionError,
    _decode_eval,
    build_arg_parser,
    emit_gaps,
    main,
    resolve_gaps,
)
from scripts.northstar_cert.record_results import record_results


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


def _junit(tmp_path: Path, *, failing: bool = True) -> Path:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite")
    ET.SubElement(
        suite,
        "testcase",
        file="tests/example.py",
        classname="tests.example",
        name="test_pass",
    )
    failed = ET.SubElement(
        suite,
        "testcase",
        file="tests/example.py",
        classname="tests.example",
        name="test_fail",
    )
    if failing:
        ET.SubElement(failed, "failure", message="wrong result")
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _record(tmp_path: Path, run_id: str, *, failing: bool = True) -> tuple[Path, Path]:
    db = tmp_path / "results.sqlite3"
    evidence = tmp_path / "evidence"
    record_results(
        manifest_path=_manifest(tmp_path),
        junit_path=_junit(tmp_path, failing=failing),
        tier="t1",
        run_id=run_id,
        db_path=db,
        evidence_root=evidence,
        repo_root=tmp_path,
    )
    return db, evidence


def test_emit_gaps_is_dry_run_only_and_deduplicates_recurring_failures(
    tmp_path: Path,
) -> None:
    db, evidence = _record(tmp_path, "run-one")
    output = tmp_path / "gaps-dryrun"
    first = emit_gaps(
        db_path=db,
        run_id="run-one",
        output_dir=output,
        evidence_root=evidence,
        bundle_path=tmp_path / "bundles/run-one",
    )
    assert len(first) == 2
    payloads = [json.loads(path.read_text()) for path in first]
    assert all(payload["dry_run"] is True for payload in payloads)
    assert {payload["check_id"] for payload in payloads} == {
        "NSC-C14-FAIL",
        "NSC-C14-ABSENT",
    }
    assert not (tmp_path / "loopqueue").exists()

    _record(tmp_path, "run-two")
    second = emit_gaps(
        db_path=db,
        run_id="run-two",
        output_dir=output,
        evidence_root=evidence,
        bundle_path=tmp_path / "bundles/run-two",
    )
    assert set(first) == set(second)
    updated = [json.loads(path.read_text()) for path in second]
    assert all(payload["frequency"] == 2 for payload in updated)
    assert all(payload["observed_runs"] == ["run-one", "run-two"] for payload in updated)


def test_not_evaluable_eval_row_always_emits_gap(tmp_path: Path) -> None:
    db, evidence = _record(tmp_path, "absence-run")
    paths = emit_gaps(
        db_path=db,
        run_id="absence-run",
        output_dir=tmp_path / "gaps-dryrun",
        evidence_root=evidence,
        bundle_path=tmp_path / "bundle",
    )
    gaps = [json.loads(path.read_text()) for path in paths]
    missing = next(gap for gap in gaps if gap["check_id"] == "NSC-C14-ABSENT")
    assert missing["actual"].startswith("NOT_EVALUABLE(no_writer_evidence")
    assert missing["hard_gate"] is True


def test_emit_gaps_refuses_zero_eval_rows(tmp_path: Path) -> None:
    with pytest.raises(GapEmissionError, match="zero eval_results rows"):
        emit_gaps(
            db_path=tmp_path / "empty.sqlite3",
            run_id="missing-run",
            output_dir=tmp_path / "gaps-dryrun",
            evidence_root=tmp_path / "evidence",
            bundle_path=tmp_path / "bundle",
        )


def test_the_cli_full_void_path_stays_rc_70_distinct_from_the_partial_voided_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The recorder-driven full-VOID abort (nothing could be read at all) keeps
    its own rc 70 — deliberately NOT merged with the non-zero exit a run takes
    when SOME rows voided but emission otherwise completed (see the `voided`
    tests below, which get a different, distinct code)."""
    code = main(
        [
            "--run-id",
            "missing-run",
            "--db",
            str(tmp_path / "empty.sqlite3"),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--bundle",
            str(tmp_path / "bundle"),
            "--output-dir",
            str(tmp_path / "gaps-dryrun"),
        ]
    )
    report = json.loads(capsys.readouterr().out)
    assert code == 70
    assert "zero eval_results rows" in report["error"]


# ------------------------------------------------------------------ output dir + mode


def test_output_dir_defaults_stay_dry_run_and_only_live_switches_them() -> None:
    args = build_arg_parser().parse_args(["--run-id", "r"])
    assert args.output_dir is None and args.live is False and args.resolve is False
    assert DEFAULT_OUTPUT_DIR == Path("var/northstar-cert/gaps-dryrun")
    assert DEFAULT_LIVE_OUTPUT_DIR == Path("var/northstar-cert/gaps")


def test_cli_writes_to_an_explicit_output_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db, evidence = _record(tmp_path, "run-one")
    output = tmp_path / "elsewhere"

    code = main(
        [
            "--run-id",
            "run-one",
            "--db",
            str(db),
            "--evidence-root",
            str(evidence),
            "--bundle",
            str(tmp_path / "bundle"),
            "--output-dir",
            str(output),
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["dry_run"] is True
    assert report["output_dir"] == str(output)
    assert len(list(output.glob("*.json"))) == 2


def test_live_mode_labels_the_artifact_and_needs_both_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db, evidence = _record(tmp_path, "run-one")
    output = tmp_path / "gaps"
    argv = [
        "--run-id",
        "run-one",
        "--db",
        str(db),
        "--evidence-root",
        str(evidence),
        "--bundle",
        str(tmp_path / "bundle"),
        "--output-dir",
        str(output),
        "--live",
    ]

    monkeypatch.delenv(LIVE_ENV_FLAG, raising=False)
    assert main(argv) == 2
    assert LIVE_ENV_FLAG in json.loads(capsys.readouterr().out)["error"]
    assert not output.exists()

    monkeypatch.setenv(LIVE_ENV_FLAG, "1")
    assert main(argv) == 0
    capsys.readouterr()
    gaps = [json.loads(path.read_text()) for path in output.glob("*.json")]
    assert gaps and all(gap["dry_run"] is False for gap in gaps)
    assert all(gap["schema"] == "omniagentos.northstar-gap.v1" for gap in gaps)


# ------------------------------------------------------------------ resolution


def test_resolve_stamps_gaps_whose_check_now_passes(tmp_path: Path) -> None:
    db, evidence = _record(tmp_path, "run-one")
    output = tmp_path / "gaps"
    emit_gaps(
        db_path=db,
        run_id="run-one",
        output_dir=output,
        evidence_root=evidence,
        bundle_path=tmp_path / "bundle",
    )
    _record(tmp_path, "run-two", failing=False)
    emit_gaps(
        db_path=db,
        run_id="run-two",
        output_dir=output,
        evidence_root=evidence,
        bundle_path=tmp_path / "bundle",
    )

    resolved = resolve_gaps(db_path=db, run_id="run-two", output_dir=output)

    assert len(resolved) == 1
    gaps = {
        json.loads(path.read_text())["check_id"]: json.loads(path.read_text())
        for path in output.glob("*.json")
    }
    # The fixed check is stamped, and the artifact is KEPT.
    assert gaps["NSC-C14-FAIL"]["resolved_run"] == "run-two"
    assert gaps["NSC-C14-FAIL"]["resolved_at"].endswith("Z")
    assert gaps["NSC-C14-FAIL"]["frequency"] == 1
    # The still-broken hard gate is untouched.
    assert "resolved_run" not in gaps["NSC-C14-ABSENT"]
    assert gaps["NSC-C14-ABSENT"]["observed_runs"] == ["run-one", "run-two"]


def test_resolution_is_idempotent_and_keeps_the_first_closing_run(tmp_path: Path) -> None:
    db, evidence = _record(tmp_path, "run-one")
    output = tmp_path / "gaps"
    emit_gaps(
        db_path=db,
        run_id="run-one",
        output_dir=output,
        evidence_root=evidence,
        bundle_path=tmp_path / "bundle",
    )
    _record(tmp_path, "run-two", failing=False)
    assert len(resolve_gaps(db_path=db, run_id="run-two", output_dir=output)) == 1
    _record(tmp_path, "run-three", failing=False)
    assert resolve_gaps(db_path=db, run_id="run-three", output_dir=output) == []

    gap = next(
        json.loads(path.read_text())
        for path in output.glob("*.json")
        if json.loads(path.read_text())["check_id"] == "NSC-C14-FAIL"
    )
    assert gap["resolved_run"] == "run-two"


def test_a_resolved_gap_that_breaks_again_loses_its_resolution(tmp_path: Path) -> None:
    """A stamp left standing over live breakage is the favourable-absence shape:
    the loop would read the gap as closed while certification says it is open."""
    db, evidence = _record(tmp_path, "run-one")
    output = tmp_path / "gaps"
    emit_gaps(
        db_path=db,
        run_id="run-one",
        output_dir=output,
        evidence_root=evidence,
        bundle_path=tmp_path / "bundle",
    )
    _record(tmp_path, "run-two", failing=False)
    resolve_gaps(db_path=db, run_id="run-two", output_dir=output)

    _record(tmp_path, "run-three")
    emit_gaps(
        db_path=db,
        run_id="run-three",
        output_dir=output,
        evidence_root=evidence,
        bundle_path=tmp_path / "bundle",
    )

    gap = next(
        json.loads(path.read_text())
        for path in output.glob("*.json")
        if json.loads(path.read_text())["check_id"] == "NSC-C14-FAIL"
    )
    assert "resolved_run" not in gap
    assert "resolved_at" not in gap
    assert gap["regressions"] == [
        {
            "resolved_run": "run-two",
            "resolved_at": gap["regressions"][0]["resolved_at"],
            "regressed_run": "run-three",
            "regressed_at": gap["regressions"][0]["regressed_at"],
        }
    ]
    assert gap["observed_runs"] == ["run-one", "run-three"]


def test_resolve_leaves_other_projects_alone(tmp_path: Path) -> None:
    db, evidence = _record(tmp_path, "run-one")
    output = tmp_path / "gaps"
    emit_gaps(
        db_path=db,
        run_id="run-one",
        output_dir=output,
        evidence_root=evidence,
        bundle_path=tmp_path / "bundle",
        project="other-project",
    )
    _record(tmp_path, "run-two", failing=False)

    assert resolve_gaps(db_path=db, run_id="run-two", output_dir=output, project="estate") == []
    assert all("resolved_run" not in json.loads(p.read_text()) for p in output.glob("*.json"))


def test_resolve_on_an_absent_output_dir_is_a_no_op(tmp_path: Path) -> None:
    db, _ = _record(tmp_path, "run-one")
    assert resolve_gaps(db_path=db, run_id="run-one", output_dir=tmp_path / "absent") == []


# ------------------------------------------------------------------ VOID is not a gap


def _void_manifest(tmp_path: Path) -> Path:
    """Three checks on one carrier (a vacuous binding) plus one real failure."""
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
            "id": f"NSC-C14-VOID{index}",
            "binding": {"type": "pytest", "target": "tests/example.py::test_pass", "shared": True},
        }
        for index in range(3)
    ]
    checks.append(
        {
            **base,
            "id": "NSC-C14-FAIL",
            "binding": {"type": "pytest", "target": "tests/example.py::test_fail"},
        }
    )
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "checks": checks}), encoding="utf-8")
    return path


def _record_void(tmp_path: Path, run_id: str) -> tuple[Path, Path]:
    db = tmp_path / "results.sqlite3"
    evidence = tmp_path / "evidence"
    record_results(
        manifest_path=_void_manifest(tmp_path),
        junit_path=_junit(tmp_path),
        tier="t1",
        run_id=run_id,
        db_path=db,
        evidence_root=evidence,
        repo_root=tmp_path,
    )
    return db, evidence


def test_void_rows_are_counted_and_never_emitted_as_gaps(tmp_path: Path) -> None:
    """An instrument fault must never be reported as a candidate product defect.

    The recorder writes VOID as void=1 AND not_evaluable=1; an emitter that
    reads only the three-metric vocabulary turns every instrument failure into
    work for the loop — three product gaps for one broken binding.
    """
    db, evidence = _record_void(tmp_path, "void-run")

    paths = emit_gaps(
        db_path=db,
        run_id="void-run",
        output_dir=tmp_path / "gaps",
        evidence_root=evidence,
        bundle_path=tmp_path / "bundle",
    )

    assert paths.voided == 3
    emitted = [json.loads(path.read_text()) for path in paths]
    assert [gap["check_id"] for gap in emitted] == ["NSC-C14-FAIL"]
    assert not any("vacuous_binding" in gap["actual"] for gap in emitted)
    assert len(list((tmp_path / "gaps").glob("*.json"))) == 1


def test_voided_rows_are_named_not_just_counted(tmp_path: Path) -> None:
    """The library call retains each VOID row's identity, not just a count.

    A bare count is the same favourable-absence shape the undescribable path
    was closed against: `voided` alone tells a reader THAT rows were
    suppressed but not WHICH ones, so nobody can go verify the instrument
    fault without re-running the whole certification pass.
    """
    db, evidence = _record_void(tmp_path, "void-run")

    paths = emit_gaps(
        db_path=db,
        run_id="void-run",
        output_dir=tmp_path / "gaps",
        evidence_root=evidence,
        bundle_path=tmp_path / "bundle",
    )

    assert len(paths.voided_rows) == 3
    assert all(row.startswith("NSC-C14-VOID") for row in paths.voided_rows)
    assert all("instrument_error:vacuous_binding" in row for row in paths.voided_rows)


def test_the_cli_summary_reports_the_void_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Nothing-broke and nothing-was-measurable must be distinguishable on the
    face of the summary — and now also on the exit status, since a summary
    field alone is invisible to every consumer but the JSON reader."""
    db, evidence = _record_void(tmp_path, "void-run")

    code = main(
        [
            "--run-id",
            "void-run",
            "--db",
            str(db),
            "--evidence-root",
            str(evidence),
            "--bundle",
            str(tmp_path / "bundle"),
            "--output-dir",
            str(tmp_path / "gaps"),
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 2
    assert report["voided"] == 3
    assert len(report["gaps"]) == 1
    assert len(report["voided_rows"]) == 3
    assert all("NSC-C14-VOID" in row for row in report["voided_rows"])
    assert "3 eval row(s) voided" in captured.err


def test_a_voided_check_is_not_mistaken_for_a_pass_by_resolution(tmp_path: Path) -> None:
    """VOID is not PASS: an unmeasurable check must not close an open gap."""
    db, evidence = _record_void(tmp_path, "run-one")
    output = tmp_path / "gaps"
    emit_gaps(
        db_path=db,
        run_id="run-one",
        output_dir=output,
        evidence_root=evidence,
        bundle_path=tmp_path / "bundle",
    )
    resolved = resolve_gaps(db_path=db, run_id="run-one", output_dir=output, project="estate")
    assert resolved == []
    assert all("resolved_run" not in json.loads(p.read_text()) for p in output.glob("*.json"))


# --------------------------------------------- an instrument error is not a verdict (R1-005)


def _row(
    identifier: str, metrics: dict[str, float], per_case: dict[str, object]
) -> dict[str, object]:
    return {
        "id": identifier,
        "metrics_json": json.dumps(metrics),
        "per_case_json": json.dumps(per_case),
        "deterministic_passed": False,
    }


@pytest.mark.parametrize(
    ("label", "metrics", "per_case"),
    [
        # pass AND fail at once: the row contradicts itself.
        ("contradictory", {"pass": 1, "fail": 1, "not_evaluable": 0, "void": 0}, {"NSC-X": 1}),
        # no verdict bit set at all: absence, which is never favourable.
        ("absent", {"pass": 0, "fail": 0, "not_evaluable": 0, "void": 0}, {"NSC-X": 1}),
        # a PASS metric the deterministic check disagrees with.
        (
            "pass_without_determinism",
            {"pass": 1, "fail": 0, "not_evaluable": 0, "void": 0},
            {"NSC-X": 1},
        ),
    ],
)
def test_undecodable_verdict_metrics_void_rather_than_become_a_product_gap(
    label: str, metrics: dict[str, float], per_case: dict[str, object]
) -> None:
    """Labelling a row `instrument_error` and then emitting it anyway is the defect.

    These rows were decoded to NOT_EVALUABLE — an EMITTABLE verdict — while
    their own reason string said the instrument, not the product, was at fault.
    The gap that came out sent the next agent to debug working code.
    """
    decoded = _decode_eval(_row(label, metrics, per_case), {})
    assert decoded.verdict is CheckVerdict.VOID
    assert decoded.reason == "instrument_error:invalid_or_absent_verdict_metrics"


def test_a_reason_blob_that_will_not_decode_voids_whatever_the_flags_say() -> None:
    """The same rule one level down, for both an emittable verdict and a PASS.

    A row whose own annotation is corrupt was not measured cleanly, so it may
    neither become a gap nor close one.
    """
    corrupt_reason = {"NSC-X": 1, "__nsc_reason__:!!not-base64!!": 1}
    failing = _decode_eval(_row("fail", {"pass": 0, "fail": 1}, corrupt_reason), {})
    assert failing.verdict is CheckVerdict.VOID
    assert failing.reason == "instrument_error:invalid_reason_encoding"

    passing_row = _row("pass", {"pass": 1}, corrupt_reason)
    passing_row["deterministic_passed"] = True
    assert _decode_eval(passing_row, {}).verdict is CheckVerdict.VOID


def test_instrument_error_rows_are_counted_in_voided_and_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end: the suppressed rows land in the ONE summary counter, `voided`,
    are named in `voided_rows`, and the run exits non-zero because of them."""
    decoded = [
        FailedEval("NSC-REAL", CheckVerdict.FAIL, "pytest_failure:boom", {}),
        FailedEval(
            "NSC-BROKEN-INSTRUMENT",
            CheckVerdict.VOID,
            "instrument_error:invalid_or_absent_verdict_metrics",
            {},
        ),
    ]
    monkeypatch.setattr(emit_gaps_module, "_read_evals", lambda _db, _run: decoded)

    code = main(
        [
            "--run-id",
            "instrument-run",
            "--db",
            str(tmp_path / "results.sqlite3"),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--bundle",
            str(tmp_path / "bundle"),
            "--output-dir",
            str(tmp_path / "gaps"),
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 2
    assert report["voided"] == 1
    assert len(report["gaps"]) == 1
    assert report["voided_rows"] == [
        "NSC-BROKEN-INSTRUMENT:instrument_error:invalid_or_absent_verdict_metrics"
    ]
    assert "NSC-BROKEN-INSTRUMENT" in captured.err
    written = [json.loads(path.read_text()) for path in (tmp_path / "gaps").glob("*.json")]
    assert [gap["check_id"] for gap in written] == ["NSC-REAL"]


# ------------------------------------------------- suite-scoped case metadata


def _suite_check(check_id: str, capability: str):
    from scripts.northstar_cert.record_results import ManifestCheck

    return ManifestCheck(
        id=check_id,
        capability=capability,
        binding_type="pytest",
        target="tests/x.py::test_one",
        tier="t1",
        gate=False,
        requires=(),
        scope="scenario",
        provenance=("unit-test",),
        shared=False,
    )


def _fail_result_row(*, run_id: str, result_id: str, suite_id: str, version: int, check_id: str):
    from omniagentos.lab.contracts import EvalResult, EvalSplit

    return EvalResult(
        id=result_id,
        experiment_id=run_id,
        arm="champion",
        suite_id=suite_id,
        suite_version=version,
        split=EvalSplit.DEV,
        metrics={"pass": 0.0, "fail": 1.0, "not_evaluable": 0.0},
        per_case={check_id: {"pass": 0.0, "fail": 1.0, "not_evaluable": 0.0}},
        deterministic_passed=False,
    )


def _two_suite_store(tmp_path: Path):
    """v1 and v2 both owning a case for one check id, with different capabilities."""
    from omniagentos.lab.db import LabStore
    from scripts.northstar_cert.record_results import _ensure_case, _ensure_suite

    db_path = tmp_path / "results.sqlite3"
    store = LabStore(str(db_path))
    v1 = _ensure_suite(store, version=1, manifest_digest="1" * 64)
    _ensure_case(store, v1, _suite_check("NSC-C01-01", "C-01-IN-V1"))
    v2 = _ensure_suite(store, version=2, manifest_digest="2" * 64)
    _ensure_case(store, v2, _suite_check("NSC-C01-01", "C-99-IN-V2"))
    return db_path, store, v1, v2


def test_each_eval_row_is_described_by_its_own_suites_case(tmp_path: Path) -> None:
    """Case metadata is joined per (suite_id, check_id). A check_id-only map is
    decided by database traversal order once two suite versions own a row for
    the same check -- and that metadata supplies capability/scope to the gap's
    durable IDENTITY, so the loser's gap id is wrong in a field nobody
    re-checks. A run may legitimately carry rows from both suites."""
    db_path, store, v1, v2 = _two_suite_store(tmp_path)
    try:
        store.record_eval_result(
            _fail_result_row(
                run_id="mixed", result_id="evr_v1", suite_id=v1, version=1, check_id="NSC-C01-01"
            )
        )
        store.record_eval_result(
            _fail_result_row(
                run_id="mixed", result_id="evr_v2", suite_id=v2, version=2, check_id="NSC-C01-01"
            )
        )
    finally:
        store._store.close()

    decoded = emit_gaps_module._read_evals(db_path, "mixed")

    assert [item.metadata.get("capability") for item in decoded] == ["C-01-IN-V1", "C-99-IN-V2"]
    assert decoded.skipped == ()


@pytest.mark.parametrize("suite_id", ["", "evs_northstar_cert_v9"])
def test_a_row_with_no_case_in_its_own_suite_is_never_described_by_a_guess(
    tmp_path: Path, suite_id: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """There is no global fallback. An empty suite id is exactly as unknown as
    a suite that holds no case for the check: describing either from whatever
    else the database contains mints a durable finding id from guessed
    metadata. The row is skipped by name, and a run with nothing else left
    refuses rather than emitting."""
    db_path, store, _v1, _v2 = _two_suite_store(tmp_path)
    try:
        store.record_eval_result(
            _fail_result_row(
                run_id="orphan",
                result_id="evr_orphan",
                suite_id=suite_id,
                version=0,
                check_id="NSC-C01-01",
            )
        )
    finally:
        store._store.close()

    with pytest.raises(GapEmissionError, match="usable suite-scoped case metadata"):
        emit_gaps_module._read_evals(db_path, "orphan")
    assert "unknown_suite_identity:NSC-C01-01" in capsys.readouterr().err


def test_an_undescribable_row_is_named_beside_the_rows_that_are_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mixed run keeps what it can describe and names what it cannot -- the
    count rides in the summary next to `voided`, so a reader can tell
    'nothing broke' from 'nothing was describable'."""
    db_path, store, v1, _v2 = _two_suite_store(tmp_path)
    try:
        store.record_eval_result(
            _fail_result_row(
                run_id="mixed", result_id="evr_ok", suite_id=v1, version=1, check_id="NSC-C01-01"
            )
        )
        store.record_eval_result(
            _fail_result_row(
                run_id="mixed", result_id="evr_orphan", suite_id="", version=0, check_id="NSC-GHOST"
            )
        )
    finally:
        store._store.close()

    decoded = emit_gaps_module._read_evals(db_path, "mixed")

    assert [item.check_id for item in decoded] == ["NSC-C01-01"]
    assert decoded.skipped == ("unknown_suite_identity:NSC-GHOST",)
    assert "unknown_suite_identity:NSC-GHOST" in capsys.readouterr().err

    emitted = emit_gaps(
        db_path=db_path,
        run_id="mixed",
        output_dir=tmp_path / "gaps",
        evidence_root=tmp_path / "evidence",
        bundle_path=tmp_path / "bundle",
        dry_run=True,
    )
    assert emitted.undescribable == ("unknown_suite_identity:NSC-GHOST",)


# ------------------------------------------- fail-closed on partial readability


def _suite_with_case(tmp_path: Path, check_id: str, *, gate: bool = True):
    from omniagentos.lab.db import LabStore
    from scripts.northstar_cert.record_results import ManifestCheck, _ensure_case, _ensure_suite

    db_path = tmp_path / "results.sqlite3"
    store = LabStore(str(db_path))
    suite = _ensure_suite(store, version=1, manifest_digest="1" * 64)
    _ensure_case(
        store,
        suite,
        ManifestCheck(
            id=check_id,
            capability="C-01",
            binding_type="pytest",
            target="tests/x.py::test_one",
            tier="t1",
            gate=gate,
            requires=(),
            scope="scenario",
            provenance=("unit-test",),
            shared=False,
        ),
    )
    return db_path, store, suite


def _verdict_row(*, result_id: str, run_id: str, suite_id: str, check_id: str, passed: bool):
    from omniagentos.lab.contracts import EvalResult, EvalSplit

    return EvalResult(
        id=result_id,
        experiment_id=run_id,
        arm="champion",
        suite_id=suite_id,
        suite_version=1,
        split=EvalSplit.DEV,
        metrics={
            "pass": 1.0 if passed else 0.0,
            "fail": 0.0 if passed else 1.0,
            "not_evaluable": 0.0,
        },
        per_case={check_id: {"pass": float(passed), "fail": float(not passed)}},
        deterministic_passed=passed,
    )


def test_a_mixed_run_emits_what_it_can_and_still_exits_two(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Describable gaps are not held hostage — but a row this reader could not
    describe may itself be a genuine FAIL, so the run must not look successful.
    Only the EXIT STATUS is consumed downstream; a summary field alone is
    invisible."""
    db_path, store, suite = _suite_with_case(tmp_path, "NSC-READABLE")
    try:
        store.record_eval_result(
            _verdict_row(
                result_id="evr_red",
                run_id="mixed",
                suite_id=suite,
                check_id="NSC-READABLE",
                passed=False,
            )
        )
        store.record_eval_result(
            _verdict_row(
                result_id="evr_orphan",
                run_id="mixed",
                suite_id="",
                check_id="NSC-ORPHAN",
                passed=False,
            )
        )
    finally:
        store._store.close()

    code = main(
        [
            "--run-id",
            "mixed",
            "--db",
            str(db_path),
            "--evidence-root",
            str(tmp_path / "evidence"),
            "--bundle",
            str(tmp_path / "bundle"),
            "--output-dir",
            str(tmp_path / "gaps"),
        ]
    )

    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert code == 2
    assert report["undescribable"] == ["unknown_suite_identity:NSC-ORPHAN"]
    assert len(report["gaps"]) == 1  # the describable red still emitted
    written = [json.loads(path.read_text()) for path in (tmp_path / "gaps").glob("*.json")]
    assert [gap["check_id"] for gap in written] == ["NSC-READABLE"]
    assert "exiting 2" in captured.err


def test_a_check_with_an_undescribable_row_is_never_resolved(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A false `resolved` on a real red is the worst outcome this tool can
    produce: the gap stops being reported and nobody looks again. A check with
    ANY skipped row this run is excluded from resolution entirely — even when
    another row for that same check says PASS."""
    db_path, store, suite = _suite_with_case(tmp_path, "NSC-C01-RED")
    try:
        store.record_eval_result(
            _verdict_row(
                result_id="evr_old",
                run_id="old",
                suite_id=suite,
                check_id="NSC-C01-RED",
                passed=False,
            )
        )
    finally:
        store._store.close()

    gaps_dir = tmp_path / "gaps"
    emitted = emit_gaps(
        db_path=db_path,
        run_id="old",
        output_dir=gaps_dir,
        evidence_root=tmp_path / "evidence",
        bundle_path=tmp_path / "bundle-old",
        dry_run=True,
    )
    assert len(emitted) == 1
    gap_path = emitted[0]

    from omniagentos.lab.db import LabStore

    store = LabStore(str(db_path))
    try:
        # the same check, reported PASS by a readable row and FAIL by one whose
        # suite identity is unknown
        store.record_eval_result(
            _verdict_row(
                result_id="evr_pass",
                run_id="mixed",
                suite_id=suite,
                check_id="NSC-C01-RED",
                passed=True,
            )
        )
        store.record_eval_result(
            _verdict_row(
                result_id="evr_red",
                run_id="mixed",
                suite_id="",
                check_id="NSC-C01-RED",
                passed=False,
            )
        )
    finally:
        store._store.close()

    resolved = resolve_gaps(db_path=db_path, run_id="mixed", output_dir=gaps_dir)

    assert resolved == []
    assert "resolved_run" not in json.loads(gap_path.read_text(encoding="utf-8"))
    assert "NSC-C01-RED excluded from resolution" in capsys.readouterr().err


def test_a_fully_described_pass_still_resolves_in_the_same_run(tmp_path: Path) -> None:
    """The exclusion is targeted, not a blanket refusal: a check whose rows were
    all readable resolves normally even when a DIFFERENT check was skipped."""
    db_path, store, suite = _suite_with_case(tmp_path, "NSC-CLEAN")
    try:
        store.record_eval_result(
            _verdict_row(
                result_id="evr_old",
                run_id="old",
                suite_id=suite,
                check_id="NSC-CLEAN",
                passed=False,
            )
        )
    finally:
        store._store.close()

    gaps_dir = tmp_path / "gaps"
    emitted = emit_gaps(
        db_path=db_path,
        run_id="old",
        output_dir=gaps_dir,
        evidence_root=tmp_path / "evidence",
        bundle_path=tmp_path / "bundle-old",
        dry_run=True,
    )
    gap_path = emitted[0]

    from omniagentos.lab.db import LabStore

    store = LabStore(str(db_path))
    try:
        store.record_eval_result(
            _verdict_row(
                result_id="evr_pass",
                run_id="mixed",
                suite_id=suite,
                check_id="NSC-CLEAN",
                passed=True,
            )
        )
        store.record_eval_result(
            _verdict_row(
                result_id="evr_orphan",
                run_id="mixed",
                suite_id="",
                check_id="NSC-OTHER",
                passed=False,
            )
        )
    finally:
        store._store.close()

    resolved = resolve_gaps(db_path=db_path, run_id="mixed", output_dir=gaps_dir)

    assert resolved == [gap_path]
    assert json.loads(gap_path.read_text(encoding="utf-8"))["resolved_run"] == "mixed"
