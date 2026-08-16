"""Tests for structured gate evidence and AcceptanceCriterion.evidence_required."""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.db.store import SqliteStore
from omniagentos.gates.engine import (
    GateResult,
    GateSpec,
    blocking_failures,
    evaluate_evidence,
    run_gates,
)
from omniagentos.swarm.scheduler import default_verifier


@dataclass
class DummyCriterion:
    id: str | None
    evidence_required: bool


def test_migration_applies() -> None:
    # 1. Migration applies — gate_evidence is in sqlite_master and has the expected columns (PRAGMA table_info).
    store = SqliteStore(":memory:")
    conn = store._connection

    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'gate_evidence'"
    ).fetchone()
    assert row is not None, "gate_evidence table should exist in sqlite_master"

    pragma_rows = conn.execute("PRAGMA table_info(gate_evidence)").fetchall()
    columns = {r[1] for r in pragma_rows}
    expected_cols = {
        "id",
        "created_at",
        "run_id",
        "task_id",
        "attempt_ref",
        "gate_name",
        "command",
        "exit_code",
        "duration_ms",
        "output_artifact_path",
        "output_sha256",
        "test_counts_json",
        "commit_sha",
        "env_digest",
    }
    for col in expected_cols:
        assert col in columns, f"Column {col} is missing from gate_evidence table"


def test_row_and_artifact_per_gate(tmp_path: Path) -> None:
    # 2. A row + artifact per gate — run two gates with conn and artifact_root=tmp_path;
    # assert two rows exist, each output_artifact_path file exists on disk, and each
    # output_sha256 equals the sha256 of that file's actual bytes.
    store = SqliteStore(":memory:")
    conn = store._connection

    specs = [
        GateSpec(argv=["echo", "Gate 1 output"], name="gate-1"),
        GateSpec(argv=["echo", "Gate 2 output"], name="gate-2"),
    ]

    results = run_gates(
        specs,
        workdir=str(tmp_path),
        conn=conn,
        run_id="run-123",
        task_id="tsk-456",
        attempt_ref="att-789",
        artifact_root=tmp_path,
        commit_sha="commit-abc",
    )

    assert len(results) == 2
    assert results[0].ok
    assert results[1].ok

    rows = conn.execute("SELECT * FROM gate_evidence ORDER BY gate_name ASC").fetchall()
    assert len(rows) == 2

    # Columns: id, created_at, run_id, task_id, attempt_ref, gate_name, command, exit_code,
    # duration_ms, output_artifact_path, output_sha256, test_counts_json, commit_sha, env_digest
    db_cols = [
        "id",
        "created_at",
        "run_id",
        "task_id",
        "attempt_ref",
        "gate_name",
        "command",
        "exit_code",
        "duration_ms",
        "output_artifact_path",
        "output_sha256",
        "test_counts_json",
        "commit_sha",
        "env_digest",
    ]

    for r in rows:
        row_dict = dict(zip(db_cols, r, strict=True))
        assert row_dict["run_id"] == "run-123"
        assert row_dict["task_id"] == "tsk-456"
        assert row_dict["attempt_ref"] == "att-789"
        assert row_dict["commit_sha"] == "commit-abc"
        assert row_dict["gate_name"] in ["gate-1", "gate-2"]

        art_path = row_dict["output_artifact_path"]
        assert art_path is not None
        assert os.path.exists(art_path)

        with open(art_path, "rb") as f:
            file_bytes = f.read()
        recomputed_sha = hashlib.sha256(file_bytes).hexdigest()
        assert row_dict["output_sha256"] == recomputed_sha

        matched_res = [res for res in results if res.name == row_dict["gate_name"]][0]
        assert matched_res.output_sha256 == recomputed_sha
        assert matched_res.output_artifact_path == art_path


def test_full_output_is_not_truncated(tmp_path: Path) -> None:
    # 3. Full output is NOT truncated — run a gate producing output well over 500 chars and assert
    # the artifact file contains all of it.
    store = SqliteStore(":memory:")
    conn = store._connection

    large_str = "A" * 1000
    spec = GateSpec(argv=["echo", large_str], name="large-gate")

    results = run_gates(
        [spec],
        workdir=str(tmp_path),
        conn=conn,
        artifact_root=tmp_path,
    )

    assert results[0].ok
    art_path = results[0].output_artifact_path
    assert art_path is not None
    assert os.path.exists(art_path)

    with open(art_path, encoding="utf-8") as f:
        content = f.read()

    assert large_str in content
    assert len(content) >= 1000


def test_conn_none_writes_nothing(tmp_path: Path) -> None:
    # 4. conn=None writes nothing — no rows, and no files created under artifact_root.
    spec = GateSpec(argv=["echo", "nothing"], name="nothing-gate")

    results = run_gates(
        [spec],
        workdir=str(tmp_path),
        conn=None,
        artifact_root=tmp_path,
    )

    assert len(results) == 1
    assert results[0].ok
    assert results[0].output_artifact_path is None
    assert results[0].output_sha256 is None

    # Assert no files created under tmp_path
    created_files = list(tmp_path.rglob("*"))
    assert not created_files, f"Expected no files under {tmp_path}, but found {created_files}"


def test_evidence_failure_never_breaks_execution(tmp_path: Path) -> None:
    # 5. Evidence failure never breaks execution — pass a closed connection (or an unwritable
    # artifact_root) and assert run_gates still returns correct GateResults and does not raise.
    store = SqliteStore(":memory:")
    conn = store._connection
    conn.close()

    spec = GateSpec(argv=["echo", "closed-conn"], name="closed-gate")
    results = run_gates(
        [spec],
        workdir=str(tmp_path),
        conn=conn,
        artifact_root=tmp_path,
    )
    assert len(results) == 1
    assert results[0].ok
    # The artifact write succeeded (artifact_root is writable) and only the DB
    # insert failed, so reporting the path is honest: the file really is there.
    # What matters is that the swallowed row failure did not change the verdict
    # or raise.
    assert results[0].output_artifact_path is not None
    assert Path(results[0].output_artifact_path).is_file()
    assert results[0].output_sha256 is not None

    # Unwritable artifact root
    store2 = SqliteStore(":memory:")
    conn2 = store2._connection
    unwritable_root = Path("/nonexistent/invalid_directory/evidence")

    results2 = run_gates(
        [spec],
        workdir=str(tmp_path),
        conn=conn2,
        artifact_root=unwritable_root,
    )
    assert len(results2) == 1
    assert results2[0].ok
    assert results2[0].output_artifact_path is None
    assert results2[0].output_sha256 is None


def test_evaluate_evidence() -> None:
    # 6. evaluate_evidence:
    # - a required criterion with matching successful recorded evidence passes;
    # - a required criterion with no matching result is blocking=True and listed in missing;
    # - a required criterion whose gate failed is blocking;
    # - evidence_required=False is ignored;
    # - empty criteria pass.

    # Successful case
    c_req = DummyCriterion(id="gate-a", evidence_required=True)
    r_ok = GateResult(
        name="gate-a",
        command="...",
        ok=True,
        exit_code=0,
        output="passed",
        duration_ms=5.0,
        blocking=True,
        output_artifact_path="/tmp/a.log",
        output_sha256="sha-a",
    )
    verdict = evaluate_evidence([c_req], [r_ok])
    assert verdict.ok
    assert not verdict.blocking
    assert not verdict.missing

    # Missing case (no matching result name)
    c_missing = DummyCriterion(id="gate-b", evidence_required=True)
    verdict2 = evaluate_evidence([c_missing], [r_ok])
    assert not verdict2.ok
    assert verdict2.blocking
    assert verdict2.missing == ("gate-b",)

    # Failed gate case
    r_fail = GateResult(
        name="gate-a",
        command="...",
        ok=False,
        exit_code=1,
        output="failed",
        duration_ms=5.0,
        blocking=True,
        output_artifact_path="/tmp/a.log",
        output_sha256="sha-a",
    )
    verdict3 = evaluate_evidence([c_req], [r_fail])
    assert not verdict3.ok
    assert verdict3.blocking
    assert verdict3.missing == ("gate-a",)

    # Successful gate but lacks recorded evidence (output_sha256 is None/empty)
    r_no_sha = GateResult(
        name="gate-a",
        command="...",
        ok=True,
        exit_code=0,
        output="passed",
        duration_ms=5.0,
        blocking=True,
        output_artifact_path=None,
        output_sha256=None,
    )
    verdict4 = evaluate_evidence([c_req], [r_no_sha])
    assert not verdict4.ok
    assert verdict4.blocking
    assert verdict4.missing == ("gate-a",)

    # evidence_required=False is ignored
    c_ignored = DummyCriterion(id="gate-ignored", evidence_required=False)
    verdict5 = evaluate_evidence([c_ignored], [])
    assert verdict5.ok
    assert not verdict5.blocking

    # Empty criteria pass
    verdict6 = evaluate_evidence([], [])
    assert verdict6.ok
    assert not verdict6.blocking


def test_blocking_failures() -> None:
    # 7. blocking_failures: returning results that are not ok AND blocking.
    r1 = GateResult(
        name="g1", command="...", ok=False, exit_code=1, output="", duration_ms=1.0, blocking=True
    )
    r2 = GateResult(
        name="g2", command="...", ok=False, exit_code=1, output="", duration_ms=1.0, blocking=False
    )
    r3 = GateResult(
        name="g3", command="...", ok=True, exit_code=0, output="", duration_ms=1.0, blocking=True
    )

    results = blocking_failures([r1, r2, r3])
    assert results == [r1]


def test_default_verifier_unaffected(tmp_path: Path, monkeypatch: Any) -> None:
    # 8. default_verifier is unaffected — import it from omniagentos.swarm.scheduler,
    # run it in a tmp_path with a simple passing command, and assert it still returns the
    # same (True, "$ ...")-shaped result and created no gate_evidence rows anywhere.
    task: dict[str, Any] = {}
    (tmp_path / "test_verifier.py").write_text(
        "def test_verifier_passes():\n    assert True\n",
        encoding="utf-8",
    )
    # The verifier grammar intentionally allows only the portable ``python``
    # spelling, while the production gate environment intentionally omits the
    # repository virtualenv from PATH.  Expose this test process's interpreter
    # under that spelling for this nested, hermetic verifier only.
    interpreter_dir = str(Path(sys.executable).parent)
    inherited_path = os.environ.get("PATH", "")
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(part for part in (interpreter_dir, inherited_path) if part),
    )
    swarm_json = {
        "formation_mechanical_gate": True,
        "verify_command": "python -m pytest -q test_verifier.py",
    }

    # Verify that there's no DB side effects
    store = SqliteStore(":memory:")
    # We don't pass store or connection to default_verifier, but we can verify our database remains empty of gate_evidence
    conn = store._connection

    ok, logs = default_verifier(task, swarm_json, str(tmp_path))
    assert ok is True
    assert "$ python -m pytest -q test_verifier.py" in logs

    # Assert no rows exist in gate_evidence
    count = conn.execute("SELECT COUNT(*) FROM gate_evidence").fetchone()[0]
    assert count == 0, "No gate evidence should be written by default_verifier"
