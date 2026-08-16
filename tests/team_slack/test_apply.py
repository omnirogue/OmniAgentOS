"""apply(): the four board-mutating verbs, plus my_queue/report."""

from __future__ import annotations

import sys
from collections.abc import Callable

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.team.slack_updates import Command, apply, parse_command, permalink
from omniagentos.team.store import TeamStore

PERMALINK = permalink("C0000EXAMPLE", "1700000000.000100")


class TestDone:
    def test_a_card_with_no_acceptance_criteria_needs_no_evidence(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Simple", ref="U1", owner_employee_id=employees["bob"])
        reply = apply(
            Command(verb="done", ref="U1", note="shipped"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert "done" in reply
        row = collab_store.get_board_task(card.id)
        assert row is not None
        assert row["status"] == "done"
        assert team_store.list_evidence(card.id) == []

    def test_a_card_with_acceptance_criteria_gets_evidence_auto_attached(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(
            title="Needs proof",
            ref="U2",
            owner_employee_id=employees["bob"],
            acceptance_criteria="the thing works",
        )
        reply = apply(
            Command(verb="done", ref="U2", note="ran the demo"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert reply.startswith("✓")
        assert "done" in reply

        row = collab_store.get_board_task(card.id)
        assert row is not None
        assert row["status"] == "done"
        # done != verified: apply() never calls verify_task.
        assert row["verified_at"] is None
        assert row["verified_by"] is None

        evidence = team_store.list_evidence(card.id)
        assert len(evidence) == 1
        assert evidence[0]["kind"] == "note"
        assert evidence[0]["ref"] == PERMALINK
        assert evidence[0]["title"] == "ran the demo"
        assert evidence[0]["attribution"] == "manual"

    def test_a_note_free_done_falls_back_to_a_default_evidence_title(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(
            title="Needs proof",
            ref="U3",
            owner_employee_id=employees["bob"],
            acceptance_criteria="the thing works",
        )
        apply(
            Command(verb="done", ref="U3", note=""),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        evidence = team_store.list_evidence(card.id)
        assert evidence[0]["title"] == "completed via Slack"

    def test_no_matching_task_is_a_clean_reply_not_an_exception(
        self, collab_store: CollabStore, team_store: TeamStore, employees: dict[str, str]
    ) -> None:
        reply = apply(
            Command(verb="done", ref="U404", note="x"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert reply == "no matching task"


class TestBlocked:
    def test_status_and_reason_land_and_an_event_is_written(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Stuck", ref="U4", owner_employee_id=employees["bob"])
        reply = apply(
            Command(verb="blocked", ref="U4", note="waiting on Alice's review"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert "blocked" in reply
        assert "waiting on Alice's review" in reply

        row = collab_store.get_board_task(card.id)
        assert row is not None
        assert row["status"] == "blocked"
        assert row["blocked_reason"] == "waiting on Alice's review"

        events = team_store.list_events(card.id)
        assert any(e["event"] == "status_change" and e["to_status"] == "blocked" for e in events)

    def test_cross_owner_is_refused(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        make_card(title="Alice's card", ref="U5", owner_employee_id=employees["alice"])
        reply = apply(
            Command(verb="blocked", ref="U5", note="not mine"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert reply == "not your task"


class TestProgress:
    def test_evidence_and_a_comment_event_both_land(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="In flight", ref="U6", owner_employee_id=employees["bob"])
        reply = apply(
            Command(verb="progress", ref="U6", note="talked to the customer"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert "talked to the customer" in reply

        evidence = team_store.list_evidence(card.id)
        assert len(evidence) == 1
        assert evidence[0]["kind"] == "note"
        assert evidence[0]["ref"] == f"{PERMALINK}#p"
        assert evidence[0]["title"] == "talked to the customer"

        events = team_store.list_events(card.id)
        comments = [e for e in events if e["event"] == "comment"]
        assert any(e["note"] == "talked to the customer" for e in comments)


class TestClaim:
    def test_an_open_card_is_claimed(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Up for grabs", ref="U7", owner_employee_id=employees["bob"])
        reply = apply(
            Command(verb="claim", ref="U7"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert reply.startswith("✓")
        row = collab_store.get_board_task(card.id)
        assert row is not None
        assert row["status"] == "claimed"
        assert row["claimed_by"] == "human:emp_bob"
        claim_event = [
            event for event in team_store.list_events(card.id) if event["event"] == "status_change"
        ][-1]
        assert claim_event["actor"] == employees["bob"]
        assert claim_event["from_status"] == "open"
        assert claim_event["to_status"] == "claimed"

    def test_non_owner_cannot_claim_an_unowned_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Agent card", ref="AGT-1")
        reply = apply(
            Command(verb="claim", ref="AGT-1"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert reply == "not your task"
        row = collab_store.get_board_task(card.id)
        assert row is not None
        assert row["status"] == "open"

    def test_a_pre_claimed_card_conflicts(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Contested", ref="U8", owner_employee_id=employees["bob"])
        first = apply(
            Command(verb="claim", ref="U8"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert first.startswith("✓")
        second = apply(
            Command(verb="claim", ref="U8"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert "conflict" in second
        row = collab_store.get_board_task(card.id)
        assert row is not None
        assert row["claim_version"] == 1  # only the FIRST claim moved it

    def test_pool_claim_transfers_owner_when_the_store_supports_it(
        self, team_store: TeamStore
    ) -> None:
        pool = {
            "id": "btk_pool",
            "title": "Pool card",
            "status": "open",
            "owner_employee_id": None,
            "parent_task_id": None,
            "source": "manual",
            "goal_id": "goal_1",
            "acceptance_criteria": "works",
            "claim_version": 0,
        }

        class NewClaimStore:
            captured: dict[str, object] = {}

            def list_board_tasks(self, *, archived: int = 0) -> list[dict[str, object]]:
                return [pool]

            def claim_task(
                self,
                task_id: str,
                agent_id: str,
                expect_version: int,
                *,
                actor: str = "system",
                owner_employee_id: str | None = None,
            ) -> bool:
                self.captured = {
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "expect_version": expect_version,
                    "actor": actor,
                    "owner_employee_id": owner_employee_id,
                }
                return True

        collab = NewClaimStore()
        reply = apply(
            Command(verb="claim", ref="btk_pool"),
            "emp_bob",
            PERMALINK,
            collab=collab,  # type: ignore[arg-type]
            team=team_store,
        )
        assert reply.startswith("✓")
        assert collab.captured["owner_employee_id"] == "emp_bob"


class TestMyQueue:
    def test_renders_the_senders_buckets(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        make_card(title="Ready one", ref="R1", owner_employee_id=employees["bob"])
        blocked = make_card(title="Blocked one", ref="B1", owner_employee_id=employees["bob"])
        collab_store.update_board_task(
            blocked.id, {"status": "blocked", "blocked_reason": "waiting"}
        )
        make_card(title="Not mine", ref="N1", owner_employee_id=employees["alice"])

        reply = apply(
            Command(verb="my_queue"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert "Ready one" in reply
        assert "Blocked one" in reply
        assert "Not mine" not in reply


class TestReport:
    def test_import_error_gets_a_graceful_reply(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A ``None`` entry in sys.modules forces the next import to raise
        # ImportError, deterministically, regardless of whether the module
        # exists on disk (it does not yet -- P4/another parallel package owns
        # it -- but this keeps the test honest if that changes).
        monkeypatch.setitem(sys.modules, "omniagentos.team.report", None)
        reply = apply(
            Command(verb="report"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
        )
        assert reply == "report module not yet installed"


def _task_command(text: str) -> Command:
    command = parse_command(text)
    assert command is not None and command.verb == "task"
    return command


class TestTask:
    def test_creates_an_owned_card_the_assigner_can_track_by_id(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        slack_map: dict[str, str],
    ) -> None:
        reply = apply(
            _task_command("<@U0BOB> task fix the login bug"),
            employees["owner"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
        )

        cards = collab_store.list_board_tasks()
        assert len(cards) == 1
        card = cards[0]
        assert card["title"] == "fix the login bug"
        assert card["owner_employee_id"] == employees["bob"]
        assert card["priority"] == "normal"
        assert card["size"] == "M"
        assert card["status"] == "open"
        assert card["ref"] is None  # no ref minted: the btk_ id is the handle
        assert card["description"] == (
            f"Assigned by {employees['owner']} via Slack.\n<@U0BOB> task fix the login bug"
        )
        assert card["acceptance_criteria"] == (
            f"Assigner ({employees['owner']}) confirms completion in thread."
        )
        assert card["goal_id"] is None

        assert reply.startswith(f"Created {card['id']}: fix the login bug → {employees['bob']}")
        assert "(normal)" in reply
        assert f"Track with: done {card['id']}" in reply

    def test_the_create_event_records_the_assigner_not_the_assignee(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        slack_map: dict[str, str],
    ) -> None:
        apply(
            _task_command("task <@U0ANDY> write the migration"),
            employees["alice"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
        )

        card_id = collab_store.list_board_tasks()[0]["id"]
        events = team_store.list_events(card_id)
        assert [(event["event"], event["actor"]) for event in events] == [
            ("create", employees["alice"])
        ]

    def test_top_flag_makes_the_card_urgent_and_says_so(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        slack_map: dict[str, str],
    ) -> None:
        reply = apply(
            _task_command("<@U0BOB> task fix the outage !top"),
            employees["owner"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
        )

        card = collab_store.list_board_tasks()[0]
        assert card["priority"] == "urgent"
        assert card["title"] == "fix the outage"
        assert "(urgent)" in reply

    def test_a_company_flag_resolves_the_general_engineering_goal(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        slack_map: dict[str, str],
        initech_goal: str,
    ) -> None:
        reply = apply(
            _task_command("<@U0BOB> task ship the fix !top #initech"),
            employees["owner"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
        )

        card = collab_store.list_board_tasks()[0]
        assert card["goal_id"] == initech_goal
        assert "(urgent, initech)" in reply

    def test_a_slug_alias_resolves_to_the_same_goal(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        slack_map: dict[str, str],
        initech_goal: str,
    ) -> None:
        reply = apply(
            _task_command("<@U0BOB> task ship the fix #omni"),
            employees["owner"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
        )

        assert collab_store.list_board_tasks()[0]["goal_id"] == initech_goal
        assert "(normal, initech)" in reply

    def test_an_unknown_slug_still_creates_the_card_and_the_reply_says_so(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        slack_map: dict[str, str],
    ) -> None:
        reply = apply(
            _task_command("<@U0BOB> task ship the fix #nosuchco"),
            employees["owner"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
        )

        card = collab_store.list_board_tasks()[0]
        assert card["goal_id"] is None
        assert "no company goal matched #nosuchco" in reply

    def test_an_unknown_assignee_creates_nothing_and_names_the_map_file(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        slack_map: dict[str, str],
    ) -> None:
        reply = apply(
            _task_command("<@U0STRANGER> task do the thing"),
            employees["owner"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
        )

        assert collab_store.list_board_tasks() == []
        assert "unknown assignee" in reply
        assert "configs/team_slack_map.yaml" in reply
        assert "<@" not in reply  # never echo a mention: it would ping

    def test_any_employee_may_assign_to_any_employee_including_owner(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        slack_map: dict[str, str],
    ) -> None:
        apply(
            _task_command("<@U0TEAM> task review the pricing page"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
        )

        card = collab_store.list_board_tasks()[0]
        assert card["owner_employee_id"] == employees["owner"]
        assert card["description"].startswith(f"Assigned by {employees['bob']} via Slack.")

    def test_the_new_card_lands_in_the_assignees_ready_queue(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        slack_map: dict[str, str],
    ) -> None:
        apply(
            _task_command("<@U0BOB> task fix the outage !top"),
            employees["owner"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
        )

        ready = team_store.team_queues()[employees["bob"]].ready
        assert [(card.title, card.priority) for card in ready] == [("fix the outage", "urgent")]
