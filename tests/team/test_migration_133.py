"""Migration 133 against a POPULATED database — the commitments rebuild.

Widening ``team_commitments.kind`` means rebuilding the table, and this table is
the accountability RECORD: a rebuild that dropped or altered a row would erase a
promise somebody made, silently and irreversibly. So this file builds a database
at v132, writes commitments into it, migrates, and asserts every row came
through with its content intact and ``slot`` backfilled to 1.

Rowid preservation is deliberately NOT asserted: nothing reads this table by
rowid (``list_commitments`` orders by day/employee/kind/slot/created_at, with
rowid only as a last-resort tiebreak between rows already distinguished by
their slot). Contrast ``tests/team/test_migration_132.py``, where the
``task_events`` rebuild's rowid order IS load-bearing and is asserted
byte-for-byte.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import _migration_files, migrate

_NOW = "2026-08-14T09:00:00Z"

_COMMITMENT_COLUMNS = (
    "id, day, employee_id, task_id, kind, title, expected_outcome, status, source, "
    "carried_from, resolved_at, resolved_by, resolution_note, created_at, updated_at"
)


def _through(version: int) -> list[tuple[int, Path]]:
    return [(number, path) for number, path in _migration_files() if number <= version]


@pytest.fixture
def populated_v132(tmp_path: Path) -> Path:
    """A v132 database with a card, a MISSED->CARRIED chain, and an improvement.

    The subset patch is scoped to the BUILD with an explicit context manager,
    never the ``monkeypatch`` fixture: a fixture-scoped patch would still be in
    place while the test itself calls ``migrate``, so the migration under test
    would never be applied and every assertion below would pass vacuously.
    """
    db_path = tmp_path / "populated-132.db"
    with pytest.MonkeyPatch.context() as patched:
        patched.setattr("omniagentos.db.migrate._migration_files", lambda: _through(132))
        assert migrate(str(db_path)) == 132
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO employees (id, name, status, created_at) "
            "VALUES ('emp_x', 'X', 'active', ?)",
            (_NOW,),
        )
        connection.execute(
            "INSERT INTO board_tasks (id, title, created_at, updated_at) "
            "VALUES ('btk_1', 'A card', ?, ?)",
            (_NOW, _NOW),
        )
        connection.executemany(
            f"INSERT INTO team_commitments ({_COMMITMENT_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "tcm_task",
                    "2026-08-13",
                    "emp_x",
                    "btk_1",
                    "task",
                    "Finish the card",
                    "done with evidence",
                    "missed",
                    "auto",
                    None,
                    _NOW,
                    "system",
                    "did not reach done",
                    _NOW,
                    _NOW,
                ),
                (
                    "tcm_carried",
                    "2026-08-14",
                    "emp_x",
                    "btk_1",
                    "task",
                    "Finish the card",
                    "carried from 2026-08-13: done with evidence",
                    "committed",
                    "auto",
                    # THE PRODUCTION SHAPE: a carry chain. Any database that has
                    # ever missed a card commitment has one of these, and a
                    # rebuild whose new table points its self-reference at the
                    # OLD table dies right here on the DROP.
                    "tcm_task",
                    None,
                    None,
                    "",
                    _NOW,
                    _NOW,
                ),
                (
                    "tcm_imp",
                    "2026-08-13",
                    "emp_x",
                    None,
                    "improvement",
                    "One significant OmniAgentOS improvement",
                    "an evidence-backed card",
                    "delivered",
                    "auto",
                    None,
                    _NOW,
                    "system",
                    "GH-1 something (size M)",
                    _NOW,
                    _NOW,
                ),
            ],
        )
        connection.commit()
    return db_path


def _rows(connection: sqlite3.Connection) -> list[tuple[object, ...]]:
    return list(
        connection.execute(f"SELECT {_COMMITMENT_COLUMNS} FROM team_commitments ORDER BY id")
    )


class TestTheRebuildPreservesEveryPromise:
    def test_every_row_survives_with_its_content(self, populated_v132: Path) -> None:
        with sqlite3.connect(populated_v132) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            before = _rows(connection)
            assert list(connection.execute("PRAGMA foreign_key_check")) == []

        assert migrate(str(populated_v132)) >= 133

        with sqlite3.connect(populated_v132) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            after = _rows(connection)
            assert list(connection.execute("PRAGMA foreign_key_check")) == []
            slots = dict(connection.execute("SELECT id, slot FROM team_commitments"))
        assert after == before
        assert slots == {"tcm_task": 1, "tcm_carried": 1, "tcm_imp": 1}, (
            "slot backfills to 1, never to NULL"
        )

    def test_the_commitment_still_survives_the_purge_of_its_card(
        self, populated_v132: Path
    ) -> None:
        """``ON DELETE SET NULL`` must survive the rebuild: purging a card must
        not destroy the record that somebody committed to it."""
        migrate(str(populated_v132))
        with sqlite3.connect(populated_v132) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("DELETE FROM board_tasks WHERE id = 'btk_1'")
            row = connection.execute(
                "SELECT task_id FROM team_commitments WHERE id = 'tcm_task'"
            ).fetchone()
        assert row == (None,)


class TestTheWidenedVocabularyAndTheSlotRule:
    def test_automation_is_admitted_and_nonsense_still_is_not(self, populated_v132: Path) -> None:
        migrate(str(populated_v132))
        with sqlite3.connect(populated_v132) as connection:
            connection.execute(
                f"INSERT INTO team_commitments ({_COMMITMENT_COLUMNS}, slot) "
                "VALUES ('tcm_auto', '2026-08-14', 'emp_x', NULL, 'automation', 'a', 'b', "
                "'committed', 'auto', NULL, NULL, NULL, '', ?, ?, 1)",
                (_NOW, _NOW),
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT INTO team_commitments ({_COMMITMENT_COLUMNS}, slot) "
                    "VALUES ('tcm_bad', '2026-08-14', 'emp_x', NULL, 'telepathy', 'a', 'b', "
                    "'committed', 'auto', NULL, NULL, NULL, '', ?, ?, 1)",
                    (_NOW, _NOW),
                )

    def test_one_row_per_slot_and_three_slots_coexist(self, populated_v132: Path) -> None:
        """The rule that makes the generator idempotent AND caps the day at
        three automations: a re-run collides with its own rows."""
        migrate(str(populated_v132))
        # Its own day: the fixture's carry chain already occupies 2026-08-14.
        insert = (
            f"INSERT INTO team_commitments ({_COMMITMENT_COLUMNS}, slot) "
            "VALUES (?, '2026-08-20', 'emp_x', NULL, ?, 'a', 'b', 'committed', 'auto', "
            f"NULL, NULL, NULL, '', '{_NOW}', '{_NOW}', ?)"
        )
        with sqlite3.connect(populated_v132) as connection:
            for slot in (1, 2, 3):
                connection.execute(insert, (f"tcm_a{slot}", "automation", slot))
            # A fourth row in an occupied slot is refused...
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert, ("tcm_a_dupe", "automation", 2))
            # ...while the improvement slot is a SEPARATE kind, so slot 1 of it
            # coexists with slot 1 of automation.
            connection.execute(insert, ("tcm_i1", "improvement", 1))
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(insert, ("tcm_i_dupe", "improvement", 1))
            count = connection.execute(
                "SELECT COUNT(*) FROM team_commitments WHERE day = '2026-08-20'"
            ).fetchone()[0]
        assert count == 4

    def test_task_rows_are_untouched_by_the_slot_rule(self, populated_v132: Path) -> None:
        """The slot index is PARTIAL: two task commitments for one person-day
        (different cards) must still coexist, and they all carry slot 1."""
        migrate(str(populated_v132))
        with sqlite3.connect(populated_v132) as connection:
            connection.execute(
                "INSERT INTO board_tasks (id, title, created_at, updated_at) "
                "VALUES ('btk_2', 'Another', ?, ?)",
                (_NOW, _NOW),
            )
            for identifier, card in (("tcm_t1", "btk_1"), ("tcm_t2", "btk_2")):
                connection.execute(
                    f"INSERT INTO team_commitments ({_COMMITMENT_COLUMNS}, slot) "
                    "VALUES (?, '2026-08-20', 'emp_x', ?, 'task', 'a', 'b', 'committed', "
                    f"'auto', NULL, NULL, NULL, '', '{_NOW}', '{_NOW}', 1)",
                    (identifier, card),
                )
            # ...but the same card twice in one person-day is still refused.
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT INTO team_commitments ({_COMMITMENT_COLUMNS}, slot) "
                    "VALUES ('tcm_t3', '2026-08-20', 'emp_x', 'btk_1', 'task', 'a', 'b', "
                    f"'committed', 'auto', NULL, NULL, NULL, '', '{_NOW}', '{_NOW}', 1)"
                )


class TestFreshDatabase:
    def test_migrate_reports_133(self, tmp_path: Path) -> None:
        assert migrate(str(tmp_path / "fresh.db")) >= 133


class TestTheCarryChainSurvives:
    """The BLOCKER this migration shipped with once: the rebuilt table declared
    ``carried_from REFERENCES team_commitments(id)`` — the OLD table, dropped
    three statements later. With ``PRAGMA foreign_keys=ON`` (the migration
    runner's state) that DROP performs an implicit DELETE of the parent rows,
    and every copied carry chain fails the constraint, aborting the migration.
    A database that has never missed a commitment has no chain and migrates
    cleanly, which is exactly why the original tests missed it."""

    def test_the_migration_succeeds_and_the_link_survives(self, populated_v132: Path) -> None:
        with sqlite3.connect(populated_v132) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            assert connection.execute(
                "SELECT carried_from FROM team_commitments WHERE id = 'tcm_carried'"
            ).fetchone() == ("tcm_task",), "precondition: the fixture carries a chain"

        assert migrate(str(populated_v132)) >= 133

        with sqlite3.connect(populated_v132) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            assert connection.execute(
                "SELECT carried_from FROM team_commitments WHERE id = 'tcm_carried'"
            ).fetchone() == ("tcm_task",)
            assert list(connection.execute("PRAGMA foreign_key_check")) == []

    def test_the_reference_is_self_referential_after_the_rename(self, populated_v132: Path) -> None:
        """Declared against the rebuild's temporary name; SQLite rewrites it to
        the final one during the RENAME, so the shipped schema self-references."""
        migrate(str(populated_v132))
        with sqlite3.connect(populated_v132) as connection:
            sql = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'team_commitments'"
                ).fetchone()[0]
            )
        assert 'carried_from     TEXT REFERENCES "team_commitments"(id)' in sql
        # The temporary name survives only inside the explanatory comment SQLite
        # keeps with the DDL — never in a constraint.
        constraints = [line for line in sql.splitlines() if not line.strip().startswith("--")]
        assert not [line for line in constraints if "team_commitments_133" in line]

    def test_a_dangling_carry_link_is_still_refused_afterwards(self, populated_v132: Path) -> None:
        """Self-referential means ENFORCED, not merely spelled: the constraint
        must still refuse a link to a commitment that does not exist."""
        migrate(str(populated_v132))
        with sqlite3.connect(populated_v132) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT INTO team_commitments ({_COMMITMENT_COLUMNS}, slot) "
                    "VALUES ('tcm_dangling', '2026-08-15', 'emp_x', NULL, 'improvement', 'a', "
                    "'b', 'carried', 'auto', 'tcm_nonexistent', NULL, NULL, '', ?, ?, 1)",
                    (_NOW, _NOW),
                )


class TestTheSlotIsBounded:
    """The unique index stops a DUPLICATE slot; only the CHECK stops a slot that
    was never promised. Slot 0 and -1 matter beyond bookkeeping: they reach
    Python's list indexing in the resolver, where they would select the LAST
    qualifying card rather than fail."""

    @pytest.mark.parametrize("slot", [-1, 0, 4, 99])
    def test_an_out_of_range_automation_slot_is_refused(
        self, populated_v132: Path, slot: int
    ) -> None:
        migrate(str(populated_v132))
        with sqlite3.connect(populated_v132) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT INTO team_commitments ({_COMMITMENT_COLUMNS}, slot) "
                    "VALUES ('tcm_bad_slot', '2026-08-14', 'emp_x', NULL, 'automation', 'a', "
                    "'b', 'committed', 'auto', NULL, NULL, NULL, '', ?, ?, ?)",
                    (_NOW, _NOW, slot),
                )

    @pytest.mark.parametrize("kind", ["task", "improvement"])
    def test_an_unslotted_kind_is_pinned_to_slot_one(self, populated_v132: Path, kind: str) -> None:
        migrate(str(populated_v132))
        with sqlite3.connect(populated_v132) as connection:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    f"INSERT INTO team_commitments ({_COMMITMENT_COLUMNS}, slot) "
                    "VALUES ('tcm_slotted', '2026-08-14', 'emp_x', NULL, ?, 'a', "
                    "'b', 'committed', 'auto', NULL, NULL, NULL, '', ?, ?, 2)",
                    (kind, _NOW, _NOW),
                )
