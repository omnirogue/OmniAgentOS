"""Daily commitments: generation, resolution, carry, and the ordering race.

The properties worth defending here are all "runs twice" properties. The 06:55
job and the 07:00 report both touch these rows, a laptop reboots mid-pass, and
an operator re-runs the generator by hand — so every one of generate, resolve
and carry has to be safe to repeat, and the two jobs have to agree on the order
they run in (review S2).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.team import commitments
from omniagentos.team.store import TeamStore

#: The stored timestamp format every table uses (``omniagentos.contracts``).
_STORED_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

DAY = "2026-08-14"
NEXT_DAY = "2026-08-15"


@pytest.fixture
def devs(employees: dict[str, str]) -> list[str]:
    """The two people who carry commitments (the operator, the operator, does not)."""
    return [employees["bob"], employees["alice"]]


def _company_goal(
    store: SqliteStore,
    goals_store: CompanyGoalsStore,
    *,
    slug: str = "omniagentos",
    name: str = "OmniAgentOS",
) -> str:
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (f"co_{slug}", slug, name, "active", utc_now_iso()),
    )
    goal = goals_store.create_goal(
        org_company_id=f"co_{slug}", title=f"General engineering — {name}", horizon="quarter"
    )
    return str(goal["id"])


def _finish(
    collab_store: CollabStore,
    team_store: TeamStore,
    card: BoardTask,
    *,
    actor: str,
    kind: str = "commit",
    ref: str | None = None,
    quality_gate: str = "pass",
) -> None:
    """Move a card to done the way a person does: evidence first, then status."""
    team_store.add_evidence(
        kind=kind,
        ref=ref or f"ev-{card.id}",
        repo="omnios",
        task_id=card.id,
        quality_gate=quality_gate,
    )
    collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value}, actor=actor)


def _backdate_done_event(store: SqliteStore, task_id: str, timestamp: str) -> None:
    """Rewrite the done event's clock — the only way to test another day."""
    store._connection.execute(
        "UPDATE task_events SET created_at = ? WHERE task_id = ? AND event = 'status_change' "
        "AND to_status = 'done'",
        (timestamp, task_id),
    )
    store._connection.commit()


def _ship_automation(
    collab_store: CollabStore,
    team_store: TeamStore,
    store: SqliteStore,
    make_card: Callable[..., BoardTask],
    employees: dict[str, str],
    *,
    title: str,
    ref: str,
    maturity: str | None = "assisted",
    evidence_kind: str = "commit",
    quality_gate: str = "pass",
    day: str = DAY,
    at: str = "12:00:00",
) -> BoardTask:
    """One card that qualifies as an AUTOMATION: maturity set, evidence filed,
    done at a named moment of the local day."""
    card = make_card(
        title=title,
        ref=ref,
        owner_employee_id=employees["bob"],
        acceptance_criteria="it runs itself",
    )
    if maturity is not None:
        collab_store.update_board_task(card.id, {"automation_maturity": maturity})
    _finish(
        collab_store,
        team_store,
        card,
        actor=employees["bob"],
        kind=evidence_kind,
        ref=f"ev-{ref}",
        quality_gate=quality_gate,
    )
    _backdate_done_event(store, card.id, _local_moment_as_utc(day, at))
    return card


def _local_moment_as_utc(day: str, at: str) -> str:
    """``day``/``at`` name a moment of the LOCAL day; stamp it as real UTC.

    Appending "Z" verbatim conflated local with UTC: on a Pacific runner,
    "06:00:00Z" is 23:00 of the PREVIOUS local day, so the dawn automation
    left the day it was meant to fill (Sol review of #497, F6)."""
    moment = datetime.fromisoformat(f"{day}T{at}")
    return moment.astimezone().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestGeneration:
    def test_active_cards_and_the_improvement_slot(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        devs: list[str],
        employees: dict[str, str],
    ) -> None:
        card = make_card(
            title="In flight",
            owner_employee_id=employees["bob"],
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        commitments.generate_for_day(team_store, DAY)
        rows = team_store.list_commitments(day=DAY, employee_id=employees["bob"])
        kinds = [row["kind"] for row in rows]
        assert kinds.count("improvement") == 1
        assert [row["task_id"] for row in rows if row["kind"] == "task"] == [card.id]
        improvement = next(row for row in rows if row["kind"] == "improvement")
        assert improvement["title"] == commitments.IMPROVEMENT_TITLE
        assert improvement["status"] == "committed"
        assert improvement["source"] == "auto"
        # Everyone active gets the slots, including the person with no cards:
        # one improvement + three automations (the operator's ruling 2026-08-14).
        assert [
            (row["kind"], row["slot"])
            for row in team_store.list_commitments(day=DAY, employee_id=employees["alice"])
        ] == [("improvement", 1), ("automation", 1), ("automation", 2), ("automation", 3)]
        # The operator does not answer a commitment check to themselves.
        assert team_store.list_commitments(day=DAY, employee_id=employees["owner"]) == []

    def test_an_open_card_counts_only_when_it_is_due_that_day(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        due = make_card(
            title="Due today", owner_employee_id=employees["bob"], due_date=f"{DAY}T18:00"
        )
        make_card(
            title="Due later", owner_employee_id=employees["bob"], due_date=f"{NEXT_DAY}T18:00"
        )
        make_card(title="No deadline", owner_employee_id=employees["bob"])
        commitments.generate_for_day(team_store, DAY)
        rows = team_store.list_commitments(day=DAY, employee_id=employees["bob"])
        assert [row["task_id"] for row in rows if row["kind"] == "task"] == [due.id]

    def test_the_cap_is_four_and_urgent_wins_the_slots(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        for index in range(6):
            make_card(
                title=f"Card {index}",
                owner_employee_id=employees["bob"],
                status=BoardTaskStatus.CLAIMED.value,
                priority="low",
            )
        urgent = make_card(
            title="Urgent",
            owner_employee_id=employees["bob"],
            status=BoardTaskStatus.CLAIMED.value,
            priority="urgent",
        )
        commitments.generate_for_day(team_store, DAY)
        tasks = [
            row
            for row in team_store.list_commitments(day=DAY, employee_id=employees["bob"])
            if row["kind"] == "task"
        ]
        assert len(tasks) == commitments.COMMITMENT_TASK_CAP
        assert urgent.id in {row["task_id"] for row in tasks}

    def test_re_running_the_generator_creates_nothing_new(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        make_card(
            title="In flight",
            owner_employee_id=employees["bob"],
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        commitments.generate_for_day(team_store, DAY)
        before = team_store.list_commitments(day=DAY)
        commitments.generate_for_day(team_store, DAY)
        after = team_store.list_commitments(day=DAY)
        assert [row["id"] for row in before] == [row["id"] for row in after]


class TestTaskResolution:
    @pytest.fixture
    def committed(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> BoardTask:
        card = make_card(
            title="Ship it",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it ships",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        return card

    def test_a_card_that_reached_done_is_delivered(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        committed: BoardTask,
        employees: dict[str, str],
    ) -> None:
        _finish(collab_store, team_store, committed, actor=employees["bob"])
        _backdate_done_event(store, committed.id, f"{DAY}T12:00:00Z")
        counts = commitments.resolve_day(team_store, DAY)
        row = next(r for r in team_store.list_commitments(day=DAY) if r["task_id"] == committed.id)
        assert row["status"] == "delivered"
        assert row["resolved_by"] == "system"
        assert row["resolved_at"]
        assert counts["delivered"] == 1

    def test_an_unfinished_card_is_missed_and_carried(
        self, team_store: TeamStore, committed: BoardTask, employees: dict[str, str]
    ) -> None:
        commitments.resolve_day(team_store, DAY)
        missed = next(
            r for r in team_store.list_commitments(day=DAY) if r["task_id"] == committed.id
        )
        assert missed["status"] == "missed"
        assert "did not reach done" in str(missed["resolution_note"])
        carried = team_store.list_commitments(day=NEXT_DAY, employee_id=employees["bob"])
        assert [row["status"] for row in carried] == ["carried"]
        assert carried[0]["carried_from"] == missed["id"]
        assert carried[0]["task_id"] == committed.id

    def test_a_failed_verification_resolves_missed_with_the_reason(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        committed: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """review S7: outcome-based, not activity-based. Done is not delivered
        if a verifier refused it."""
        _finish(collab_store, team_store, committed, actor=employees["bob"])
        _backdate_done_event(store, committed.id, f"{DAY}T12:00:00Z")
        team_store.fail_verification(committed.id, employees["alice"], "no tests at all")
        commitments.resolve_day(team_store, DAY)
        row = next(r for r in team_store.list_commitments(day=DAY) if r["task_id"] == committed.id)
        assert row["status"] == "missed"
        assert "no tests at all" in str(row["resolution_note"])

    def test_a_cancelled_card_is_missed_but_never_carried(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        committed: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """Work the team decided NOT to do must not re-commit somebody every
        morning forever."""
        collab_store.update_board_task(
            committed.id, {"status": BoardTaskStatus.CANCELLED.value}, actor=employees["owner"]
        )
        commitments.resolve_day(team_store, DAY)
        assert team_store.list_commitments(day=NEXT_DAY, employee_id=employees["bob"]) == []

    def test_re_resolving_the_day_changes_nothing(
        self, team_store: TeamStore, committed: BoardTask
    ) -> None:
        """The 07:00 report re-runs what the 06:55 job already did."""
        commitments.resolve_day(team_store, DAY)
        first = team_store.list_commitments(day=DAY)
        carried_first = team_store.list_commitments(day=NEXT_DAY)
        second = commitments.resolve_day(team_store, DAY)
        assert second == {"delivered": 0, "missed": 0, "carries_repaired": 0}
        assert team_store.list_commitments(day=DAY) == first
        assert team_store.list_commitments(day=NEXT_DAY) == carried_first

    def test_the_generated_then_carried_collision_links_instead_of_raising(
        self,
        team_store: TeamStore,
        committed: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """review S2 (BLOCKER): the 06:55 generator may already have created
        tomorrow's row for the same still-active card. The carry LINKS it."""
        commitments.generate_for_day(team_store, NEXT_DAY, employee_ids=[employees["bob"]])
        existing = next(
            row
            for row in team_store.list_commitments(day=NEXT_DAY)
            if row["task_id"] == committed.id
        )
        commitments.resolve_day(team_store, DAY)
        rows = [
            row
            for row in team_store.list_commitments(day=NEXT_DAY)
            if row["task_id"] == committed.id
        ]
        assert len(rows) == 1, "a collision must link, never duplicate"
        assert rows[0]["id"] == existing["id"]
        missed = next(
            r for r in team_store.list_commitments(day=DAY) if r["task_id"] == committed.id
        )
        assert rows[0]["carried_from"] == missed["id"]

    def test_the_natural_ordering_generate_then_resolve_links_the_carry(
        self,
        team_store: TeamStore,
        committed: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """Round-2 review, item 3 — the shape that actually happens in production.

        No crash, no hand-edited row: the 06:55 generator runs for DAY 2 first
        (the card is still active, so it mints a ``(DAY 2, emp, task)`` row with
        ``carried_from`` NULL), and only THEN does DAY 1's resolution find its
        commitment missing. The repair must LINK that row, not skip it as
        "already carried"."""
        commitments.generate_for_day(team_store, NEXT_DAY, employee_ids=[employees["bob"]])
        generated = next(
            row
            for row in team_store.list_commitments(day=NEXT_DAY)
            if row["task_id"] == committed.id
        )
        assert generated["carried_from"] is None, "precondition: the generator does not link"

        commitments.resolve_day(team_store, DAY)

        missed = next(
            r for r in team_store.list_commitments(day=DAY) if r["task_id"] == committed.id
        )
        assert missed["status"] == "missed"
        rows = [
            row
            for row in team_store.list_commitments(day=NEXT_DAY)
            if row["task_id"] == committed.id
        ]
        assert len(rows) == 1, "the generator's row is reused, never duplicated"
        assert rows[0]["id"] == generated["id"]
        assert rows[0]["carried_from"] == missed["id"]

    def test_an_existing_but_UNLINKED_next_day_row_is_linked_by_the_repair(
        self,
        team_store: TeamStore,
        store: SqliteStore,
        committed: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """Sol review, item 2. The collision a bare existence check misses: the
        06:55 generator already minted tomorrow's row for the same still-active
        card, so a row EXISTS — but with ``carried_from`` NULL. Asking "does a
        row exist?" reads that as "already carried" and leaves the miss
        unchained forever.

        This one reaches the state by HAND on purpose: it simulates the crash
        window (the miss committed, its link lost) that no ordering of the real
        jobs can produce, so the repair pass is exercised against a state the
        system can only ever arrive at by dying mid-write. The natural ordering
        — generate DAY 2, then resolve DAY 1 — is the test above."""
        commitments.resolve_day(team_store, DAY)
        # Simulate the crash shape: the miss is terminal, its carry link is
        # gone, and a generator-minted unlinked row sits in its place.
        store._connection.execute(
            "UPDATE team_commitments SET carried_from = NULL WHERE day = ? AND status = 'carried'",
            (NEXT_DAY,),
        )
        store._connection.commit()
        missed = next(
            r for r in team_store.list_commitments(day=DAY) if r["task_id"] == committed.id
        )
        assert missed["status"] == "missed"
        assert [
            row["carried_from"]
            for row in team_store.list_commitments(day=NEXT_DAY)
            if row["task_id"] == committed.id
        ] == [None], "precondition: the next-day row exists but is unlinked"

        counts = commitments.resolve_day(team_store, DAY)

        assert counts["carries_repaired"] == 1, "a LINK is a repair, not a no-op"
        rows = [
            row
            for row in team_store.list_commitments(day=NEXT_DAY)
            if row["task_id"] == committed.id
        ]
        assert len(rows) == 1, "linking must never duplicate the row"
        assert rows[0]["carried_from"] == missed["id"]
        # And it stays idempotent: an already-linked row is left alone.
        assert commitments.resolve_day(team_store, DAY)["carries_repaired"] == 0

    def test_an_already_linked_carry_keeps_its_original_link(
        self,
        team_store: TeamStore,
        store: SqliteStore,
        committed: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """A chain of slips must read back in the order it happened, so a later
        repair never re-points an earlier link."""
        commitments.resolve_day(team_store, DAY)
        original = next(
            row
            for row in team_store.list_commitments(day=NEXT_DAY)
            if row["task_id"] == committed.id
        )["carried_from"]
        team_store.mint_carry(
            carried_from="tcm_some_other_miss",
            day=NEXT_DAY,
            employee_id=employees["bob"],
            task_id=committed.id,
            title="Ship it",
        )
        again = next(
            row
            for row in team_store.list_commitments(day=NEXT_DAY)
            if row["task_id"] == committed.id
        )
        assert again["carried_from"] == original

    def test_a_missed_row_with_no_carry_is_repaired_on_the_next_run(
        self,
        team_store: TeamStore,
        store: SqliteStore,
        committed: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """round-3 §2: the miss and its carry are written in one transaction, so
        this gap should not exist — but a process kill does not consult a
        design. The repair pass closes it, idempotently."""
        commitments.resolve_day(team_store, DAY)
        store._connection.execute("DELETE FROM team_commitments WHERE status = 'carried'")
        store._connection.commit()
        counts = commitments.resolve_day(team_store, DAY)
        assert counts["carries_repaired"] == 1
        carried = team_store.list_commitments(day=NEXT_DAY, employee_id=employees["bob"])
        assert [row["status"] for row in carried] == ["carried"]
        # And running it again repairs nothing: the row is already there.
        assert commitments.resolve_day(team_store, DAY)["carries_repaired"] == 0


class TestTheImprovementSlot:
    @pytest.fixture
    def goal_id(self, store: SqliteStore, goals_store: CompanyGoalsStore) -> str:
        return _company_goal(store, goals_store)

    def _slot(self, team_store: TeamStore, employee_id: str) -> dict[str, object]:
        return next(
            row
            for row in team_store.list_commitments(day=DAY, employee_id=employee_id)
            if row["kind"] == "improvement"
        )

    def test_an_evidence_backed_ml_card_delivers_the_slot(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        goal_id: str,
        employees: dict[str, str],
    ) -> None:
        card = make_card(
            title="Make the gate faster",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it is faster",
            goal_id=goal_id,
            size="M",
            ref="GH-1",
        )
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _finish(collab_store, team_store, card, actor=employees["bob"])
        _backdate_done_event(store, card.id, f"{DAY}T12:00:00Z")
        commitments.resolve_day(team_store, DAY)
        row = self._slot(team_store, employees["bob"])
        assert row["status"] == "delivered"
        assert "GH-1" in str(row["resolution_note"])

    def test_a_small_unverified_card_does_not_satisfy_it(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        goal_id: str,
        employees: dict[str, str],
    ) -> None:
        """review S8: cosmetic S-size work nobody checked is not an improvement."""
        card = make_card(
            title="Fix a typo",
            owner_employee_id=employees["bob"],
            acceptance_criteria="typo gone",
            goal_id=goal_id,
            size="S",
        )
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _finish(collab_store, team_store, card, actor=employees["bob"])
        _backdate_done_event(store, card.id, f"{DAY}T12:00:00Z")
        commitments.resolve_day(team_store, DAY)
        assert self._slot(team_store, employees["bob"])["status"] == "missed"
        # ...until somebody verifies it, which is the documented upgrade path.

    def test_a_verified_small_card_does_satisfy_it(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        goal_id: str,
        employees: dict[str, str],
    ) -> None:
        card = make_card(
            title="Fix a typo",
            owner_employee_id=employees["bob"],
            acceptance_criteria="typo gone",
            goal_id=goal_id,
            size="S",
        )
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _finish(collab_store, team_store, card, actor=employees["bob"])
        _backdate_done_event(store, card.id, f"{DAY}T12:00:00Z")
        team_store.verify_task(card.id, employees["alice"])
        commitments.resolve_day(team_store, DAY)
        row = self._slot(team_store, employees["bob"])
        assert row["status"] == "delivered"
        assert "verified" in str(row["resolution_note"])

    @pytest.mark.parametrize(
        ("kind", "quality_gate"),
        [("note", "pass"), ("commit", "reverted")],
    )
    def test_a_bare_note_or_a_reverted_commit_does_not_count(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        goal_id: str,
        employees: dict[str, str],
        kind: str,
        quality_gate: str,
    ) -> None:
        """round-3 §7: a sentence somebody typed, and work that did not land,
        are both evidence that something HAPPENED — not that it landed."""
        card = make_card(
            title="Big refactor",
            owner_employee_id=employees["bob"],
            acceptance_criteria="refactored",
            goal_id=goal_id,
            size="L",
        )
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _finish(
            collab_store,
            team_store,
            card,
            actor=employees["bob"],
            kind=kind,
            quality_gate=quality_gate,
        )
        _backdate_done_event(store, card.id, f"{DAY}T12:00:00Z")
        commitments.resolve_day(team_store, DAY)
        assert self._slot(team_store, employees["bob"])["status"] == "missed"

    def test_a_card_on_another_companys_goal_does_not_count(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        goals_store: CompanyGoalsStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        other = _company_goal(store, goals_store, slug="globex", name="Globex")
        card = make_card(
            title="Customer work",
            owner_employee_id=employees["bob"],
            acceptance_criteria="shipped",
            goal_id=other,
            size="L",
        )
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _finish(collab_store, team_store, card, actor=employees["bob"])
        _backdate_done_event(store, card.id, f"{DAY}T12:00:00Z")
        commitments.resolve_day(team_store, DAY)
        assert self._slot(team_store, employees["bob"])["status"] == "missed"


class TestLocalDays:
    def test_a_late_evening_finish_counts_for_the_local_day(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """round-3 §8: 23:30 LOCAL is the next day in UTC on most of the planet.
        A commitment resolved on the UTC calendar would call that a miss."""
        card = make_card(
            title="Late finish",
            owner_employee_id=employees["bob"],
            acceptance_criteria="finished",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _finish(collab_store, team_store, card, actor=employees["bob"])
        start, end = commitments.local_day_bounds(DAY)
        # One second before the local day ends — whatever UTC offset that is.
        late = commitments.local_day_bounds(NEXT_DAY)[0]
        assert start < late
        _backdate_done_event(store, card.id, _one_second_before(late))
        commitments.resolve_day(team_store, DAY)
        row = next(r for r in team_store.list_commitments(day=DAY) if r["task_id"] == card.id)
        assert row["status"] == "delivered"
        assert commitments.local_day(_one_second_before(end)) == DAY

    def test_the_bounds_are_half_open(self) -> None:
        _start, end = commitments.local_day_bounds(DAY)
        next_start, _next_end = commitments.local_day_bounds(NEXT_DAY)
        assert end == next_start
        assert commitments.local_day(end) == NEXT_DAY


def _one_second_before(timestamp: str) -> str:
    """The stored-format instant one second before ``timestamp``."""
    moment = datetime.strptime(timestamp, _STORED_FORMAT) - timedelta(seconds=1)
    return moment.strftime(_STORED_FORMAT)


class TestACarriedRowIsStillOwed:
    """``carried`` is provenance, not an outcome.

    The bug this class pins: ``resolve_day`` used to judge only ``committed``
    rows, so a commitment carried forward sat terminal-'carried' forever and was
    NEVER judged on its own day. The person still owed the work and the report
    counted it as neither delivered nor missed — the exact hole this table
    exists to close.
    """

    @pytest.fixture
    def carried(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> tuple[BoardTask, dict[str, Any]]:
        """Yesterday's miss, carried into DAY."""
        card = make_card(
            title="Slipping",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it ships",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        yesterday = "2026-08-13"
        commitments.generate_for_day(team_store, yesterday, employee_ids=[employees["bob"]])
        commitments.resolve_day(team_store, yesterday)
        row = next(r for r in team_store.list_commitments(day=DAY) if r["task_id"] == card.id)
        assert row["status"] == "carried", "precondition: the row under test is a carry"
        return card, dict(row)

    def test_a_carried_row_that_delivers_resolves_delivered(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        carried: tuple[BoardTask, dict[str, Any]],
        employees: dict[str, str],
    ) -> None:
        card, row = carried
        _finish(collab_store, team_store, card, actor=employees["bob"])
        _backdate_done_event(store, card.id, f"{DAY}T12:00:00Z")
        counts = commitments.resolve_day(team_store, DAY)
        resolved = team_store.get_commitment(str(row["id"]))
        assert resolved is not None
        assert resolved["status"] == "delivered"
        assert resolved["resolved_by"] == "system"
        assert counts["delivered"] >= 1

    def test_a_carried_row_that_misses_again_carries_again(
        self,
        team_store: TeamStore,
        carried: tuple[BoardTask, dict[str, Any]],
        employees: dict[str, str],
    ) -> None:
        card, row = carried
        commitments.resolve_day(team_store, DAY)
        second = team_store.get_commitment(str(row["id"]))
        assert second is not None
        assert second["status"] == "missed"
        # ...and a SECOND-generation carry, chained through carried_from.
        third = [
            entry
            for entry in team_store.list_commitments(day=NEXT_DAY)
            if entry["task_id"] == card.id
        ]
        assert [entry["status"] for entry in third] == ["carried"]
        assert third[0]["carried_from"] == row["id"]
        assert third[0]["carried_from"] != second["carried_from"], "the chain moves forward"
        # The prefix re-states rather than nesting one clause per slipped day.
        assert str(third[0]["expected_outcome"]).count("carried from") == 1

    def test_resolving_a_carried_row_twice_is_still_a_no_op(
        self, team_store: TeamStore, carried: tuple[BoardTask, dict[str, Any]]
    ) -> None:
        commitments.resolve_day(team_store, DAY)
        after_first = team_store.list_commitments(day=DAY)
        assert commitments.resolve_day(team_store, DAY) == {
            "delivered": 0,
            "missed": 0,
            "carries_repaired": 0,
        }
        assert team_store.list_commitments(day=DAY) == after_first

    def test_a_carried_row_is_not_directly_resolvable_by_hand(
        self,
        api: httpx.AsyncClient,
        carried: tuple[BoardTask, dict[str, Any]],
    ) -> None:
        """PATCH still refuses a 'carried' row: ``resolve_day`` is the only
        writer of a carry's outcome, so an operator cannot pre-empt the
        judgement (and cannot mint or clear a carry by hand either)."""
        _card, row = carried
        response = asyncio.run(
            api.patch(
                f"/api/team/commitments/{row['id']}",
                json={"status": "delivered", "resolution_note": "counting it"},
            )
        )
        assert response.status_code == 400, response.text
        assert "history" in response.text
        unchanged = asyncio.run(api.get(f"/api/team/commitments?day={DAY}")).json()["commitments"]
        assert {entry["status"] for entry in unchanged if entry["id"] == row["id"]} == {"carried"}


class TestOrchestrationOrder:
    def test_run_daily_resolves_yesterday_before_generating_today(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """review S2: ONE order. Yesterday's miss carries into today, and the
        generator then finds that row instead of colliding with it."""
        card = make_card(
            title="Slipping",
            owner_employee_id=employees["bob"],
            acceptance_criteria="ships",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        result = commitments.run_daily(team_store, today=NEXT_DAY)
        # The card, the improvement slot and all three automation slots.
        assert result["resolved"]["missed"] == 5
        today_rows = [
            row
            for row in team_store.list_commitments(day=NEXT_DAY, employee_id=employees["bob"])
            if row["task_id"] == card.id
        ]
        assert len(today_rows) == 1
        assert today_rows[0]["status"] == "carried"
        assert today_rows[0]["carried_from"] is not None


class TestTheDailyAutomationSlots:
    """the operator's ruling 2026-08-14: three new automations or skills a day, per dev.

    The slot is deliberately harder to satisfy than "a card got done": it
    requires the card to SAY the system took work over
    (``automation_maturity`` at 'assisted' or above), backed by pass-gated
    evidence. Otherwise the number counts activity, and a person could satisfy
    a three-automations-a-day bar by closing three ordinary cards.
    """

    def _automation_rows(
        self, team_store: TeamStore, employee_id: str, day: str = DAY
    ) -> list[Any]:
        return [
            row
            for row in team_store.list_commitments(day=day, employee_id=employee_id)
            if row["kind"] == "automation"
        ]

    def test_three_slots_are_generated_and_re_running_adds_none(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        first = self._automation_rows(team_store, employees["bob"])
        assert [row["slot"] for row in first] == [1, 2, 3]
        assert [row["title"] for row in first] == [
            "New automation or skill (1/3)",
            "New automation or skill (2/3)",
            "New automation or skill (3/3)",
        ]
        assert {row["status"] for row in first} == {"committed"}
        assert {row["source"] for row in first} == {"auto"}
        assert all("automation_maturity" in str(row["expected_outcome"]) for row in first)

        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        again = self._automation_rows(team_store, employees["bob"])
        assert [row["id"] for row in again] == [row["id"] for row in first]

    def test_the_generator_returns_three_distinct_slots_on_every_run(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        """Asserted on what generate_for_day RETURNS, not on a re-read: the
        read-back key is exactly what broke here once. Keyed by ``kind`` alone,
        an ``INSERT OR IGNORE`` collision hands slot 1 back three times, and a
        caller rendering the morning DM from the return value shows one slot
        three times while the database holds three."""
        first = [
            row
            for row in commitments.generate_for_day(
                team_store, DAY, employee_ids=[employees["bob"]]
            )
            if row["kind"] == "automation"
        ]
        assert [row["slot"] for row in first] == [1, 2, 3]
        assert len({row["id"] for row in first}) == 3

        second = [
            row
            for row in commitments.generate_for_day(
                team_store, DAY, employee_ids=[employees["bob"]]
            )
            if row["kind"] == "automation"
        ]
        assert [row["slot"] for row in second] == [1, 2, 3]
        assert [row["id"] for row in second] == [row["id"] for row in first]

    def test_two_qualifying_cards_fill_two_slots_and_the_third_is_missed(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _ship_automation(
            collab_store, team_store, store, make_card, employees, title="Auto one", ref="A-1"
        )
        _ship_automation(
            collab_store,
            team_store,
            store,
            make_card,
            employees,
            title="Auto two",
            ref="A-2",
            maturity="autonomous",
            # Shipped LATER in the day: slot order follows the done events, not
            # the row ids (two cards created in one second tie on created_at).
            at="15:00:00",
        )

        commitments.resolve_day(team_store, DAY)

        rows = self._automation_rows(team_store, employees["bob"])
        assert [row["status"] for row in rows] == ["delivered", "delivered", "missed"]
        # Distinct cards per slot: one automation cannot fill two slots.
        assert "A-1" in str(rows[0]["resolution_note"])
        assert "A-2" in str(rows[1]["resolution_note"])
        assert "assisted" in str(rows[0]["resolution_note"])
        assert "autonomous" in str(rows[1]["resolution_note"])
        assert "no 3rd automation" in str(rows[2]["resolution_note"])
        assert "2 of 3 shipped" in str(rows[2]["resolution_note"])

    def test_a_second_resolution_pass_gives_the_same_card_the_same_slot(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """Slot n takes the n-th qualifying card, so a partially-resolved day
        cannot re-shuffle which automation filled which slot."""
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _ship_automation(
            collab_store, team_store, store, make_card, employees, title="Auto one", ref="A-1"
        )
        rows = self._automation_rows(team_store, employees["bob"])
        commitments.resolve_day(team_store, DAY)
        after = self._automation_rows(team_store, employees["bob"])
        assert [row["id"] for row in after] == [row["id"] for row in rows]
        assert commitments.resolve_day(team_store, DAY) == {
            "delivered": 0,
            "missed": 0,
            "carries_repaired": 0,
        }
        assert [
            row["resolution_note"] for row in self._automation_rows(team_store, employees["bob"])
        ] == [row["resolution_note"] for row in after]

    @pytest.mark.parametrize(
        ("maturity", "evidence_kind", "quality_gate", "why"),
        [
            ("human", "commit", "pass", "a card done by hand is work, not automation"),
            (None, "commit", "pass", "an unset field is not a favourable default"),
            ("assisted", "note", "pass", "a typed sentence is not evidence of shipping"),
            ("assisted", "commit", "reverted", "work that did not land does not count"),
        ],
    )
    def test_a_card_that_does_not_qualify_leaves_every_slot_missed(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        maturity: str | None,
        evidence_kind: str,
        quality_gate: str,
        why: str,
    ) -> None:
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _ship_automation(
            collab_store,
            team_store,
            store,
            make_card,
            employees,
            title="Not an automation",
            ref="A-9",
            maturity=maturity,
            evidence_kind=evidence_kind,
            quality_gate=quality_gate,
        )
        commitments.resolve_day(team_store, DAY)
        rows = self._automation_rows(team_store, employees["bob"])
        assert [row["status"] for row in rows] == ["missed"] * 3, why
        assert "0 of 3 shipped" in str(rows[0]["resolution_note"])

    def test_a_card_from_another_day_does_not_fill_todays_slot(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _ship_automation(
            collab_store,
            team_store,
            store,
            make_card,
            employees,
            title="Yesterday's automation",
            ref="A-0",
            day="2026-08-13",
        )
        commitments.resolve_day(team_store, DAY)
        rows = self._automation_rows(team_store, employees["bob"])
        assert [row["status"] for row in rows] == ["missed"] * 3

    def test_automation_slots_never_carry(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        """The deliberate divergence from task commitments: a missed SLOT is a
        day that did not produce one, and tomorrow already mints three fresh.
        Carrying would reach fifteen open automation rows by Friday."""
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        commitments.resolve_day(team_store, DAY)
        assert [
            row["status"] for row in self._automation_rows(team_store, employees["bob"])
        ] == ["missed"] * 3

        commitments.generate_for_day(team_store, NEXT_DAY, employee_ids=[employees["bob"]])
        tomorrow = self._automation_rows(team_store, employees["bob"], day=NEXT_DAY)
        assert [row["slot"] for row in tomorrow] == [1, 2, 3], "exactly three fresh slots"
        assert {row["status"] for row in tomorrow} == {"committed"}
        assert {row["carried_from"] for row in tomorrow} == {None}, "slots never chain"

    def test_the_improvement_slot_is_untouched_by_the_automation_rule(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        goals_store: CompanyGoalsStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """An omniagentos M-size card with evidence still delivers the
        improvement slot even though its automation_maturity is unset — the two
        rules are independent."""
        goal_id = _company_goal(store, goals_store)
        card = make_card(
            title="Make the gate faster",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it is faster",
            goal_id=goal_id,
            size="M",
            ref="GH-1",
        )
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _finish(collab_store, team_store, card, actor=employees["bob"])
        _backdate_done_event(store, card.id, f"{DAY}T12:00:00Z")
        commitments.resolve_day(team_store, DAY)
        improvement = next(
            row
            for row in team_store.list_commitments(day=DAY, employee_id=employees["bob"])
            if row["kind"] == "improvement"
        )
        assert improvement["status"] == "delivered"
        assert [
            row["status"] for row in self._automation_rows(team_store, employees["bob"])
        ] == ["missed"] * 3


class TestSlotBoundsAndSlottedKinds:
    """Migration 133's CHECK and the store's mirror of it (review round 2)."""

    @pytest.mark.parametrize("slot", [-1, 0, 4, 99])
    def test_the_store_refuses_an_out_of_range_automation_slot(
        self, team_store: TeamStore, employees: dict[str, str], slot: int
    ) -> None:
        """A negative slot is not merely untidy: it reaches Python's list
        indexing in the resolver, where -1 selects the LAST qualifying card."""
        with pytest.raises(ValueError, match="slot must be between 1 and 3"):
            team_store.create_commitment(
                day=DAY,
                employee_id=employees["bob"],
                kind="automation",
                slot=slot,
                title="New automation or skill",
            )

    @pytest.mark.parametrize("kind", ["task", "improvement"])
    def test_an_unslotted_kind_is_pinned_to_slot_one(
        self, team_store: TeamStore, employees: dict[str, str], kind: str
    ) -> None:
        with pytest.raises(ValueError, match="slot must be between 1 and 1"):
            team_store.create_commitment(
                day=DAY,
                employee_id=employees["bob"],
                kind=kind,
                slot=2,
                title="Something",
            )

    @pytest.mark.parametrize("kind", ["improvement", "automation"])
    def test_a_slotted_commitment_may_not_name_a_card(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        kind: str,
    ) -> None:
        """It would be indexed by slot and looked up by card, so the idempotent
        re-run could not find its own row — a 500 on the second call."""
        card = make_card(title="A card", owner_employee_id=employees["bob"])
        with pytest.raises(ValueError, match=f"a {kind} commitment cannot name a task_id"):
            team_store.create_commitment(
                day=DAY,
                employee_id=employees["bob"],
                kind=kind,
                task_id=card.id,
                title="Something",
            )

    def test_a_hand_written_negative_slot_cannot_reach_the_resolver(
        self, team_store: TeamStore, store: SqliteStore, employees: dict[str, str]
    ) -> None:
        """Defence in depth: even with the store bypassed, the schema refuses
        the row, so ``resolve_day`` can never index a list backwards."""
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        with pytest.raises(sqlite3.IntegrityError):
            store._connection.execute(
                "UPDATE team_commitments SET slot = -1 WHERE kind = 'automation' AND slot = 3"
            )


class TestInterruptedAutomationResolution:
    """Review round 2, item 4: a half-judged day plus a changed qualifying set.

    Slot assignments are derived from an ORDERED snapshot. If the day is
    interrupted after slot 1 is frozen, and the retry's snapshot has since
    gained a card that sorts EARLIER (a done event backdated by a collector, a
    card whose evidence landed late), then re-deriving by position hands slot 2
    the card slot 1 already claimed — the same automation counted twice, frozen
    forever because a resolved row is never re-judged.
    """

    def _automation_rows(self, team_store: TeamStore, employee_id: str) -> list[Any]:
        return [
            row
            for row in team_store.list_commitments(day=DAY, employee_id=employee_id)
            if row["kind"] == "automation"
        ]

    def test_a_retry_never_re_assigns_a_card_a_frozen_slot_already_took(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        late = _ship_automation(
            collab_store,
            team_store,
            store,
            make_card,
            employees,
            title="Shipped at noon",
            ref="A-NOON",
            at="12:00:00",
        )
        rows = self._automation_rows(team_store, employees["bob"])

        # The interruption: slot 1 is judged and frozen, the process dies.
        team_store.resolve_commitments(
            [(str(rows[0]["id"]), "delivered", f"card={late.id} A-NOON Shipped at noon (assisted)")]
        )

        # ...and the retry's snapshot has GAINED a card that sorts earlier.
        early = _ship_automation(
            collab_store,
            team_store,
            store,
            make_card,
            employees,
            title="Shipped at dawn",
            ref="A-DAWN",
            at="06:00:00",
        )

        commitments.resolve_day(team_store, DAY)

        after = self._automation_rows(team_store, employees["bob"])
        assert [row["status"] for row in after] == ["delivered", "delivered", "missed"]
        assert f"card={late.id}" in str(after[0]["resolution_note"]), "frozen, not re-judged"
        assert f"card={early.id}" in str(after[1]["resolution_note"])
        claimed = [
            str(row["resolution_note"]).split("card=")[1].split(" ")[0]
            for row in after
            if row["status"] == "delivered"
        ]
        assert len(set(claimed)) == 2, "no card may fill two slots"

    def test_all_of_one_persons_slots_are_written_in_one_transaction(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Atomicity is the correctness boundary, not a batching nicety: a day
        judged half against one snapshot and half against another is how the
        double assignment above becomes possible in the first place."""
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        _ship_automation(
            collab_store, team_store, store, make_card, employees, title="One", ref="A-1"
        )
        batches: list[int] = []
        original = TeamStore.resolve_commitments

        def _spy(self: TeamStore, entries: Any, **kwargs: Any) -> int:
            batches.append(len(list(entries)))
            return original(self, entries, **kwargs)

        monkeypatch.setattr(TeamStore, "resolve_commitments", _spy)
        commitments.resolve_day(team_store, DAY)
        assert batches == [3], "one write for all three slots, not three writes"


class TestReopenedAndRecompletedToday:
    def test_a_card_re_completed_today_counts_for_today(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """Review round 2, item 5. The qualifier used to take the card's FIRST
        done event ever and ask whether it fell inside the day — so a card
        finished last week, reopened, and re-completed this morning was
        invisible. The SELECT is windowed now."""
        commitments.generate_for_day(team_store, DAY, employee_ids=[employees["bob"]])
        card = _ship_automation(
            collab_store,
            team_store,
            store,
            make_card,
            employees,
            title="Finished, reopened, finished again",
            ref="A-RE",
            day="2026-08-10",
        )
        # Reopen and finish it again, today.
        collab_store.update_board_task(
            card.id, {"status": BoardTaskStatus.IN_PROGRESS.value}, actor=employees["bob"]
        )
        collab_store.update_board_task(
            card.id, {"status": BoardTaskStatus.DONE.value}, actor=employees["bob"]
        )
        store._connection.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ? AND event = 'status_change' "
            "AND to_status = 'done' AND created_at > ?",
            (f"{DAY}T12:00:00Z", card.id, "2026-08-10T23:59:59Z"),
        )
        store._connection.commit()

        commitments.resolve_day(team_store, DAY)

        rows = [
            row
            for row in team_store.list_commitments(day=DAY, employee_id=employees["bob"])
            if row["kind"] == "automation"
        ]
        assert rows[0]["status"] == "delivered"
        assert "A-RE" in str(rows[0]["resolution_note"])
