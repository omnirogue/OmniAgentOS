"""HTTP contract for the accountability surfaces (migration 132).

Three endpoints and one widened body: ``POST /tasks/{id}/verify`` gains a
verdict, ``/commitments`` gains CRUD with immutability rules, and
``/accountability`` answers "what did each person promise, and what does the
board say happened" for one local day.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.team import commitments
from omniagentos.team.store import TeamStore


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def hermetic_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        ref="GH-7",
    )
    team_store.add_evidence(kind="commit", ref="c0ffee", repo="omnios", task_id=card.id)
    collab_store.update_board_task(
        card.id, {"status": BoardTaskStatus.DONE.value}, actor=employees["bob"]
    )
    return card


class TestVerifyVerdict:
    def test_the_default_outcome_is_unchanged(
        self, api: httpx.AsyncClient, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        """Every pre-131 caller sends no ``outcome`` and must behave as before."""
        response = _run(
            api.post(f"/api/team/tasks/{done_card.id}/verify", json={"verifier": employees["alice"]})
        )
        assert response.status_code == 200, response.text
        assert response.json()["verified_by"] == employees["alice"]

    def test_a_fail_stamps_the_card_and_records_the_reason(
        self, api: httpx.AsyncClient, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        response = _run(
            api.post(
                f"/api/team/tasks/{done_card.id}/verify",
                json={"verifier": employees["alice"], "outcome": "fail", "reason": "no tests"},
            )
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["verification_failed_reason"] == "no tests"
        assert body["verified_at"] is None

    def test_a_fail_with_no_reason_is_a_400(
        self, api: httpx.AsyncClient, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        response = _run(
            api.post(
                f"/api/team/tasks/{done_card.id}/verify",
                json={"verifier": employees["alice"], "outcome": "fail"},
            )
        )
        assert response.status_code == 400, response.text

    def test_an_unknown_outcome_is_a_400(
        self, api: httpx.AsyncClient, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        response = _run(
            api.post(
                f"/api/team/tasks/{done_card.id}/verify",
                json={"verifier": employees["alice"], "outcome": "maybe"},
            )
        )
        assert response.status_code == 400, response.text

    def test_verifying_through_the_route_captures_learning_once(
        self,
        api: httpx.AsyncClient,
        team_store: TeamStore,
        done_card: BoardTask,
        employees: dict[str, str],
    ) -> None:
        """The hook runs at the ROUTE layer, after the commit — and only on the
        FIRST successful verification (review S5)."""
        for _ in range(2):
            _run(
                api.post(
                    f"/api/team/tasks/{done_card.id}/verify", json={"verifier": employees["alice"]}
                )
            )
        markers = [
            event
            for event in team_store.list_events(done_card.id)
            if str(event["note"]).startswith("learning_capture:")
        ]
        assert len(markers) == 1


class TestCommitmentsCrud:
    def test_create_list_and_idempotency(
        self, api: httpx.AsyncClient, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        body = {
            "employee_id": employees["bob"],
            "title": "Finish GH-7",
            "day": "2026-08-14",
            "task_id": done_card.id,
        }
        first = _run(api.post("/api/team/commitments", json=body))
        assert first.status_code == 201, first.text
        assert first.json()["source"] == "operator"
        second = _run(api.post("/api/team/commitments", json=body))
        assert second.status_code == 200, "a duplicate returns the existing row, not a second one"
        assert second.json()["id"] == first.json()["id"]
        listed = _run(api.get("/api/team/commitments?day=2026-08-14"))
        assert [row["id"] for row in listed.json()["commitments"]] == [first.json()["id"]]

    def test_unknown_employee_and_unknown_card_are_404s(
        self, api: httpx.AsyncClient, employees: dict[str, str]
    ) -> None:
        missing_person = _run(
            api.post("/api/team/commitments", json={"employee_id": "emp_nobody", "title": "x"})
        )
        assert missing_person.status_code == 404
        missing_card = _run(
            api.post(
                "/api/team/commitments",
                json={"employee_id": employees["bob"], "title": "x", "task_id": "btk_nope"},
            )
        )
        assert missing_card.status_code == 404

    @pytest.mark.parametrize("kind", ["improvement", "automation"])
    def test_a_slotted_commitment_may_not_name_a_card(
        self, api: httpx.AsyncClient, done_card: BoardTask, employees: dict[str, str], kind: str
    ) -> None:
        """Review round 2, item 3: a 400 with the reason, not a 500 from the
        store's read-back assertion. A slotted row is indexed by slot and would
        be looked up by card, so its own idempotent re-run could not find it."""
        response = _run(
            api.post(
                "/api/team/commitments",
                json={
                    "employee_id": employees["bob"],
                    "title": "Something",
                    "kind": kind,
                    "task_id": done_card.id,
                },
            )
        )
        assert response.status_code == 400, response.text
        assert f"a {kind} commitment cannot name a task_id" in response.text

    def test_a_slotted_commitment_without_a_card_is_accepted(
        self, api: httpx.AsyncClient, employees: dict[str, str]
    ) -> None:
        response = _run(
            api.post(
                "/api/team/commitments",
                json={
                    "employee_id": employees["bob"],
                    "title": "New automation or skill",
                    "kind": "automation",
                },
            )
        )
        assert response.status_code == 201, response.text
        assert response.json()["slot"] == 1

    def test_resolving_requires_a_note_and_a_done_card(
        self, api: httpx.AsyncClient, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        created = _run(
            api.post(
                "/api/team/commitments",
                json={
                    "employee_id": employees["bob"],
                    "title": "Finish GH-7",
                    "task_id": done_card.id,
                },
            )
        ).json()
        no_note = _run(
            api.patch(f"/api/team/commitments/{created['id']}", json={"status": "delivered"})
        )
        assert no_note.status_code == 400
        resolved = _run(
            api.patch(
                f"/api/team/commitments/{created['id']}",
                json={"status": "delivered", "resolution_note": "landed in PR 7"},
            )
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["status"] == "delivered"
        assert resolved.json()["resolved_by"] == "emp_owner"

    def test_a_task_commitment_cannot_be_delivered_while_the_card_is_open(
        self,
        api: httpx.AsyncClient,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """round-3 §3: the board is the evidence; the endpoint is only a ruling."""
        card = make_card(title="Still open", owner_employee_id=employees["bob"])
        created = _run(
            api.post(
                "/api/team/commitments",
                json={"employee_id": employees["bob"], "title": "x", "task_id": card.id},
            )
        ).json()
        response = _run(
            api.patch(
                f"/api/team/commitments/{created['id']}",
                json={"status": "delivered", "resolution_note": "trust me"},
            )
        )
        assert response.status_code == 400, response.text
        assert "not done" in response.text

    def test_a_resolved_row_accepts_only_note_appends(
        self, api: httpx.AsyncClient, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        """review S10: a miss is history. It cannot be rewritten to delivered."""
        created = _run(
            api.post(
                "/api/team/commitments",
                json={"employee_id": employees["bob"], "title": "x", "task_id": done_card.id},
            )
        ).json()
        _run(
            api.patch(
                f"/api/team/commitments/{created['id']}",
                json={"status": "missed", "resolution_note": "ran out of day"},
            )
        )
        rewritten = _run(
            api.patch(
                f"/api/team/commitments/{created['id']}",
                json={"status": "delivered", "resolution_note": "actually it landed"},
            )
        )
        assert rewritten.status_code == 400, rewritten.text
        appended = _run(
            api.patch(
                f"/api/team/commitments/{created['id']}",
                json={"resolution_note": "blocked on review, not a slip"},
            )
        )
        assert appended.status_code == 200, appended.text
        assert appended.json()["status"] == "missed"
        assert "ran out of day" in appended.json()["resolution_note"]
        assert "blocked on review" in appended.json()["resolution_note"]

    def test_carried_is_never_settable_by_hand(
        self, api: httpx.AsyncClient, done_card: BoardTask, employees: dict[str, str]
    ) -> None:
        created = _run(
            api.post(
                "/api/team/commitments",
                json={"employee_id": employees["bob"], "title": "x", "task_id": done_card.id},
            )
        ).json()
        response = _run(
            api.patch(
                f"/api/team/commitments/{created['id']}",
                json={"status": "carried", "resolution_note": "moving it"},
            )
        )
        assert response.status_code == 400, response.text

    def test_an_absent_commitment_is_a_404(self, api: httpx.AsyncClient) -> None:
        response = _run(api.patch("/api/team/commitments/tcm_nope", json={"resolution_note": "hi"}))
        assert response.status_code == 404


class TestAccountabilityView:
    def test_the_shape_carries_the_tri_state_and_evidence_detail(
        self,
        api: httpx.AsyncClient,
        collab_store: CollabStore,
        team_store: TeamStore,
        done_card: BoardTask,
        employees: dict[str, str],
    ) -> None:
        day = commitments.local_today()
        collab_store.update_board_task(
            done_card.id,
            {"automation_maturity": "assisted", "automation_note": "auto-run the smoke suite"},
        )
        commitments.generate_for_day(team_store, day)
        _run(
            api.post(
                f"/api/team/tasks/{done_card.id}/verify",
                json={"verifier": employees["alice"], "outcome": "fail", "reason": "no tests"},
            )
        )

        response = _run(api.get(f"/api/team/accountability?day={day}"))
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["day"] == day
        people = {person["employee_id"]: person for person in payload["people"]}
        # The operator carries no commitments and no accountability row.
        assert employees["owner"] not in people
        bob = people[employees["bob"]]
        assert bob["improvement_of_day"]["title"] == commitments.IMPROVEMENT_TITLE
        # The three daily automation slots ride the generic commitments list —
        # the endpoint needs no shape change to carry a new kind, and this
        # assertion is what proves that stays true (the operator's ruling 2026-08-14).
        automation = [row for row in bob["commitments"] if row["kind"] == "automation"]
        assert [row["slot"] for row in automation] == [1, 2, 3]
        assert {row["status"] for row in automation} == {"committed"}
        # At most eight rows a day: 4 cards + 1 improvement + 3 automations.
        assert len(bob["commitments"]) <= (
            commitments.COMMITMENT_TASK_CAP + 1 + commitments.AUTOMATION_SLOTS_PER_DAY
        )
        card = next(item for item in bob["done_today"] if item["id"] == done_card.id)
        assert card["completion_state"] == "failed_verification"
        assert card["verification_failed_reason"] == "no tests"
        assert card["automation_maturity"] == "assisted"
        # review S12: evidence ITEMS, not a bare count.
        assert card["evidence"] == [
            {"kind": "commit", "repo": "omnios", "ref": "c0ffee", "quality_gate": "pass"}
        ]
        assert bob["counts"]["done_today"] >= 0
        assert bob["points_pace"] is not None
        assert isinstance(bob["learning_captures"], int)
        # Activity reported NEXT TO outcomes: the one evidence row filed today.
        assert bob["evidence_today"] == 1

    def test_blocked_and_overdue_are_counted(
        self,
        api: httpx.AsyncClient,
        collab_store: CollabStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        blocked = make_card(
            title="Stuck", owner_employee_id=employees["bob"], acceptance_criteria="unstuck"
        )
        collab_store.update_board_task(
            blocked.id,
            {"status": BoardTaskStatus.BLOCKED.value, "blocked_reason": "waiting on review"},
            actor=employees["bob"],
        )
        make_card(title="Late", owner_employee_id=employees["bob"], due_date="2020-01-01T10:00")
        day = commitments.local_today()
        payload = _run(api.get(f"/api/team/accountability?day={day}")).json()
        bob = next(
            person for person in payload["people"] if person["employee_id"] == employees["bob"]
        )
        assert [item["id"] for item in bob["blocked"]] == [blocked.id]
        # Sol review, item 4: a blocked list with no reasons tells a reader that
        # somebody is stuck and nothing about what would unstick them.
        assert bob["blocked"][0]["blocked_reason"] == "waiting on review"
        assert bob["overdue"] == 1

    def test_a_malformed_day_is_a_400(self, api: httpx.AsyncClient) -> None:
        assert _run(api.get("/api/team/accountability?day=yesterday")).status_code == 400


class TestEvidenceToday:
    def test_it_counts_todays_rows_on_this_persons_cards_only(
        self,
        api: httpx.AsyncClient,
        team_store: TeamStore,
        collab_store: CollabStore,
        store: SqliteStore,
        done_card: BoardTask,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """Sol review, item 4. Three ways the number could lie, all pinned:
        somebody else's card, a card with no owner, and a row filed yesterday."""
        other = make_card(title="Alice's", owner_employee_id=employees["alice"])
        team_store.add_evidence(kind="commit", ref="other", repo="omnios", task_id=other.id)
        unowned = make_card(title="Agent card")
        team_store.add_evidence(kind="commit", ref="unowned", repo="omnios", task_id=unowned.id)
        stale = team_store.add_evidence(
            kind="commit", ref="stale", repo="omnios", task_id=done_card.id
        )
        store._connection.execute(
            "UPDATE task_evidence SET created_at = ? WHERE id = ?",
            ("2020-01-01T00:00:00Z", stale),
        )
        store._connection.commit()

        day = commitments.local_today()
        payload = _run(api.get(f"/api/team/accountability?day={day}")).json()
        people = {person["employee_id"]: person for person in payload["people"]}
        assert people[employees["bob"]]["evidence_today"] == 1
        assert people[employees["alice"]]["evidence_today"] == 1


class TestReportPreviewIsPure:
    def test_previewing_does_not_resolve_or_carry_anything(
        self,
        api: httpx.AsyncClient,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """Sol review, item 1 (BLOCKER). ``GET /report/preview`` used to call
        ``resolve_day`` through ``gather``, so a READ froze yesterday's rows and
        minted carries — on whatever ``?day=`` it was handed, including a day
        that is not over. The rows must come back byte-identical."""
        make_card(
            title="Still going",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it ships",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        yesterday = (date.fromisoformat(commitments.local_today()) - timedelta(days=1)).isoformat()
        commitments.generate_for_day(team_store, yesterday, employee_ids=[employees["bob"]])
        before = team_store.list_commitments()
        assert {row["status"] for row in before} == {"committed"}

        response = _run(api.get(f"/api/team/report/preview?day={commitments.local_today()}"))
        assert response.status_code == 200, response.text

        assert team_store.list_commitments() == before

    def test_the_scheduled_gather_still_resolves(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        """The flag is off by DEFAULT, not removed: the 07:00 path still writes."""
        from omniagentos.team import report as team_report

        make_card(
            title="Still going",
            owner_employee_id=employees["bob"],
            acceptance_criteria="it ships",
            status=BoardTaskStatus.IN_PROGRESS.value,
        )
        today = commitments.local_today()
        yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()
        commitments.generate_for_day(team_store, yesterday, employee_ids=[employees["bob"]])
        team_report.gather(team_store, today, resolve=True)
        assert {row["status"] for row in team_store.list_commitments(day=yesterday)} == {"missed"}
