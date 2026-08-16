"""Migration 118 must apply to the OPERATOR database, not just to fresh ones.

A reverted earlier attempt at C4 shipped ``116_sessions_cost_quality.sql`` and it
reached the operator database before it was reverted from the tree. That database
therefore carries a ``cost_quality`` column on ``sessions`` that no migration in
this repo records, while every freshly-migrated database lacks it. SQLite has no
``ADD COLUMN IF NOT EXISTS`` and the migrator executes plain ``.sql``, so a
``PRAGMA table_info`` guard cannot live inside a migration file.

118 is therefore idempotent BY CONSTRUCTION: it adds only column names that are
absent from BOTH shapes and deliberately never touches ``cost_quality``. This
test is what keeps that true — it drives the real migration file through the real
migrator's statement splitter against both shapes, so a future edit that
re-introduces a colliding column name fails here instead of failing on the
operator database alone.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _iter_sql_statements

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "omniagentos"
    / "db"
    / "migrations"
    / "118_session_cost_estimate.sql"
)

# The packaged post-114 shape, reduced to the columns 118 cares about.
_FRESH_SESSIONS = "id TEXT PRIMARY KEY, cost_usd REAL DEFAULT NULL"
# The operator shape: identical plus the stray column the reverted attempt added,
# copied verbatim from that migration's CREATE TABLE.
_OPERATOR_SESSIONS = (
    _FRESH_SESSIONS
    + ", cost_quality TEXT DEFAULT NULL CHECK (cost_quality IN ('exact', 'estimated', 'unknown'))"
)


def _executable_sql(statement: str) -> str:
    """``statement`` with its ``--`` commentary removed.

    The migrator hands whole comment blocks to ``execute`` along with the DDL,
    and 118's header discusses ``cost_quality`` at length. Only what SQLite acts
    on is what can collide.
    """
    return "\n".join(line for line in statement.splitlines() if not line.lstrip().startswith("--"))


def _apply(columns: str) -> list[str]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(f"CREATE TABLE sessions ({columns})")
        for statement in _iter_sql_statements(MIGRATION.read_text(encoding="utf-8")):
            connection.execute(statement)
        return [str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")]
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("label", "columns"),
    [("fresh", _FRESH_SESSIONS), ("operator", _OPERATOR_SESSIONS)],
)
def test_118_applies_to_both_schema_shapes(label: str, columns: str) -> None:
    names = _apply(columns)

    assert "cost_estimate_usd" in names
    assert "cost_estimate_source" in names
    # The stray column is left exactly as found — neither re-added nor dropped.
    assert ("cost_quality" in names) is (label == "operator")


def test_118_never_touches_cost_quality() -> None:
    """The one column name that would fail on the operator database."""
    body = MIGRATION.read_text(encoding="utf-8")
    statements = _iter_sql_statements(body)

    assert statements, "migration 118 must contain executable SQL"
    for statement in statements:
        assert "cost_quality" not in _executable_sql(statement), (
            "118 must not add or alter cost_quality: the operator database already "
            "has it from a reverted migration, so the ALTER would fail there alone"
        )


def test_118_rejects_a_negative_estimate() -> None:
    """An estimate is dollars spent; a negative one is a bug, not a discount."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(f"CREATE TABLE sessions ({_FRESH_SESSIONS})")
        for statement in _iter_sql_statements(MIGRATION.read_text(encoding="utf-8")):
            connection.execute(statement)
        connection.execute("INSERT INTO sessions (id, cost_estimate_usd) VALUES ('a', 0.18)")
        connection.execute("INSERT INTO sessions (id, cost_estimate_usd) VALUES ('b', NULL)")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO sessions (id, cost_estimate_usd) VALUES ('c', -1.0)")
    finally:
        connection.close()
