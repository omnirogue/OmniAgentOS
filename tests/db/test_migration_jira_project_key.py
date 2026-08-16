"""Migration A — jira_project_key uniqueness (JG1-E7).

SQL is unnumbered under ./migrations-staging/; tests reference tables, not
filenames under omniagentos/db/migrations/.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

STAGING_SQL = (
    Path(__file__).resolve().parents[2] / "migrations-staging" / "jira_project_key.sql"
)


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _bootstrap_projects_table(connection: sqlite3.Connection) -> None:
    """Minimal projects table matching 014_projects core columns."""
    connection.execute(
        """
        CREATE TABLE projects (
            id                TEXT PRIMARY KEY,
            name              TEXT NOT NULL UNIQUE,
            root_dirs_json    TEXT NOT NULL DEFAULT '[]',
            vault_subfolder   TEXT NOT NULL DEFAULT '',
            budget_usd        REAL,
            allowed_tools_json TEXT NOT NULL DEFAULT '[]',
            allowed_dirs_json TEXT NOT NULL DEFAULT '[]',
            created_at        TEXT NOT NULL
        )
        """
    )


def test_jira_project_key_unique_and_nulls_unconstrained(tmp_path: Path) -> None:
    """Far side: IntegrityError on duplicate key; two NULL keys insert OK."""
    assert STAGING_SQL.is_file(), "migrations-staging/jira_project_key.sql must exist"
    db_path = str(tmp_path / "jira_map.db")
    connection = _connect(db_path)
    try:
        _bootstrap_projects_table(connection)
        sql = STAGING_SQL.read_text(encoding="utf-8")
        connection.executescript(sql)

        # Column present.
        cols = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(projects)").fetchall()
        }
        assert "jira_project_key" in cols

        now = "2026-07-30T00:00:00Z"
        connection.execute(
            "INSERT INTO projects (id, name, created_at, jira_project_key) "
            "VALUES (?, ?, ?, ?)",
            ("proj_a", "Alpha", now, "ACM"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO projects (id, name, created_at, jira_project_key) "
                "VALUES (?, ?, ?, ?)",
                ("proj_b", "Beta", now, "ACM"),
            )

        # Multiple NULLs are unconstrained by the partial unique index.
        connection.execute(
            "INSERT INTO projects (id, name, created_at, jira_project_key) "
            "VALUES (?, ?, ?, ?)",
            ("proj_c", "Gamma", now, None),
        )
        connection.execute(
            "INSERT INTO projects (id, name, created_at, jira_project_key) "
            "VALUES (?, ?, ?, ?)",
            ("proj_d", "Delta", now, None),
        )
        nulls = connection.execute(
            "SELECT COUNT(*) AS n FROM projects WHERE jira_project_key IS NULL"
        ).fetchone()["n"]
        assert nulls == 2

        # Distinct live keys allowed.
        connection.execute(
            "INSERT INTO projects (id, name, created_at, jira_project_key) "
            "VALUES (?, ?, ?, ?)",
            ("proj_e", "Epsilon", now, "HOO"),
        )
        keys = {
            row["jira_project_key"]
            for row in connection.execute(
                "SELECT jira_project_key FROM projects WHERE jira_project_key IS NOT NULL"
            ).fetchall()
        }
        assert keys == {"ACM", "HOO"}
        assert keys <= {"ACM", "CA", "INI", "HOO", "OAOS"}
    finally:
        connection.close()
