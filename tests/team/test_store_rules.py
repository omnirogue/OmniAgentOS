"""The write rules migration 123 arms on ``CollabStore.update_board_task``.

Each rule is scoped on purpose. The board is shared with every agent path in
the repo — the swarm scheduler blocks its own cards with no human-readable
reason and finishes root cards with no evidence — so a rule that bound EVERY
card would not be a stricter product, it would be a broken scheduler. The gates
bind the HUMAN contract: a card that names an owner (and, for done, acceptance
criteria). Cards without one behave exactly as they did before 123, and the
last class here proves it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.team.store import TeamStore


@pytest.fixture
def owned(make_card: Callable[..., BoardTask], employees: dict[str, str]) -> BoardTask:
    return make_card(
        title="Human card",
        owner_employee_id=employees["bob"],
        acceptance_criteria="the gate holds",
    )


def _events(team_store: TeamStore, task_id: str) -> list[dict[str, object]]:
    return team_store.list_events(task_id)


class TestBlockedNeedsAReason:
    def test_blocking_an_owned_card_without_a_reason_is_refused(
        self, collab_store: CollabStore, owned: BoardTask
    ) -> None:
        with pytest.raises(ValueError, match="without blocked_reason"):
            collab_store.update_board_task(owned.id, {"status": BoardTaskStatus.BLOCKED.value})
        row = collab_store.get_board_task(owned.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.OPEN.value

    def test_a_reason_in_the_same_patch_is_enough(
        self, collab_store: CollabStore, team_store: TeamStore, owned: BoardTask
    ) -> None:
        assert (
            collab_store.update_board_task(
                owned.id,
                {
                    "status": BoardTaskStatus.BLOCKED.value,
                    "blocked_reason": "waiting on Alice's review",
                },
                actor="emp_bob",
            )
            is True
        )
        row = collab_store.get_board_task(owned.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.BLOCKED.value
        assert row["blocked_reason"] == "waiting on Alice's review"
        event = _events(team_store, owned.id)[-1]
        assert event["event"] == "status_change"
        assert event["from_status"] == BoardTaskStatus.OPEN.value
        assert event["to_status"] == BoardTaskStatus.BLOCKED.value
        assert event["actor"] == "emp_bob"
        assert event["note"] == "waiting on Alice's review"

    def test_a_reason_already_on_the_row_is_enough(
        self, collab_store: CollabStore, owned: BoardTask
    ) -> None:
        collab_store.update_board_task(owned.id, {"blocked_reason": "flaky CI"})
        assert (
            collab_store.update_board_task(owned.id, {"status": BoardTaskStatus.BLOCKED.value})
            is True
        )

    def test_blanking_the_reason_on_an_existing_blocked_card_is_refused(
        self, collab_store: CollabStore, owned: BoardTask
    ) -> None:
        collab_store.update_board_task(
            owned.id,
            {"status": BoardTaskStatus.BLOCKED.value, "blocked_reason": "waiting on review"},
        )
        with pytest.raises(ValueError, match="without blocked_reason"):
            collab_store.update_board_task(owned.id, {"blocked_reason": "  "})
        row = collab_store.get_board_task(owned.id)
        assert row is not None
        assert row["blocked_reason"] == "waiting on review"

    def test_an_agent_card_still_blocks_with_no_reason(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        """``SwarmScheduler._block_task`` and ``_block_remaining_for_budget``
        write exactly this patch. Refusing it would break the swarm, and the
        reason lives in the run's event stream anyway."""
        card = make_card(title="Swarm card")
        assert (
            collab_store.update_board_task(card.id, {"status": BoardTaskStatus.BLOCKED.value})
            is True
        )


class TestDoneIsEarned:
    def test_an_owned_card_with_acceptance_needs_evidence(
        self, collab_store: CollabStore, owned: BoardTask
    ) -> None:
        with pytest.raises(ValueError, match="without evidence"):
            collab_store.update_board_task(owned.id, {"status": BoardTaskStatus.DONE.value})
        row = collab_store.get_board_task(owned.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.OPEN.value

    def test_one_piece_of_evidence_opens_the_gate(
        self, collab_store: CollabStore, team_store: TeamStore, owned: BoardTask
    ) -> None:
        team_store.add_evidence(kind="commit", ref="c0ffee", repo="omnios", task_id=owned.id)
        assert (
            collab_store.update_board_task(owned.id, {"status": BoardTaskStatus.DONE.value}) is True
        )

    def test_a_card_with_no_acceptance_criteria_is_not_gated(
        self,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """Half the predicate. A card nobody wrote acceptance criteria for
        cannot be objectively verified, so gating it would only teach people to
        leave the field empty."""
        card = make_card(title="Owned but unspecified", owner_employee_id=employees["alice"])
        assert (
            collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value}) is True
        )

    def test_an_agent_card_is_not_gated(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        card = make_card(title="Agent card", acceptance_criteria="tests pass")
        assert (
            collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value}) is True
        )

    def test_adding_an_owner_to_a_done_acceptance_card_rechecks_evidence(
        self,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Late owner", acceptance_criteria="tests pass")
        collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value})
        with pytest.raises(ValueError, match="without evidence"):
            collab_store.update_board_task(card.id, {"owner_employee_id": employees["bob"]})
        assert collab_store.get_board_task(card.id)["owner_employee_id"] is None

    def test_adding_acceptance_to_a_done_owned_card_rechecks_evidence(
        self,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Late acceptance", owner_employee_id=employees["bob"])
        collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value})
        with pytest.raises(ValueError, match="without evidence"):
            collab_store.update_board_task(card.id, {"acceptance_criteria": "tests pass"})
        assert collab_store.get_board_task(card.id)["acceptance_criteria"] == ""


class TestRefUniqueness:
    def test_a_duplicate_ref_is_a_value_error_not_an_integrity_error(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        make_card(title="First", ref="PR-100")
        second = make_card(title="Second")
        with pytest.raises(ValueError, match="ref_conflict"):
            collab_store.update_board_task(second.id, {"ref": "PR-100"})
        with pytest.raises(ValueError, match="ref_conflict"):
            make_card(title="Third", ref="PR-100")

    def test_a_card_can_keep_its_own_ref(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        card = make_card(title="Stable", ref="PR-101")
        assert collab_store.update_board_task(card.id, {"ref": "PR-101"}) is True

    def test_blank_refs_never_collide(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        """``''`` would occupy the partial unique index and make the SECOND
        unreffed card collide with the first. Blank collapses to NULL."""
        make_card(title="No ref A", ref="")
        make_card(title="No ref B", ref="   ")
        card = make_card(title="No ref C")
        assert collab_store.update_board_task(card.id, {"ref": ""}) is True
        rows = collab_store.list_board_tasks()
        assert [row["ref"] for row in rows] == [None, None, None]

    def test_refs_are_uppercased_on_create_and_update(
        self, collab_store: CollabStore, make_card: Callable[..., BoardTask]
    ) -> None:
        created = make_card(title="Created lowercase", ref="u3")
        updated = make_card(title="Updated lowercase")
        assert collab_store.get_board_task(created.id)["ref"] == "U3"
        assert collab_store.update_board_task(updated.id, {"ref": "ops-2"}) is True
        assert collab_store.get_board_task(updated.id)["ref"] == "OPS-2"
        with pytest.raises(ValueError, match="ref_conflict"):
            make_card(title="Case duplicate", ref="U3")


class TestTheAuditTrail:
    def test_creation_writes_a_create_event(
        self, team_store: TeamStore, make_card: Callable[..., BoardTask]
    ) -> None:
        card = make_card(title="Fresh")
        events = _events(team_store, card.id)
        assert [event["event"] for event in events] == ["create"]
        assert events[0]["to_status"] == BoardTaskStatus.OPEN.value
        assert events[0]["from_status"] is None

    def test_status_moves_record_from_and_to(
        self, collab_store: CollabStore, team_store: TeamStore, make_card: Callable[..., BoardTask]
    ) -> None:
        card = make_card(title="Travels")
        collab_store.update_board_task(
            card.id, {"status": BoardTaskStatus.IN_PROGRESS.value}, actor="emp_alice"
        )
        collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value})
        moves = [
            (event["from_status"], event["to_status"])
            for event in _events(team_store, card.id)
            if event["event"] == "status_change"
        ]
        assert moves == [
            (BoardTaskStatus.OPEN.value, BoardTaskStatus.IN_PROGRESS.value),
            (BoardTaskStatus.IN_PROGRESS.value, BoardTaskStatus.DONE.value),
        ]

    def test_an_ownership_change_is_an_assign_event(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Handover")
        collab_store.update_board_task(
            card.id, {"owner_employee_id": employees["alice"]}, actor="emp_owner"
        )
        last = _events(team_store, card.id)[-1]
        assert last["event"] == "assign"
        assert last["actor"] == "emp_owner"
        assert last["note"] == f"owner:{employees['alice']}"

    def test_status_and_ownership_change_append_both_events(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Move and hand over")
        collab_store.update_board_task(
            card.id,
            {
                "status": BoardTaskStatus.IN_PROGRESS.value,
                "owner_employee_id": employees["alice"],
            },
            actor="emp_owner",
        )

        mutation_events = _events(team_store, card.id)[-2:]
        assert [event["event"] for event in mutation_events] == ["status_change", "assign"]
        assert mutation_events[0]["to_status"] == BoardTaskStatus.IN_PROGRESS.value
        assert mutation_events[1]["note"] == f"owner:{employees['alice']}"
        prefix, owner = str(mutation_events[1]["note"]).split(":", 1)
        assert prefix == "owner"
        assert owner == employees["alice"]

    def test_a_plain_field_edit_is_a_comment_event(
        self, collab_store: CollabStore, team_store: TeamStore, make_card: Callable[..., BoardTask]
    ) -> None:
        card = make_card(title="Retitled")
        collab_store.update_board_task(card.id, {"title": "Retitled again"})
        last = _events(team_store, card.id)[-1]
        assert last["event"] == "comment"
        assert last["note"] == "title"

    def test_the_trail_reads_in_the_order_it_was_written(
        self, collab_store: CollabStore, team_store: TeamStore, make_card: Callable[..., BoardTask]
    ) -> None:
        """``created_at`` is second-resolution, so a burst of writes lands on
        ONE timestamp. Ties must break on insertion order — breaking them on the
        random uuid reports moves in an order that never happened."""
        card = make_card(title="Busy second")
        for status in (
            BoardTaskStatus.IN_PROGRESS.value,
            BoardTaskStatus.AWAITING_APPROVAL.value,
            BoardTaskStatus.DONE.value,
        ):
            collab_store.update_board_task(card.id, {"status": status})
        events = _events(team_store, card.id)
        assert [event["event"] for event in events] == [
            "create",
            "status_change",
            "status_change",
            "status_change",
        ]
        assert [event["to_status"] for event in events][1:] == [
            BoardTaskStatus.IN_PROGRESS.value,
            BoardTaskStatus.AWAITING_APPROVAL.value,
            BoardTaskStatus.DONE.value,
        ]

    def test_a_refused_mutation_leaves_no_event(
        self, collab_store: CollabStore, team_store: TeamStore, owned: BoardTask
    ) -> None:
        """The event and the mutation share one transaction, so a refusal must
        roll BOTH back — an audit trail with a move the row never made is worse
        than none."""
        before = len(_events(team_store, owned.id))
        with pytest.raises(ValueError):
            collab_store.update_board_task(owned.id, {"status": BoardTaskStatus.DONE.value})
        assert len(_events(team_store, owned.id)) == before

    def test_a_missing_card_is_a_false_not_an_event(
        self, collab_store: CollabStore, team_store: TeamStore
    ) -> None:
        assert collab_store.update_board_task("btk_nope", {"title": "ghost"}) is False
        assert _events(team_store, "btk_nope") == []


class TestProtectedColumns:
    def test_verification_columns_are_not_patchable(
        self, collab_store: CollabStore, owned: BoardTask
    ) -> None:
        """Verification is the one claim a card's owner must not be able to make
        for themselves — the same protection ``claimed_by`` has against
        ``claim_task``. It is settable only through ``TeamStore.verify_task``."""
        for column in ("verified_at", "verified_by"):
            with pytest.raises(ValueError, match="unknown columns"):
                collab_store.update_board_task(owned.id, {column: "2026-08-10T00:00:00Z"})

    def test_claimed_by_is_still_refused(self, collab_store: CollabStore, owned: BoardTask) -> None:
        with pytest.raises(ValueError, match="unknown columns"):
            collab_store.update_board_task(owned.id, {"claimed_by": "agt_x"})

    def test_create_forces_verification_columns_null(
        self, collab_store: CollabStore, team_store: TeamStore
    ) -> None:
        card = BoardTask(
            title="Cannot arrive verified",
            verified_at="2026-08-01T00:00:00Z",
            verified_by="emp_owner",
        )
        collab_store.create_board_task(card)
        stored = collab_store.get_board_task(card.id)
        assert stored is not None
        assert stored["verified_at"] is None
        assert stored["verified_by"] is None
        assert [event["event"] for event in team_store.list_events(card.id)] == ["create"]


class TestVerifiedAndBaselineImmutability:
    def test_verified_size_is_frozen_except_for_the_operator(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Counted work", owner_employee_id=employees["bob"], size="M")
        collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value})
        team_store.verify_task(card.id, employees["alice"])

        with pytest.raises(ValueError, match="verified_task_immutable_size"):
            collab_store.update_board_task(card.id, {"size": "L"}, actor=employees["bob"])
        assert collab_store.get_board_task(card.id)["size"] == "M"
        assert collab_store.update_board_task(card.id, {"size": "S"}, actor=employees["owner"])
        assert collab_store.get_board_task(card.id)["size"] == "S"

    @pytest.mark.parametrize(
        "fields",
        [
            {"size": "L"},
            {"status": BoardTaskStatus.DONE.value},
            {"owner_employee_id": "emp_alice"},
        ],
    )
    def test_baseline_patch_is_refused_even_for_the_operator(
        self,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        fields: dict[str, str],
    ) -> None:
        card = make_card(
            title="Baseline record",
            owner_employee_id=employees["bob"],
            source="baseline-2026-08-03",
        )
        with pytest.raises(ValueError, match="baseline_immutable"):
            collab_store.update_board_task(card.id, fields, actor=employees["owner"])
