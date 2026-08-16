"""Migration 123 — the Team Work OS schema, pinned.

Three properties this file exists to defend:

* every column, table and index the store's SQL names actually EXISTS (a store
  method that queries a missing column fails at request time, not at boot);
* the closed vocabularies in ``omniagentos.team.contracts`` and the CHECK
  constraints in the migration AGREE — two copies of a vocabulary that drift
  produce a store that validates one set and a database that admits another;
* the roster data-fix is guarded, so an operator who already named someone is
  never overwritten by a migration re-run on an older database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate
from omniagentos.db.store import SqliteStore
from omniagentos.team.contracts import (
    ATTRIBUTIONS,
    COMMITMENT_KINDS,
    COMMITMENT_SOURCES,
    COMMITMENT_STATUSES,
    EVIDENCE_KINDS,
    QUALITY_GATES,
    TASK_EVENTS,
)

_NEW_BOARD_COLUMNS = {
    "parent_task_id",
    "goal_id",
    "owner_employee_id",
    "ref",
    "size",
    "acceptance_criteria",
    "blocked_reason",
    "verified_at",
    "verified_by",
    "due_date",
    "source",
}

# Migration 131. Nullable with no backfill on purpose: an existing card has no
# failed verification and no measured automation maturity, and inventing either
# would be a claim the board cannot support.
_ACCOUNTABILITY_BOARD_COLUMNS = {
    "automation_maturity",
    "automation_note",
    "verification_failed_at",
    "verification_failed_by",
    "verification_failed_reason",
}


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {str(row[1]): row for row in connection.execute(f"PRAGMA table_info({table})")}


def _object_sql(connection: sqlite3.Connection, name: str) -> str | None:
    row = connection.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()
    return None if row is None else str(row[0])


@pytest.fixture
def fresh_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "fresh-123.db"
    assert migrate(str(db_path)) >= 123
    return db_path


class TestBoardColumns:
    def test_every_new_board_column_exists(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            columns = _columns(connection, "board_tasks")
        assert _NEW_BOARD_COLUMNS <= columns.keys()

    def test_defaults_keep_pre_123_rows_valid(self, fresh_db: Path) -> None:
        """The NOT NULL additions all carry defaults; the rest are nullable."""
        with sqlite3.connect(fresh_db) as connection:
            columns = _columns(connection, "board_tasks")
        # (name, notnull, default)
        assert (columns["size"][3], columns["size"][4]) == (1, "'M'")
        assert (columns["acceptance_criteria"][3], columns["acceptance_criteria"][4]) == (1, "''")
        assert (columns["blocked_reason"][3], columns["blocked_reason"][4]) == (1, "''")
        assert (columns["source"][3], columns["source"][4]) == (1, "''")
        for nullable in (
            "parent_task_id",
            "goal_id",
            "owner_employee_id",
            "ref",
            "verified_at",
            "verified_by",
            "due_date",
        ):
            assert columns[nullable][3] == 0, nullable

    def test_size_vocabulary_is_enforced(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            now = "2026-08-10T00:00:00Z"
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO board_tasks (id, title, size, created_at, updated_at) "
                    "VALUES ('btk_bad_size', 'x', 'XL', ?, ?)",
                    (now, now),
                )

    def test_company_goals_gained_an_owner(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            assert "owner_employee_id" in _columns(connection, "company_goals")


class TestNewTables:
    def test_all_three_tables_exist(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            names = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
        assert {"task_evidence", "task_events", "prod_snapshots"} <= names

    def test_evidence_is_unique_on_kind_repo_ref(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            now = "2026-08-10T00:00:00Z"
            connection.execute(
                "INSERT INTO task_evidence (id, kind, ref, repo, created_at) "
                "VALUES ('tev_1', 'commit', 'abc123', 'omnios', ?)",
                (now,),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO task_evidence (id, kind, ref, repo, created_at) "
                    "VALUES ('tev_2', 'commit', 'abc123', 'omnios', ?)",
                    (now,),
                )

    def test_snapshot_primary_key_is_day_and_employee(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            keys = [
                str(row[1])
                for row in connection.execute("PRAGMA table_info(prod_snapshots)")
                if row[5]
            ]
        assert keys == ["day", "employee_id"]

    def test_unmeasurable_snapshot_columns_stay_nullable(self, fresh_db: Path) -> None:
        """A day with no sessions is UNMEASURED, not measured-zero."""
        with sqlite3.connect(fresh_db) as connection:
            columns = _columns(connection, "prod_snapshots")
        for nullable in (
            "avg_active_sessions",
            "peak_sessions",
            "merged_prs",
            "first_pass_rate",
            "production_x",
        ):
            assert columns[nullable][3] == 0, nullable
        assert columns["verified_points"][3] == 1


class TestVocabulariesAgreeWithTheSchema:
    """The contracts module and the CHECK constraints are two copies of one
    vocabulary. Drift means the store admits a value the database refuses (or
    the reverse), and only one of those failures is visible before production."""

    def test_evidence_kinds(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            sql = _object_sql(connection, "task_evidence") or ""
        for kind in EVIDENCE_KINDS:
            assert f"'{kind}'" in sql, kind

    def test_attribution_and_quality_gate(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            sql = _object_sql(connection, "task_evidence") or ""
        for value in (*ATTRIBUTIONS, *QUALITY_GATES):
            assert f"'{value}'" in sql, value

    def test_event_vocabulary(self, fresh_db: Path) -> None:
        """Includes 131's ``verify_failed``: the tuple gained it and the CHECK
        was rebuilt to match, so this loop is the drift alarm for both."""
        with sqlite3.connect(fresh_db) as connection:
            sql = _object_sql(connection, "task_events") or ""
        for event in TASK_EVENTS:
            assert f"'{event}'" in sql, event
        assert "verify_failed" in TASK_EVENTS

    def test_the_rebuilt_events_table_still_refuses_an_unknown_event(self, fresh_db: Path) -> None:
        """A widened CHECK must widen, not open: 131 rebuilt this table, and a
        rebuild that dropped the constraint would be invisible above."""
        with sqlite3.connect(fresh_db) as connection:
            now = "2026-08-14T00:00:00Z"
            connection.execute(
                "INSERT INTO board_tasks (id, title, created_at, updated_at) "
                "VALUES ('btk_ev', 'x', ?, ?)",
                (now, now),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO task_events (id, task_id, actor, event, created_at) "
                    "VALUES ('tve_x', 'btk_ev', 'emp_owner', 'telepathy', ?)",
                    (now,),
                )

    def test_an_unknown_evidence_kind_is_refused(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO task_evidence (id, kind, ref, created_at) "
                    "VALUES ('tev_x', 'telepathy', 'r', '2026-08-10T00:00:00Z')"
                )


class TestIndexes:
    def test_every_declared_index_exists(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            names = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
            }
        assert {
            "idx_board_tasks_parent",
            "idx_board_tasks_owner_status",
            "idx_board_tasks_goal",
            "idx_board_tasks_ref",
            "idx_evidence_task",
            "idx_evidence_unattr",
            "idx_task_events_task",
        } <= names

    def test_ref_index_is_unique_and_partial(self, fresh_db: Path) -> None:
        """Partial, not a plain UNIQUE column: a blank/absent ref must not
        collide with the next card that also has none."""
        with sqlite3.connect(fresh_db) as connection:
            sql = _object_sql(connection, "idx_board_tasks_ref") or ""
            assert "UNIQUE" in sql.upper()
            assert "WHERE ref IS NOT NULL" in sql
            now = "2026-08-10T00:00:00Z"
            for card_id in ("btk_no_ref_1", "btk_no_ref_2"):
                connection.execute(
                    "INSERT INTO board_tasks (id, title, created_at, updated_at) "
                    "VALUES (?, 'x', ?, ?)",
                    (card_id, now, now),
                )
            connection.execute(
                "INSERT INTO board_tasks (id, title, ref, created_at, updated_at) "
                "VALUES ('btk_ref_1', 'x', 'PR-1', ?, ?)",
                (now, now),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO board_tasks (id, title, ref, created_at, updated_at) "
                    "VALUES ('btk_ref_2', 'x', 'PR-1', ?, ?)",
                    (now, now),
                )

    def test_unattributed_index_is_partial(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            sql = _object_sql(connection, "idx_evidence_unattr") or ""
        assert "WHERE task_id IS NULL" in sql


class TestDeveloperAccountability:
    """Migration 131's additions, held to the same standard as 123's."""

    def test_the_five_new_board_columns_exist_and_are_nullable(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            columns = _columns(connection, "board_tasks")
        assert _ACCOUNTABILITY_BOARD_COLUMNS <= columns.keys()
        for nullable in sorted(_ACCOUNTABILITY_BOARD_COLUMNS):
            assert columns[nullable][3] == 0, nullable
            assert columns[nullable][4] is None, nullable

    def test_the_commitments_table_and_its_vocabularies(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            sql = _object_sql(connection, "team_commitments") or ""
            columns = _columns(connection, "team_commitments")
        for value in (*COMMITMENT_KINDS, *COMMITMENT_STATUSES, *COMMITMENT_SOURCES):
            assert f"'{value}'" in sql, value
        # resolved_by distinguishes the deterministic morning pass from an
        # operator ruling — without it both read as the same row.
        assert {"resolved_by", "carried_from", "expected_outcome"} <= columns.keys()

    def test_one_commitment_per_person_day_and_card(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            now = "2026-08-14T00:00:00Z"
            connection.execute(
                "INSERT INTO employees (id, name, status, created_at) "
                "VALUES ('emp_x', 'X', 'active', ?)",
                (now,),
            )
            connection.execute(
                "INSERT INTO board_tasks (id, title, created_at, updated_at) "
                "VALUES ('btk_c', 'x', ?, ?)",
                (now, now),
            )
            insert = (
                "INSERT INTO team_commitments "
                "(id, day, employee_id, task_id, kind, title, created_at, updated_at) "
                "VALUES (?, '2026-08-14', 'emp_x', ?, ?, 'x', ?, ?)"
            )
            connection.execute(insert, ("tcm_1", "btk_c", "task", now, now))
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert, ("tcm_2", "btk_c", "task", now, now))
            # NULL task_ids never collide: the index is PARTIAL.
            connection.execute(insert, ("tcm_3", None, "task", now, now))
            connection.execute(insert, ("tcm_4", None, "improvement", now, now))
            with pytest.raises(sqlite3.IntegrityError):
                # ...but there is exactly ONE improvement slot per person-day.
                connection.execute(insert, ("tcm_5", None, "improvement", now, now))

    def test_a_commitment_survives_the_purge_of_its_card(self, fresh_db: Path) -> None:
        """ON DELETE SET NULL, same rationale as task_evidence: purging a card
        must not destroy the record that somebody committed to it."""
        with sqlite3.connect(fresh_db) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            now = "2026-08-14T00:00:00Z"
            connection.execute(
                "INSERT INTO employees (id, name, status, created_at) "
                "VALUES ('emp_y', 'Y', 'active', ?)",
                (now,),
            )
            connection.execute(
                "INSERT INTO board_tasks (id, title, created_at, updated_at) "
                "VALUES ('btk_gone', 'x', ?, ?)",
                (now, now),
            )
            connection.execute(
                "INSERT INTO team_commitments "
                "(id, day, employee_id, task_id, kind, title, created_at, updated_at) "
                "VALUES ('tcm_keep', '2026-08-14', 'emp_y', 'btk_gone', 'task', 'x', ?, ?)",
                (now, now),
            )
            connection.execute("DELETE FROM board_tasks WHERE id = 'btk_gone'")
            row = connection.execute(
                "SELECT task_id FROM team_commitments WHERE id = 'tcm_keep'"
            ).fetchone()
        assert row == (None,)

    def test_the_commitment_indexes_exist_and_are_partial(self, fresh_db: Path) -> None:
        with sqlite3.connect(fresh_db) as connection:
            names = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
            }
            by_task = _object_sql(connection, "idx_team_commitments_day_task") or ""
            # Migration 133 replaced 132's single-improvement index with a
            # per-SLOT one covering both slotted kinds (three automation slots a
            # day, the operator 2026-08-14).
            by_slot = _object_sql(connection, "idx_team_commitments_day_slot") or ""
        assert {
            "idx_team_commitments_day_task",
            "idx_team_commitments_day_slot",
            "idx_team_commitments_employee_day",
        } <= names
        assert "idx_team_commitments_day_improvement" not in names, (
            "133 replaced it; two overlapping slot rules would disagree"
        )
        assert "WHERE task_id IS NOT NULL" in by_task
        assert "(day, employee_id, kind, slot)" in by_slot
        assert "WHERE kind IN ('improvement', 'automation')" in by_slot


class TestRosterDataFix:
    def test_roles_are_set_and_existing_titles_survive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The UPDATEs are guarded on NULL/'' — a migration re-run on a live
        database must not overwrite a title an operator chose."""
        db_path = tmp_path / "upgrade-123.db"
        monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(122))
        assert migrate(str(db_path)) == 122
        with sqlite3.connect(db_path) as connection:
            now = "2026-08-09T00:00:00Z"
            connection.executemany(
                "INSERT INTO employees (id, name, role, status, created_at) "
                "VALUES (?, ?, ?, 'active', ?)",
                [
                    ("emp_owner", "the operator", None, now),
                    ("emp_alice", "Alice", "", now),
                    ("emp_bob", "Bob", "principal-engineer", now),
                    ("emp_frank", "Frank", None, now),
                ],
            )
            connection.commit()

        monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(123))
        assert migrate(str(db_path)) == 123
        with sqlite3.connect(db_path) as connection:
            roles = {
                str(row[0]): row[1] for row in connection.execute("SELECT id, role FROM employees")
            }
        assert roles["emp_owner"] == "operator"
        assert roles["emp_alice"] == "reviewer-merger"
        # Already titled: untouched, not re-titled by the migration.
        assert roles["emp_bob"] == "principal-engineer"
        # Not named by the data fix: left exactly as 098 seeded it.
        assert roles["emp_frank"] is None


class TestReMigrationIsANoOp:
    def test_second_construction_applies_nothing_and_passes_checksums(self, tmp_path: Path) -> None:
        db_path = tmp_path / "twice.db"
        first = SqliteStore(str(db_path))
        with sqlite3.connect(db_path) as connection:
            applied = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
        # A second store on the same file re-runs migrate_connection: it must
        # find every version applied, re-verify the checksums, and add nothing.
        second = SqliteStore(str(db_path))
        with sqlite3.connect(db_path) as connection:
            again = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
            version_123 = connection.execute(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = 123"
            ).fetchone()[0]
        assert again == applied
        assert version_123 == 1
        assert first is not second
