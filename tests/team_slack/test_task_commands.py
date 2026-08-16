"""The ``/task`` command family (v3, the operator's ruling 2026-08-13).

Four surfaces, one file:

* **Grammar** — every ``/task`` sub-verb parses deterministically (including
  the natural trailing deadline phrases); a malformed ``/task`` message is
  ``task_unknown`` (answered, never silent), ordinary chatter is still
  ``None``, and every pre-existing bare verb keeps parsing unchanged.
* **The permission matrix** — every cell, allowed AND denied, with the polite
  denial replies.
* **DM flows** — assign/reassign DM the assignee, done/note DM the assigner
  (or the owner when the noter IS the assigner); one action, one DM.
* **Delegation vs ad-hoc** — a ref-shaped token drains the queue (the operator/Alice),
  a free title creates a new owned card (anyone, never to self).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.db.store import SqliteStore
from omniagentos.team import tasks as team_tasks
from omniagentos.team.slack_updates import Command, apply, parse_command, team_updates_handle
from omniagentos.team.store import TeamStore

from .conftest import message_event

PERMALINK = "https://slack.com/archives/C0000EXAMPLE/p1700000000000100"

#: 2026-08-12 09:30 local (-04:00) is a WEDNESDAY — every deadline expectation
#: below is pinned to this clock.
NOW = datetime(2026, 8, 12, 9, 30, tzinfo=timezone(timedelta(hours=-4)))


class FakeNotifier:
    def __init__(self) -> None:
        self.dms: list[tuple[str, str]] = []

    def post_dm(self, slack_user_id: str, text: str, **_: Any) -> bool:
        self.dms.append((slack_user_id, text))
        return True


def _cmd(text: str) -> Command:
    command = parse_command(text)
    assert command is not None, f"expected a command from {text!r}"
    return command


# ==========================================================================
# grammar
# ==========================================================================


class TestSlashGrammar:
    def test_bare_slash_task_and_help_parse_to_the_help_card(self) -> None:
        assert _cmd("/task").verb == "task_help"
        assert _cmd("/task help").verb == "task_help"
        assert _cmd("/TASK HELP").verb == "task_help"

    def test_mine_is_the_existing_my_queue(self) -> None:
        assert _cmd("/task mine").verb == "my_queue"

    def test_queue_with_and_without_a_company(self) -> None:
        assert _cmd("/task queue").verb == "task_queue"
        assert _cmd("/task queue").company is None
        assert _cmd("/task queue #initech").company == "initech"
        assert _cmd("/task queue initech").company == "initech"  # '#' optional here
        assert _cmd("/task queue not a company").verb == "task_unknown"

    def test_claim_gets_its_own_family_verb(self) -> None:
        # task_claim (not the bare 'claim') so the family's active-roster gate
        # binds — review finding 2026-08-13: an inactive-but-mapped employee
        # must not pull queue work through /task claim.
        command = _cmd("/task claim U3")
        assert (command.verb, command.ref) == ("task_claim", "U3")
        assert _cmd("/task claim btk_ab12cd").ref == "btk_ab12cd"
        assert _cmd("/task claim not-a-ref!").verb == "task_unknown"

    def test_done_with_and_without_a_note(self) -> None:
        command = _cmd("/task done U3 shipped it")
        assert (command.verb, command.ref, command.note) == ("task_done", "U3", "shipped it")
        assert _cmd("/task done U3").note == ""
        assert _cmd("/task done").verb == "task_unknown"

    def test_note_requires_text(self) -> None:
        command = _cmd("/task note U3 waiting on staging")
        assert (command.verb, command.ref, command.note) == (
            "task_note",
            "U3",
            "waiting on staging",
        )
        assert _cmd("/task note U3").verb == "task_unknown"

    def test_reassign_accepts_both_orders(self) -> None:
        ref_first = _cmd("/task reassign U3 <@U0ALICE>")
        assert (ref_first.verb, ref_first.ref, ref_first.assignee_slack_id) == (
            "task_reassign",
            "U3",
            "U0ALICE",
        )
        mention_first = _cmd("/task reassign <@U0ALICE> U3")
        assert (mention_first.ref, mention_first.assignee_slack_id) == ("U3", "U0ALICE")
        assert _cmd("/task reassign U3").verb == "task_unknown"

    def test_add_with_company_priority_and_deadline(self) -> None:
        command = _cmd("/task add Fix checkout flow #initech !high tomorrow")
        assert command.verb == "task_add"
        assert command.title == "Fix checkout flow"
        assert command.company == "initech"
        assert command.priority == "high"
        assert command.deadline == "tomorrow"

    @pytest.mark.parametrize(
        ("flag", "priority"), [("!top", "urgent"), ("!high", "high"), ("!low", "low")]
    )
    def test_every_priority_flag(self, flag: str, priority: str) -> None:
        assert _cmd(f"/task add Ship it {flag} #acmeuni").priority == priority

    def test_add_with_an_ac_suffix(self) -> None:
        command = _cmd("/task add Fix checkout #initech | ac: checkout completes in test")
        assert command.title == "Fix checkout"
        assert command.note == "checkout completes in test"

    def test_add_deadline_after_the_ac_suffix_still_parses(self) -> None:
        command = _cmd("/task add Fix checkout #initech | ac: checkout works by friday")
        assert command.note == "checkout works"
        assert command.deadline == "by friday"

    def test_add_with_no_title_is_unknown(self) -> None:
        assert _cmd("/task add").verb == "task_unknown"
        assert _cmd("/task add #initech !top").verb == "task_unknown"

    def test_assign_with_a_ref_is_a_delegation(self) -> None:
        command = _cmd("/task assign <@U0BOB> U3")
        assert (command.verb, command.ref, command.title) == ("task_assign", "U3", None)
        assert command.assignee_slack_id == "U0BOB"

    def test_assign_with_a_title_is_ad_hoc(self) -> None:
        command = _cmd("/task assign <@U0BOB> fix the login bug by friday")
        assert (command.verb, command.ref) == ("task_assign", None)
        assert command.title == "fix the login bug"
        assert command.deadline == "by friday"

    def test_assign_with_flags(self) -> None:
        command = _cmd("/task assign <@U0ALICE> ship the fix !top #omni in 2 hours")
        assert (command.priority, command.company, command.deadline) == (
            "urgent",
            "omni",
            "in 2 hours",
        )

    def test_assign_without_a_mention_is_unknown(self) -> None:
        assert _cmd("/task assign fix the login bug").verb == "task_unknown"

    def test_unknown_subverb_is_answered_not_silent(self) -> None:
        assert _cmd("/task frobnicate U3").verb == "task_unknown"

    def test_chatter_is_still_silently_ignored(self) -> None:
        assert parse_command("anyone free for standup?") is None
        assert parse_command("task force meeting at 3") is None

    def test_every_bare_verb_still_parses_unchanged(self) -> None:
        assert _cmd("done U3 shipped it").verb == "done"
        assert _cmd("progress S5 talked to the customer").verb == "progress"
        assert _cmd("blocked OPS-2 waiting on Alice").verb == "blocked"
        assert _cmd("claim UP-1").verb == "claim"
        assert _cmd("!top U3").verb == "top"
        assert _cmd("my queue").verb == "my_queue"
        assert _cmd("report").verb == "report"
        assert _cmd("<@U0BOB> task fix the login bug").verb == "task"


class TestDeadlineGrammar:
    @pytest.mark.parametrize(
        ("text", "head", "phrase"),
        [
            ("ship the fix by friday", "ship the fix", "by friday"),
            ("ship it today", "ship it", "today"),
            ("ship it tomorrow", "ship it", "tomorrow"),
            ("ship it immediately", "ship it", "immediately"),
            ("ship it in 30 minutes", "ship it", "in 30 minutes"),
            ("ship it in 2 hours", "ship it", "in 2 hours"),
            ("ship it in 3 days", "ship it", "in 3 days"),
            ("ship it Tomorrow", "ship it", "Tomorrow"),
        ],
    )
    def test_trailing_phrases_split(self, text: str, head: str, phrase: str) -> None:
        assert team_tasks.split_deadline(text) == (head, phrase)

    def test_a_mid_sentence_day_word_is_not_a_deadline(self) -> None:
        assert team_tasks.split_deadline("make today count") == ("make today count", None)
        assert team_tasks.split_deadline("review tomorrow's plan first") == (
            "review tomorrow's plan first",
            None,
        )

    @pytest.mark.parametrize(
        ("phrase", "expected"),
        [
            ("immediately", "2026-08-12T09:30-04:00"),
            ("in 30 minutes", "2026-08-12T10:00-04:00"),
            ("in 2 hours", "2026-08-12T11:30-04:00"),
            ("in 3 days", "2026-08-15T09:30-04:00"),
            ("today", "2026-08-12T18:00-04:00"),
            ("tomorrow", "2026-08-13T10:00-04:00"),
            ("by friday", "2026-08-14T10:00-04:00"),
            # 'by wednesday' ON a Wednesday at 09:30: today's 10:00 slot is
            # still in the future, so it is today.
            ("by wednesday", "2026-08-12T10:00-04:00"),
        ],
    )
    def test_parse_deadline_on_a_fixed_clock(self, phrase: str, expected: str) -> None:
        assert team_tasks.parse_deadline(phrase, now=NOW) == expected

    def test_a_weekday_whose_slot_has_passed_rolls_a_week(self) -> None:
        eleven = NOW.replace(hour=11)  # Wednesday 11:00 — past the 10:00 slot
        assert team_tasks.parse_deadline("by wed", now=eleven) == "2026-08-19T10:00-04:00"

    def test_none_and_junk_parse_to_none(self) -> None:
        assert team_tasks.parse_deadline(None, now=NOW) is None
        assert team_tasks.parse_deadline("whenever", now=NOW) is None


# ==========================================================================
# apply — permission matrix, DM routing, delegation vs ad-hoc
# ==========================================================================


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


def _apply(
    collab: CollabStore,
    team: TeamStore,
    slack_map: dict[str, str],
    notifier: FakeNotifier,
    text: str,
    employee_id: str,
) -> str:
    return apply(
        _cmd(text),
        employee_id,
        PERMALINK,
        collab=collab,
        team=team,
        slack_map=slack_map,
        notifier=notifier,
    )


@pytest.fixture
def pool_ref(
    collab_store: CollabStore,
    make_card: Callable[..., BoardTask],
    initech_goal: str,
) -> str:
    """One pool-conformant ownerless card, ref ``Q1``."""
    make_card(
        title="Queue work",
        ref="Q1",
        goal_id=initech_goal,
        acceptance_criteria="the queue contract",
    )
    return "Q1"


class TestAddPermissions:
    def test_owner_queues_a_pool_eligible_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        initech_goal: str,
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task add Fix checkout flow #initech !high tomorrow",
            employees["owner"],
        )

        assert reply.startswith("✓ queued")
        assert "⏰ due" in reply
        card = collab_store.list_board_tasks()[0]
        assert card["owner_employee_id"] is None  # queued, not assigned
        assert card["goal_id"] == initech_goal
        assert card["priority"] == "high"
        assert card["acceptance_criteria"] == "Fix checkout flow"  # defaulted from the title
        assert card["due_date"] is not None and "T10:00" in card["due_date"]
        # Pool-eligible for real: the shared queue lists it.
        assert [pool.id for pool in team_store.pool_cards()] == [card["id"]]
        assert notifier.dms == []  # add assigns nobody, so it DMs nobody

    def test_the_ac_suffix_overrides_the_default_criteria(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        initech_goal: str,
    ) -> None:
        _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task add Fix checkout #initech | ac: checkout completes in test",
            employees["owner"],
        )
        assert collab_store.list_board_tasks()[0]["acceptance_criteria"] == (
            "checkout completes in test"
        )

    @pytest.mark.parametrize("who", ["alice", "bob"])
    def test_everyone_but_owner_is_politely_denied(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        initech_goal: str,
        who: str,
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task add Fix checkout #initech",
            employees[who],
        )
        assert "only the operator adds cards to the shared queue" in reply
        assert collab_store.list_board_tasks() == []

    def test_add_without_a_company_is_refused(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task add Fix checkout",
            employees["owner"],
        )
        assert "needs a company" in reply
        assert collab_store.list_board_tasks() == []

    def test_add_with_an_unknown_company_is_refused_not_orphaned(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task add Fix checkout #nosuchco",
            employees["owner"],
        )
        assert "no company goal matched #nosuchco" in reply
        assert collab_store.list_board_tasks() == []  # never a goal-less queue orphan


class TestQueueDelegation:
    @pytest.mark.parametrize("who", ["owner", "alice"])
    def test_owner_and_alice_delegate_a_queue_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        store: SqliteStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        pool_ref: str,
        who: str,
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task assign <@U0BOB> {pool_ref} tomorrow",
            employees[who],
        )

        assert reply.startswith("✓ Q1 → emp_bob")
        assert "⏰ due" in reply
        card = collab_store.list_board_tasks()[0]
        assert card["owner_employee_id"] == employees["bob"]
        assert card["due_date"] is not None and "T10:00" in card["due_date"]
        # One DM, to the assignee, naming the assigner and the deadline.
        assert [slack_id for slack_id, _ in notifier.dms] == ["U0BOB"]
        text = notifier.dms[0][1]
        assert "assigned you Q1" in text
        assert team_tasks.display_name(employees[who]) in text
        assert "⏰ due" in text
        # The audit event's actor IS the assigner (what done-DM routing reads),
        # and the note carries no 'owner:' token (no double DM via the watcher).
        events = team_store.list_events(str(card["id"]))
        assert events[-1]["event"] == "assign"
        assert events[-1]["actor"] == employees[who]
        assert "owner:" not in events[-1]["note"]

    def test_bob_is_politely_denied_delegation(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        pool_ref: str,
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task assign <@U0ALICE> {pool_ref}",
            employees["bob"],
        )
        assert "queue delegation is the operator/Alice only" in reply
        assert collab_store.list_board_tasks()[0]["owner_employee_id"] is None
        assert notifier.dms == []

    def test_delegating_an_owned_card_points_at_reassign(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
    ) -> None:
        make_card(title="Owned", ref="O1", owner_employee_id=employees["bob"])
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task assign <@U0ALICE> O1",
            employees["owner"],
        )
        assert "owned by Bob" in reply
        assert "/task reassign" in reply
        assert notifier.dms == []

    def test_unknown_ref_is_a_clean_reply(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task assign <@U0ALICE> ZZ9",
            employees["owner"],
        )
        assert reply == "no matching task"


class TestAdhocAssign:
    @pytest.mark.parametrize(
        ("who", "target", "target_slack"),
        [
            ("owner", "bob", "U0BOB"),
            ("alice", "bob", "U0BOB"),
            ("bob", "alice", "U0ALICE"),
        ],
    )
    def test_each_of_the_three_assigns_to_another(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        who: str,
        target: str,
        target_slack: str,
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task assign <@{target_slack}> review the pricing page",
            employees[who],
        )

        assert reply.startswith("✓ created")
        card = collab_store.list_board_tasks()[0]
        assert card["owner_employee_id"] == employees[target]
        assert [slack_id for slack_id, _ in notifier.dms] == [target_slack]
        assert "assigned you" in notifier.dms[0][1]

    def test_self_assign_is_politely_denied(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task assign <@U0BOB> review the pricing page",
            employees["bob"],
        )
        assert "handing work to a teammate" in reply
        assert collab_store.list_board_tasks() == []
        assert notifier.dms == []

    def test_deadline_lands_on_the_card_and_in_the_dm(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task assign <@U0BOB> fix the login bug tomorrow",
            employees["owner"],
        )
        card = collab_store.list_board_tasks()[0]
        assert card["due_date"] is not None and "T10:00" in card["due_date"]
        assert "⏰ due" in reply
        assert "⏰ due" in notifier.dms[0][1]

    def test_an_inactive_assignee_is_refused(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        store: SqliteStore,
        goals_store: CompanyGoalsStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        store._connection.execute(
            "UPDATE employees SET status = 'inactive' WHERE id = ?", (employees["frank"],)
        )
        store._connection.commit()
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task assign <@U0ANDY> review the pricing page",
            employees["owner"],
        )
        assert "not on the active roster" in reply
        assert collab_store.list_board_tasks() == []

    def test_an_inactive_sender_is_refused(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        store: SqliteStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        store._connection.execute(
            "UPDATE employees SET status = 'inactive' WHERE id = ?", (employees["frank"],)
        )
        store._connection.commit()
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task assign <@U0BOB> review the pricing page",
            employees["frank"],
        )
        assert "you're not on the active roster" in reply
        assert collab_store.list_board_tasks() == []


class TestDone:
    def _delegate(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
        pool_ref: str,
    ) -> None:
        _apply(
            collab_store,
            team_store,
            slack_map,
            FakeNotifier(),
            f"/task assign <@U0BOB> {pool_ref}",
            employees["owner"],
        )

    def test_the_owner_marks_done_and_the_assigner_is_dmed(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        pool_ref: str,
    ) -> None:
        self._delegate(collab_store, team_store, slack_map, employees, pool_ref)

        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task done Q1 shipped",
            employees["bob"],
        )

        assert reply.startswith("✓ Q1 → done")
        card = collab_store.list_board_tasks()[0]
        assert card["status"] == "done"
        # done -> the ASSIGNER (the operator delegated it), '<owner> completed <REF> — <title>'.
        assert [slack_id for slack_id, _ in notifier.dms] == ["U0TEAM"]
        assert "Bob completed Q1" in notifier.dms[0][1]
        assert "Queue work" in notifier.dms[0][1]

    @pytest.mark.parametrize("who", ["owner", "alice"])
    def test_done_is_owner_only_even_for_owner_and_alice(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        pool_ref: str,
        who: str,
    ) -> None:
        self._delegate(collab_store, team_store, slack_map, employees, pool_ref)

        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task done Q1", employees[who]
        )

        assert "only the owner (Bob) can mark Q1 done" in reply
        assert collab_store.list_board_tasks()[0]["status"] != "done"
        assert notifier.dms == []

    def test_done_on_an_ownerless_card_points_at_claim(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        pool_ref: str,
    ) -> None:
        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task done Q1", employees["bob"]
        )
        assert "has no owner" in reply
        assert "/task claim" in reply

    def test_done_on_a_self_created_card_dms_nobody(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
    ) -> None:
        # Created by its own owner: the assigner fallback IS the owner, and a
        # DM to yourself is noise, not information.
        collab_store.create_board_task(
            BoardTask(title="Own thing", ref="S1", owner_employee_id=employees["bob"]),
            actor=employees["bob"],
        )
        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task done S1", employees["bob"]
        )
        assert reply.startswith("✓ S1 → done")
        assert notifier.dms == []


class TestNote:
    @pytest.fixture
    def assigned_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
        pool_ref: str,
    ) -> str:
        """Q1, delegated by the operator to Bob — assigner=the operator, owner=Bob."""
        _apply(
            collab_store,
            team_store,
            slack_map,
            FakeNotifier(),
            f"/task assign <@U0BOB> {pool_ref}",
            employees["owner"],
        )
        return pool_ref

    def test_a_note_writes_a_comment_event_and_dms_the_assigner(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        assigned_card: str,
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task note Q1 waiting on staging access",
            employees["bob"],
        )

        assert reply.startswith("✓ noted on Q1")
        card_id = str(collab_store.list_board_tasks()[0]["id"])
        events = team_store.list_events(card_id)
        assert events[-1]["event"] == "comment"
        assert events[-1]["actor"] == employees["bob"]
        assert events[-1]["note"] == "waiting on staging access"
        # noter (Bob) != assigner (the operator) -> the ASSIGNER is DMed.
        assert [slack_id for slack_id, _ in notifier.dms] == ["U0TEAM"]
        assert "waiting on staging access" in notifier.dms[0][1]

    def test_when_the_noter_is_the_assigner_the_owner_is_dmed_instead(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        assigned_card: str,
    ) -> None:
        _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task note Q1 any progress?",
            employees["owner"],
        )
        assert [slack_id for slack_id, _ in notifier.dms] == ["U0BOB"]

    def test_a_third_party_note_still_reaches_the_assigner(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        assigned_card: str,
    ) -> None:
        _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task note Q1 I hit this bug too",
            employees["alice"],
        )
        assert [slack_id for slack_id, _ in notifier.dms] == ["U0TEAM"]

    def test_no_matching_task(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task note ZZ9 hello",
            employees["bob"],
        )
        assert reply == "no matching task"


class TestReassign:
    @pytest.mark.parametrize("who", ["owner", "alice"])
    def test_owner_and_alice_reassign_anyones_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
        who: str,
    ) -> None:
        make_card(title="Handoff", ref="H1", owner_employee_id=employees["bob"])

        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task reassign H1 <@U0ALICE>" if who == "owner" else "/task reassign H1 <@U0TEAM>",
            employees[who],
        )

        target = employees["alice"] if who == "owner" else employees["owner"]
        target_slack = "U0ALICE" if who == "owner" else "U0TEAM"
        assert reply.startswith(f"✓ H1 → {target}")
        assert "was emp_bob" in reply  # the reply notes the old owner
        card = collab_store.list_board_tasks()[0]
        assert card["owner_employee_id"] == target
        assert [slack_id for slack_id, _ in notifier.dms] == [target_slack]
        assert "reassigned H1 to you" in notifier.dms[0][1]
        # The event note names the old owner without the watcher's owner: token.
        events = team_store.list_events(str(card["id"]))
        assert events[-1]["event"] == "assign"
        assert "was emp_bob" in events[-1]["note"]
        assert "owner:" not in events[-1]["note"]

    def test_the_current_owner_hands_their_own_card_off(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
    ) -> None:
        make_card(title="Handoff", ref="H2", owner_employee_id=employees["bob"])
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task reassign H2 <@U0ALICE>",
            employees["bob"],
        )
        assert reply.startswith("✓ H2 → emp_alice")
        assert collab_store.list_board_tasks()[0]["owner_employee_id"] == employees["alice"]

    def test_a_non_owner_non_delegator_is_politely_denied(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
    ) -> None:
        make_card(title="Alice's", ref="H3", owner_employee_id=employees["alice"])
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task reassign H3 <@U0TEAM>",
            employees["bob"],
        )
        assert "only the operator/Alice or the current owner" in reply
        assert collab_store.list_board_tasks()[0]["owner_employee_id"] == employees["alice"]
        assert notifier.dms == []

    def test_reassigning_to_the_current_owner_is_a_no_op_reply(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
    ) -> None:
        make_card(title="Same", ref="H4", owner_employee_id=employees["alice"])
        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task reassign H4 <@U0ALICE>",
            employees["owner"],
        )
        assert reply == "H4 is already Alice's"
        assert notifier.dms == []


class TestQueueAndHelp:
    def test_queue_renders_grouped_by_company(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
        initech_goal: str,
    ) -> None:
        make_card(
            title="Fire drill",
            ref="Q2",
            goal_id=initech_goal,
            acceptance_criteria="works",
            priority="urgent",
        )
        make_card(title="Routine", ref="Q3", goal_id=initech_goal, acceptance_criteria="works")

        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task queue", employees["bob"]
        )

        assert reply.startswith("📋 Shared queue — 2")
        assert "*#initech* (2)" in reply
        assert "🔥 Q2 Fire drill" in reply
        assert "• Q3 Routine" in reply
        assert "/task claim" in reply  # the claim footer

    def test_queue_company_filter_resolves_aliases(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
        initech_goal: str,
    ) -> None:
        make_card(title="Rogue work", ref="Q5", goal_id=initech_goal, acceptance_criteria="w")
        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task queue #omni", employees["owner"]
        )
        assert "Q5 Rogue work" in reply

    def test_an_empty_queue_says_so(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task queue", employees["owner"]
        )
        assert "empty" in reply

    def test_help_names_every_verb(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        reply = _apply(collab_store, team_store, slack_map, notifier, "/task help", employees["owner"])
        for verb in ("add", "assign", "claim", "done", "note", "reassign", "queue", "mine"):
            assert f"/task {verb}" in reply

    def test_an_unknown_task_command_points_at_help(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task frobnicate", employees["owner"]
        )
        assert "/task help" in reply

    def test_task_mine_renders_the_senders_queue(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
    ) -> None:
        make_card(title="Mine one", ref="M1", owner_employee_id=employees["bob"])
        reply = _apply(
            collab_store, team_store, slack_map, notifier, "/task mine", employees["bob"]
        )
        assert "Mine one" in reply

    def test_task_claim_claims(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        pool_ref: str,
    ) -> None:
        reply = _apply(
            collab_store, team_store, slack_map, notifier, f"/task claim {pool_ref}",
            employees["bob"],
        )
        assert reply.startswith("✓")
        card = collab_store.list_board_tasks()[0]
        assert card["status"] == "claimed"
        assert card["claimed_by"] == "human:emp_bob"


class TestHandleWiring:
    """One end-to-end pass through team_updates_handle: the threaded reply and
    the single DM both fire from a real Socket-Mode-shaped event."""

    def test_a_slash_assign_replies_in_thread_and_dms_the_assignee(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
    ) -> None:
        replies: list[tuple[str, str, str]] = []
        event = message_event(user="U0TEAM", text="/task assign <@U0BOB> fix the login bug")

        team_updates_handle(
            event,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
            poster=lambda channel, ts, text: replies.append((channel, ts, text)),
            notifier=notifier,
        )

        assert len(replies) == 1
        assert replies[0][0] == "C0000EXAMPLE"
        assert replies[0][2].startswith("✓ created")
        assert [slack_id for slack_id, _ in notifier.dms] == ["U0BOB"]

    def test_a_malformed_slash_command_still_gets_a_reply(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
    ) -> None:
        replies: list[str] = []
        team_updates_handle(
            message_event(user="U0BOB", text="/task wat"),
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
            poster=lambda channel, ts, text: replies.append(text),
            notifier=notifier,
        )
        assert replies and "/task help" in replies[0]


# ==========================================================================
# review round-1 fixes (2026-08-13): the four probe-confirmed gaps stay closed
# ==========================================================================


class TestReviewRoundFixes:
    def test_an_inactive_sender_cannot_slash_claim(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        store: SqliteStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        pool_ref: str,
    ) -> None:
        store._connection.execute(
            "UPDATE employees SET status = 'inactive' WHERE id = ?", (employees["bob"],)
        )
        store._connection.commit()

        reply = _apply(
            collab_store, team_store, slack_map, notifier,
            f"/task claim {pool_ref}", employees["bob"],
        )

        assert "active roster" in reply
        card = collab_store.list_board_tasks()[0]
        assert card["owner_employee_id"] is None  # the claim never happened

    def test_delegation_refuses_a_non_queue_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
    ) -> None:
        # Ownerless and open but goal-less: an agent/system card, not queue work.
        make_card(title="Agent work", ref="A9")

        reply = _apply(
            collab_store, team_store, slack_map, notifier,
            "/task assign <@U0BOB> A9", employees["alice"],
        )

        assert "isn't a shared-queue card" in reply
        card = collab_store.list_board_tasks()[0]
        assert card["owner_employee_id"] is None
        assert notifier.dms == []

    def test_an_absurd_deadline_amount_still_gets_a_reply(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        initech_goal: str,
    ) -> None:
        reply = _apply(
            collab_store, team_store, slack_map, notifier,
            "/task add pay the bill #initech in 99999999 days", employees["owner"],
        )

        assert reply.startswith("✓ queued")  # answered, card created
        card = collab_store.list_board_tasks()[0]
        assert card["due_date"] is None  # absurd amount = no deadline

    def test_company_flag_is_case_insensitive(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        initech_goal: str,
    ) -> None:
        reply = _apply(
            collab_store, team_store, slack_map, notifier,
            "/task add Fix the funnel #Initech", employees["owner"],
        )

        assert reply.startswith("✓ queued")
        card = collab_store.list_board_tasks()[0]
        assert card["goal_id"] == initech_goal
        assert "#Initech" not in str(card["title"])
