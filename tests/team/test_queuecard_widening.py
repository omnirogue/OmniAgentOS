"""QueueCard's v4 additive fields (source + due_date) parse tolerantly.

The widening replaces notify's old ``_due_dates`` direct SELECT: the queue
projections themselves now carry the deadline and the Work-vs-Tasks
discriminator, and rows from older SELECTs that never projected either column
must still render as ordinary cards.
"""

from __future__ import annotations

from collections.abc import Callable

from omniagentos.collab.contracts import BoardTask
from omniagentos.team.contracts import TASK_ADHOC_SOURCE, QueueCard
from omniagentos.team.store import TeamStore


class TestTolerantParsing:
    def test_a_row_without_the_new_columns_still_parses(self) -> None:
        card = QueueCard.from_row(
            {"id": "btk_1", "title": "Old shape", "ref": None, "status": "open", "size": "M"}
        )
        assert card.source is None
        assert card.due_date is None

    def test_projected_columns_carry_through(self) -> None:
        card = QueueCard.from_row(
            {
                "id": "btk_2",
                "title": "New shape",
                "ref": "T1",
                "status": "open",
                "size": "S",
                "source": TASK_ADHOC_SOURCE,
                "due_date": "2026-08-14T10:00-04:00",
            }
        )
        assert card.source == TASK_ADHOC_SOURCE
        assert card.due_date == "2026-08-14T10:00-04:00"

    def test_empty_strings_read_as_absent(self) -> None:
        # Migration 123 defaults source to '' — an unstamped Work card must not
        # surface a distracting empty-string "source" on the wire.
        card = QueueCard.from_row(
            {
                "id": "btk_3",
                "title": "Blank",
                "ref": None,
                "status": "open",
                "size": "M",
                "source": "",
                "due_date": "",
            }
        )
        assert card.source is None
        assert card.due_date is None


class TestStoreProjection:
    def test_pool_cards_carry_due_date(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        goals_store,
        store,
    ) -> None:
        from omniagentos.contracts import utc_now_iso

        store._connection.execute(
            "INSERT INTO org_companies (id, slug, name, status, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("co_acme", "acme", "ACME", "active", utc_now_iso()),
        )
        goal = goals_store.create_goal(
            org_company_id="co_acme", title="General engineering", horizon="quarter"
        )
        make_card(
            title="Pool card",
            ref="Q2",
            goal_id=str(goal["id"]),
            acceptance_criteria="meets the pool contract",
            due_date="2026-08-21",
        )
        cards = team_store.pool_cards()
        assert [card.ref for card in cards] == ["Q2"]
        assert cards[0].due_date == "2026-08-21"
        assert cards[0].source is None  # unstamped Work reads as absent

    def test_team_queues_cards_carry_source_and_due_date(
        self, team_store: TeamStore, make_card: Callable[..., BoardTask], employees: dict[str, str]
    ) -> None:
        make_card(
            title="Bob's task",
            ref="T1",
            owner_employee_id=employees["bob"],
            source=TASK_ADHOC_SOURCE,
            due_date="2026-08-14",
        )
        bucket = team_store.team_queues(employee_ids=[employees["bob"]])[employees["bob"]]
        assert [card.ref for card in bucket.ready] == ["T1"]
        assert bucket.ready[0].source == TASK_ADHOC_SOURCE
        assert bucket.ready[0].due_date == "2026-08-14"
