"""Auto-dispatch v3: MACHINE-ONLY. The compute-pool bridge, the cap, dedupe,
fail-closed envelopes — and the proof that the human-assignment path is gone.

the operator's ruling (2026-08-13): no auto-assignment of tasks to people. The pool is
drained by ``/task claim`` and the operator/Alice delegation; this daemon only enqueues
``org_json.dispatch.target == 'compute-pool'`` cards to the wq-server.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.team import dispatch
from omniagentos.team.dispatch import DispatchAction, dispatch_once


class FakeQueueClient:
    def __init__(self, *, deduped: bool = False, fail: bool = False) -> None:
        self.submits: list[dict[str, Any]] = []
        self.deduped = deduped
        self.fail = fail

    def enqueue(self, submit: dict[str, Any]) -> tuple[str, bool]:
        if self.fail:
            raise OSError("wq-server unreachable")
        self.submits.append(submit)
        return f"wq_TEST{len(self.submits)}", self.deduped


@pytest.fixture
def goal_id(store: SqliteStore, goals_store: CompanyGoalsStore, employees: dict[str, str]) -> str:
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("co_acme", "acme", "ACME Corp", "active", utc_now_iso()),
    )
    goal = goals_store.create_goal(
        org_company_id="co_acme",
        title="General engineering — ACME Corp",
        horizon="quarter",
        owner_employee_id=employees["alice"],
    )
    return str(goal["id"])


@pytest.fixture
def pool_card(collab_store: CollabStore, goal_id: str) -> Callable[..., BoardTask]:
    def factory(**fields: Any) -> BoardTask:
        fields.setdefault("title", "Pool work")
        fields.setdefault("goal_id", goal_id)
        fields.setdefault("acceptance_criteria", "the pool contract")
        card = BoardTask(**fields)
        collab_store.create_board_task(card)
        return card

    return factory


def _events(store: SqliteStore, task_id: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store._connection.execute(
            "SELECT event, actor, note FROM task_events WHERE task_id = ? ORDER BY rowid",
            (task_id,),
        )
    ]


def _make_compute_pool_card(
    collab_store: CollabStore,
    pool_card: Callable[..., BoardTask],
    card_fields: dict[str, Any] | None = None,
    **envelope: Any,
) -> BoardTask:
    card = pool_card(title="Machine work", **(card_fields or {}))
    dispatch_envelope: dict[str, Any] = {
        "target": "compute-pool",
        "base_sha": "a" * 40,
        "acceptance_cmd": "pytest -q tests/smoke",
        "owned_paths": ["scripts/**"],
        **envelope,
    }
    # A key explicitly passed as None means "absent from the envelope".
    dispatch_envelope = {k: v for k, v in dispatch_envelope.items() if v is not None}
    collab_store.update_board_task(card.id, {"org_json": json.dumps({"dispatch": dispatch_envelope})})
    return card


class TestHumansAreNeverAssigned:
    """The v3 contract itself: the daemon does not touch human work."""

    def test_a_human_pool_card_gets_no_action_no_owner_no_event(
        self,
        collab_store: CollabStore,
        team_store: Any,
        store: SqliteStore,
        pool_card: Callable[..., BoardTask],
    ) -> None:
        card = pool_card(title="Human work")

        actions = dispatch_once(collab_store, team_store, wq_client=FakeQueueClient())

        assert actions == []
        row = collab_store.get_board_task(card.id)
        assert row is not None and row["owner_employee_id"] is None
        assert row["status"] == "open"  # still claimable via /task claim
        # No assign event, no comment — the card's trail is untouched.
        assert [event["event"] for event in _events(store, card.id)] == ["create"]

    def test_human_cards_do_not_consume_the_cap(
        self,
        collab_store: CollabStore,
        team_store: Any,
        pool_card: Callable[..., BoardTask],
    ) -> None:
        # Three human cards ahead of one machine card (urgent sorts the humans
        # first is irrelevant — created first, FIFO): the machine card must
        # still be reached and enqueued under cap=1.
        for index in range(3):
            pool_card(title=f"Human {index}")
        machine = _make_compute_pool_card(collab_store, pool_card)
        client = FakeQueueClient()

        actions = dispatch_once(collab_store, team_store, wq_client=client, cap=1)

        assert [(action.task_id, action.kind) for action in actions] == [(machine.id, "enqueue")]

    def test_the_human_assignment_machinery_is_gone(self) -> None:
        """Regression pin: the removed surface must not quietly return."""
        for symbol in ("_pick_assignee", "_assign_guarded", "_dm_assignment", "_Roster"):
            assert not hasattr(dispatch, symbol)


class TestComputePool:
    def test_compute_pool_card_enqueues(
        self,
        collab_store: CollabStore,
        team_store: Any,
        store: SqliteStore,
        pool_card: Callable[..., BoardTask],
    ) -> None:
        card = _make_compute_pool_card(collab_store, pool_card)
        client = FakeQueueClient()

        actions = dispatch_once(collab_store, team_store, wq_client=client)

        assert [action.kind for action in actions] == ["enqueue"]
        assert actions[0].employee_id is None  # nothing assigns humans
        (submit,) = client.submits
        assert submit["idempotency_key"] == f"team-dispatch:{card.id}"
        assert f"board_task:{card.id}" in submit["labels"]
        assert "company:acme" in submit["labels"]
        assert f"board_task: {card.id}" in submit["brief_inline"]
        assert "company: acme" in submit["brief_inline"]
        assert submit["submitted_by"] == "team-dispatch"
        assert submit["base_sha"] == "a" * 40
        # Fail-closed passthrough: both come from the envelope, never a default.
        assert submit["acceptance_cmd"] == "pytest -q tests/smoke"
        assert submit["owned_paths"] == ["scripts/**"]
        # The card stays ownerless — machines claim from the wq, not the board.
        row = collab_store.get_board_task(card.id)
        assert row is not None and row["owner_employee_id"] is None
        assert _events(store, card.id)[-1]["note"] == "auto_dispatch to compute-pool wq:wq_TEST1"

    def test_cap_bounds_fresh_enqueues(
        self,
        collab_store: CollabStore,
        team_store: Any,
        pool_card: Callable[..., BoardTask],
    ) -> None:
        for _ in range(3):
            _make_compute_pool_card(collab_store, pool_card)
        client = FakeQueueClient()

        actions = dispatch_once(collab_store, team_store, wq_client=client, cap=2)

        assert sum(1 for action in actions if action.kind == "enqueue") == 2
        assert len(client.submits) == 2

    def test_deduped_enqueue_appends_no_second_event(
        self,
        collab_store: CollabStore,
        team_store: Any,
        store: SqliteStore,
        pool_card: Callable[..., BoardTask],
    ) -> None:
        card = _make_compute_pool_card(collab_store, pool_card)
        client = FakeQueueClient(deduped=True)

        actions = dispatch_once(collab_store, team_store, wq_client=client)

        assert actions[0].kind == "skip" and "(deduped)" in actions[0].detail
        events = _events(store, card.id)
        assert all("wq:" not in event["note"] for event in events)

    def test_deduped_enqueue_does_not_consume_the_cap(
        self,
        collab_store: CollabStore,
        team_store: Any,
        pool_card: Callable[..., BoardTask],
    ) -> None:
        """The starvation class: an already-enqueued card at the front of the
        FIFO pool must not spend cap slots and starve fresh machine work."""
        # urgent makes the already-enqueued card sort ahead of the fresh one
        # even when both are created inside the same created_at second.
        first = _make_compute_pool_card(
            collab_store, pool_card, card_fields={"priority": "urgent"}
        )
        fresh = _make_compute_pool_card(collab_store, pool_card)
        dispatch_once(
            collab_store,
            team_store,
            wq_client=FakeQueueClient(),
            cap=1,
        )  # enqueues `first` (urgent sorts first)

        actions = dispatch_once(
            collab_store, team_store, wq_client=FakeQueueClient(), cap=1
        )

        kinds = {action.task_id: action.kind for action in actions}
        assert kinds[first.id] == "skip"
        assert kinds[fresh.id] == "enqueue"

    def test_already_enqueued_card_skips_without_a_wq_call(
        self,
        collab_store: CollabStore,
        team_store: Any,
        pool_card: Callable[..., BoardTask],
    ) -> None:
        _make_compute_pool_card(collab_store, pool_card)
        first = dispatch_once(collab_store, team_store, wq_client=FakeQueueClient())
        assert [action.kind for action in first] == ["enqueue"]

        # A client that would blow up if contacted proves the pre-check works.
        second = dispatch_once(collab_store, team_store, wq_client=FakeQueueClient(fail=True))
        assert [action.kind for action in second] == ["skip"]
        assert second[0].detail == "already enqueued"

    def test_missing_acceptance_cmd_or_owned_paths_refuses_the_card(
        self,
        collab_store: CollabStore,
        team_store: Any,
        pool_card: Callable[..., BoardTask],
    ) -> None:
        no_acceptance = _make_compute_pool_card(collab_store, pool_card, acceptance_cmd=None)
        no_paths = _make_compute_pool_card(collab_store, pool_card, owned_paths=None)
        # A bare string is refused too — list('scripts/**') would explode into
        # single-character globs.
        str_paths = _make_compute_pool_card(collab_store, pool_card, owned_paths="scripts/**")
        client = FakeQueueClient()

        actions = dispatch_once(collab_store, team_store, wq_client=client)

        details = {action.task_id: (action.kind, action.detail) for action in actions}
        assert details[no_acceptance.id] == ("skip", "no acceptance_cmd in envelope")
        assert details[no_paths.id] == ("skip", "no owned_paths in envelope")
        assert details[str_paths.id] == ("skip", "no owned_paths in envelope")
        assert client.submits == []

    def test_dry_run_previews_refusals_and_writes_nothing(
        self,
        collab_store: CollabStore,
        team_store: Any,
        store: SqliteStore,
        pool_card: Callable[..., BoardTask],
    ) -> None:
        enqueued = _make_compute_pool_card(collab_store, pool_card)
        dispatch_once(collab_store, team_store, wq_client=FakeQueueClient())
        broken = _make_compute_pool_card(collab_store, pool_card, acceptance_cmd=None)
        fresh = _make_compute_pool_card(collab_store, pool_card)

        actions = dispatch_once(collab_store, team_store, dry_run=True)

        details = {action.task_id: (action.kind, action.detail) for action in actions}
        assert details[enqueued.id] == ("skip", "already enqueued")
        assert details[broken.id] == ("skip", "no acceptance_cmd in envelope")
        assert details[fresh.id] == ("enqueue", "dry-run")
        # Dry-run wrote no event for the would-be enqueue.
        assert all("wq:" not in event["note"] for event in _events(store, fresh.id))

    def test_unreachable_server_is_log_and_skip(
        self,
        collab_store: CollabStore,
        team_store: Any,
        pool_card: Callable[..., BoardTask],
    ) -> None:
        machine = _make_compute_pool_card(collab_store, pool_card)
        second = _make_compute_pool_card(collab_store, pool_card)
        client = FakeQueueClient(fail=True)

        actions = dispatch_once(collab_store, team_store, wq_client=client)

        by_task = {action.task_id: action for action in actions}
        assert by_task[machine.id].kind == "skip"
        assert "wq unreachable" in by_task[machine.id].detail
        # The failure did not sink the pass: the next card was still attempted.
        assert by_task[second.id].kind == "skip"


class TestCliGate:
    def test_gate_off_is_a_clean_no_op(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(dispatch.ENV_GATE, raising=False)
        assert dispatch.main(["--once"]) == 0

    def test_once_is_required(self) -> None:
        with pytest.raises(SystemExit):
            dispatch.main([])


def test_action_serialization_shape() -> None:
    action = DispatchAction(task_id="btk_x", kind="enqueue", detail="wq:wq_1")
    assert action.as_dict() == {
        "task_id": "btk_x",
        "kind": "enqueue",
        "employee_id": None,
        "detail": "wq:wq_1",
    }
