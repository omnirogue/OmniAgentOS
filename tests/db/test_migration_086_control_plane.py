"""Migration 086 (control-plane schema): tables, columns, CHECKs, indexes."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate
from omniagentos.taskcontract.store import TaskContractStore

_CONTEXT_PACKAGE_COLUMNS = {
    "id",
    "task_id",
    "manifest_json",
    "content_hash",
    "completeness_json",
    "status",
    "created_at",
    "updated_at",
}
_VERIFICATION_REPORT_COLUMNS = {
    "id",
    "task_id",
    "artifact_hash",
    "verdict",
    "verifier",
    "reviewer_lineage",
    "implementer_lineage",
    "findings_json",
    "evidence_json",
    "created_at",
}
_AUDIT_REPORT_COLUMNS = {
    "id",
    "scope_type",
    "scope_id",
    "result",
    "categories_json",
    "trace_refs_json",
    "created_at",
}
_ACCEPTANCE_RECORD_COLUMNS = {
    "id",
    "project_id",
    "release_candidate_hash",
    "disposition",
    "exceptions_json",
    "evidence_json",
    "created_at",
}
_ROUTING_HISTORY_COLUMNS = {
    "id",
    "task_id",
    "decision_kind",
    "features_json",
    "route_json",
    "cost_json",
    "outcome_json",
    "decided_at",
    "outcome_at",
}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _table_info(connection: sqlite3.Connection, table: str) -> list[tuple]:
    return list(connection.execute(f"PRAGMA table_info({table})").fetchall())


def _index_columns(connection: sqlite3.Connection, index_name: str) -> list[str]:
    rows = connection.execute(f"PRAGMA index_info({index_name})").fetchall()
    # PRAGMA index_info: seqno, cid, name — order by seqno
    ordered = sorted(rows, key=lambda row: int(row[0]))
    return [str(row[2]) for row in ordered]


def _index_table(connection: sqlite3.Connection, index_name: str) -> str | None:
    for table in (
        "context_packages",
        "verification_reports",
        "audit_reports",
        "acceptance_records",
        "routing_history",
        "events",
        "swarm_deps",
        "task_contracts",
        "task_contract_transitions",
    ):
        for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
            if str(row[1]) == index_name:
                return table
    return None


def test_migration_086_applies_from_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "x.db"
    version = migrate(str(db_path))
    assert version >= 86

    with sqlite3.connect(db_path) as connection:
        versions = {
            int(row[0])
            for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
        }
        assert 86 in versions

        assert _CONTEXT_PACKAGE_COLUMNS <= _columns(connection, "context_packages")
        assert _VERIFICATION_REPORT_COLUMNS <= _columns(connection, "verification_reports")
        assert _AUDIT_REPORT_COLUMNS <= _columns(connection, "audit_reports")
        assert _ACCEPTANCE_RECORD_COLUMNS <= _columns(connection, "acceptance_records")
        assert _ROUTING_HISTORY_COLUMNS <= _columns(connection, "routing_history")


def test_migration_086_applies_over_a_v085_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_files = _migration_files()
    truncated = [(v, p) for v, p in real_files if v <= 85]
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: truncated)

    db_path = tmp_path / "upgrade.db"
    assert migrate(str(db_path)) <= 85

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO swarm_deps (swarm_run_id, task_id, depends_on_task_id) "
            "VALUES ('swr_pre', 'btk_a', 'btk_b')"
        )
        connection.execute(
            "INSERT INTO events (ts, type, actor, action, target_type, target_id, "
            "payload_json, trace_id) "
            "VALUES ('2026-07-27T00:00:00Z', 'test.event', 'api', 'create', "
            "'task', 'tsk_pre', '{}', 'tr_pre')"
        )
        connection.commit()

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: real_files)
    assert migrate(str(db_path)) >= 86

    with sqlite3.connect(db_path) as connection:
        dep = connection.execute(
            "SELECT swarm_run_id, task_id, depends_on_task_id, condition "
            "FROM swarm_deps WHERE swarm_run_id = 'swr_pre'"
        ).fetchone()
        assert dep is not None
        assert dep[0] == "swr_pre"
        assert dep[1] == "btk_a"
        assert dep[2] == "btk_b"
        assert dep[3] == "VERIFIED"

        event = connection.execute(
            "SELECT type, actor, action, target_id FROM events WHERE target_id = 'tsk_pre'"
        ).fetchone()
        assert event is not None
        assert event[0] == "test.event"
        assert event[1] == "api"
        assert event[2] == "create"
        assert event[3] == "tsk_pre"


def test_events_columns_are_nullable_additions(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    assert migrate(str(db_path)) >= 86

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO events (ts, type, actor, action, target_type, target_id, "
            "payload_json, trace_id) "
            "VALUES ('2026-07-27T00:00:00Z', 'legacy.event', 'api', 'note', "
            "'', '', '{}', '')"
        )
        connection.commit()
        row = connection.execute(
            "SELECT execution_id, sequence FROM events WHERE type = 'legacy.event'"
        ).fetchone()
        assert row is not None
        assert row[0] is None
        assert row[1] is None


def test_task_contracts_ddl_in_086_matches_store_py(tmp_path: Path) -> None:
    migrated_db = tmp_path / "migrated.db"
    assert migrate(str(migrated_db)) >= 86

    store_db = tmp_path / "store_only.db"
    store = TaskContractStore(store_db)
    try:
        with sqlite3.connect(migrated_db) as migrated, sqlite3.connect(store_db) as store_conn:
            assert _table_info(migrated, "task_contracts") == _table_info(
                store_conn, "task_contracts"
            )
            assert _table_info(migrated, "task_contract_transitions") == _table_info(
                store_conn, "task_contract_transitions"
            )
    finally:
        store.close()


def test_086_checksum_recorded(tmp_path: Path) -> None:
    db_path = tmp_path / "checksum.db"
    assert migrate(str(db_path)) >= 86

    path_086 = next(path for version, path in _migration_files() if version == 86)
    expected = hashlib.sha256(path_086.read_bytes()).hexdigest()

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT checksum FROM schema_migrations WHERE version = 86"
        ).fetchone()
    assert row is not None
    assert row[0] == expected


def test_spec_13_1_indexes_exist(tmp_path: Path) -> None:
    db_path = tmp_path / "indexes.db"
    assert migrate(str(db_path)) >= 86

    expected: dict[str, tuple[str, list[str]]] = {
        "idx_verification_reports_artifact": ("verification_reports", ["artifact_hash"]),
        "idx_verification_reports_task": ("verification_reports", ["task_id", "created_at"]),
        "idx_events_execution_sequence": ("events", ["execution_id", "sequence"]),
        "idx_context_packages_task_status": ("context_packages", ["task_id", "status"]),
        "idx_audit_reports_scope": ("audit_reports", ["scope_type", "scope_id"]),
        "idx_acceptance_records_project": ("acceptance_records", ["project_id", "created_at"]),
        "idx_routing_history_kind": ("routing_history", ["decision_kind", "decided_at"]),
        "idx_routing_history_task": ("routing_history", ["task_id"]),
    }

    with sqlite3.connect(db_path) as connection:
        for index_name, (table, columns) in expected.items():
            names = {
                str(row[1])
                for row in connection.execute(f"PRAGMA index_list({table})").fetchall()
            }
            assert index_name in names, f"missing index {index_name} on {table}"
            assert _index_table(connection, index_name) == table
            assert _index_columns(connection, index_name) == columns


def test_context_packages_status_check_rejects_unknown_status(tmp_path: Path) -> None:
    db_path = tmp_path / "ctx.db"
    assert migrate(str(db_path)) >= 86

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO context_packages ("
            "  id, task_id, manifest_json, content_hash, status, created_at, updated_at"
            ") VALUES ("
            "  'ctx_ok', 'tsk_1', '{}', 'hash1', 'ready', "
            "  '2026-07-27T00:00:00Z', '2026-07-27T00:00:00Z'"
            ")"
        )
        connection.commit()
        row = connection.execute(
            "SELECT status FROM context_packages WHERE id = 'ctx_ok'"
        ).fetchone()
        assert row is not None
        assert row[0] == "ready"

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO context_packages ("
                "  id, task_id, manifest_json, content_hash, status, created_at, updated_at"
                ") VALUES ("
                "  'ctx_bad', 'tsk_1', '{}', 'hash2', 'bogus', "
                "  '2026-07-27T00:00:00Z', '2026-07-27T00:00:00Z'"
                ")"
            )


def test_verification_reports_verdict_check_rejects_unknown_verdict(tmp_path: Path) -> None:
    db_path = tmp_path / "vr.db"
    assert migrate(str(db_path)) >= 86

    with sqlite3.connect(db_path) as connection:
        for i, verdict in enumerate(("pass", "fail", "insufficient_evidence")):
            connection.execute(
                "INSERT INTO verification_reports ("
                "  id, artifact_hash, verdict, created_at"
                ") VALUES (?, ?, ?, ?)",
                (f"vr_{i}", f"art_{i}", verdict, "2026-07-27T00:00:00Z"),
            )
        connection.commit()
        count = connection.execute("SELECT COUNT(*) FROM verification_reports").fetchone()
        assert count is not None
        assert int(count[0]) == 3

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO verification_reports ("
                "  id, artifact_hash, verdict, created_at"
                ") VALUES ('vr_err', 'art_err', 'error', '2026-07-27T00:00:00Z')"
            )


def test_audit_reports_and_acceptance_records_checks(tmp_path: Path) -> None:
    db_path = tmp_path / "audit_acc.db"
    assert migrate(str(db_path)) >= 86

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO audit_reports ("
            "  id, scope_type, scope_id, result, created_at"
            ") VALUES ("
            "  'aud_ok', 'task', 'tsk_1', 'pass', '2026-07-27T00:00:00Z'"
            ")"
        )
        connection.execute(
            "INSERT INTO acceptance_records ("
            "  id, project_id, release_candidate_hash, disposition, created_at"
            ") VALUES ("
            "  'acc_ok', 'prj_1', 'rc_hash', 'PASS_WITH_ACCEPTED_EXCEPTIONS', "
            "  '2026-07-27T00:00:00Z'"
            ")"
        )
        connection.commit()

        assert (
            connection.execute(
                "SELECT result FROM audit_reports WHERE id = 'aud_ok'"
            ).fetchone()[0]
            == "pass"
        )
        assert (
            connection.execute(
                "SELECT disposition FROM acceptance_records WHERE id = 'acc_ok'"
            ).fetchone()[0]
            == "PASS_WITH_ACCEPTED_EXCEPTIONS"
        )

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO audit_reports ("
                "  id, scope_type, scope_id, result, created_at"
                ") VALUES ("
                "  'aud_bad_scope', 'board', 'x', 'pass', '2026-07-27T00:00:00Z'"
                ")"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO audit_reports ("
                "  id, scope_type, scope_id, result, created_at"
                ") VALUES ("
                "  'aud_bad_result', 'task', 'x', 'error', '2026-07-27T00:00:00Z'"
                ")"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO acceptance_records ("
                "  id, project_id, release_candidate_hash, disposition, created_at"
                ") VALUES ("
                "  'acc_bad', 'prj_1', 'rc_hash', 'MAYBE', '2026-07-27T00:00:00Z'"
                ")"
            )


def test_context_packages_primary_key_rejects_duplicate_id(tmp_path: Path) -> None:
    db_path = tmp_path / "pk.db"
    assert migrate(str(db_path)) >= 86

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO context_packages ("
            "  id, task_id, manifest_json, content_hash, status, created_at, updated_at"
            ") VALUES ("
            "  'ctx_dup', 'tsk_1', '{}', 'hash1', 'building', "
            "  '2026-07-27T00:00:00Z', '2026-07-27T00:00:00Z'"
            ")"
        )
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO context_packages ("
                "  id, task_id, manifest_json, content_hash, status, created_at, updated_at"
                ") VALUES ("
                "  'ctx_dup', 'tsk_2', '{}', 'hash2', 'ready', "
                "  '2026-07-27T00:00:00Z', '2026-07-27T00:00:00Z'"
                ")"
            )


def test_migration_sequence_is_unique_and_086_lands_once() -> None:
    migrations = _migration_files()
    versions = [version for version, _ in migrations]
    assert versions.count(86) == 1
    assert len(versions) == len(set(versions))
