"""The THIRD completion state (migration 132): a verification that was refused.

Before 131 a verifier who looked at a done card and found it wanting had one
move — say nothing — and the card read "done, unverified", which is also what a
card nobody has looked at reads. These tests pin the difference, and the four
state transitions around it: refuse, repair-by-verify, reopen-clears, and the
one card class that may never be touched at all.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omniagentos.collab.contracts import BASELINE_SOURCE, BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.team.store import TeamStore, completion_state


@pytest.fixture
def done_card(
    make_card: Callable[..., BoardTask],
    collab_store: CollabStore,
    team_store: TeamStore,
    employees: dict[str, str],
) -> BoardTask:
    """An owned, evidence-backed card that has legitimately reached done."""
    card = make_card(
        title="Ship the thing",
        owner_employee_id=employees["bob"],
        acceptance_criteria="the thing ships",
    )
    team_store.add_evidence(kind="commit", ref="deadbeef", repo="omnios", task_id=card.id)
    collab_store.update_board_task(
        card.id, {"status": BoardTaskStatus.DONE.value}, actor=employees["bob"]
    )
    return card


def _row(collab_store: CollabStore, task_id: str) -> dict[str, object]:
    row = collab_store.get_board_task(task_id)
    assert row is not None
    return row


class TestTheRefusal:
    def test_failing_stamps_the_card_and_writes_the_event(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        done_card: BoardTask,
        employees: dict[str, str],
    ) -> None:
        card = team_store.fail_verification(done_card.id, employees["alice"], "no tests")
        assert card is not None
        assert card["verification_failed_by"] == employees["alice"]
        assert card["verification_failed_reason"] == "no tests"
        assert completion_state(_row(collab_store, done_card.id)) == "failed_verification"
        event = team_store.list_events(done_card.id)[-1]
        assert event["event"] == "verify_failed"
        assert event["note"] == "no tests"
        assert event["actor"] == employees["alice"]

    def test_a_reason_is_required(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        with pytest.raises(ValueError, match="reason is required"):
            team_store.fail_verification(done_card.id, employees["alice"], "   ")

    def test_the_owner_may_not_refuse_their_own_card(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        with pytest.raises(ValueError, match="cannot fail-verify their own"):
            team_store.fail_verification(done_card.id, employees["bob"], "not good enough")

    def test_the_operator_may_refuse_their_own_card(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """Same exemption the human verify path grants: nobody counter-signs the operator."""
        card = make_card(
            title="Operator card",
            owner_employee_id=employees["owner"],
            acceptance_criteria="it works",
        )
        team_store.add_evidence(kind="note", ref="n1", task_id=card.id)
        collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value})
        assert team_store.fail_verification(card.id, employees["owner"], "on reflection, no")

    def test_only_a_done_card_can_fail(
        self, team_store: TeamStore, make_card: Callable[..., BoardTask], employees: dict[str, str]
    ) -> None:
        card = make_card(title="Open card", owner_employee_id=employees["bob"])
        with pytest.raises(ValueError, match="only a done task"):
            team_store.fail_verification(card.id, employees["alice"], "not finished")

    def test_an_absent_card_is_none_not_an_error(
        self, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        assert team_store.fail_verification("btk_nope", employees["alice"], "x") is None

    def test_failing_clears_both_verification_stamps(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        """review S4: a card must never read verified AND failed at once."""
        team_store.verify_task(done_card.id, employees["alice"])
        card = team_store.fail_verification(done_card.id, employees["alice"], "regression found")
        assert card is not None
        assert card["verified_at"] is None
        assert card["verified_by"] is None
        assert completion_state(card) == "failed_verification"

    def test_a_baseline_card_is_immutable(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """review S3 (BLOCKER): the baseline is everyone's production_x
        denominator. A refusal that unstamped one would shrink the denominator
        and inflate the ratio — so it is refused, operator included."""
        card = make_card(title="Baseline card", source=BASELINE_SOURCE)
        collab_store._connection.execute(
            "UPDATE board_tasks SET status = ?, verified_at = ?, verified_by = ? WHERE id = ?",
            (BoardTaskStatus.DONE.value, "2026-08-03T00:00:00Z", "emp_owner", card.id),
        )
        collab_store._connection.commit()
        for verifier in (employees["alice"], employees["owner"]):
            with pytest.raises(ValueError, match="baseline_immutable"):
                team_store.fail_verification(card.id, verifier, "looks wrong")
        row = _row(collab_store, card.id)
        assert row["verified_at"] == "2026-08-03T00:00:00Z"


class TestRepairAndWithdrawal:
    def test_a_later_good_verify_clears_the_failure(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        team_store.fail_verification(done_card.id, employees["alice"], "no tests")
        card = team_store.verify_task(done_card.id, employees["alice"])
        assert card is not None
        assert card["verification_failed_at"] is None
        assert card["verification_failed_by"] is None
        assert card["verification_failed_reason"] is None
        assert completion_state(card) == "verified"
        # The reason survives the repair, in the append-only trail.
        notes = [
            event["note"]
            for event in team_store.list_events(done_card.id)
            if event["event"] == "verify_failed"
        ]
        assert notes == ["no tests"]

    def test_unverify_never_touches_the_failure_stamps(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        """round-3 §4: withdrawing a PASS says nothing about a refusal. Only a
        successful verify or a reopen clears failure state."""
        team_store.fail_verification(done_card.id, employees["alice"], "no tests")
        card = team_store.unverify_task(done_card.id, employees["alice"])
        assert card is not None
        assert card["verification_failed_at"] is not None
        assert card["verification_failed_reason"] == "no tests"
        assert completion_state(card) == "failed_verification"

    def test_reopening_the_card_clears_the_failure_too(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        done_card: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """review F3: a reopened card returns to CLEAN unverified — a verdict on
        work that is being redone describes something that no longer exists."""
        team_store.fail_verification(done_card.id, employees["alice"], "no tests")
        assert collab_store.update_board_task(
            done_card.id,
            {"status": BoardTaskStatus.IN_PROGRESS.value},
            actor=employees["bob"],
        )
        row = _row(collab_store, done_card.id)
        assert row["verification_failed_at"] is None
        assert row["verification_failed_by"] is None
        assert row["verification_failed_reason"] is None
        assert completion_state(row) is None  # not done: no completion state at all

    def test_reopening_a_verified_card_still_withdraws_the_verification(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        done_card: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """The 123 rule is unchanged by 131 — proven here so the extension
        cannot silently replace it."""
        team_store.verify_task(done_card.id, employees["alice"])
        collab_store.update_board_task(
            done_card.id,
            {"status": BoardTaskStatus.IN_PROGRESS.value},
            actor=employees["bob"],
        )
        row = _row(collab_store, done_card.id)
        assert row["verified_at"] is None
        assert row["verified_by"] is None
        assert [event["event"] for event in team_store.list_events(done_card.id)].count(
            "unverify"
        ) == 1


class TestItCorrectsYesterdaysCommitment:
    def test_a_delivered_commitment_gains_a_post_resolution_note(
        self,
        team_store: TeamStore,
        done_card: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """round-3 §6: a commitment resolved 'delivered' this morning can be
        refused this afternoon. The resolution is NOT rewritten (that would make
        yesterday's report unreproducible) — but a report that still reads a
        plain "delivered" is quietly wrong, so the correction rides the note."""
        row, _outcome = team_store.create_commitment(
            day="2026-08-14",
            employee_id=employees["bob"],
            kind="task",
            task_id=done_card.id,
            title="Ship the thing",
        )
        team_store.resolve_commitment(
            str(row["id"]), status="delivered", resolution_note="GH-7 reached done"
        )
        team_store.fail_verification(done_card.id, employees["alice"], "no tests")
        updated = team_store.get_commitment(str(row["id"]))
        assert updated is not None
        assert updated["status"] == "delivered", "history is preserved, not rewritten"
        assert "GH-7 reached done" in str(updated["resolution_note"])
        assert "verification failed post-resolution" in str(updated["resolution_note"])

    def test_the_note_is_appended_once_however_often_it_is_refused(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        row, _outcome = team_store.create_commitment(
            day="2026-08-14",
            employee_id=employees["bob"],
            kind="task",
            task_id=done_card.id,
            title="Ship the thing",
        )
        team_store.resolve_commitment(str(row["id"]), status="delivered", resolution_note="done")
        for _ in range(2):
            team_store.fail_verification(done_card.id, employees["alice"], "still no tests")
        updated = team_store.get_commitment(str(row["id"]))
        assert updated is not None
        assert str(updated["resolution_note"]).count("verification failed post-resolution") == 1

    def test_a_committed_row_is_left_alone(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        """An UNRESOLVED commitment needs no correction: ``resolve_day`` will
        read the failure state directly and resolve it 'missed' (review S7)."""
        row, _outcome = team_store.create_commitment(
            day="2026-08-14",
            employee_id=employees["bob"],
            kind="task",
            task_id=done_card.id,
            title="Ship the thing",
        )
        team_store.fail_verification(done_card.id, employees["alice"], "no tests")
        updated = team_store.get_commitment(str(row["id"]))
        assert updated is not None
        assert updated["status"] == "committed"
        assert updated["resolution_note"] == ""


class TestTheStampsAreNotPatchable:
    @pytest.mark.parametrize(
        "column",
        ["verification_failed_at", "verification_failed_by", "verification_failed_reason"],
    )
    def test_a_patch_naming_a_failure_column_is_refused(
        self, collab_store: CollabStore, done_card: BoardTask, column: str
    ) -> None:
        """Exactly like verified_at/verified_by: a verdict must not be
        settable by the person the verdict is about."""
        with pytest.raises(ValueError, match="unknown columns"):
            collab_store.update_board_task(done_card.id, {column: "2026-08-14T00:00:00Z"})


class TestCompletionState:
    def test_the_three_states_and_the_absence_of_one(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        done_card: BoardTask,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        assert completion_state(_row(collab_store, done_card.id)) == "unverified"
        team_store.fail_verification(done_card.id, employees["alice"], "nope")
        assert completion_state(_row(collab_store, done_card.id)) == "failed_verification"
        team_store.verify_task(done_card.id, employees["alice"])
        assert completion_state(_row(collab_store, done_card.id)) == "verified"
        open_card = make_card(title="Open", owner_employee_id=employees["bob"])
        assert completion_state(_row(collab_store, open_card.id)) is None


class TestAutomationMaturity:
    def test_a_known_value_patches_and_an_unknown_one_is_refused(
        self, collab_store: CollabStore, done_card: BoardTask
    ) -> None:
        assert collab_store.update_board_task(
            done_card.id,
            {"automation_maturity": "assisted", "automation_note": "auto-run the smoke suite"},
        )
        row = _row(collab_store, done_card.id)
        assert row["automation_maturity"] == "assisted"
        assert row["automation_note"] == "auto-run the smoke suite"
        with pytest.raises(ValueError, match="automation_maturity must be one of"):
            collab_store.update_board_task(done_card.id, {"automation_maturity": "magic"})

    def test_clearing_it_back_to_untracked_is_legal(
        self, collab_store: CollabStore, done_card: BoardTask
    ) -> None:
        """NULL means UNTRACKED, and un-saying a guess must stay possible."""
        collab_store.update_board_task(done_card.id, {"automation_maturity": "autonomous"})
        assert collab_store.update_board_task(done_card.id, {"automation_maturity": None})
        assert _row(collab_store, done_card.id)["automation_maturity"] is None
