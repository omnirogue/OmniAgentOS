"""Daily productivity snapshots, and the queue view they are computed beside.

A snapshot is a REPLACE, not an append: two rows for one person-day are two
answers to one question, and every roll-up downstream would have to guess which
is current.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.team.contracts import (
    ACTIVE_QUEUE_FLOOR,
    READY_QUEUE_FLOOR,
    ProdSnapshot,
    TeamQueueBuckets,
)
from omniagentos.team.store import TeamStore

DAY = "2026-08-10"


def team_queues_for(
    team_store: TeamStore, employee_id: str, today: str | None = None
) -> TeamQueueBuckets:
    return team_store.team_queues(employee_ids=[employee_id], today=today)[employee_id]


class TestSnapshotUpsert:
    def test_two_writes_for_one_day_leave_one_row(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        team_store.upsert_snapshot(
            day=DAY, employee_id=employees["bob"], verified_points=3, merged_prs=1
        )
        stored = team_store.upsert_snapshot(
            day=DAY,
            employee_id=employees["bob"],
            verified_points=8,
            verified_outcomes=2,
            merged_prs=4,
            production_x=1.7,
            breakdown={"cards": ["btk_1", "btk_2"]},
        )
        rows = team_store.list_snapshots(day=DAY)
        assert len(rows) == 1
        assert stored["verified_points"] == 8
        assert stored["verified_outcomes"] == 2
        assert stored["merged_prs"] == 4
        assert stored["production_x"] == 1.7

    def test_a_different_day_is_a_different_row(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        team_store.upsert_snapshot(day=DAY, employee_id=employees["bob"], verified_points=1)
        team_store.upsert_snapshot(
            day="2026-08-11", employee_id=employees["bob"], verified_points=2
        )
        team_store.upsert_snapshot(day=DAY, employee_id=employees["alice"], verified_points=5)
        assert len(team_store.list_snapshots()) == 3
        assert len(team_store.list_snapshots(employee_id=employees["bob"])) == 2

    def test_unmeasured_stays_null_not_zero(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        """A day with no sessions did not have zero sessions — it was not
        measured. The contract keeps the two answers distinguishable."""
        stored = team_store.upsert_snapshot(day=DAY, employee_id=employees["alice"])
        assert stored["avg_active_sessions"] is None
        assert stored["peak_sessions"] is None
        assert stored["first_pass_rate"] is None
        assert stored["verified_points"] == 0
        snapshot = ProdSnapshot.from_row(stored)
        assert snapshot.avg_active_sessions is None
        assert snapshot.verified_points == 0
        assert snapshot.breakdown == {}

    def test_a_recompute_replaces_the_breakdown_it_does_not_merge(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        team_store.upsert_snapshot(
            day=DAY, employee_id=employees["alice"], breakdown={"cards": ["btk_old"]}
        )
        stored = team_store.upsert_snapshot(
            day=DAY, employee_id=employees["alice"], breakdown={"cards": ["btk_new"]}
        )
        assert ProdSnapshot.from_row(stored).breakdown == {"cards": ["btk_new"]}

    def test_absent_snapshot_reads_as_none(self, team_store: TeamStore) -> None:
        assert team_store.get_snapshot(DAY, "emp_nobody") is None


class TestTeamQueues:
    def test_cards_land_in_the_right_bucket(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        bob = employees["bob"]
        ready = make_card(title="Ready", owner_employee_id=bob, ref="PR-1")
        active = make_card(title="Active", owner_employee_id=bob)
        blocked = make_card(title="Blocked", owner_employee_id=bob)
        review = make_card(title="Review", owner_employee_id=bob)
        done = make_card(title="Done", owner_employee_id=bob)
        collab_store.update_board_task(active.id, {"status": BoardTaskStatus.IN_PROGRESS.value})
        collab_store.update_board_task(
            blocked.id,
            {"status": BoardTaskStatus.BLOCKED.value, "blocked_reason": "waiting on review"},
        )
        collab_store.update_board_task(
            review.id, {"status": BoardTaskStatus.AWAITING_APPROVAL.value}
        )
        collab_store.update_board_task(done.id, {"status": BoardTaskStatus.DONE.value})

        row = collab_store.get_board_task(done.id)
        assert row is not None
        buckets = team_queues_for(team_store, bob, today=str(row["updated_at"])[:10])
        assert [card.id for card in buckets.ready] == [ready.id]
        assert [card.ref for card in buckets.ready] == ["PR-1"]
        assert [card.id for card in buckets.active] == [active.id]
        assert [card.id for card in buckets.blocked] == [blocked.id]
        assert [card.id for card in buckets.review] == [review.id]
        assert [card.id for card in buckets.done_today] == [done.id]
        assert buckets.counts == {
            "ready": 1,
            "active": 1,
            "blocked": 1,
            "review": 1,
            "done_today": 1,
        }

    def test_done_yesterday_is_not_done_today(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        done = make_card(title="Old news", owner_employee_id=employees["alice"])
        collab_store.update_board_task(done.id, {"status": BoardTaskStatus.DONE.value})
        buckets = team_queues_for(team_store, employees["alice"], today="2020-01-01")
        assert buckets.done_today == []

    def test_agent_cards_belong_to_nobodys_queue(
        self, team_store: TeamStore, make_card: Callable[..., BoardTask], employees: dict[str, str]
    ) -> None:
        make_card(title="Agent card")
        buckets = team_store.team_queues()
        assert set(buckets) >= set(employees.values())
        assert all(bucket.counts["ready"] == 0 for bucket in buckets.values())

    def test_archived_cards_leave_the_queue(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Archived", owner_employee_id=employees["alice"])
        collab_store.update_board_task(card.id, {"archived_at": "2026-08-10T00:00:00Z"})
        assert team_queues_for(team_store, employees["alice"]).ready == []

    def test_ready_below_5_flags_a_person_about_to_run_dry(
        self, team_store: TeamStore, make_card: Callable[..., BoardTask], employees: dict[str, str]
    ) -> None:
        alice = employees["alice"]
        assert team_queues_for(team_store, alice).ready_below_5 is True
        for index in range(READY_QUEUE_FLOOR):
            make_card(title=f"Ready {index}", owner_employee_id=alice)
        assert team_queues_for(team_store, alice).ready_below_5 is False

    def test_active_below_5_capacity_boundary_does_not_change_ready_signal(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        alice = employees["alice"]
        for index in range(READY_QUEUE_FLOOR):
            make_card(title=f"Ready capacity {index}", owner_employee_id=alice)
        before = team_queues_for(team_store, alice)
        assert before.ready_below_5 is False
        assert before.active_below_5 is True

        for index in range(ACTIVE_QUEUE_FLOOR - 1):
            card = make_card(title=f"Active capacity {index}", owner_employee_id=alice)
            collab_store.claim_task(card.id, f"agt_{index}", 0)

        at_four = team_queues_for(team_store, alice)
        assert len(at_four.active) == ACTIVE_QUEUE_FLOOR - 1
        assert at_four.active_below_5 is True

        fifth = make_card(title="Active capacity 4", owner_employee_id=alice)
        collab_store.claim_task(fifth.id, "agt_4", 0)
        after = team_queues_for(team_store, alice)
        assert len(after.active) == ACTIVE_QUEUE_FLOOR
        assert after.active_below_5 is False
        # The active-capacity addition does not rename, remove, or re-derive
        # the report's ready-depth signal.
        assert after.ready_below_5 is before.ready_below_5 is False

    def test_no_card_can_hide_behind_an_off_roster_owner(
        self, team_store: TeamStore, make_card: Callable[..., BoardTask], employees: dict[str, str]
    ) -> None:
        """The default roster is COMPLETE, not merely convenient: migration
        123's foreign key means an owner who is not an employee cannot be
        persisted, so there is no card the roster-wide read can miss."""
        with pytest.raises(sqlite3.IntegrityError):
            make_card(title="Ghost-owned", owner_employee_id="emp_not_in_roster")
        buckets = team_store.team_queues()
        assert set(employees.values()) <= set(buckets)

    def test_an_idle_person_gets_an_empty_queue_not_a_missing_one(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        buckets = team_store.team_queues()
        assert buckets[employees["owner"]].counts == {
            "ready": 0,
            "active": 0,
            "blocked": 0,
            "review": 0,
            "done_today": 0,
        }

    def test_serialization_carries_the_derived_fields(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        payload = team_queues_for(team_store, employees["owner"]).model_dump_with_counts()
        assert payload["counts"]["ready"] == 0
        assert payload["ready_below_5"] is True
        assert payload["active_below_5"] is True
        assert payload["employee_id"] == employees["owner"]
