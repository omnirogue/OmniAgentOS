"""Migration 132 against a POPULATED database — the rebuild that must not lie.

Widening ``task_events``'s CHECK means rebuilding the table, and a table rebuild
is the one migration shape that can silently corrupt an append-only audit trail.
The specific hazard is ROWIDs: ``TeamStore.verify_task`` finds the first verify
event with ``ORDER BY created_at ASC, rowid ASC`` and re-stamps that original
timestamp so a re-verify cannot move a card into a later scoring week. Since
``created_at`` is second-resolution, ``rowid`` is what breaks the tie — a copy
that renumbered rowids by scan order would reorder same-second events and hand
back the wrong "first" verify.

So this file builds a database at pre, writes a same-second trail into it and
DELETES a row to leave a gap in the rowid sequence (a contiguous sequence would
survive even a naive copy, and the test would pass vacuously), migrates, and
asserts the trail came through byte-identically. Falsified against a copy
without the explicit ``rowid`` column: five of these six tests fail, because
the migration's own COUNT/MAX guard aborts the transaction.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate

_SAME_SECOND = "2026-08-10T09:00:00Z"

_EVENT_COLUMNS = "id, task_id, actor, event, from_status, to_status, note, created_at"


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


@pytest.fixture
def populated_pre(tmp_path: Path) -> Path:
    """A pre database with a card, four surviving events, and a rowid GAP.

    The subset patch is scoped to the build with an explicit context manager,
    NOT the ``monkeypatch`` fixture: a fixture-scoped patch would still be in
    place while the test itself calls ``migrate``, so the migration under test
    would never be applied and every assertion below would pass vacuously.
    """
    db_path = tmp_path / "populated-130.db"
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr("omniagentos.db.migrate._migration_files", lambda: _through(130))
        assert migrate(str(db_path)) == 130
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO board_tasks (id, title, status, created_at, updated_at) "
            "VALUES ('btk_trail', 'A card with history', 'done', ?, ?)",
            (_SAME_SECOND, _SAME_SECOND),
        )
        connection.executemany(
            f"INSERT INTO task_events ({_EVENT_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                ("tve_1", "btk_trail", "emp_owner", "create", None, None, "made it", _SAME_SECOND),
                ("tve_2", "btk_trail", "emp_owner", "assign", None, None, "owner", _SAME_SECOND),
                (
                    "tve_3",
                    "btk_trail",
                    "emp_bob",
                    "status_change",
                    "open",
                    "done",
                    "",
                    _SAME_SECOND,
                ),
                ("tve_4", "btk_trail", "emp_alice", "verify", "done", "done", "human", _SAME_SECOND),
                (
                    "tve_5",
                    "btk_trail",
                    "emp_alice",
                    "unverify",
                    None,
                    None,
                    "emp_alice",
                    "2026-08-11T09:00:00Z",
                ),
            ],
        )
        # A GAP in the rowid sequence is what makes this fixture discriminating:
        # with contiguous rowids a naive copy renumbers to the same values and
        # the test passes vacuously. Deleting the second row leaves 1,3,4,5 —
        # which a scan-order copy would silently rewrite to 1,2,3,4.
        connection.execute("DELETE FROM task_events WHERE id = 'tve_2'")
        connection.commit()
    return db_path


def _events(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return list(
        connection.execute(f"SELECT rowid, {_EVENT_COLUMNS} FROM task_events ORDER BY rowid")
    )


def _foreign_key_violations(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return list(connection.execute("PRAGMA foreign_key_check"))


class TestTheRebuildPreservesTheTrail:
    def test_every_row_survives_with_its_rowid_and_content(self, populated_pre: Path) -> None:
        with sqlite3.connect(populated_pre) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            before = _events(connection)
            assert _foreign_key_violations(connection) == []

        # >=, not ==: later migrations keep landing on main, and pinning the
        # head version here would make every one of them fail this file.
        assert migrate(str(populated_pre)) >= 132

        with sqlite3.connect(populated_pre) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            after = _events(connection)
            # round-3 §9: the rebuild must not leave a dangling child row.
            assert _foreign_key_violations(connection) == []
        assert after == before, "rowids AND row content must both survive the copy"

    def test_the_first_verify_lookup_still_finds_the_original(self, populated_pre: Path) -> None:
        """The behaviour the rowid preservation exists for: with three rows
        sharing one second, ``ORDER BY created_at, rowid`` must still return
        the verify that actually happened first."""
        migrate(str(populated_pre))
        with sqlite3.connect(populated_pre) as connection:
            row = connection.execute(
                "SELECT id, created_at FROM task_events WHERE task_id = 'btk_trail' "
                "AND event = 'verify' ORDER BY created_at ASC, rowid ASC LIMIT 1"
            ).fetchone()
        assert row == ("tve_4", _SAME_SECOND)

    def test_the_index_is_recreated(self, populated_pre: Path) -> None:
        """A DROP takes the old table's indexes with it; the queue reads that
        scan a card's trail depend on this one."""
        migrate(str(populated_pre))
        with sqlite3.connect(populated_pre) as connection:
            sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'idx_task_events_task'"
            ).fetchone()
        assert sql is not None
        assert "task_events(task_id, created_at)" in str(sql[0])

    def test_the_widened_check_admits_verify_failed_and_still_refuses_nonsense(
        self, populated_pre: Path
    ) -> None:
        migrate(str(populated_pre))
        with sqlite3.connect(populated_pre) as connection:
            connection.execute(
                f"INSERT INTO task_events ({_EVENT_COLUMNS}) "
                "VALUES ('tve_6', 'btk_trail', 'emp_alice', 'verify_failed', 'done', 'done', "
                "'no tests', '2026-08-12T09:00:00Z')"
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT INTO task_events ({_EVENT_COLUMNS}) "
                    "VALUES ('tve_7', 'btk_trail', 'emp_alice', 'telepathy', NULL, NULL, '', "
                    "'2026-08-12T09:00:00Z')"
                )

    def test_the_cascade_still_binds_after_the_rename(self, populated_pre: Path) -> None:
        """The rebuilt child keeps ``ON DELETE CASCADE``: purging an archived
        card must not fail on a dangling reference."""
        migrate(str(populated_pre))
        with sqlite3.connect(populated_pre) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM board_tasks WHERE id = 'btk_trail'")
            remaining = connection.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = 'btk_trail'"
            ).fetchone()[0]
        assert remaining == 0


class TestFreshDatabase:
    def test_migrate_reports_132(self, tmp_path: Path) -> None:
        assert migrate(str(tmp_path / "fresh.db")) >= 131
