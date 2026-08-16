"""Migration B — company goals spine (JG2-BE, migration 098).

The FINAL-plan SQL for Migration B was lost; ``098_company_goals.sql`` is
RE-DERIVED from the surviving constraint set. These tests are the binding
contract for that re-derivation: far-side FK refusal (bad ``org_company_id``
raises ``sqlite3.IntegrityError``), COALESCE-style partial link uniqueness
(092's idiom), and fresh-DB vs upgraded-DB schema equality (GMP-6 ritual).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate

MIGRATION_VERSION = 98
MIGRATION_NAME = "098_company_goals.sql"
NEW_TABLES = ("employees", "company_goals", "company_goal_jira_links")
NOW = "2026-07-31T00:00:00Z"


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _columns(connection: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    return {
        str(row["name"]): row
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def _seed_company(connection: sqlite3.Connection, company_id: str = "co_seed") -> str:
    connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?,?,?,?,?)",
        (company_id, company_id.replace("co_", ""), company_id, "active", NOW),
    )
    return company_id


def _insert_goal(
    connection: sqlite3.Connection,
    goal_id: str,
    org_company_id: str,
    *,
    horizon: str = "long_term",
    parent_goal_id: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO company_goals "
        "(id, org_company_id, title, horizon, parent_goal_id, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (goal_id, org_company_id, goal_id, horizon, parent_goal_id, "active", NOW, NOW),
    )


def _migrated_db(tmp_path: Path, name: str = "company-goals.db") -> str:
    db_path = str(tmp_path / name)
    assert migrate(db_path) >= MIGRATION_VERSION
    return db_path


# ---------------------------------------------------------------------------
# Packaging / upgrade path
# ---------------------------------------------------------------------------


def test_098_is_packaged_under_its_own_name() -> None:
    packaged = dict(_migration_files())
    assert MIGRATION_VERSION in packaged, "098_company_goals.sql must be packaged"
    assert packaged[MIGRATION_VERSION].name == MIGRATION_NAME


def test_098_adds_company_goal_tables_to_a_v097_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrade path: a populated v097 DB gains the three tables, nothing else moves."""
    db_path = str(tmp_path / "upgrade-098.db")
    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(97))
    assert migrate(db_path) == 97

    connection = _connect(db_path)
    try:
        assert not (set(NEW_TABLES) & _tables(connection))
        _seed_company(connection, "co_pre098")
    finally:
        connection.close()

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(98))
    assert migrate(db_path) == 98

    connection = _connect(db_path)
    try:
        assert set(NEW_TABLES) <= _tables(connection)
        # Pre-existing row survives the upgrade.
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM org_companies WHERE id = 'co_pre098'"
            ).fetchone()[0]
            == 1
        )
        employees = _columns(connection, "employees")
        assert set(employees) == {
            "id",
            "name",
            "role",
            "jira_account_id",
            "status",
            "created_at",
        }
        assert employees["id"]["pk"] == 1
        assert employees["name"]["notnull"] == 1
        assert employees["jira_account_id"]["notnull"] == 0
        assert employees["status"]["notnull"] == 1
        assert str(employees["status"]["dflt_value"]).strip("'") == "active"
        assert employees["created_at"]["notnull"] == 1

        goals = _columns(connection, "company_goals")
        assert set(goals) == {
            "id",
            "org_company_id",
            "title",
            "horizon",
            "parent_goal_id",
            "status",
            "created_at",
            "updated_at",
        }
        assert goals["org_company_id"]["notnull"] == 1
        assert goals["title"]["notnull"] == 1
        assert goals["horizon"]["notnull"] == 1
        assert goals["parent_goal_id"]["notnull"] == 0
        assert str(goals["status"]["dflt_value"]).strip("'") == "active"

        links = _columns(connection, "company_goal_jira_links")
        assert set(links) == {
            "id",
            "goal_id",
            "jira_project_key",
            "jira_issue_key",
            "link_kind",
            "created_at",
        }
        assert links["goal_id"]["notnull"] == 1
        assert links["jira_project_key"]["notnull"] == 1
        assert links["jira_issue_key"]["notnull"] == 0
        assert links["link_kind"]["notnull"] == 1
    finally:
        connection.close()


def test_horizon_column_is_check_free(tmp_path: Path) -> None:
    """Horizon vocabulary is validated app-side (repo idiom), not by a schema CHECK."""
    connection = _connect(_migrated_db(tmp_path, "horizon.db"))
    try:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'company_goals'"
        ).fetchone()[0]
        assert "CHECK" not in str(sql).upper()
        company = _seed_company(connection)
        # The DB accepts any string; the service layer is the only gate.
        _insert_goal(connection, "cgl_free", company, horizon="whatever")
    finally:
        connection.close()


def test_org_company_index_exists(tmp_path: Path) -> None:
    connection = _connect(_migrated_db(tmp_path, "index.db"))
    try:
        index_names = [
            str(row["name"])
            for row in connection.execute("PRAGMA index_list(company_goals)").fetchall()
        ]
        columns: set[str] = set()
        for name in index_names:
            columns.update(
                str(row["name"])
                for row in connection.execute(f"PRAGMA index_info('{name}')").fetchall()
            )
        assert "org_company_id" in columns, "company_goals must be indexed by org_company_id"
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Far side: referential integrity actually refuses bad rows
# ---------------------------------------------------------------------------


def test_company_goal_refuses_unknown_org_company(tmp_path: Path) -> None:
    """Far side (decisive): a bogus org_company_id raises sqlite3.IntegrityError."""
    connection = _connect(_migrated_db(tmp_path, "fk-company.db"))
    try:
        company = _seed_company(connection)
        _insert_goal(connection, "cgl_ok", company)

        with pytest.raises(sqlite3.IntegrityError):
            _insert_goal(connection, "cgl_bad", "co_does_not_exist")

        assert (
            connection.execute("SELECT COUNT(*) FROM company_goals").fetchone()[0] == 1
        ), "refused insert must leave no row behind"
    finally:
        connection.close()


def test_parent_goal_fk_refuses_unknown_parent(tmp_path: Path) -> None:
    connection = _connect(_migrated_db(tmp_path, "fk-parent.db"))
    try:
        company = _seed_company(connection)
        _insert_goal(connection, "cgl_parent", company)
        # NULL parent is unconstrained; a real parent resolves.
        _insert_goal(
            connection, "cgl_child", company, horizon="short_term", parent_goal_id="cgl_parent"
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_goal(
                connection, "cgl_orphan", company, horizon="short_term", parent_goal_id="cgl_ghost"
            )
    finally:
        connection.close()


def test_employee_name_is_unique_and_jira_account_id_is_nullable(tmp_path: Path) -> None:
    connection = _connect(_migrated_db(tmp_path, "employees.db"))
    try:
        connection.execute(
            "INSERT INTO employees (id, name, role, jira_account_id, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("emp_owner", "the operator", None, None, "active", NOW),
        )
        connection.execute(
            "INSERT INTO employees (id, name, role, jira_account_id, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("emp_frank", "Frank", None, None, "active", NOW),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO employees (id, name, role, jira_account_id, status, created_at) "
                "VALUES (?,?,?,?,?,?)",
                ("emp_other", "the operator", None, None, "active", NOW),
            )
        unmapped = connection.execute(
            "SELECT COUNT(*) FROM employees WHERE jira_account_id IS NULL"
        ).fetchone()[0]
        assert unmapped == 2, "operator maps jira_account_id later — NULLs must coexist"
    finally:
        connection.close()


def test_jira_link_partial_uniqueness_follows_092_idiom(tmp_path: Path) -> None:
    """COALESCE-style uniqueness: one project-level link, one link per issue."""
    connection = _connect(_migrated_db(tmp_path, "links.db"))
    try:
        company = _seed_company(connection)
        _insert_goal(connection, "cgl_a", company)
        _insert_goal(connection, "cgl_b", company)

        def link(link_id: str, goal_id: str, project: str, issue: str | None) -> None:
            connection.execute(
                "INSERT INTO company_goal_jira_links "
                "(id, goal_id, jira_project_key, jira_issue_key, link_kind, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (link_id, goal_id, project, issue, "project" if issue is None else "issue", NOW),
            )

        link("cgj_1", "cgl_a", "HOO", None)
        with pytest.raises(sqlite3.IntegrityError):
            link("cgj_dup_project", "cgl_a", "HOO", None)
        # Another goal may link the same project.
        link("cgj_2", "cgl_b", "HOO", None)
        # Distinct issues under the same (goal, project) are separate rows.
        link("cgj_3", "cgl_a", "HOO", "HOO-1")
        link("cgj_4", "cgl_a", "HOO", "HOO-2")
        with pytest.raises(sqlite3.IntegrityError):
            link("cgj_dup_issue", "cgl_a", "HOO", "HOO-1")

        # An issue key names its own project, so the SAME issue may not be
        # linked twice to one goal by smuggling it under another project key
        # (index is on (goal_id, jira_issue_key), not (goal, project, issue)).
        with pytest.raises(sqlite3.IntegrityError):
            link("cgj_cross_project_issue", "cgl_a", "ACM", "HOO-1")
        # A DIFFERENT goal may still link that issue.
        link("cgj_5", "cgl_b", "HOO", "HOO-1")

        with pytest.raises(sqlite3.IntegrityError):
            link("cgj_orphan", "cgl_ghost", "ACM", None)

        assert (
            connection.execute("SELECT COUNT(*) FROM company_goal_jira_links").fetchone()[0] == 5
        )
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# GMP-6 ritual: fresh DB and upgraded DB agree
# ---------------------------------------------------------------------------


def _company_goal_schema(db_path: str) -> list[tuple]:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            """
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            WHERE tbl_name IN ('employees', 'company_goals', 'company_goal_jira_links')
            ORDER BY type, name, tbl_name
            """
        ).fetchall()
    finally:
        connection.close()


def test_fresh_and_upgraded_company_goal_schema_are_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = str(tmp_path / "fresh-098.db")
    upgraded = str(tmp_path / "upgraded-098.db")

    assert migrate(fresh) >= MIGRATION_VERSION

    monkeypatch.setattr("omniagentos.db.migrate._migration_files", lambda: _through(97))
    assert migrate(upgraded) == 97
    monkeypatch.undo()
    assert migrate(upgraded) >= MIGRATION_VERSION

    fresh_schema = _company_goal_schema(fresh)
    assert fresh_schema, "migration 098 must create the company-goal objects"
    assert fresh_schema == _company_goal_schema(upgraded)
