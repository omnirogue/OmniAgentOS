"""Reference solution — data access layer for widgets."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def create_widget(conn: sqlite3.Connection, widget_id: str, name: str) -> None:
    conn.execute(
        "INSERT INTO widgets (id, name, created_at) VALUES (?, ?, ?)",
        (widget_id, name, _now()),
    )
    conn.commit()


def set_state(conn: sqlite3.Connection, widget_id: str, state: str) -> None:
    conn.execute("UPDATE widgets SET state = ? WHERE id = ?", (state, widget_id))
    conn.commit()


def set_owner(conn: sqlite3.Connection, widget_id: str, owner: str | None) -> None:
    conn.execute("UPDATE widgets SET owner = ? WHERE id = ?", (owner, widget_id))
    conn.commit()


def get_widget(conn: sqlite3.Connection, widget_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, name, state, created_at, owner FROM widgets WHERE id = ?",
        (widget_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "state": row[2],
        "created_at": row[3],
        "owner": row[4],
    }
