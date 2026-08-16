"""Behavioural tests for scripts/import-reset-queue.py.

Every test builds its OWN throwaway ``tmp_path`` database and passes it explicitly (``--db`` /
``db_path=``) — this file never resolves ``OMNIAGENTOS_DB`` or the packaged default, so it can
never reach the live database the rest of this checkout serves from (P7's brief: "The importer
must NOT run against the live DB in any test").

``scripts/import-reset-queue.py`` is loaded via :func:`importlib.util.spec_from_file_location`
(the repo's own convention for hyphenated script modules — see
``tests/scripts/test_task_resume_index.py``) so ``run_import`` and the CLI's exit-code contract
can both be exercised without a subprocess where a subprocess is not needed.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.orgdims.store import OrgDimsStore
from tests.support.db_template import migrated_db

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_SCRIPT = REPO_ROOT / "scripts" / "import-reset-queue.py"
PRODUCTION_YAML = (
    REPO_ROOT / "omniagentos" / "team" / "data" / "production_reset_2026_08_10.yaml"
)

KNOWN_ROSTER = (
    ("emp_owner", "the operator", "operator"),
    ("emp_alice", "Alice", "reviewer-merger"),
    ("emp_bob", "Bob", "candidate-author"),
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("import_reset_queue_under_test", SOURCE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The module under test declares dataclasses under ``from __future__ import annotations``;
    # resolving their (stringified) field annotations looks the module up in ``sys.modules`` by
    # name, which only exists once it is registered here — before exec_module runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def module() -> ModuleType:
    return _load_module()


@pytest.fixture()
def prepared_db(tmp_path: Path) -> Iterator[str]:
    """A freshly migrated database with the six companies and the three-person roster present —
    the state a real production database is in by the time this importer runs (org taxonomy
    seeded at app startup, employees seeded by ``company_goals.seed_employees``, roles set by
    migration 123)."""
    db_path = migrated_db(CollabStore, tmp_path / "team.db")
    collab_store = CollabStore(db_path)
    store = collab_store._store
    OrgDimsStore(store._connection).seed_taxonomy()
    goals_store = CompanyGoalsStore(store)
    for employee_id, name, role in KNOWN_ROSTER:
        goals_store.ensure_employee(employee_id=employee_id, name=name, role=role)
    yield db_path


def _connection(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


def _counts(db_path: str) -> tuple[int, int, int]:
    connection = _connection(db_path)
    try:
        tasks = connection.execute("SELECT COUNT(*) FROM board_tasks").fetchone()[0]
        goals = connection.execute("SELECT COUNT(*) FROM company_goals").fetchone()[0]
        evidence = connection.execute("SELECT COUNT(*) FROM task_evidence").fetchone()[0]
        return int(tasks), int(goals), int(evidence)
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# dry-run writes nothing
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(module: ModuleType, prepared_db: str) -> None:
    before = _counts(prepared_db)
    assert before == (0, 0, 0)

    result = module.run_import(
        db_path=prepared_db, yaml_path=PRODUCTION_YAML, dry_run=True, actor="test"
    )

    assert result.dry_run is True
    assert result.created_tasks == []
    assert result.created_goals == []
    assert result.planned_tasks, "dry-run should have planned every task"
    assert result.planned_goals, "dry-run should have planned every goal"
    assert _counts(prepared_db) == (0, 0, 0)


def test_dry_run_does_not_write_the_import_log(
    module: ModuleType, prepared_db: str, tmp_path: Path
) -> None:
    log_path = tmp_path / "IMPORT-LOG.md"
    exit_code = module.main(
        ["--db", prepared_db, "--yaml", str(PRODUCTION_YAML), "--dry-run", "--log-path", str(log_path)]
    )
    assert exit_code == 0
    assert not log_path.exists()


# ---------------------------------------------------------------------------
# real run creates goals + tasks with the right shape
# ---------------------------------------------------------------------------


def test_real_run_creates_goals_and_tasks_with_correct_owner_ref_status_blocked_reason(
    module: ModuleType, prepared_db: str
) -> None:
    result = module.run_import(
        db_path=prepared_db, yaml_path=PRODUCTION_YAML, dry_run=False, actor="test"
    )

    assert result.refused == [], result.refused
    assert len(result.created_goals) == 10
    assert len(result.created_tasks) == 31

    connection = _connection(prepared_db)
    try:
        u3 = dict(
            connection.execute(
                "SELECT ref, owner_employee_id, status, blocked_reason, size, priority "
                "FROM board_tasks WHERE ref = 'U3'"
            ).fetchone()
        )
        assert u3["owner_employee_id"] == "emp_alice"
        assert u3["status"] == "blocked"
        assert u3["blocked_reason"] == "depends on U2"
        assert u3["size"] == "M"
        assert u3["priority"] == "high"

        owner3 = dict(
            connection.execute(
                "SELECT owner_employee_id, status, blocked_reason FROM board_tasks WHERE ref = 'OPS-3'"
            ).fetchone()
        )
        assert owner3["owner_employee_id"] == "emp_owner"
        assert owner3["status"] == "blocked"
        assert "telemetry ships first" in owner3["blocked_reason"]

        u1_status = connection.execute(
            "SELECT status FROM board_tasks WHERE ref = 'U1'"
        ).fetchone()[0]
        assert u1_status == "open"  # named status-hint exception despite no dep either way

        # hierarchy: U1's parent is UP-1's real board_tasks id.
        row = connection.execute(
            "SELECT c.id AS child_id, p.ref AS parent_ref FROM board_tasks c "
            "JOIN board_tasks p ON c.parent_task_id = p.id WHERE c.ref = 'U1'"
        ).fetchone()
        assert row is not None
        assert row["parent_ref"] == "UP-1"

        # goal ladder: U1 -> cluster-green-main -> ns-n1, and the goal's owner is set.
        goal_row = connection.execute(
            "SELECT cg.title, cg.owner_employee_id, cg.parent_goal_id FROM board_tasks bt "
            "JOIN company_goals cg ON cg.id = bt.goal_id WHERE bt.ref = 'U1'"
        ).fetchone()
        assert goal_row["title"] == "Green main & zero PR backlog"
        assert goal_row["owner_employee_id"] == "emp_alice"
        assert goal_row["parent_goal_id"] is not None

        # ref is unique-partial-indexed and every task's ref round-trips.
        all_refs = {
            row["ref"] for row in connection.execute("SELECT ref FROM board_tasks WHERE ref IS NOT NULL")
        }
        assert "U1" in all_refs
        assert len(all_refs) == 31

        # owned_paths lands in org_json (machine-readable, where the attribution
        # engine's owned-paths matcher reads it) as well as the description line.
        u3_org = json.loads(
            connection.execute(
                "SELECT org_json FROM board_tasks WHERE ref = 'U3'"
            ).fetchone()[0]
        )
        assert isinstance(u3_org.get("owned_paths"), list)
        assert "omniagentos/collab/store.py" in u3_org["owned_paths"]
    finally:
        connection.close()


def test_baseline_tasks_are_done_verified_and_carry_manual_doc_evidence(
    module: ModuleType, prepared_db: str
) -> None:
    module.run_import(db_path=prepared_db, yaml_path=PRODUCTION_YAML, dry_run=False, actor="test")

    connection = _connection(prepared_db)
    try:
        for ref, owner in (
            ("BASE-ALICE", "emp_alice"),
            ("BASE-OPS", "emp_owner"),
            ("BASE-BOB", "emp_bob"),
        ):
            row = dict(
                connection.execute(
                    "SELECT id, status, verified_at, verified_by, owner_employee_id "
                    "FROM board_tasks WHERE ref = ?",
                    (ref,),
                ).fetchone()
            )
            assert row["owner_employee_id"] == owner
            assert row["status"] == "done"
            assert row["verified_at"] is not None
            assert row["verified_by"] == "emp_owner"

            evidence = connection.execute(
                "SELECT kind, attribution, task_id FROM task_evidence WHERE task_id = ?",
                (row["id"],),
            ).fetchall()
            assert len(evidence) == 1
            assert evidence[0]["kind"] == "doc"
            assert evidence[0]["attribution"] == "manual"

        total_evidence = connection.execute("SELECT COUNT(*) FROM task_evidence").fetchone()[0]
        assert total_evidence == 3
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


def test_reimport_creates_nothing(module: ModuleType, prepared_db: str) -> None:
    first = module.run_import(
        db_path=prepared_db, yaml_path=PRODUCTION_YAML, dry_run=False, actor="test"
    )
    assert len(first.created_tasks) == 31
    assert len(first.created_goals) == 10
    before = _counts(prepared_db)

    second = module.run_import(
        db_path=prepared_db, yaml_path=PRODUCTION_YAML, dry_run=False, actor="test"
    )

    assert second.created_tasks == []
    assert second.created_goals == []
    assert sorted(second.skipped_tasks) == sorted(first.created_tasks)
    assert sorted(second.skipped_goals) == sorted(first.created_goals)
    assert _counts(prepared_db) == before


def test_reimport_repairs_missing_baseline_evidence_and_verification_idempotently(
    module: ModuleType, prepared_db: str, tmp_path: Path
) -> None:
    first = module.run_import(
        db_path=prepared_db, yaml_path=PRODUCTION_YAML, dry_run=False, actor="first-import"
    )
    assert first.refused == []

    connection = _connection(prepared_db)
    try:
        baseline = connection.execute(
            "SELECT id, verified_at FROM board_tasks WHERE ref = 'BASE-ALICE'"
        ).fetchone()
        assert baseline is not None and baseline["verified_at"] is not None
        task_id = str(baseline["id"])
        original_verified_at = str(baseline["verified_at"])
        connection.execute(
            "DELETE FROM task_evidence WHERE kind = 'doc' AND repo = '' "
            "AND ref = 'baseline-2026-08-03-alice'"
        )
        connection.execute(
            "UPDATE board_tasks SET verified_at = NULL, verified_by = NULL WHERE id = ?",
            (task_id,),
        )
        connection.commit()
        events_before_repair = int(
            connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
        )
    finally:
        connection.close()

    repaired = module.run_import(
        db_path=prepared_db, yaml_path=PRODUCTION_YAML, dry_run=False, actor="repair-import"
    )
    assert repaired.refused == []
    assert "BASE-ALICE" in repaired.skipped_tasks
    assert repaired.repairs == [
        ("BASE-ALICE", "evidence"),
        ("BASE-ALICE", "verification"),
    ]

    connection = _connection(prepared_db)
    try:
        restored = connection.execute(
            "SELECT verified_at, verified_by FROM board_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        assert restored is not None
        assert restored["verified_at"] == original_verified_at
        assert restored["verified_by"] == "emp_owner"
        evidence = connection.execute(
            "SELECT task_id, attribution, actor FROM task_evidence "
            "WHERE kind = 'doc' AND repo = '' AND ref = 'baseline-2026-08-03-alice'"
        ).fetchall()
        assert [tuple(row) for row in evidence] == [(task_id, "manual", "repair-import")]
        events_after_repair = int(
            connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
        )
        assert events_after_repair == events_before_repair + 2
    finally:
        connection.close()

    log_path = tmp_path / "repair-log.md"
    module._write_import_log(repaired, log_path)
    log_text = log_path.read_text(encoding="utf-8")
    assert "## Repairs (2)" in log_text
    assert "`BASE-ALICE`: restored evidence" in log_text
    assert "`BASE-ALICE`: restored verification" in log_text

    clean = module.run_import(
        db_path=prepared_db, yaml_path=PRODUCTION_YAML, dry_run=False, actor="third-import"
    )
    assert clean.repairs == []
    connection = _connection(prepared_db)
    try:
        final_event_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
        )
        final_evidence_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM task_evidence WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
        )
        assert final_event_count == events_after_repair
        assert final_evidence_count == 1
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# IMPORT-LOG.md
# ---------------------------------------------------------------------------


def test_import_log_written_at_the_injected_path_on_a_real_run(
    module: ModuleType, prepared_db: str, tmp_path: Path
) -> None:
    log_path = tmp_path / "logs" / "IMPORT-LOG.md"
    exit_code = module.main(
        ["--db", prepared_db, "--yaml", str(PRODUCTION_YAML), "--log-path", str(log_path)]
    )
    assert exit_code == 0
    assert log_path.exists()
    text = log_path.read_text(encoding="utf-8")
    assert "created 10" in text  # goals
    assert "created 31" in text  # tasks
    assert "ns-n1" in text


# ---------------------------------------------------------------------------
# refusal + exit codes (small synthetic fixtures, not the full production file)
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_unknown_company_slug_is_refused_and_exits_1(
    module: ModuleType, prepared_db: str, tmp_path: Path
) -> None:
    fixture = _write_yaml(
        tmp_path / "bad-company.yaml",
        {
            "companies": [{"slug": "not-a-real-company", "name": "Nope"}],
            "goals": [
                {
                    "ref": "g1",
                    "title": "A goal",
                    "horizon": "long_term",
                    "company": "not-a-real-company",
                    "owner": "emp_owner",
                }
            ],
            "tasks": [],
        },
    )
    result = module.run_import(db_path=prepared_db, yaml_path=fixture, dry_run=False, actor="test")
    assert result.refused
    assert result.refused[0][0] == "goal:g1"


def test_cli_exits_1_when_something_is_refused(prepared_db: str, tmp_path: Path) -> None:
    fixture = _write_yaml(
        tmp_path / "bad-company.yaml",
        {
            "companies": [{"slug": "not-a-real-company", "name": "Nope"}],
            "goals": [
                {
                    "ref": "g1",
                    "title": "A goal",
                    "horizon": "long_term",
                    "company": "not-a-real-company",
                    "owner": "emp_owner",
                }
            ],
            "tasks": [],
        },
    )
    # A REAL (non-dry-run) invocation writes IMPORT-LOG.md — --log-path must point at tmp_path,
    # never the script's own devtasks/ default, or this test would leave a file in the checkout.
    log_path = tmp_path / "IMPORT-LOG.md"
    completed = subprocess.run(
        [
            sys.executable,
            str(SOURCE_SCRIPT),
            "--db",
            prepared_db,
            "--yaml",
            str(fixture),
            "--log-path",
            str(log_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1, completed.stderr
    assert log_path.exists()


def test_cli_exits_2_on_malformed_yaml(prepared_db: str, tmp_path: Path) -> None:
    fixture = tmp_path / "broken.yaml"
    fixture.write_text("tasks: [this is not a mapping", encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(SOURCE_SCRIPT), "--db", prepared_db, "--yaml", str(fixture)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2, completed.stderr


def test_cli_exits_0_on_a_clean_dry_run_of_the_production_file(prepared_db: str) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SOURCE_SCRIPT),
            "--db",
            prepared_db,
            "--yaml",
            str(PRODUCTION_YAML),
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "would create" in completed.stdout


@pytest.mark.parametrize("dry_run", [False, True], ids=["real-run", "dry-run"])
def test_cli_preflight_refuses_empty_roster_with_real_seed_remedy(
    module: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    dry_run: bool,
) -> None:
    db_path = migrated_db(CollabStore, tmp_path / f"empty-roster-{dry_run}.db")
    collab_store = CollabStore(db_path)
    OrgDimsStore(collab_store._store._connection).seed_taxonomy()
    log_path = tmp_path / f"empty-roster-{dry_run}.md"
    argv = [
        "--db",
        db_path,
        "--yaml",
        str(PRODUCTION_YAML),
        "--log-path",
        str(log_path),
    ]
    if dry_run:
        argv.append("--dry-run")

    assert module.main(argv) == 2
    captured = capsys.readouterr()
    assert "emp_owner" in captured.err
    assert "emp_alice" in captured.err
    assert "emp_bob" in captured.err
    assert "omniagentos.company_goals.seed_employees.seed_employees" in captured.err
    assert _counts(db_path) == (0, 0, 0)
    assert not log_path.exists()


def test_cli_preflight_refuses_missing_companies_with_ensure_seeded_remedy(
    module: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = migrated_db(CollabStore, tmp_path / "empty-companies.db")
    collab_store = CollabStore(db_path)
    goals_store = CompanyGoalsStore(collab_store._store)
    for employee_id, name, role in KNOWN_ROSTER:
        goals_store.ensure_employee(employee_id=employee_id, name=name, role=role)

    assert module.main(["--db", db_path, "--yaml", str(PRODUCTION_YAML), "--dry-run"]) == 2
    captured = capsys.readouterr()
    for slug in ("initech", "globex", "acmeuni", "hooli", "omniagentos", "personal"):
        assert slug in captured.err
    assert "omniagentos.orgdims.service.OrgDimsService" in captured.err
    assert "ensure_seeded" in captured.err
    assert _counts(db_path) == (0, 0, 0)


def test_task_integrity_error_is_refused_with_ref_and_column_and_import_continues(
    module: ModuleType, prepared_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _write_yaml(
        tmp_path / "task-integrity.yaml",
        {
            "companies": [{"slug": "omniagentos", "name": "OmniAgentOS"}],
            "goals": [],
            "tasks": [
                {
                    "ref": "DB-BAD",
                    "title": "Refused by sqlite",
                    "owner": None,
                    "size": "S",
                    "acceptance_criteria": "",
                },
                {
                    "ref": "DB-GOOD",
                    "title": "Still processed",
                    "owner": None,
                    "size": "S",
                    "acceptance_criteria": "",
                },
            ],
        },
    )
    original = module.CollabStore.create_board_task

    def flaky_create(store: Any, task: Any, *, actor: str = "system") -> None:
        if task.ref == "DB-BAD":
            raise sqlite3.IntegrityError("NOT NULL constraint failed: board_tasks.title")
        original(store, task, actor=actor)

    monkeypatch.setattr(module.CollabStore, "create_board_task", flaky_create)
    result = module.run_import(
        db_path=prepared_db, yaml_path=fixture, dry_run=False, actor="test"
    )
    assert result.refused == [
        ("DB-BAD", "integrity error: NOT NULL constraint failed: board_tasks.title")
    ]
    assert result.created_tasks == ["DB-GOOD"]


def test_goal_integrity_error_is_refused_with_ref_and_column_and_import_continues(
    module: ModuleType, prepared_db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = _write_yaml(
        tmp_path / "goal-integrity.yaml",
        {
            "companies": [{"slug": "omniagentos", "name": "OmniAgentOS"}],
            "goals": [
                {
                    "ref": "goal-bad",
                    "title": "Refused goal",
                    "horizon": "long_term",
                    "company": "omniagentos",
                    "owner": "emp_owner",
                },
                {
                    "ref": "goal-good",
                    "title": "Processed goal",
                    "horizon": "long_term",
                    "company": "omniagentos",
                    "owner": "emp_owner",
                },
            ],
            "tasks": [],
        },
    )
    original = module.CompanyGoalsStore.create_goal

    def flaky_goal(store: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs["title"] == "Refused goal":
            raise sqlite3.IntegrityError("NOT NULL constraint failed: company_goals.title")
        return original(store, **kwargs)

    monkeypatch.setattr(module.CompanyGoalsStore, "create_goal", flaky_goal)
    result = module.run_import(
        db_path=prepared_db, yaml_path=fixture, dry_run=False, actor="test"
    )
    assert result.refused == [
        ("goal:goal-bad", "integrity error: NOT NULL constraint failed: company_goals.title")
    ]
    assert result.created_goals == ["goal-good"]


def test_owner_not_in_known_roster_is_refused(
    module: ModuleType, prepared_db: str, tmp_path: Path
) -> None:
    fixture = _write_yaml(
        tmp_path / "bad-owner.yaml",
        {
            "companies": [{"slug": "omniagentos", "name": "OmniAgentOS"}],
            "goals": [],
            "tasks": [
                {
                    "ref": "BAD-1",
                    "title": "A task with an unknown owner",
                    "owner": "emp_someone_else",
                    "size": "S",
                    "acceptance_criteria": "n/a",
                    "status": "open",
                }
            ],
        },
    )
    result = module.run_import(db_path=prepared_db, yaml_path=fixture, dry_run=False, actor="test")
    assert result.refused
    assert result.refused[0][0] == "BAD-1"
    assert _counts(prepared_db) == (0, 0, 0)
