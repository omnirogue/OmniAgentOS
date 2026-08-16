"""FROZEN acceptance check for fx_004_migration_dal.

Gates: the append-only migration discipline was respected, a fresh database
built from migrations really has the column, and the DAL moved with it.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
DB_DIR = ROOT / "db"
MIGRATIONS = DB_DIR / "migrations"
sys.path.insert(0, str(DB_DIR))

import dal  # noqa: E402
from migrate import migrate  # noqa: E402


def _fresh() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


def test_existing_migrations_untouched() -> None:
    init = (MIGRATIONS / "001_init.sql").read_text(encoding="utf-8")
    state = (MIGRATIONS / "002_add_state.sql").read_text(encoding="utf-8")
    schema = (DB_DIR / "schema.sql").read_text(encoding="utf-8")
    assert "owner" not in init.lower(), "001_init.sql was edited (append-only violated)"
    assert "owner" not in state.lower(), "002_add_state.sql was edited (append-only violated)"
    assert "owner" not in schema.lower(), "schema.sql was hand-edited"
    assert "ALTER TABLE widgets ADD COLUMN state" in state


def test_exactly_one_new_migration() -> None:
    names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
    assert len(names) == 3, f"expected 3 migrations, found {names}"
    assert names[0] == "001_init.sql"
    assert names[1] == "002_add_state.sql"
    assert names[2].startswith("003_"), f"third migration is misnumbered: {names[2]}"


def test_new_migration_adds_nullable_owner() -> None:
    third = sorted(MIGRATIONS.glob("003_*.sql"))[0].read_text(encoding="utf-8").lower()
    assert "alter table" in third and "owner" in third
    assert "not null" not in third, "owner must be nullable"


def test_fresh_database_has_owner_column() -> None:
    conn = _fresh()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(widgets)")}
    assert columns == {"id", "name", "state", "created_at", "owner"}


def test_dal_owner_round_trip() -> None:
    conn = _fresh()
    dal.create_widget(conn, "w1", "sprocket")
    widget = dal.get_widget(conn, "w1")
    assert widget is not None
    assert widget["owner"] is None

    dal.set_owner(conn, "w1", "owner")
    widget = dal.get_widget(conn, "w1")
    assert widget is not None
    assert widget["owner"] == "owner"

    dal.set_owner(conn, "w1", None)
    widget = dal.get_widget(conn, "w1")
    assert widget is not None
    assert widget["owner"] is None


def test_existing_dal_behavior_preserved() -> None:
    conn = _fresh()
    dal.create_widget(conn, "w2", "cog")
    widget = dal.get_widget(conn, "w2")
    assert widget is not None
    assert widget["id"] == "w2"
    assert widget["name"] == "cog"
    assert widget["state"] == "new"
    assert widget["created_at"]

    dal.set_state(conn, "w2", "active")
    widget = dal.get_widget(conn, "w2")
    assert widget is not None
    assert widget["state"] == "active"

    assert dal.get_widget(conn, "missing") is None


def test_migration_is_idempotent_across_runs() -> None:
    conn = _fresh()
    assert migrate(conn) == [], "re-running migrate must be a no-op"


@pytest.mark.parametrize("name", ["migrate.py"])
def test_migration_runner_untouched(name: str) -> None:
    source = (DB_DIR / name).read_text(encoding="utf-8")
    assert "owner" not in source, f"db/{name} was modified"
