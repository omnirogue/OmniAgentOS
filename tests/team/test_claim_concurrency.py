"""Concurrency: exactly one claimant, and no rule that a race can walk around.

The claim CAS is pre-existing (``tests/lab/collab/test_store.py`` races two
agents); this file widens it to eight and then asks the question migration 123
introduces — whether the new READ-then-WRITE rules survive concurrency. They do
only because the read happens INSIDE ``BEGIN IMMEDIATE``: a validator that read
the row before the transaction would let two writers each approve against a
stale snapshot and jointly commit a state neither one proposed (the defect
``CompanyGoalsStore.update_goal`` documents in its docstring).
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from omniagentos.collab.contracts import Agent, BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.team.store import TeamStore

_RACERS = 8


class TestClaimRace:
    def test_employee_claim_sets_owner_and_losing_racer_cannot_change_it(
        self, collab_store: CollabStore, employees: dict[str, str]
    ) -> None:
        task = BoardTask(title="Pool claim")
        collab_store.create_board_task(task)

        assert (
            collab_store.claim_task(
                task.id,
                "human:emp_bob",
                0,
                actor=employees["bob"],
                owner_employee_id=employees["bob"],
            )
            is True
        )
        assert (
            collab_store.claim_task(
                task.id,
                "human:emp_alice",
                0,
                actor=employees["alice"],
                owner_employee_id=employees["alice"],
            )
            is False
        )

        row = collab_store.get_board_task(task.id)
        assert row is not None
        assert row["owner_employee_id"] == employees["bob"]
        assert row["claimed_by"] == "human:emp_bob"
        assert row["claim_version"] == 1
        assign = [
            event
            for event in TeamStore(collab_store._store).list_events(task.id)
            if event["event"] == "assign"
        ]
        assert assign[-1]["note"] == "owner:emp_bob"

    def test_eight_employee_owners_race_and_the_row_matches_the_only_winner(
        self, collab_store: CollabStore, team_store: TeamStore
    ) -> None:
        goals = CompanyGoalsStore(collab_store._store)
        owners = [f"emp_r{index}" for index in range(_RACERS)]
        for owner in owners:
            goals.ensure_employee(employee_id=owner, name=owner, role="team")
        task = BoardTask(title="Contended pool card")
        collab_store.create_board_task(task)
        barrier = Barrier(_RACERS)

        def claim(owner: str) -> tuple[str, bool]:
            barrier.wait()
            won = collab_store.claim_task(
                task.id,
                f"human:{owner}",
                0,
                actor=owner,
                owner_employee_id=owner,
            )
            return owner, won

        with ThreadPoolExecutor(max_workers=_RACERS) as pool:
            results = [
                future.result() for future in [pool.submit(claim, owner) for owner in owners]
            ]

        winners = [owner for owner, won in results if won]
        row = collab_store.get_board_task(task.id)
        assert row is not None
        assert len(winners) == 1
        assert row["owner_employee_id"] == winners[0]
        assert row["claimed_by"] == f"human:{winners[0]}"
        assert row["claim_version"] == 1
        moves = [
            event for event in team_store.list_events(task.id) if event["event"] == "status_change"
        ]
        assert len(moves) == 1

    def test_eight_agents_race_and_exactly_one_wins(self, collab_store: CollabStore) -> None:
        agents = [Agent(name=f"racer-{i}", expertise=["coding"]) for i in range(_RACERS)]
        for agent in agents:
            collab_store.register_agent(agent)
        task = BoardTask(title="Hot potato", required_expertise=["coding"])
        collab_store.create_board_task(task)

        barrier = Barrier(_RACERS)

        def race(agent_id: str) -> bool:
            barrier.wait()
            return collab_store.claim_task(task.id, agent_id, 0)

        with ThreadPoolExecutor(max_workers=_RACERS) as pool:
            results = [
                future.result() for future in [pool.submit(race, agent.id) for agent in agents]
            ]

        assert results.count(True) == 1
        row = collab_store.get_board_task(task.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.CLAIMED.value
        assert row["claim_version"] == 1
        assert row["claimed_by"] in {agent.id for agent in agents}

    def test_winning_claim_records_the_actor_in_the_same_trail(
        self, collab_store: CollabStore, team_store: TeamStore
    ) -> None:
        agent = Agent(name="human claimant", expertise=["coding"])
        collab_store.register_agent(agent)
        task = BoardTask(title="Claim with audit")
        collab_store.create_board_task(task)

        assert collab_store.claim_task(task.id, agent.id, 0, actor="emp_bob") is True
        event = team_store.list_events(task.id)[-1]
        assert event["event"] == "status_change"
        assert event["actor"] == "emp_bob"
        assert event["from_status"] == BoardTaskStatus.OPEN.value
        assert event["to_status"] == BoardTaskStatus.CLAIMED.value
        assert event["note"] == agent.id


class TestCompareAndSet:
    def test_a_stale_expect_status_loses_without_writing(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        """The 409-equivalent: ``False``, and the row is untouched. (It is not
        an exception — every existing caller reads the boolean.)"""
        card = make_card(title="Moving target")
        assert (
            collab_store.update_board_task(
                card.id,
                {"status": BoardTaskStatus.IN_PROGRESS.value},
                expect_status=BoardTaskStatus.OPEN.value,
            )
            is True
        )
        assert (
            collab_store.update_board_task(
                card.id,
                {"status": BoardTaskStatus.DONE.value},
                expect_status=BoardTaskStatus.OPEN.value,
            )
            is False
        )
        row = collab_store.get_board_task(card.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.IN_PROGRESS.value

    def test_a_lost_cas_writes_no_event(
        self, collab_store: CollabStore, team_store: TeamStore, make_card: Callable[..., BoardTask]
    ) -> None:
        card = make_card(title="Quiet loser")
        before = len(team_store.list_events(card.id))
        collab_store.update_board_task(
            card.id, {"title": "renamed"}, expect_status=BoardTaskStatus.DONE.value
        )
        assert len(team_store.list_events(card.id)) == before

    def test_eight_racers_on_one_cas_produce_one_winner(
        self, collab_store: CollabStore, team_store: TeamStore, make_card: Callable[..., BoardTask]
    ) -> None:
        card = make_card(title="One transition")
        barrier = Barrier(_RACERS)

        def race(index: int) -> bool:
            barrier.wait()
            return collab_store.update_board_task(
                card.id,
                {"status": BoardTaskStatus.IN_PROGRESS.value, "result_ref": f"run_{index}"},
                expect_status=BoardTaskStatus.OPEN.value,
                actor=f"emp_{index}",
            )

        with ThreadPoolExecutor(max_workers=_RACERS) as pool:
            results = [f.result() for f in [pool.submit(race, i) for i in range(_RACERS)]]

        assert results.count(True) == 1
        # One winner, one status_change event: the trail counts mutations, not
        # attempts.
        moves = [e for e in team_store.list_events(card.id) if e["event"] == "status_change"]
        assert len(moves) == 1


class TestTheDoneGateSurvivesARace:
    @pytest.mark.parametrize("attempt", range(4))
    def test_a_parent_never_lands_done_beside_an_open_subtask(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        attempt: int,
    ) -> None:
        """One thread finishes the parent, another re-opens the subtask. Whoever
        wins, the pair must never end up (parent done, subtask open) — that is
        the state the gate exists to make unreachable."""
        parent = make_card(
            title=f"Parent {attempt}",
            owner_employee_id=employees["bob"],
            acceptance_criteria="everything below is finished",
        )
        subtask = make_card(title=f"Subtask {attempt}", parent_task_id=parent.id)
        team_store.add_evidence(kind="commit", ref=f"sha-{attempt}", task_id=parent.id)
        collab_store.update_board_task(subtask.id, {"status": BoardTaskStatus.DONE.value})

        barrier = Barrier(2)

        def finish_parent() -> object:
            barrier.wait()
            try:
                return collab_store.update_board_task(
                    parent.id, {"status": BoardTaskStatus.DONE.value}
                )
            except ValueError:
                return "refused"

        def reopen_subtask() -> object:
            barrier.wait()
            return collab_store.update_board_task(
                subtask.id, {"status": BoardTaskStatus.IN_PROGRESS.value}
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            finish_future = pool.submit(finish_parent)
            reopen_future = pool.submit(reopen_subtask)
            finished = finish_future.result()
            assert reopen_future.result() is True

        parent_row = collab_store.get_board_task(parent.id)
        subtask_row = collab_store.get_board_task(subtask.id)
        assert parent_row is not None
        assert subtask_row is not None
        assert subtask_row["status"] == BoardTaskStatus.IN_PROGRESS.value
        # Exactly the two SERIAL outcomes, and nothing in between: either the
        # parent finished before the subtask reopened (True, and it is done), or
        # it saw the reopened subtask and was refused (and it is not done).
        # "Refused, but done anyway" and "granted, but not done" are both the
        # shape of a validator reading outside its own transaction.
        if finished == "refused":
            assert parent_row["status"] != BoardTaskStatus.DONE.value
        else:
            assert finished is True
            assert parent_row["status"] == BoardTaskStatus.DONE.value
            assert any(
                event["to_status"] == BoardTaskStatus.DONE.value
                for event in team_store.list_events(parent.id)
            )
