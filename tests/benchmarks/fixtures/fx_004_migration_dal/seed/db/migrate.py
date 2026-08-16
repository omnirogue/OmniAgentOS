"""Apply numbered migrations in order. Append-only; never edit an applied file."""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def applied(conn: sqlite3.Connection) -> set[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
    return {row[0] for row in conn.execute("SELECT name FROM schema_migrations")}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Apply every not-yet-applied migration; return the names applied."""
    done = applied(conn)
    ran: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in done:
            continue
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute("INSERT INTO schema_migrations (name) VALUES (?)", (path.name,))
        ran.append(path.name)
    conn.commit()
    return ran
