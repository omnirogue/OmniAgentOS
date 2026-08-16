"""Guards for the feature-health -> loop-queue finding adapter.

Modeled on ``tests/scripts/test_nscert_file_gap_findings.py``. Every test
builds its own scratch queue AND its own scratch feature-health ledger under
``tmp_path`` (via the ``FH_VAR_DIR`` override ``fh.py`` itself already
supports) — the real queue at ``var/loopqueue`` and the real ledger at
``var/feature-health/`` are never touched.

It also carries the guards for the two modules the filer is one third of:
``fh.py``'s attribution/incompleteness contracts and ``system_ledger.py``'s
rendering of the same records. They live here rather than in files of their
own because the three only make sense as one chain — a fact fh.py records, the
filer refuses to file, and the system ledger has to render honestly.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

import scripts.feature_health.file_issue_findings as filer
from scripts.northstar_cert.canonical import content_identity

FEATURE = "goals"
TIER = "tier1"
# The real matrix entry for (goals, tier1) — configs/feature-health.yaml.
# Hardcoded rather than loaded, so these tests fail loudly (not silently) if
# that file's shape ever changes under them.
GOALS_TIER1_PATHS = [
    "tests/goals",
    "tests/feature_health/tier1/test_goals_collectors.py",
]

SYNTHETIC_MATRIX: dict[str, dict[str, object]] = {
    FEATURE: {
        "tier1": GOALS_TIER1_PATHS,
        "tier2": [],
        "tier3": ["tests/feature_health/tier3/test_ui_api_paths.py::TestGoalsPaths"],
        "playwright": False,
    }
}


# --------------------------------------------------------------------------- fixtures


def _queue(tmp_path: Path) -> Path:
    queue = tmp_path / "loopqueue"
    for name in ("findings", "rejected", "parked"):
        (queue / name).mkdir(parents=True, exist_ok=True)
    return queue


def _clean_snapshot(sha: str = "a" * 40, digest: str = "clean-digest") -> dict[str, Any]:
    return {
        "sha": sha,
        "tracked_dirty": [],
        "untracked_executable": [],
        "untracked_other": [],
        "digest": digest,
        "status_error": False,
    }


def _eligible_provenance(sha: str = "a" * 40) -> dict[str, Any]:
    """A start/end snapshot pair that binds cleanly to ``sha`` -- the shape
    ``fh.py append`` writes for an ordinary, safe run."""
    snap = _clean_snapshot(sha=sha)
    return {"start": snap, "end": snap, "eligible": True, "ineligible_reason": None}


def _record(**overrides: Any) -> dict[str, Any]:
    """A feature-health.v1 ledger record in exactly the shape fh.py appends."""
    base: dict[str, Any] = {
        "schema": "feature-health.v1",
        "ts": "2026-08-11T06:00:00+00:00",
        "git_sha": "a" * 40,
        "git_dirty": 0,
        "provenance": _eligible_provenance(),
        "tier": TIER,
        "feature": FEATURE,
        "env": "isolated",
        "status": "ok",
        "passed": 3,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "expected_failures": 0,
        "duration_s": 1.23,
        "cost_usd": None,
        "cost_quality": None,
        "report_path": "var/feature-health/junit-tier1-20260811T060000Z.xml",
        "failures": [],
        "runner": "manual",
        "host_load": 0.5,
    }
    base.update(overrides)
    return base


def _var_dir(tmp_path: Path) -> Path:
    return tmp_path / "fh-var"


def _write_ledger(
    var_dir: Path, *records: dict[str, Any], shard: str = "ledger-202608.jsonl"
) -> Path:
    var_dir.mkdir(parents=True, exist_ok=True)
    path = var_dir / shard
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def _arm(monkeypatch: pytest.MonkeyPatch, var_dir: Path) -> None:
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    monkeypatch.setenv(filer.LIVE_ENV_FLAG, "1")


def _wire_only(monkeypatch: pytest.MonkeyPatch, var_dir: Path) -> None:
    """Point fh.py at the scratch ledger without arming live filing."""
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    monkeypatch.delenv(filer.LIVE_ENV_FLAG, raising=False)


def _ledger_lines(queue: Path) -> list[dict[str, Any]]:
    ledger = queue / "ledger.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line]


def _unexpected_failure_record(**overrides: Any) -> dict[str, Any]:
    return _record(
        failed=1,
        failures=[
            {
                "nodeid": "tests/goals/test_x.py::test_thing",
                "message": "AssertionError: boom",
                "expected": False,
                "known_issue_id": None,
            }
        ],
        **overrides,
    )


def _known_issue_only_record(**overrides: Any) -> dict[str, Any]:
    return _record(
        failed=1,
        expected_failures=1,
        failures=[
            {
                "nodeid": "tests/goals/test_x.py::test_thing",
                "message": "AssertionError: known",
                "expected": True,
                "known_issue_id": "FH-1",
            }
        ],
        **overrides,
    )


def _error_record(**overrides: Any) -> dict[str, Any]:
    return _record(
        status="error",
        did_not_run=True,
        report_path="var/feature-health/junit-tier1-20260811T060000Z.xml",
        **overrides,
    )


def _abort_record(**overrides: Any) -> dict[str, Any]:
    base = {
        "feature": fh_lane_feature(),
        "tier": "tier2",
        "status": "error",
        "did_not_run": True,
        "aborted": True,
        "abort_reason": "load-guard",
        "abort_detail": "load 12.0 > 8.0",
        "report_path": None,
    }
    base.update(overrides)
    return _record(**base)


def fh_lane_feature() -> str:
    return filer.fh.LANE_FEATURE


# --------------------------------------------------------------------------- identity


NODEID = "tests/goals/test_x.py::test_thing"


def _one(
    record: dict[str, Any],
    tmp_path: Path,
    *,
    feature: str = FEATURE,
    tier: str = TIER,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """The single envelope this record files, or None when it files nothing."""
    shard = tmp_path / "ledger-202608.jsonl"
    envelopes = filer.build_findings(feature, tier, record, shard, SYNTHETIC_MATRIX, **kwargs)
    assert len(envelopes) <= 1, envelopes
    return envelopes[0] if envelopes else None


def test_finding_id_is_the_sha256_of_the_payload_alone(tmp_path: Path) -> None:
    envelope = _one(_unexpected_failure_record(), tmp_path)
    assert envelope is not None
    assert envelope["id"] == content_identity(envelope["payload"])
    assert envelope["id"].startswith("sha256:")
    assert len(envelope["id"]) == len("sha256:") + 64


def test_identity_ignores_everything_that_changes_between_runs(tmp_path: Path) -> None:
    """git_sha, ts, report_path, host_load and the pytest MESSAGE all differ;
    feature/tier/nodeid do not -- the id must not move."""
    first = _one(
        _unexpected_failure_record(),
        tmp_path,
        created_at="2026-08-09T06:10:00Z",
        base_sha="a" * 40,
    )
    moved = _unexpected_failure_record(
        ts="2026-08-26T06:10:00+00:00",
        git_sha="b" * 40,
        host_load=9.9,
        report_path="var/feature-health/junit-tier1-20260826T000000Z.xml",
    )
    moved["failures"][0]["message"] = "AssertionError: assert 41 == 42 (took 3.7s)"
    second = _one(moved, tmp_path, created_at="2026-08-26T06:10:00Z", base_sha="b" * 40)
    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert first["payload"] == second["payload"]
    # ...and the run-specific data is still carried, just not hashed.
    assert second["feature_health"]["git_sha"] == "b" * 40
    assert second["feature_health"]["message"].startswith("AssertionError: assert 41 == 42")
    assert second["base_sha"] == "b" * 40


def test_a_different_failing_test_is_a_different_finding(tmp_path: Path) -> None:
    other = _record(
        failed=1,
        failures=[
            {
                "nodeid": "tests/goals/test_y.py::test_other",
                "message": "boom",
                "expected": False,
                "known_issue_id": None,
            }
        ],
    )
    a = _one(_unexpected_failure_record(), tmp_path)
    b = _one(other, tmp_path)
    assert a is not None and b is not None
    assert a["id"] != b["id"]


def test_one_envelope_per_failing_nodeid(tmp_path: Path) -> None:
    """Identity is per nodeid, never the companion set: a record with two
    unexpected failures is TWO findings, each naming exactly one test."""
    record = _record(
        failed=2,
        failures=[
            {"nodeid": NODEID, "message": "boom", "expected": False, "known_issue_id": None},
            {
                "nodeid": "tests/goals/test_x.py::test_second",
                "message": "boom too",
                "expected": False,
                "known_issue_id": None,
            },
        ],
    )
    shard = tmp_path / "ledger-202608.jsonl"
    envelopes = filer.build_findings(FEATURE, TIER, record, shard, SYNTHETIC_MATRIX)
    assert len(envelopes) == 2
    assert [e["payload"]["nodeid"] for e in envelopes] == [  # nodeid-sorted, run-stable
        "tests/goals/test_x.py::test_second",
        NODEID,
    ]
    assert all(e["payload"]["failing_tests"] == [e["payload"]["nodeid"]] for e in envelopes)
    # ...and NODEID's identity is the SAME id it gets on its own, so a grown
    # failure set cannot re-file a failure that is already standing.
    alone = _one(_unexpected_failure_record(), tmp_path)
    assert alone is not None and alone["id"] == envelopes[1]["id"]


def test_priority_is_two_for_an_unexpected_failure(tmp_path: Path) -> None:
    envelope = _one(_unexpected_failure_record(), tmp_path)
    assert envelope is not None
    assert envelope["priority"] == 2


def test_title_names_feature_tier_and_the_nodeid_tail(tmp_path: Path) -> None:
    envelope = _one(_unexpected_failure_record(), tmp_path)
    assert envelope is not None
    assert envelope["title"].startswith("feature-health: goals/tier1")
    assert "test_x.py::test_thing" in envelope["title"]
    assert len(envelope["title"]) <= 200


def test_paths_come_from_the_matrix(tmp_path: Path) -> None:
    envelope = _one(_unexpected_failure_record(), tmp_path)
    assert envelope is not None
    assert envelope["paths"] == GOALS_TIER1_PATHS


def test_a_known_issue_only_record_yields_no_envelope(tmp_path: Path) -> None:
    assert _one(_known_issue_only_record(), tmp_path) is None


def test_a_clean_record_yields_no_envelope(tmp_path: Path) -> None:
    assert _one(_record(), tmp_path) is None


def test_mixed_expected_and_unexpected_failures_names_only_the_unexpected(tmp_path: Path) -> None:
    record = _record(
        failed=2,
        expected_failures=1,
        failures=[
            {
                "nodeid": "tests/goals/test_a.py::test_known",
                "message": "known",
                "expected": True,
                "known_issue_id": "FH-2",
            },
            {
                "nodeid": "tests/goals/test_b.py::test_new",
                "message": "new break",
                "expected": False,
                "known_issue_id": None,
            },
        ],
    )
    envelope = _one(record, tmp_path)
    assert envelope is not None
    assert envelope["payload"]["failing_tests"] == ["tests/goals/test_b.py::test_new"]
    assert "test_known" not in envelope["payload"]["symptom"]
    assert "test_new" in envelope["payload"]["symptom"]


# --------------------------------------------------------------------------- instrument errors


@pytest.mark.parametrize("builder", [_error_record, _abort_record])
def test_an_unmeasurable_record_is_never_a_finding(tmp_path: Path, builder) -> None:
    """status:error / aborted is an INSTRUMENT reading, not product work."""
    record = builder()
    shard = tmp_path / "ledger-202608.jsonl"
    assert filer.is_instrument_error(record) is True
    assert filer.build_findings(FEATURE, TIER, record, shard, SYNTHETIC_MATRIX) == []


def test_instrument_error_describes_the_reason_without_filing(tmp_path: Path) -> None:
    shard = tmp_path / "ledger-202608.jsonl"
    entry = filer.instrument_error(fh_lane_feature(), "tier2", _abort_record(), shard)
    assert entry["reason"] == "aborted"
    assert entry["abort_reason"] == "load-guard"
    assert "load-guard" in entry["message"]
    assert "instrument error" in entry["message"]


def test_an_error_record_is_reported_but_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _error_record())
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.filed == [] and result.would_file == []
    assert list((queue / "findings").glob("*.json")) == []
    assert _ledger_lines(queue) == []
    assert len(result.instrument_errors) == 1
    entry = result.instrument_errors[0]
    assert (entry["feature"], entry["tier"], entry["reason"]) == (FEATURE, TIER, "did_not_run")


def test_cli_names_instrument_errors_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _error_record())
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    code = filer.main(["--queue", str(queue), "--live"])

    captured = capsys.readouterr()
    assert code == 0
    assert "instrument error" in captured.err
    report = json.loads(captured.out)
    assert report["filed"] == []
    assert len(report["instrument_errors"]) == 1


# --------------------------------------------------------------------------- provenance binding


def test_filed_base_sha_is_the_records_own_git_sha_not_the_filers_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug: base_sha used to come from `git -C _REPO_ROOT rev-parse HEAD` at
    FILING time -- i.e. this test process's own checkout, whatever it happens to
    be, completely independent of what the record measured. A record measured
    at a DIFFERENT sha than this checkout's current HEAD must still file with
    ITS OWN sha, never the filer's."""
    measured_sha = "c" * 40
    var_dir = _var_dir(tmp_path)
    _write_ledger(
        var_dir,
        _unexpected_failure_record(git_sha=measured_sha, provenance=_eligible_provenance(sha=measured_sha)),
    )
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.filed, result.instrument_errors
    envelope = json.loads((queue / "findings" / f"{result.filed[0].replace(':', '_')}.json").read_text())
    assert envelope["base_sha"] == measured_sha
    # ...and never a substitute for whatever HEAD this filer process happens to
    # be running from — the bug this closes computed base_sha from
    # `git -C _REPO_ROOT rev-parse HEAD` at filing time, which is this
    # checkout's real, live HEAD and can never equal the synthetic "c"*40.
    import subprocess

    live_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=filer._REPO_ROOT, capture_output=True, text=True
    ).stdout.strip()
    assert envelope["base_sha"] != live_head


@pytest.mark.parametrize(
    "mutate,expected_reason",
    [
        (lambda r: r.update(provenance=None), "missing_provenance"),
        (
            lambda r: r["provenance"].update(
                end={**_clean_snapshot(), "tracked_dirty": ["omniagentos/dag/exec.py"]},
                eligible=False,
                ineligible_reason="tracked_dirty",
            ),
            "tracked_dirty",
        ),
        (
            lambda r: r["provenance"].update(
                end={**_clean_snapshot(), "untracked_executable": ["scripts/new_helper.py"]},
                eligible=False,
                ineligible_reason="untracked_executable_input",
            ),
            "untracked_executable_input",
        ),
        (
            lambda r: r["provenance"].update(
                end=_clean_snapshot(sha="d" * 40), eligible=False, ineligible_reason="sha_mutated_during_run"
            ),
            "sha_mutated_during_run",
        ),
        (
            lambda r: r["provenance"].update(start=None, eligible=False, ineligible_reason="missing_start_snapshot"),
            "missing_start_snapshot",
        ),
        (lambda r: r.update(git_sha="not-a-sha"), "invalid_git_sha"),
    ],
)
def test_unsafe_provenance_files_nothing_and_is_a_named_instrument_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutate, expected_reason
) -> None:
    """Every unsafe class -- missing provenance (legacy records), tracked dirt,
    executable-untracked input, a SHA that moved mid-run, a missing start
    snapshot, and an unparseable SHA -- must produce ZERO product findings and
    a NAMED instrument error. Never a silent, favourable pass."""
    record = _unexpected_failure_record()
    mutate(record)
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, record)
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.filed == [] and result.would_file == []
    assert list((queue / "findings").glob("*.json")) == []
    assert len(result.instrument_errors) == 1
    entry = result.instrument_errors[0]
    assert entry["reason"] == f"unsafe_provenance:{expected_reason}"
    assert "instrument error, not filed" in entry["message"].lower()


def test_non_executable_untracked_artifact_does_not_suppress_filing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An untracked artifact OUTSIDE the executable roots (var/, coverage output,
    .venv scratch...) is recorded in provenance but must not, by itself, make an
    otherwise-clean record ineligible -- only tracked dirt and
    executable-untracked input are unsafe."""
    record = _unexpected_failure_record()
    record["provenance"]["end"] = {
        **_clean_snapshot(),
        "untracked_other": ["var/feature-health/reports/scratch.xml"],
    }
    # start/end must still agree for eligibility; recompute eligible=True here
    # the same way fh.py would (only tracked_dirty/untracked_executable gate it).
    record["provenance"]["eligible"] = True
    record["provenance"]["ineligible_reason"] = None
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, record)
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.filed != []
    assert result.instrument_errors == []


def test_record_provenance_defect_classifies_every_unsafe_shape() -> None:
    assert filer.record_provenance_defect(_record(provenance="not-a-dict")) == "missing_provenance"
    assert filer.record_provenance_defect(_record()) is None  # the eligible default
    unsafe = _record()
    unsafe["provenance"]["eligible"] = False
    unsafe["provenance"]["ineligible_reason"] = "tracked_dirty"
    assert filer.record_provenance_defect(unsafe) == "tracked_dirty"
    mismatched = _record()
    mismatched["provenance"]["end"]["sha"] = "b" * 40
    assert filer.record_provenance_defect(mismatched) == "provenance_sha_mismatch"


def test_fh_provenance_snapshot_classifies_tracked_vs_untracked_executable_input() -> None:
    """fh.py's own classifier, unit-tested directly (not through a synthetic
    record): a tracked-modified path and an untracked path under an executable
    root are both dirt; an untracked path elsewhere is not."""
    import scripts.feature_health.fh as fh

    lines = [
        " M omniagentos/dag/exec.py",
        "?? scripts/new_helper.py",
        "?? var/feature-health/reports/scratch.xml",
        "?? tests/goals/test_new.py",
    ]
    classified = fh.classify_git_status(lines)
    assert classified == {
        "tracked_dirty": ["omniagentos/dag/exec.py"],
        "untracked_executable": ["scripts/new_helper.py", "tests/goals/test_new.py"],
        "untracked_other": ["var/feature-health/reports/scratch.xml"],
    }
    assert fh.classify_git_status(None) is None


def test_fh_provenance_for_record_requires_a_start_snapshot() -> None:
    import scripts.feature_health.fh as fh

    result = fh._provenance_for_record(None)
    assert result["eligible"] is False
    assert result["ineligible_reason"] == "missing_start_snapshot"


def test_fh_provenance_for_record_detects_a_mid_run_mutation(monkeypatch: pytest.MonkeyPatch) -> None:
    """The end snapshot is taken live at append time; monkeypatch it to a fixed
    value so the mismatch-detection branch is exercised deterministically."""
    import scripts.feature_health.fh as fh

    start = {
        "sha": "a" * 40,
        "tracked_dirty": [],
        "untracked_executable": [],
        "untracked_other": [],
        "digest": "d1",
        "status_error": False,
    }
    end = {**start, "digest": "d2"}  # same sha, tree moved during the run
    monkeypatch.setattr(fh, "provenance_snapshot", lambda: end)
    result = fh._provenance_for_record(start)
    assert result["eligible"] is False
    assert result["ineligible_reason"] == "tree_mutated_during_run"


def test_fh_provenance_for_record_detects_a_sha_change_mid_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.feature_health.fh as fh

    start = {
        "sha": "a" * 40,
        "tracked_dirty": [],
        "untracked_executable": [],
        "untracked_other": [],
        "digest": "d1",
        "status_error": False,
    }
    end = {**start, "sha": "b" * 40}
    monkeypatch.setattr(fh, "provenance_snapshot", lambda: end)
    result = fh._provenance_for_record(start)
    assert result["eligible"] is False
    assert result["ineligible_reason"] == "sha_mutated_during_run"


def test_fh_provenance_for_record_eligible_when_clean_and_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.feature_health.fh as fh

    snap = {
        "sha": "a" * 40,
        "tracked_dirty": [],
        "untracked_executable": [],
        "untracked_other": ["var/scratch.xml"],
        "digest": "clean",
        "status_error": False,
    }
    monkeypatch.setattr(fh, "provenance_snapshot", lambda: snap)
    result = fh._provenance_for_record(snap)
    assert result == {"start": snap, "end": snap, "eligible": True, "ineligible_reason": None}


def test_fh_snapshot_cli_writes_a_readable_provenance_file(tmp_path: Path) -> None:
    import scripts.feature_health.fh as fh

    out = tmp_path / "snap.json"
    rc = fh.main(["snapshot", "--out", str(out)])
    assert rc == 0
    data = json.loads(out.read_text())
    assert "sha" in data and "digest" in data
    assert isinstance(data.get("tracked_dirty"), (list, type(None)))


def test_fh_append_without_start_snapshot_is_ineligible_for_filing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runner-wiring guard: `fh.py append` invoked without --start-snapshot (the
    shape run.sh would produce if the wiring were ever dropped) must mark the
    record ineligible, never silently eligible."""
    import scripts.feature_health.fh as fh

    var_dir = tmp_path / "fh-var"
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    report = tmp_path / "report.xml"
    report.write_text(
        '<?xml version="1.0"?><testsuite><testcase classname="x" name="y" '
        'file="tests/goals/test_x.py" time="0.1"/></testsuite>'
    )
    rc = fh.main(
        [
            "append",
            "--tier",
            "tier1",
            "--report",
            str(report),
            "--env",
            "isolated",
            "--runner",
            "manual",
        ]
    )
    assert rc == 0
    shard = next(var_dir.glob("ledger-*.jsonl"))
    lines = [json.loads(line) for line in shard.read_text().splitlines() if line]
    assert lines
    for record in lines:
        assert record["provenance"]["eligible"] is False
        assert record["provenance"]["ineligible_reason"] == "missing_start_snapshot"


def test_fh_append_with_start_snapshot_is_eligible_when_the_tree_is_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.feature_health.fh as fh

    monkeypatch.setenv("FH_VAR_DIR", str(tmp_path / "fh-var"))
    snap = tmp_path / "start.json"
    monkeypatch.chdir(tmp_path)
    rc = fh.main(["snapshot", "--out", str(snap)])
    assert rc == 0
    report = tmp_path / "report.xml"
    report.write_text(
        '<?xml version="1.0"?><testsuite><testcase classname="x" name="y" '
        'file="tests/goals/test_x.py" time="0.1"/></testsuite>'
    )
    rc = fh.main(
        [
            "append",
            "--tier",
            "tier1",
            "--report",
            str(report),
            "--env",
            "isolated",
            "--runner",
            "manual",
            "--start-snapshot",
            str(snap),
        ]
    )
    assert rc == 0
    shard = next((tmp_path / "fh-var").glob("ledger-*.jsonl"))
    lines = [json.loads(line) for line in shard.read_text().splitlines() if line]
    assert lines
    for record in lines:
        assert "provenance" in record
        assert record["provenance"]["start"] is not None


# --------------------------------------------------------------------------- filing (task a/b/c/f)


def test_live_filing_writes_a_valid_envelope_and_one_ledger_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.live is True and result.requested_live is True
    assert result.filed and result.would_file == []
    written = list((queue / "findings").glob("*.json"))
    assert len(written) == 1
    envelope = json.loads(written[0].read_text(encoding="utf-8"))
    assert written[0].name == envelope["id"].replace(":", "_") + ".json"
    assert oct(written[0].stat().st_mode)[-3:] == "644"
    assert envelope["kind"] == "finding"
    assert envelope["payload"]["symptom"]
    assert envelope["payload"]["failing_tests"] == ["tests/goals/test_x.py::test_thing"]
    assert envelope["priority"] == 2
    assert envelope["producer"] == {
        "role": "external",
        "actor": "feature-health-filer",
        "model": "mechanical",
        "lineage": "none",
    }
    assert envelope["paths"] == GOALS_TIER1_PATHS
    assert 1 <= len(envelope["title"]) <= 200

    events = _ledger_lines(queue)
    assert len(events) == 1
    event = events[0]
    assert event["event"] == "found"
    assert event["role"] == "external"
    assert event["actor"] == "feature-health-filer"
    assert event["id"] == envelope["id"]
    assert event["ts"].endswith("Z")
    assert event["detail"]["feature"] == FEATURE
    assert event["detail"]["tier"] == TIER


def test_a_known_issue_only_record_files_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _known_issue_only_record())
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.filed == []
    assert result.would_file == []
    assert list((queue / "findings").glob("*.json")) == []
    assert _ledger_lines(queue) == []


def test_refiling_an_identical_record_is_deduped_and_writes_no_second_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    first = filer.file_issue_findings(queue=queue, live=True)
    artifact = queue / "findings" / (first.filed[0].replace(":", "_") + ".json")
    before = artifact.read_bytes()

    second = filer.file_issue_findings(queue=queue, live=True)

    assert second.filed == []
    assert second.skipped_existing == 1
    assert artifact.read_bytes() == before
    assert len(_ledger_lines(queue)) == 1


@pytest.mark.parametrize("terminal", ["parked", "rejected"])
def test_a_terminal_marker_skips_the_finding_silently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal: str
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)
    shard = sorted(var_dir.glob("ledger-*.jsonl"))[0]
    identifier = filer.build_finding(
        FEATURE, TIER, _unexpected_failure_record(), shard, filer._load_matrix(), NODEID
    )["id"]
    marker = queue / terminal / (identifier.replace(":", "_") + ".json")
    marker.write_text(json.dumps({"id": identifier, "reason": "human decision"}), encoding="utf-8")

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.filed == []
    assert result.skipped_existing == 1
    assert list((queue / "findings").glob("*.json")) == []
    assert _ledger_lines(queue) == []


# --------------------------------------------------------------------------- two-key arming (task d)


def test_live_without_env_key_is_a_refusal_not_a_quiet_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller that ASKED to write live and is not armed must be told so, not
    handed a favourable-looking dry run it cannot distinguish by exit code."""
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    _wire_only(monkeypatch, var_dir)  # FH_VAR_DIR set, FH_ISSUES_LIVE unset
    queue = _queue(tmp_path)

    with pytest.raises(filer.IssueFilingNotArmed, match=filer.LIVE_ENV_FLAG):
        filer.file_issue_findings(queue=queue, live=True)

    assert issubclass(filer.IssueFilingNotArmed, filer.IssueFilingError)
    assert list((queue / "findings").glob("*.json")) == []
    assert not (queue / "ledger.jsonl").exists()
    assert not (var_dir / "findings-dryrun").exists()


def test_env_key_without_live_flag_is_still_a_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    monkeypatch.setenv(filer.LIVE_ENV_FLAG, "1")  # armed key present...
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=False)  # ...but --live never requested

    assert result.requested_live is False
    assert result.live is False
    assert result.filed == []
    assert result.would_file
    assert list((queue / "findings").glob("*.json")) == []


def test_cli_dashdash_live_without_env_key_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    _wire_only(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    code = filer.main(["--queue", str(queue), "--live"])

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["requested_live"] is True
    assert report["live"] is False
    assert filer.LIVE_ENV_FLAG in report["error"]
    assert list((queue / "findings").glob("*.json")) == []


def test_the_write_boundary_itself_needs_both_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`live=True` from Python, with no environment key, must not silently
    reach the real queue -- the CLI check is not the only boundary."""
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    _wire_only(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    with pytest.raises(filer.IssueFilingNotArmed):
        filer.file_issue_findings(queue=queue, live=True)

    assert list((queue / "findings").glob("*.json")) == []
    assert not (queue / "ledger.jsonl").exists()


@pytest.mark.parametrize("value", ["0", "", "true", "yes"])
def test_only_an_exact_1_arms_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    monkeypatch.setenv(filer.LIVE_ENV_FLAG, value)
    queue = _queue(tmp_path)

    with pytest.raises(filer.IssueFilingNotArmed):
        filer.file_issue_findings(queue=queue, live=True)

    assert list((queue / "findings").glob("*.json")) == []


def test_cli_dry_run_is_the_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    _arm(monkeypatch, var_dir)  # armed, but no --live: still a dry run
    queue = _queue(tmp_path)

    code = filer.main(["--queue", str(queue)])

    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["requested_live"] is False
    assert report["live"] is False
    assert len(report["would_file"]) == 1
    assert report["filed"] == []
    assert list((queue / "findings").glob("*.json")) == []


# --------------------------------------------------------------------------- fail-closed (task e)


def test_missing_ledger_directory_is_exit_2_with_no_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    var_dir = tmp_path / "absent-fh-var"
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    monkeypatch.setenv(filer.LIVE_ENV_FLAG, "1")
    queue = _queue(tmp_path)

    code = filer.main(["--queue", str(queue), "--live"])

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "ledger" in report["error"]
    assert list((queue / "findings").glob("*.json")) == []
    assert not (queue / "ledger.jsonl").exists()


def test_missing_ledger_directory_raises_in_process_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = tmp_path / "absent-fh-var"
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    queue = _queue(tmp_path)
    with pytest.raises(filer.IssueFilingError, match="ledger"):
        filer.file_issue_findings(queue=queue, live=False)


def test_a_ledger_dir_with_no_shards_is_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    var_dir.mkdir(parents=True)
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    queue = _queue(tmp_path)
    with pytest.raises(filer.IssueFilingError, match="no feature-health ledger shards"):
        filer.file_issue_findings(queue=queue, live=False)


def test_missing_queue_is_refused_in_both_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    for value, live in ((None, False), ("1", True)):
        if value is not None:
            monkeypatch.setenv(filer.LIVE_ENV_FLAG, value)
        with pytest.raises(filer.IssueFilingError, match="queue root is not a directory"):
            filer.file_issue_findings(queue=tmp_path / "absent-queue", live=live)


def test_a_queue_without_parked_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    queue = _queue(tmp_path)
    (queue / "parked").rmdir()
    with pytest.raises(filer.IssueFilingError, match="missing parked/"):
        filer.file_issue_findings(queue=queue, live=False)


def test_an_unwritable_findings_dir_is_refused_rather_than_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    queue = _queue(tmp_path)
    findings = queue / "findings"
    findings.chmod(0o500)
    try:
        with pytest.raises(filer.IssueFilingError, match="findings/ is not writable"):
            filer.file_issue_findings(queue=queue, live=False)
    finally:
        findings.chmod(0o755)


def test_cli_reports_exit_2_for_an_unusable_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    code = filer.main(["--queue", str(tmp_path / "nope")])
    assert code == 2
    assert "queue root is not a directory" in json.loads(capsys.readouterr().out)["error"]


# --------------------------------------------------------------------------- newest-per-cell aggregation


def test_only_the_newest_record_per_feature_tier_is_considered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OLDER record that failed must not file once a NEWER record for the
    same (feature, tier) is clean -- matches fh.py's own `_latest_per_cell`."""
    var_dir = _var_dir(tmp_path)
    _write_ledger(
        var_dir,
        _unexpected_failure_record(ts="2026-08-10T06:00:00+00:00"),
        _record(ts="2026-08-11T06:00:00+00:00"),  # newer, clean
    )
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.filed == []
    assert result.cells_read == 1


def test_a_clean_sibling_stream_does_not_mask_a_standing_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """api_ui/tier3 is several independent sub-runs (isolated suite, live
    probes, Playwright). A clean LATER append from one stream must not erase a
    standing failure recorded by another -- fh._latest_per_run's own key."""
    var_dir = _var_dir(tmp_path)
    _write_ledger(
        var_dir,
        _unexpected_failure_record(
            ts="2026-08-11T06:00:00+00:00",
            report_path="var/feature-health/reports/20260811T060000Z-tier3-live.xml",
        ),
        _record(
            ts="2026-08-11T06:05:00+00:00",  # newer, clean, DIFFERENT stream
            report_path="var/feature-health/reports/20260811T060500Z-playwright.json",
        ),
    )
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.cells_read == 2
    assert len(result.filed) == 1


def test_a_grown_failure_set_files_only_the_new_nodeid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A -> A+B must file B and leave A's single standing finding alone; the
    old set-shaped identity minted a second artifact that also contained A."""
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record(ts="2026-08-11T06:00:00+00:00"))
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    first = filer.file_issue_findings(queue=queue, live=True)
    _write_ledger(
        var_dir,
        _record(
            ts="2026-08-11T06:10:00+00:00",
            failed=2,
            failures=[
                {"nodeid": NODEID, "message": "boom", "expected": False, "known_issue_id": None},
                {
                    "nodeid": "tests/goals/test_x.py::test_second",
                    "message": "boom too",
                    "expected": False,
                    "known_issue_id": None,
                },
            ],
        ),
    )
    second = filer.file_issue_findings(queue=queue, live=True)

    assert len(first.filed) == 1
    assert len(second.filed) == 1 and second.skipped_existing == 1
    envelopes = [
        json.loads(path.read_text(encoding="utf-8")) for path in (queue / "findings").glob("*.json")
    ]
    assert len(envelopes) == 2
    assert sum(NODEID in e["payload"]["failing_tests"] for e in envelopes) == 1


def test_a_published_artifact_with_no_found_event_is_repaired_on_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifact is written before the ledger append; if the append dies the
    finding is an ORPHAN invisible to every ledger-derived view. The next pass
    must append the missing event, not skip it forever."""
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("injected ledger append failure")

    monkeypatch.setattr(filer, "_append_ledger", _boom)
    with pytest.raises(OSError):
        filer.file_issue_findings(queue=queue, live=True)
    assert len(list((queue / "findings").glob("*.json"))) == 1
    assert _ledger_lines(queue) == []

    monkeypatch.undo()
    _arm(monkeypatch, var_dir)
    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.filed == []
    assert result.skipped_existing == 1
    assert result.ledger_repaired == 1
    events = _ledger_lines(queue)
    assert len(events) == 1
    assert events[0]["event"] == "found" and events[0]["detail"]["repaired"] is True

    # ...and a THIRD pass repairs nothing: the event is there now.
    third = filer.file_issue_findings(queue=queue, live=True)
    assert third.ledger_repaired == 0
    assert len(_ledger_lines(queue)) == 1


def test_a_dry_run_never_repairs_the_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)
    envelope = filer.build_finding(
        FEATURE,
        TIER,
        _unexpected_failure_record(),
        var_dir / "ledger-202608.jsonl",
        filer._load_matrix(),
        NODEID,
    )
    (queue / "findings" / (envelope["id"].replace(":", "_") + ".json")).write_text(
        json.dumps(envelope), encoding="utf-8"
    )

    result = filer.file_issue_findings(queue=queue, live=False)

    assert result.skipped_existing == 1
    assert result.ledger_repaired == 0
    assert not (queue / "ledger.jsonl").exists()


# --------------------------------------------------------------------------- ledger health


def test_a_shard_that_parses_to_nothing_is_refused_not_reported_healthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty/corrupt shard set returning cells_read=0 and exit 0 is
    indistinguishable, to an exit-code caller, from a clean lane."""
    var_dir = _var_dir(tmp_path)
    var_dir.mkdir(parents=True)
    (var_dir / "ledger-202608.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    queue = _queue(tmp_path)

    with pytest.raises(filer.IssueFilingError, match="no parseable feature-health ledger records"):
        filer.file_issue_findings(queue=queue, live=False)


def test_corrupt_lines_are_counted_and_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    with (var_dir / "ledger-202608.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"schema": "feature-health.v1", "ts": tor\n')  # torn line
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.records_read == 1
    assert result.shards_read == 1
    assert any("unparseable" in problem for problem in result.shard_errors)
    assert len(result.filed) == 1  # the readable record still files


def test_a_wholly_corrupt_shard_is_exit_2_with_the_shard_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    var_dir = _var_dir(tmp_path)
    var_dir.mkdir(parents=True)
    (var_dir / "ledger-202608.jsonl").write_text("{not json\n{also not\n", encoding="utf-8")
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    queue = _queue(tmp_path)

    code = filer.main(["--queue", str(queue)])

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "ledger-202608.jsonl" in report["error"]
    assert "2 unparseable line(s)" in report["error"]


# --------------------------------------------------------------------------- real matrix


def _real_matrix_owner(nodeid: str, tier: str) -> str:
    """Exactly fh.cmd_append's attribution for one tier: first prefix match wins."""
    matrix = filer.fh.load_matrix()
    covered = [feature for feature, spec in matrix.items() if spec[tier]]
    order = [(feature, prefix) for feature in covered for prefix in matrix[feature][tier]]
    return next(
        (feature for feature, prefix in order if filer.fh._matches(nodeid, str(prefix))), "other"
    )


@pytest.mark.parametrize(
    ("nodeid", "tier", "feature"),
    [
        # Attribution is PER TIER, so a claim in a different tier does not
        # out-rank api_ui's broad tier1 `tests/api` prefix -- it just records
        # the same testcase twice, in two cells, which files the same broken
        # test as TWO findings (tier is part of the identity).
        ("tests/api/test_engine_routes.py::test_contract", "tier1", "control_plane"),
        ("tests/api/test_engine_routes.py::test_contract", "tier3", "other"),
        # ...and a matrix entry that names a directory the suite does not live
        # in attributes nothing at all.
        (
            "tests/test_risk_tier.py::test_empty_or_unreadable_diff_is_high",
            "tier1",
            "landing_pipeline",
        ),
    ],
)
def test_matrix_attributes_each_carrier_to_exactly_one_cell(
    nodeid: str, tier: str, feature: str
) -> None:
    assert _real_matrix_owner(nodeid, tier) == feature


def test_matrix_carriers_exist_on_disk() -> None:
    repo = Path(filer._REPO_ROOT)
    for relative in ("tests/api/test_engine_routes.py", "tests/test_risk_tier.py"):
        assert (repo / relative).is_file(), f"{relative} is named by the matrix but absent"


def test_incomplete_coverage_files_the_red_it_found_AND_reports_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record whose declared paths were not all on disk measured a SUBSET
    nobody chose -- which an operator must know. But the failures it DID
    observe are real: suppressing them would make a missing matrix path a way
    to make a red cell disappear."""
    var_dir = _var_dir(tmp_path)
    _write_ledger(
        var_dir,
        _unexpected_failure_record(missing_paths=["tests/goals/test_gone.py"]),
    )
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    # the red is filed...
    assert len(result.filed) == 1
    written = list((queue / "findings").glob("*.json"))
    assert len(written) == 1
    envelope = json.loads(written[0].read_text(encoding="utf-8"))
    assert envelope["payload"]["nodeid"] == NODEID
    events = _ledger_lines(queue)
    assert len(events) == 1 and events[0]["event"] == "found"
    # ...and the incompleteness is reported alongside it, not instead of it.
    assert len(result.instrument_errors) == 1
    entry = result.instrument_errors[0]
    assert entry["reason"] == "missing_paths"
    assert entry["missing_paths"] == ["tests/goals/test_gone.py"]
    assert "covered only part of its declared paths" in entry["message"]


def test_incomplete_coverage_predicates_are_distinct() -> None:
    """Only `is_unmeasurable` suppresses filing; missing_paths reports only."""
    incomplete = _unexpected_failure_record(missing_paths=["tests/goals/test_gone.py"])
    shard = Path("ledger-202608.jsonl")
    assert filer.has_incomplete_coverage(incomplete) is True
    assert filer.is_unmeasurable(incomplete) is False
    assert filer.is_instrument_error(incomplete) is True
    assert len(filer.build_findings(FEATURE, TIER, incomplete, shard, SYNTHETIC_MATRIX)) == 1

    # an error-status record with the same missing paths still files NOTHING
    unmeasurable = _error_record(missing_paths=["tests/goals/test_gone.py"])
    assert filer.is_unmeasurable(unmeasurable) is True
    assert filer.build_findings(FEATURE, TIER, unmeasurable, shard, SYNTHETIC_MATRIX) == []


def test_an_error_status_record_with_failures_still_files_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A did-not-run/aborted/incomplete-run record's counts are not evidence,
    even when the partial report happens to carry failure rows."""
    var_dir = _var_dir(tmp_path)
    _write_ledger(
        var_dir,
        _unexpected_failure_record(
            status="error",
            incomplete="missing live module(s): tests/feature_health/tier3/x.py",
        ),
    )
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.filed == []
    assert list((queue / "findings").glob("*.json")) == []
    assert _ledger_lines(queue) == []
    assert len(result.instrument_errors) == 1
    assert "incomplete: missing live module(s)" in result.instrument_errors[0]["message"]


def test_a_record_with_no_counts_and_no_failures_list_is_damage() -> None:
    """It passes every field check and still cannot say a test ran, passed or
    failed -- reading it as a clean cell is the favourable absence."""
    countless = {
        "schema": filer.fh.SCHEMA,
        "feature": FEATURE,
        "tier": TIER,
        "ts": "2026-08-11T10:00:00+00:00",
        "status": "ok",
    }
    assert filer.record_defect(countless) == "no counts and no non-empty failures list"
    # one int count is enough to be gradable...
    assert filer.record_defect({**countless, "passed": 0}) is None
    assert filer.record_defect({**countless, "passed": 0, "failures": []}) is None
    # ...but an EMPTY failures list with no counts cannot tell "nothing failed"
    # from "nothing ran", and the favourable reading of that is the bug.
    assert filer.record_defect({**countless, "failures": []}) is not None
    assert (
        filer.record_defect(
            {**countless, "failures": [{"nodeid": "tests/goals/test_x.py::t", "expected": False}]}
        )
        is None
    )


@pytest.mark.parametrize(
    ("overrides", "defect"),
    [
        # a count that is not a usable measurement is damage, never a fallback
        # onto whichever sibling field happens to look fine
        ({"passed": "3"}, "passed '3' is not a non-negative int"),
        ({"failed": 1.0}, "failed 1.0 is not a non-negative int"),
        ({"errors": None}, "errors None is not a non-negative int"),
        ({"skipped": -1}, "skipped -1 is not a non-negative int"),
        ({"passed": True}, "passed True is not a non-negative int"),
        # ...and a failures field that is not a list of objects is damage too
        ({"failures": "boom"}, "failures str is not a list"),
        ({"failures": {"nodeid": "x"}}, "failures dict is not a list"),
        ({"failures": ["tests/goals/test_x.py::t"]}, "failures contains a non-object entry"),
    ],
)
def test_malformed_counts_and_failures_are_damage(overrides: dict[str, Any], defect: str) -> None:
    assert filer.record_defect(_record(**overrides)) == defect


@pytest.mark.parametrize(
    ("overrides", "defect"),
    [
        ({"feature": ""}, "no feature"),
        ({"tier": "tier9"}, "tier"),
        ({"ts": "not-a-timestamp"}, "unparseable ts"),
        ({"status": "green"}, "status"),
    ],
)
def test_a_schema_tagged_but_unusable_record_is_damage(
    overrides: dict[str, Any], defect: str
) -> None:
    """The schema TAG is not the schema: a line can be valid JSON, claim
    feature-health.v1 and still be semantically empty."""
    assert filer.record_defect(_record()) is None
    assert defect in (filer.record_defect(_record(**overrides)) or "")


def test_a_shard_of_only_invalid_records_is_exit_2_naming_the_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    var_dir = _var_dir(tmp_path)
    var_dir.mkdir(parents=True)
    (var_dir / "ledger-202608.jsonl").write_text(
        json.dumps({"schema": "feature-health.v1"}) + "\n", encoding="utf-8"
    )
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    queue = _queue(tmp_path)

    code = filer.main(["--queue", str(queue)])

    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "invalid feature-health.v1 record(s)" in report["error"]
    assert "no feature" in report["error"]


def test_valid_records_still_file_when_a_sibling_line_is_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _unexpected_failure_record())
    with (var_dir / "ledger-202608.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"schema": "feature-health.v1", "feature": "goals"}) + "\n")
        handle.write(json.dumps({"schema": "some-other.v1", "anything": True}) + "\n")
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    result = filer.file_issue_findings(queue=queue, live=True)

    assert len(result.filed) == 1
    assert result.records_read == 1
    # the invalid one is named; the foreign schema is not damage at all
    assert any("invalid" in problem for problem in result.shard_errors)
    assert not any("some-other" in problem for problem in result.shard_errors)


def test_an_orphan_is_repaired_even_after_its_cell_goes_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair sweep must be driven by what is PUBLISHED, not by what this
    pass would file: a repair keyed on rebuilt envelopes never sees an orphan
    whose failure has since stopped being recorded."""
    var_dir = _var_dir(tmp_path)
    _write_ledger(
        var_dir,
        _unexpected_failure_record(
            ts="2026-08-11T06:00:00+00:00",
            report_path="var/feature-health/reports/20260811T060000Z-tier1.xml",
        ),
    )
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("injected ledger append failure")

    monkeypatch.setattr(filer, "_append_ledger", _boom)
    with pytest.raises(OSError):
        filer.file_issue_findings(queue=queue, live=True)
    monkeypatch.undo()
    _arm(monkeypatch, var_dir)
    assert len(list((queue / "findings").glob("*.json"))) == 1
    assert _ledger_lines(queue) == []

    # the cell goes green: nothing rebuilds that envelope any more
    _write_ledger(
        var_dir,
        _record(
            ts="2026-08-11T07:00:00+00:00",
            report_path="var/feature-health/reports/20260811T070000Z-tier1.xml",
        ),
    )
    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.filed == []
    assert result.ledger_repaired == 1
    events = _ledger_lines(queue)
    assert len(events) == 1 and events[0]["detail"]["repaired"] is True


def test_the_repair_sweep_ignores_artifacts_from_other_producers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    var_dir = _var_dir(tmp_path)
    _write_ledger(var_dir, _record())  # clean: this pass files nothing
    _arm(monkeypatch, var_dir)
    queue = _queue(tmp_path)
    foreign = {
        "id": "sha256:" + "f" * 64,
        "kind": "finding",
        "title": "someone else's finding",
        "producer": {"role": "external", "actor": "northstar-cert-filer"},
        "payload": {"symptom": "not ours"},
    }
    (queue / "findings" / f"sha256_{'f' * 64}.json").write_text(
        json.dumps(foreign), encoding="utf-8"
    )

    result = filer.file_issue_findings(queue=queue, live=True)

    assert result.ledger_repaired == 0
    assert _ledger_lines(queue) == []


# --------------------------------------------------------------------------- fh.py contracts

_ENGINE_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite tests="1" failures="1">
  <testcase classname="tests.api.test_engine_routes" name="test_contract"
            file="tests/api/test_engine_routes.py" time="0.01">
    <failure message="boom">boom</failure>
  </testcase>
</testsuite>
"""


def _append_engine_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, feature: str | None
) -> list[dict[str, Any]]:
    var_dir = tmp_path / ("scoped" if feature else "full")
    report = tmp_path / f"{'scoped' if feature else 'full'}-report.xml"
    report.write_text(_ENGINE_JUNIT, encoding="utf-8")
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    argv = ["append", "--tier", "tier1", "--report", str(report)]
    if feature:
        argv += ["--feature", feature]
    assert filer.fh.main(argv) == 0
    rows: list[dict[str, Any]] = []
    for shard in sorted(var_dir.glob("ledger-*.jsonl")):
        rows.extend(
            json.loads(line) for line in shard.read_text(encoding="utf-8").splitlines() if line
        )
    return rows


_BOARD_RED = """<?xml version="1.0" encoding="utf-8"?>
<testsuite tests="1" failures="1">
  <testcase classname="tests.feature_health.tier3.test_board_claim_http"
            file="tests/feature_health/tier3/test_board_claim_http.py" name="test_claim" time="0.01">
    <failure message="boom">boom</failure>
  </testcase>
</testsuite>
"""
_BOARD_PARTIAL_GREEN = """<?xml version="1.0" encoding="utf-8"?>
<testsuite tests="1" failures="0">
  <testcase classname="tests.feature_health.tier3.test_ui_api_paths.TestBoardPaths"
            file="tests/feature_health/tier3/test_ui_api_paths.py" name="test_board" time="0.01"/>
</testsuite>
"""


def test_a_scoped_run_cannot_overwrite_another_features_stream_with_a_slice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`tasks/tier3` declares two paths; an api_ui-scoped run whose report
    contains ONE of them (TestBoardPaths) holds partial evidence for tasks and
    appends into the same stream key as tasks' own full run -- turning a full
    red result into a one-case green. Partial out-of-scope evidence is dropped."""
    var_dir = _var_dir(tmp_path)
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    full = tmp_path / "20260811T100000Z-tier3.xml"
    scoped = tmp_path / "20260811T100100Z-tier3.xml"
    full.write_text(_BOARD_RED, encoding="utf-8")
    scoped.write_text(_BOARD_PARTIAL_GREEN, encoding="utf-8")

    assert filer.fh.main(["append", "--tier", "tier3", "--report", str(full)]) == 0
    assert (
        filer.fh.main(["append", "--tier", "tier3", "--report", str(scoped), "--feature", "api_ui"])
        == 0
    )

    tasks = [
        rec
        for (feature, tier, _env, _stream), rec in filer.fh.latest_per_stream().items()
        if feature == "tasks" and tier == "tier3"
    ]
    assert len(tasks) == 1, tasks
    assert tasks[0]["failed"] == 1, tasks[0]
    sl = _system_ledger()
    assert sl._cell_verdict(sl.feature_grid()["tasks"]["tier3"]) == "FAIL(1)"
    # ...and the scoped run's OWN feature still recorded.
    assert any(
        feature == "api_ui" and tier == "tier3"
        for (feature, tier, _e, _s) in filer.fh.latest_per_stream()
    )


def test_vacuous_coverage_never_counts_as_complete_evidence(tmp_path: Path) -> None:
    """`_covers_cell` decides whether an out-of-scope record may be written.
    "Every declared on-disk path was observed" is vacuously TRUE for a feature
    whose declared paths are all absent (or empty), which would let any scoped
    run write a complete-looking record for a cell it never touched."""
    fh = filer.fh
    matrix = {
        "ghost": {"tier1": ["tests/definitely_absent_dir"], "tier2": [], "tier3": []},
        "empty": {"tier1": [], "tier2": [], "tier3": []},
        "real": {"tier1": ["tests/goals"], "tier2": [], "tier3": []},
    }
    observed = ["tests/goals/test_x.py::test_thing"]
    assert fh._covers_cell("ghost", "tier1", matrix, observed) is False
    assert fh._covers_cell("empty", "tier1", matrix, observed) is False
    assert fh._covers_cell("real", "tier1", matrix, []) is False
    assert fh._covers_cell("real", "tier1", matrix, observed) is True


@pytest.mark.parametrize("feature", [None, "api_ui"])
def test_attribution_does_not_depend_on_the_runners_feature_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, feature: str | None
) -> None:
    """--feature says which paths RUN, never how a testcase attributes.

    Scoping the prefix order to the requested features makes the engine-route
    testcase belong to control_plane in a full run and to api_ui in a
    `--feature api_ui` run (api_ui's broad `tests/api` prefix being the only
    candidate left) -- so two alternating schedules record one broken test
    under two features, and file two standing findings for it.
    """
    rows = _append_engine_report(tmp_path, monkeypatch, feature=feature)
    owners = [row["feature"] for row in rows if row.get("failures")]
    # control_plane's tier1 cell is exactly this one file, so even the scoped
    # run holds COMPLETE evidence for it and may write it (partial out-of-scope
    # evidence is what gets dropped -- see the tasks/tier3 guard above).
    assert owners == ["control_plane"]


def test_incomplete_marks_every_record_of_the_run_as_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run.sh's live block uses this when a requested module is not on disk:
    the tests that DID run must not append as a clean pass."""
    var_dir = tmp_path / "fh-var"
    report = tmp_path / "report.xml"
    report.write_text(_ENGINE_JUNIT, encoding="utf-8")
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))

    assert (
        filer.fh.main(
            [
                "append",
                "--tier",
                "tier1",
                "--report",
                str(report),
                "--incomplete",
                "missing live module(s): tests/feature_health/tier3/test_production_surface.py",
            ]
        )
        == 0
    )

    rows = [
        json.loads(line)
        for shard in sorted(var_dir.glob("ledger-*.jsonl"))
        for line in shard.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert rows and all(row["status"] == "error" for row in rows)
    assert all("test_production_surface.py" in row["incomplete"] for row in rows)


# --------------------------------------------------------------------------- system ledger


def _system_ledger():
    path = Path(filer._REPO_ROOT) / "scripts" / "feature_health" / "system_ledger.py"
    spec = importlib.util.spec_from_file_location("system_ledger_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verdict_severity_order_ranks_incomplete_above_a_pass() -> None:
    sl = _system_ledger()
    ordered = ["-", "PASS(3)", "EMPTY", "MISS+PASS(3)", "ERR", "ABORT", "FAIL(1)"]
    ranks = [sl.verdict_rank(verdict) for verdict in ordered]
    assert ranks == sorted(ranks)
    assert sl.verdict_rank("MISS+PASS(3)") > sl.verdict_rank("PASS(3)")
    assert sl.verdict_rank("FAIL(1)") == max(ranks)


def test_missing_paths_never_render_as_a_plain_pass() -> None:
    sl = _system_ledger()
    assert sl._cell_verdict({"status": "ok", "passed": 3}) == "PASS(3)"
    assert (
        sl._cell_verdict({"status": "ok", "passed": 3, "missing_paths": ["tests/gone.py"]})
        == "MISS+PASS(3)"
    )


def test_both_renderers_agree_a_sibling_red_stream_is_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fh.py's grid (summary + LATEST.md) and SYSTEM-LEDGER.md reduce through
    the SAME worst_per_cell(latest_per_stream()); a clean Playwright append
    must not be able to repaint a red live-probe cell green in either."""
    var_dir = _var_dir(tmp_path)
    _write_ledger(
        var_dir,
        _record(
            feature="api_ui",
            tier="tier3",
            env="live",
            ts="2026-08-11T10:00:00+00:00",
            passed=0,
            failed=1,
            failures=[
                {
                    "nodeid": "tests/feature_health/tier3/test_live_probes.py::test_api",
                    "message": "boom",
                    "expected": False,
                    "known_issue_id": None,
                }
            ],
            report_path="20260811T100000Z-tier3-live.xml",
        ),
        _record(
            feature="api_ui",
            tier="tier3",
            env="live",
            ts="2026-08-11T10:01:00+00:00",  # NEWER and clean
            passed=9,
            report_path="20260811T100100Z-playwright.json",
        ),
    )
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))

    fh = filer.fh
    matrix = fh.load_matrix()
    row = next(row for row in fh._grid_rows(matrix) if row[0] == "api_ui")
    assert "1F!" in row[3], f"fh.py grid rendered the sibling-red cell as {row[3]!r}"

    sl = _system_ledger()
    assert sl._cell_verdict(sl.feature_grid()["api_ui"]["tier3"]) == "FAIL(1)"


def test_one_render_of_latest_md_reads_the_ledger_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LATEST.md carries a grid, a per-sub-run detail block and an issue list.
    They are three views of one ledger and this command runs at the END of
    run.sh, while the next run may already be appending: three reads can render
    one document whose grid says PASS, whose detail says FAIL and whose issue
    list names neither."""
    fh = filer.fh
    var_dir = _var_dir(tmp_path)
    var_dir.mkdir(parents=True)
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))

    def stream(**overrides: Any) -> dict[str, Any]:
        return _record(feature="api_ui", tier="tier3", env="live", **overrides)

    red = stream(
        ts="2026-08-11T10:00:00+00:00",
        passed=0,
        failed=1,
        failures=[
            {
                "nodeid": "tests/feature_health/tier3/test_live_probes.py::test_api",
                "message": "boom",
                "expected": False,
                "known_issue_id": None,
            }
        ],
        report_path="20260811T100000Z-tier3-live.xml",
    )
    green = stream(
        ts="2026-08-11T10:05:00+00:00", passed=3, report_path="20260811T100500Z-tier3-live.xml"
    )
    playwright = stream(
        ts="2026-08-11T10:01:00+00:00", passed=9, report_path="20260811T100100Z-playwright.json"
    )
    key_live = ("api_ui", "tier3", "live", "tier3-live.xml")
    key_pw = ("api_ui", "tier3", "live", "playwright.json")
    snapshots = [
        {key_live: red, key_pw: playwright},  # first read: red
        {key_live: green, key_pw: playwright},  # a run landed between reads
        {key_live: green, key_pw: playwright},
    ]
    monkeypatch.setattr(fh, "_latest_per_run", lambda: snapshots.pop(0))

    assert fh.main(["render-latest"]) == 0

    rendered = (var_dir / "LATEST.md").read_text(encoding="utf-8")
    assert len(snapshots) == 2, "render-latest read the ledger more than once"
    grid_row = next(line for line in rendered.splitlines() if line.startswith("| api_ui |"))
    assert "1F!" in grid_row, grid_row
    detail = next(line for line in rendered.splitlines() if line.startswith("- **api_ui**"))
    assert "FAIL" in detail, detail
    assert "test_live_probes.py::test_api" in rendered  # the issue list agrees too


def test_a_rollup_row_never_contradicts_its_own_stream_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One rollup = one ledger snapshot. Two reads of a ledger a runner is
    appending to can disagree, and a frozen row whose cell says PASS while its
    own stream map says FAIL is worse than either reading alone."""
    sl = _system_ledger()
    red = {
        "feature": "api_ui",
        "tier": "tier3",
        "env": "live",
        "status": "ok",
        "ts": "2026-08-11T10:00:00+00:00",
        "failed": 1,
        "passed": 0,
        "report_path": "20260811T100000Z-tier3-live.xml",
    }
    green = {**red, "ts": "2026-08-11T10:01:00+00:00", "failed": 0, "passed": 1}
    snapshots = [
        {("api_ui", "tier3", "live", "tier3-live.xml"): red},
        {("api_ui", "tier3", "live", "tier3-live.xml"): green},
    ]
    monkeypatch.setattr(
        sl.fh, "load_matrix", lambda: {"api_ui": {"tier1": [], "tier2": [], "tier3": []}}
    )
    monkeypatch.setattr(sl.fh, "_latest_per_run", lambda: snapshots.pop(0))

    roll = sl.build_rollup(tmp_path)

    assert (
        roll["features"]["api_ui"]["tier3"]
        == roll["feature_streams"]["api_ui"]["tier3"]["live/tier3-live.xml"]
    )
    assert len(snapshots) == 1, "the rollup read the ledger more than once"


def test_the_grid_cell_is_the_worst_stream_not_the_newest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean Playwright append must not repaint a red live-probe cell green,
    and the jsonl must keep the per-stream breakdown it was collapsed from."""
    var_dir = _var_dir(tmp_path)
    red = _record(
        feature="api_ui",
        tier="tier3",
        env="live",
        ts="2026-08-11T10:00:00+00:00",
        passed=0,
        failed=1,
        failures=[
            {
                "nodeid": "tests/feature_health/tier3/test_live_probes.py::test_api",
                "message": "boom",
                "expected": False,
                "known_issue_id": None,
            }
        ],
        report_path="20260811T100000Z-tier3-live.xml",
    )
    green = _record(
        feature="api_ui",
        tier="tier3",
        env="live",
        ts="2026-08-11T10:01:00+00:00",  # NEWER, clean, different stream
        passed=4,
        report_path="20260811T100100Z-playwright.json",
    )
    _write_ledger(var_dir, red, green)
    monkeypatch.setenv("FH_VAR_DIR", str(var_dir))
    sl = _system_ledger()

    streams = sl.feature_streams()["api_ui"]["tier3"]
    assert set(streams) == {"live/tier3-live.xml", "live/playwright.json"}
    assert sl._cell_verdict(sl.feature_grid()["api_ui"]["tier3"]) == "FAIL(1)"

    roll = sl.build_rollup(Path(filer._REPO_ROOT))
    assert roll["features"]["api_ui"]["tier3"] == "FAIL(1)"
    assert roll["feature_streams"]["api_ui"]["tier3"] == {
        "live/playwright.json": "PASS(4)",
        "live/tier3-live.xml": "FAIL(1)",
    }
    assert "live/tier3-live.xml FAIL(1)" in sl.render_md(roll)


def test_default_queue_is_never_written_by_tests() -> None:
    assert filer.DEFAULT_QUEUE == Path("/Users/youruser/OmniAgentOS/var/loopqueue")
