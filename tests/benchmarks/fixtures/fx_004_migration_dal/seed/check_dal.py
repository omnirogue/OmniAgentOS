"""Visible check. Run: python -m pytest check_dal.py"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))

import dal  # noqa: E402
from migrate import migrate  # noqa: E402


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    migrate(conn)
    return conn


def test_owner_round_trip() -> None:
    conn = _conn()
    dal.create_widget(conn, "w1", "sprocket")
    dal.set_owner(conn, "w1", "owner")
    assert dal.get_widget(conn, "w1")["owner"] == "owner"


def test_owner_defaults_to_none() -> None:
    conn = _conn()
    dal.create_widget(conn, "w2", "cog")
    assert dal.get_widget(conn, "w2")["owner"] is None
