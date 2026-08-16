"""Read-only SQL views the frozen LabStore interface does not expose in bulk.

`contracts/lab-interfaces.md` §L01-domain intentionally offers only narrow,
single-key reads (``get_elo(subject, config_id)``, ``leaderboard(subject)``) --
there is no "list every subject" or "top-N configs by elo" method, because L01
is not the leaderboard's writer. `omniagentos.lab.contracts.LeaderboardEntry`
documents the leaderboard table as "Written/refreshed by the 2x-daily
Sonnet-high curation loop" -- i.e. THIS package. Doing that job means reading
the underlying `elo_ratings` / `tournaments` / `matches` tables directly.

L01's own test suite (``tests/lab/db/test_store.py``) already reads
``store._connection`` directly for assertions, so doing the same here for
read-only SELECTs mirrors an established, precedented pattern in this exact
codebase rather than reaching past a real encapsulation boundary. Every
function below is SELECT-only and takes the store's connection lock for the
duration of the read (matching `LabStore`'s own `_serialized` discipline) so a
concurrent write is never observed half-applied.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from omniagentos.lab.db import LabStore


def _connection(store: LabStore) -> sqlite3.Connection:
    return store._connection  # noqa: SLF001 -- see module docstring


def _read[T](store: LabStore, fn: Callable[[sqlite3.Connection], T]) -> T:
    with store._store._lock:  # noqa: SLF001 -- see module docstring
        return fn(_connection(store))


def distinct_subjects(store: LabStore) -> list[dict[str, str]]:
    """Every ``(subject, discipline)`` pair observed in `tournaments` or an
    existing `leaderboard`, sorted by subject. `tournaments` is treated as the
    authoritative source for a subject's discipline when both tables know it."""

    def query(conn: sqlite3.Connection) -> list[dict[str, str]]:
        by_subject: dict[str, str] = {}
        for row in conn.execute(
            "SELECT DISTINCT subject, discipline FROM leaderboard ORDER BY subject ASC"
        ).fetchall():
            by_subject[str(row["subject"])] = str(row["discipline"])
        for row in conn.execute(
            "SELECT DISTINCT subject, discipline FROM tournaments ORDER BY subject ASC"
        ).fetchall():
            by_subject[str(row["subject"])] = str(row["discipline"])
        return [
            {"subject": subject, "discipline": discipline}
            for subject, discipline in sorted(by_subject.items())
        ]

    return _read(store, query)


def top_elo(store: LabStore, subject: str, limit: int) -> list[dict[str, Any]]:
    """The top *limit* configs for *subject*, ranked by rating desc (ties broken
    by config_id ascending for determinism) -- "top orchestrations by ELO"."""

    def query(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT subject, config_id, rating, matches, wins, losses, draws, updated_at "
            "FROM elo_ratings WHERE subject = ? "
            "ORDER BY rating DESC, config_id ASC LIMIT ?",
            (subject, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    return _read(store, query)


def recent_judge_notes(store: LabStore, subject: str, config_id: str, limit: int = 3) -> list[str]:
    """Up to *limit* most recent, distinct, non-empty judge notes for
    *config_id* across all of *subject*'s tournaments (most recent first).
    Ties on ``created_at`` (whole-second resolution) are broken by SQLite's
    implicit rowid, which reflects true insertion order."""

    def query(conn: sqlite3.Connection) -> list[str]:
        tournament_ids = [
            str(row["id"])
            for row in conn.execute(
                "SELECT id FROM tournaments WHERE subject = ?", (subject,)
            ).fetchall()
        ]
        if not tournament_ids:
            return []
        placeholders = ",".join("?" for _ in tournament_ids)
        rows = conn.execute(
            f"SELECT judge_notes FROM matches "
            f"WHERE tournament_id IN ({placeholders}) "
            "AND (config_a = ? OR config_b = ?) AND judge_notes != '' "
            "ORDER BY created_at DESC, rowid DESC",
            (*tournament_ids, config_id, config_id),
        ).fetchall()
        notes: list[str] = []
        for row in rows:
            note = str(row["judge_notes"])
            if note not in notes:
                notes.append(note)
            if len(notes) >= limit:
                break
        return notes

    return _read(store, query)


def recent_tournaments(store: LabStore, subject: str, limit: int = 5) -> list[dict[str, Any]]:
    """The *limit* most recently created tournaments for *subject*."""

    def query(conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute(
            "SELECT id, subject, discipline, status, winner_config_id, created_at "
            "FROM tournaments WHERE subject = ? ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (subject, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    return _read(store, query)
