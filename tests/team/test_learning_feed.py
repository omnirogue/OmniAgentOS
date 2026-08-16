"""The task -> metacog learning feed, and the four ways it must not misbehave.

The hook exists to turn verified human work into memory candidates. What these
tests actually defend is the NEGATIVE space around it: a learning failure must
never cost a verification, a re-verify must never mint a second candidate, and
a refusal to capture must leave evidence that it was refused — otherwise
"nothing was learned here" and "the learning path is broken" look identical.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.team import learning
from omniagentos.team.store import TeamStore


@pytest.fixture(autouse=True)
def hermetic_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Metacog blobs land in tmp — never in the checkout's var/ tree."""
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "artifacts"))


@pytest.fixture
def done_card(
    make_card: Callable[..., BoardTask],
    collab_store: CollabStore,
    team_store: TeamStore,
    employees: dict[str, str],
) -> BoardTask:
    card = make_card(
        title="Ship the thing",
        owner_employee_id=employees["bob"],
        acceptance_criteria="the thing ships",
        ref="GH-9",
        size="M",
    )
    team_store.add_evidence(kind="pr", ref="9", repo="omnios", task_id=card.id, quality_gate="pass")
    collab_store.update_board_task(
        card.id, {"status": BoardTaskStatus.DONE.value}, actor=employees["bob"]
    )
    return card


def _markers(team_store: TeamStore, task_id: str) -> list[str]:
    return [
        str(event["note"])
        for event in team_store.list_events(task_id)
        if str(event["note"]).startswith("learning_capture")
    ]


def _memory(team_store: TeamStore, candidate_id: str) -> dict[str, Any]:
    row = team_store._store._connection.execute(
        "SELECT * FROM metacog_memory_records WHERE id = ?", (candidate_id,)
    ).fetchone()
    assert row is not None, "the candidate must be readable from the shared database"
    return dict(row)


class TestTheHappyPath:
    def test_a_verified_card_files_a_procedure_candidate_with_evidence_detail(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        result = team_store.record_verification(done_card.id, employees["alice"])
        assert result is not None
        assert result.first_success is True
        candidate_id = learning.on_task_verified(
            team_store,
            result.card,
            team_store.list_evidence(done_card.id),
            event_id=result.event_id,
        )
        assert candidate_id is not None
        memory = _memory(team_store, candidate_id)
        assert memory["type"] == "procedure"
        # review S12: per-evidence detail, not a bare count.
        assert "pr omnios@9 [pass]" in str(memory["statement"])
        assert "GH-9" in str(memory["statement"])
        markers = _markers(team_store, done_card.id)
        assert len(markers) == 1
        assert candidate_id in markers[0]
        assert "outcome=procedure" in markers[0]
        # The marker names the EXACT event the store minted — not "the latest
        # verify event", which under concurrency can be somebody else's.
        assert f"event={result.event_id}" in markers[0]

    def test_a_failed_verification_files_a_lesson_even_with_no_evidence(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """A refusal is itself the artifact: metacog is fail-closed on evidence,
        and "this was refused, and why" is exactly what is worth remembering."""
        card = make_card(
            title="Half-finished",
            owner_employee_id=employees["bob"],
            acceptance_criteria="works",
        )
        team_store.add_evidence(kind="note", ref="n1", task_id=card.id)
        collab_store.update_board_task(
            card.id, {"status": BoardTaskStatus.DONE.value}, actor=employees["bob"]
        )
        failed = team_store.record_verification_failure(
            card.id, employees["alice"], "no tests at all"
        )
        assert failed is not None
        assert failed.first_success is False, "a refusal is never a success"
        candidate_id = learning.on_verification_failed(
            team_store, failed.card, [], event_id=failed.event_id, reason="no tests at all"
        )
        assert candidate_id is not None
        memory = _memory(team_store, candidate_id)
        assert memory["type"] == "lesson"
        assert "no tests at all" in str(memory["statement"])
        assert "outcome=lesson" in _markers(team_store, card.id)[0]


class TestIdempotency:
    def test_the_same_verification_never_captures_twice(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        result = team_store.record_verification(done_card.id, employees["alice"])
        assert result is not None
        evidence = team_store.list_evidence(done_card.id)
        first = learning.on_task_verified(
            team_store, result.card, evidence, event_id=result.event_id
        )
        second = learning.on_task_verified(
            team_store, result.card, evidence, event_id=result.event_id
        )
        assert first is not None
        assert second is None
        assert len(_markers(team_store, done_card.id)) == 1

    def test_a_lesson_is_not_suppressed_by_an_earlier_procedure_marker(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        """round-3 §5: the marker is keyed on (event id, outcome). A card that
        was verified, then refused, must still record the lesson."""
        verified = team_store.record_verification(done_card.id, employees["alice"])
        assert verified is not None
        learning.on_task_verified(
            team_store,
            verified.card,
            team_store.list_evidence(done_card.id),
            event_id=verified.event_id,
        )
        failed = team_store.record_verification_failure(
            done_card.id, employees["alice"], "regression"
        )
        assert failed is not None
        lesson = learning.on_verification_failed(
            team_store,
            failed.card,
            team_store.list_evidence(done_card.id),
            event_id=failed.event_id,
            reason="regression",
        )
        assert lesson is not None
        outcomes = sorted(
            note.split("outcome=")[1].split(" ")[0] for note in _markers(team_store, done_card.id)
        )
        assert outcomes == ["lesson", "procedure"]


class TestItNeverCostsAVerification:
    def test_a_metacog_failure_leaves_durable_evidence_and_returns_none(
        self,
        team_store: TeamStore,
        done_card: BoardTask,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """review F2: a refusal must be distinguishable from "nothing to learn"."""
        import omniagentos.metacog.service as service_module

        def _explode(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("metacog is down")

        monkeypatch.setattr(service_module.MetacogService, "register_artifact", _explode)
        result = team_store.record_verification(done_card.id, employees["alice"])
        assert result is not None
        assert (
            learning.on_task_verified(
                team_store,
                result.card,
                team_store.list_evidence(done_card.id),
                event_id=result.event_id,
            )
            is None
        )
        # The verification itself is untouched...
        assert result.card["verified_by"] == employees["alice"]
        # ...and the refusal left a durable, distinguishable trace.
        notes = _markers(team_store, done_card.id)
        assert notes == ["learning_capture_failed: RuntimeError"]

    def test_a_zero_evidence_verify_skips_without_recording_a_failure(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """A human verify with nothing attached is a legitimate judgement call,
        not a broken pipeline — so it leaves NO failure marker."""
        card = make_card(title="Judgement call", owner_employee_id=employees["bob"])
        collab_store.update_board_task(
            card.id, {"status": BoardTaskStatus.DONE.value}, actor=employees["bob"]
        )
        verified = team_store.record_verification(card.id, employees["alice"])
        assert verified is not None
        assert (
            learning.on_task_verified(team_store, verified.card, [], event_id=verified.event_id)
            is None
        )
        assert _markers(team_store, card.id) == []


class TestTheKillSwitch:
    def test_the_flag_off_makes_the_hook_a_no_op(
        self,
        team_store: TeamStore,
        done_card: BoardTask,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(learning.LEARNING_FLAG, "0")
        result = team_store.record_verification(done_card.id, employees["alice"])
        assert result is not None
        assert (
            learning.on_task_verified(
                team_store,
                result.card,
                team_store.list_evidence(done_card.id),
                event_id=result.event_id,
            )
            is None
        )
        assert _markers(team_store, done_card.id) == []


class TestTheStoreOwnsFirstSuccessAndEventAttribution:
    """Sol review, item 3 — neither fact may be re-derived by the caller."""

    def test_first_success_is_false_after_a_fail_verify_cycle(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        """The bug this pins: a refusal CLEARS verified_at, so a route that
        asked "is verified_at NULL?" called the second success a first one and
        minted a duplicate candidate."""
        first = team_store.record_verification(done_card.id, employees["alice"])
        assert first is not None and first.first_success is True
        team_store.fail_verification(done_card.id, employees["alice"], "regression")
        card = team_store._store._connection.execute(
            "SELECT verified_at FROM board_tasks WHERE id = ?", (done_card.id,)
        ).fetchone()
        assert card["verified_at"] is None, "precondition: the stamp is gone"
        again = team_store.record_verification(done_card.id, employees["alice"])
        assert again is not None
        assert again.first_success is False, "the event history remembers what the stamp forgot"

    def test_first_success_is_false_after_an_unverify_verify_cycle(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        assert team_store.record_verification(done_card.id, employees["alice"])
        team_store.unverify_task(done_card.id, employees["owner"])
        again = team_store.record_verification(done_card.id, employees["alice"])
        assert again is not None
        assert again.first_success is False

    def test_the_returned_event_id_is_the_one_this_call_appended(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        first = team_store.record_verification(done_card.id, employees["alice"])
        second = team_store.record_verification(done_card.id, employees["alice"])
        assert first is not None and second is not None
        assert first.event_id != second.event_id
        verify_ids = [
            str(event["id"])
            for event in team_store.list_events(done_card.id)
            if str(event["event"]) == "verify"
        ]
        assert verify_ids == [first.event_id, second.event_id]

    def test_the_route_does_not_duplicate_a_candidate_after_a_fail_verify_cycle(
        self,
        api: httpx.AsyncClient,
        team_store: TeamStore,
        done_card: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """End to end: verify → fail → verify mints exactly one procedure
        candidate and one lesson, never two procedures."""
        verify = {"verifier": employees["alice"]}
        asyncio.run(api.post(f"/api/team/tasks/{done_card.id}/verify", json=verify))
        asyncio.run(
            api.post(
                f"/api/team/tasks/{done_card.id}/verify",
                json={**verify, "outcome": "fail", "reason": "regression"},
            )
        )
        asyncio.run(api.post(f"/api/team/tasks/{done_card.id}/verify", json=verify))
        outcomes = sorted(
            note.split("outcome=")[1].split(" ")[0] for note in _markers(team_store, done_card.id)
        )
        assert outcomes == ["lesson", "procedure"]


class TestTheCandidateIsScopedToItsCompany:
    def test_applicability_carries_the_company_slug(
        self,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        goals_store: CompanyGoalsStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """Sol review, item 5: an unscoped candidate surfaces a lesson from one
        brand's work as advice for another."""
        store._connection.execute(
            "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?,?,?,?,?)",
            ("co_omniagentos", "omniagentos", "OmniAgentOS", "active", utc_now_iso()),
        )
        goal = goals_store.create_goal(
            org_company_id="co_omniagentos",
            title="General engineering — OmniAgentOS",
            horizon="quarter",
        )
        card = make_card(
            title="Speed up the gate",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it is faster",
            goal_id=str(goal["id"]),
            ref="GH-11",
        )
        team_store.add_evidence(kind="pr", ref="11", repo="omnios", task_id=card.id)
        collab_store.update_board_task(
            card.id, {"status": BoardTaskStatus.DONE.value}, actor=employees["bob"]
        )
        result = team_store.record_verification(card.id, employees["alice"])
        assert result is not None
        candidate_id = learning.on_task_verified(
            team_store,
            result.card,
            team_store.list_evidence(card.id),
            event_id=result.event_id,
        )
        assert candidate_id is not None
        applicability = json.loads(str(_memory(team_store, candidate_id)["applicability_json"]))
        assert applicability["company"] == "omniagentos"
        assert applicability["board_task_id"] == card.id
        assert applicability["ref"] == "GH-11"

    def test_a_broken_scope_lookup_fails_the_capture_instead_of_unscoping_it(
        self,
        team_store: TeamStore,
        done_card: BoardTask,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Round-2 review, item 2 — the favourable-absence shape.

        A swallowed lookup error returned the SAME ``None`` a goal-less card
        returns, so a broken join minted an unscoped candidate and reported
        success. The failure must be loud: no candidate, and the durable
        ``learning_capture_failed`` marker instead."""

        def _explode(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("the join is broken")

        monkeypatch.setattr(learning, "_company_slug", _explode)
        result = team_store.record_verification(done_card.id, employees["alice"])
        assert result is not None
        assert (
            learning.on_task_verified(
                team_store,
                result.card,
                team_store.list_evidence(done_card.id),
                event_id=result.event_id,
            )
            is None
        )
        assert _markers(team_store, done_card.id) == ["learning_capture_failed: RuntimeError"]
        minted = team_store._store._connection.execute(
            "SELECT COUNT(*) AS n FROM metacog_memory_records"
        ).fetchone()
        assert minted["n"] == 0, "an unscoped candidate must never be the fallback"

    def test_a_goal_less_card_scopes_to_none_rather_than_guessing(
        self, team_store: TeamStore, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        result = team_store.record_verification(done_card.id, employees["alice"])
        assert result is not None
        candidate_id = learning.on_task_verified(
            team_store,
            result.card,
            team_store.list_evidence(done_card.id),
            event_id=result.event_id,
        )
        assert candidate_id is not None
        applicability = json.loads(str(_memory(team_store, candidate_id)["applicability_json"]))
        assert applicability["company"] is None
