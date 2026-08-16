"""Evidence: idempotent collection, the unattributed inbox, and verification.

The properties here are the ones a productivity number rests on. If collection
is not idempotent, re-running a sweep inflates output. If unattributable
artifacts are guessed at instead of parked, the number is confidently wrong. If
a person can verify their own unmachine-checked work, "verified" means nothing.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.team.store import TeamStore


@pytest.fixture
def card(make_card: Callable[..., BoardTask], employees: dict[str, str]) -> BoardTask:
    return make_card(title="Owned work", owner_employee_id=employees["bob"])


@pytest.fixture
def other_card(make_card: Callable[..., BoardTask], employees: dict[str, str]) -> BoardTask:
    return make_card(title="Other work", owner_employee_id=employees["alice"])


def _finish(collab_store: CollabStore, card: BoardTask) -> None:
    collab_store.update_board_task(card.id, {"status": BoardTaskStatus.DONE.value})


class TestIdempotentCollection:
    def test_the_same_artifact_twice_is_one_row(
        self, team_store: TeamStore, card: BoardTask
    ) -> None:
        first = team_store.add_evidence(kind="commit", ref="abc123", repo="omnios", task_id=card.id)
        second = team_store.add_evidence(
            kind="commit", ref="abc123", repo="omnios", task_id=card.id
        )
        assert first == second
        assert len(team_store.list_evidence(card.id)) == 1

    def test_the_key_is_kind_repo_and_ref_together(
        self, team_store: TeamStore, card: BoardTask
    ) -> None:
        """The same sha in a different repo, or the same ref of a different
        kind, is a DIFFERENT artifact — the key is all three."""
        ids = {
            team_store.add_evidence(kind="commit", ref="abc", repo="omnios", task_id=card.id),
            team_store.add_evidence(kind="commit", ref="abc", repo="dashboard", task_id=card.id),
            team_store.add_evidence(kind="pr", ref="abc", repo="omnios", task_id=card.id),
        }
        assert len(ids) == 3

    def test_a_re_collection_never_re_points_an_existing_row(
        self, team_store: TeamStore, card: BoardTask, other_card: BoardTask
    ) -> None:
        """A second sighting is not new information. Silently moving the row
        would let a sweep undo a human correction without saying so."""
        evidence_id = team_store.add_evidence(kind="pr", ref="7", repo="omnios", task_id=card.id)
        again = team_store.add_evidence(kind="pr", ref="7", repo="omnios", task_id=other_card.id)
        assert again == evidence_id
        stored = team_store.get_evidence(evidence_id)
        assert stored is not None
        assert stored["task_id"] == card.id

    def test_attaching_evidence_writes_an_event(
        self, team_store: TeamStore, card: BoardTask
    ) -> None:
        team_store.add_evidence(kind="test_run", ref="run-1", task_id=card.id, actor="emp_bob")
        events = [
            event for event in team_store.list_events(card.id) if event["event"] == "evidence"
        ]
        assert len(events) == 1
        assert events[0]["actor"] == "emp_bob"

    def test_an_unknown_kind_names_the_field(self, team_store: TeamStore) -> None:
        with pytest.raises(ValueError, match="kind must be one of"):
            team_store.add_evidence(kind="telepathy", ref="x")


class TestTheUnattributedInbox:
    def test_evidence_with_no_card_is_parked_not_guessed(self, team_store: TeamStore) -> None:
        team_store.add_evidence(kind="commit", ref="orphan1", repo="omnios")
        team_store.add_evidence(kind="commit", ref="orphan2", repo="omnios")
        inbox = team_store.list_unattributed()
        assert [row["ref"] for row in inbox] == ["orphan1", "orphan2"]

    def test_attributed_evidence_leaves_the_inbox(
        self, team_store: TeamStore, card: BoardTask
    ) -> None:
        evidence_id = team_store.add_evidence(kind="commit", ref="orphan", repo="omnios")
        assert len(team_store.list_unattributed()) == 1
        team_store.reattribute_evidence(evidence_id, card.id, actor="emp_owner")
        assert team_store.list_unattributed() == []
        assert [row["id"] for row in team_store.list_evidence(card.id)] == [evidence_id]


class TestReattribution:
    def test_it_records_the_prior_link_and_marks_the_row_manual(
        self, team_store: TeamStore, card: BoardTask, other_card: BoardTask
    ) -> None:
        evidence_id = team_store.add_evidence(
            kind="commit", ref="moved", repo="omnios", task_id=card.id
        )
        assert team_store.is_manual(evidence_id) is False
        updated = team_store.reattribute_evidence(evidence_id, other_card.id, actor="emp_owner")
        assert updated is not None
        assert updated["task_id"] == other_card.id
        assert updated["attribution"] == "manual"
        assert updated["meta"]["correction"] == {
            "from_task_id": card.id,
            "to_task_id": other_card.id,
            "actor": "emp_owner",
            "at": updated["meta"]["correction"]["at"],
        }
        assert team_store.is_manual(evidence_id) is True

    def test_both_trails_record_the_move(
        self, team_store: TeamStore, card: BoardTask, other_card: BoardTask
    ) -> None:
        """Neither card's history may have an unexplained gap: one loses the
        evidence, the other gains it, and both say so."""
        evidence_id = team_store.add_evidence(
            kind="commit", ref="moved", repo="omnios", task_id=card.id
        )
        team_store.reattribute_evidence(evidence_id, other_card.id, actor="emp_owner")
        assert any("detached" in str(e["note"]) for e in team_store.list_events(card.id))
        assert any("attached" in str(e["note"]) for e in team_store.list_events(other_card.id))

    def test_evidence_can_be_returned_to_the_inbox(
        self, team_store: TeamStore, card: BoardTask
    ) -> None:
        evidence_id = team_store.add_evidence(
            kind="commit", ref="wrong", repo="omnios", task_id=card.id
        )
        updated = team_store.reattribute_evidence(evidence_id, None, actor="emp_owner")
        assert updated is not None
        assert updated["task_id"] is None
        assert updated["meta"]["correction"]["to_task_id"] is None
        assert [row["id"] for row in team_store.list_unattributed()] == [evidence_id]

    def test_an_unknown_target_card_is_refused(
        self, team_store: TeamStore, card: BoardTask
    ) -> None:
        evidence_id = team_store.add_evidence(
            kind="commit", ref="x", repo="omnios", task_id=card.id
        )
        with pytest.raises(ValueError, match="task not found"):
            team_store.reattribute_evidence(evidence_id, "btk_nope", actor="emp_owner")
        stored = team_store.get_evidence(evidence_id)
        assert stored is not None
        assert stored["task_id"] == card.id
        assert stored["attribution"] == "deterministic"

    def test_unknown_evidence_is_none_not_an_error(self, team_store: TeamStore) -> None:
        assert team_store.reattribute_evidence("tev_nope", None, actor="emp_owner") is None

    def test_is_manual_is_false_for_a_row_that_does_not_exist(self, team_store: TeamStore) -> None:
        """The sweep guard fails CLOSED on absence in the safe direction: a
        missing row is not a human correction, but it is also not overwritable."""
        assert team_store.is_manual("tev_nope") is False


class TestVerification:
    def test_only_a_done_card_can_be_verified(self, team_store: TeamStore, card: BoardTask) -> None:
        with pytest.raises(ValueError, match="only a done task"):
            team_store.verify_task(card.id, "emp_alice")

    def test_mechanical_evidence_lets_the_owner_verify(
        self, collab_store: CollabStore, team_store: TeamStore, card: BoardTask
    ) -> None:
        """A passing test_run is the TEST RUNNER's claim, not the owner's, so
        there is nobody to counter-sign."""
        team_store.add_evidence(
            kind="test_run", ref="run-9", repo="omnios", task_id=card.id, quality_gate="pass"
        )
        _finish(collab_store, card)
        verified = team_store.verify_task(card.id, "emp_bob")
        assert verified is not None
        assert verified["verified_by"] == "emp_bob"
        assert verified["verified_at"]
        assert team_store.list_events(card.id)[-1]["note"] == "mechanical"

    def test_failed_mechanical_evidence_does_not_count(
        self, collab_store: CollabStore, team_store: TeamStore, card: BoardTask
    ) -> None:
        """A REVERTED commit or a REJECTED review is evidence the work happened
        AND that it did not land. It must not open the mechanical path."""
        team_store.add_evidence(
            kind="test_run", ref="run-red", task_id=card.id, quality_gate="rejected"
        )
        team_store.add_evidence(kind="pr", ref="pr-rev", task_id=card.id, quality_gate="reverted")
        _finish(collab_store, card)
        with pytest.raises(ValueError, match="cannot verify their own task"):
            team_store.verify_task(card.id, "emp_bob")

    def test_manual_test_run_does_not_let_the_owner_self_verify(
        self, collab_store: CollabStore, team_store: TeamStore, card: BoardTask
    ) -> None:
        team_store.add_evidence(
            kind="test_run",
            ref="manual-run",
            task_id=card.id,
            actor="emp_bob",
            attribution="manual",
            quality_gate="pass",
        )
        _finish(collab_store, card)
        with pytest.raises(ValueError, match="cannot verify their own task"):
            team_store.verify_task(card.id, "emp_bob")

    def test_a_non_mechanical_card_needs_a_second_person(
        self, collab_store: CollabStore, team_store: TeamStore, card: BoardTask
    ) -> None:
        team_store.add_evidence(kind="doc", ref="notes.md", task_id=card.id)
        _finish(collab_store, card)
        with pytest.raises(ValueError, match="cannot verify their own task"):
            team_store.verify_task(card.id, "emp_bob")
        verified = team_store.verify_task(card.id, "emp_alice")
        assert verified is not None
        assert verified["verified_by"] == "emp_alice"
        assert team_store.list_events(card.id)[-1]["note"] == "human"

    def test_the_operator_may_verify_their_own_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """The operator has nobody to counter-sign with; without this exemption
        their cards could never be verified at all."""
        own = make_card(title="Operator work", owner_employee_id=employees["owner"])
        _finish(collab_store, own)
        verified = team_store.verify_task(own.id, employees["owner"])
        assert verified is not None
        assert verified["verified_by"] == employees["owner"]

    def test_an_unowned_card_is_verifiable_by_anyone(
        self, collab_store: CollabStore, team_store: TeamStore, make_card: Callable[..., BoardTask]
    ) -> None:
        agent_card = make_card(title="Agent work")
        _finish(collab_store, agent_card)
        assert team_store.verify_task(agent_card.id, "emp_alice") is not None

    def test_unverify_clears_the_stamp_and_names_who_withdrew_it(
        self, collab_store: CollabStore, team_store: TeamStore, card: BoardTask
    ) -> None:
        team_store.add_evidence(kind="test_run", ref="run-10", task_id=card.id)
        _finish(collab_store, card)
        team_store.verify_task(card.id, "emp_alice")
        cleared = team_store.unverify_task(card.id, actor="emp_owner")
        assert cleared is not None
        assert cleared["verified_at"] is None
        assert cleared["verified_by"] is None
        last = team_store.list_events(card.id)[-1]
        assert last["event"] == "unverify"
        assert last["actor"] == "emp_owner"
        assert last["note"] == "emp_alice"

    def test_reverify_restores_the_first_verify_timestamp(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        card: BoardTask,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import omniagentos.collab.store as collab_store_module
        import omniagentos.team.store as team_store_module

        team_store.add_evidence(kind="doc", ref="reverify-doc", task_id=card.id)
        _finish(collab_store, card)
        clock = {"now": "2026-08-01T01:00:00Z"}
        monkeypatch.setattr(team_store_module, "utc_now_iso", lambda: clock["now"])
        monkeypatch.setattr(collab_store_module, "utc_now_iso", lambda: clock["now"])

        first = team_store.verify_task(card.id, "emp_alice")
        assert first is not None
        first_stamp = first["verified_at"]
        clock["now"] = "2026-08-05T02:00:00Z"
        team_store.unverify_task(card.id, actor="emp_owner")
        clock["now"] = "2026-08-10T03:00:00Z"
        second = team_store.verify_task(card.id, "emp_alice")

        assert second is not None
        assert second["verified_at"] == first_stamp == "2026-08-01T01:00:00Z"
        assert [
            event["created_at"]
            for event in team_store.list_events(card.id)
            if event["event"] == "verify"
        ] == ["2026-08-01T01:00:00Z", "2026-08-10T03:00:00Z"]

    def test_baseline_refuses_unverify_even_for_the_operator(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
    ) -> None:
        baseline = BoardTask(
            title="Imported baseline",
            status=BoardTaskStatus.DONE,
            owner_employee_id=employees["bob"],
            source="baseline-2026-08-03",
        )
        collab_store.create_board_task(baseline)
        verified = team_store.verify_task(baseline.id, employees["alice"])
        assert verified is not None
        with pytest.raises(ValueError, match="baseline_immutable"):
            team_store.unverify_task(baseline.id, actor=employees["owner"])
        stored = collab_store.get_board_task(baseline.id)
        assert stored is not None
        assert stored["verified_at"] == verified["verified_at"]

    def test_a_missing_card_verifies_to_none(self, team_store: TeamStore) -> None:
        assert team_store.verify_task("btk_nope", "emp_owner") is None
        assert team_store.unverify_task("btk_nope", actor="emp_owner") is None
