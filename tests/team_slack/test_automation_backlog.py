"""The automation backlog: ``/task propose`` → ``/task approve`` | ``/task reject``.

the operator's GO, 2026-08-14. Three properties this file exists to defend:

* **Proposing is open, approving is not.** Anyone on the active roster may file
  a proposal, because a proposal approves nothing — the card sits in
  ``awaiting_approval`` where it cannot be claimed, counted or dispatched.
  Only the operator moves it out of there.
* **The decision verbs are narrow.** They act on cards created by
  ``/task propose`` and on nothing else: the review bucket holds cards that got
  there for entirely different reasons, and ``approve`` must not be able to
  resurrect one of those into the open pool.
* **The compute-pool envelope is EXACT.** ``for ai`` is only meaningful if
  :mod:`omniagentos.team.dispatch` recognises what it writes, so the key path
  is asserted against that module's own constant rather than a copy of it.

Categories are goal-ladder children (``Automations — <name>`` under the
"Automate 100% of the operator's tasks" long-term goal) — zero migrations, so these
tests seed goals, not schema.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.db.store import SqliteStore
from omniagentos.team import tasks as team_tasks
from omniagentos.team.contracts import AUTOMATION_PARENT_GOAL_ID
from omniagentos.team.dispatch import COMPUTE_POOL_TARGET
from omniagentos.team.slack_updates import Command, apply, parse_command
from omniagentos.team.store import TeamStore

PERMALINK = "https://slack.com/archives/C0000EXAMPLE/p1700000000000100"

#: The categories the operator creates through the goals API. Named here as
#: DATA, so a category added tomorrow needs no code change — only a row.
_CATEGORIES = (
    "email & comms",
    "content & marketing",
    "ads",
    "finance & ops",
    "dev tooling",
    "customer service",
)


class FakeNotifier:
    """The notifier seam every /task DM flow is tested through."""

    def __init__(self) -> None:
        self.dms: list[tuple[str, str]] = []

    def post_dm(self, slack_user_id: str, text: str, **_: Any) -> bool:
        self.dms.append((slack_user_id, text))
        return True


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def automation_goals(store: SqliteStore, goals_store: CompanyGoalsStore) -> dict[str, str]:
    """The parent goal and its six category children, as the API will create them."""
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) "
        "VALUES ('co_omniagentos', 'omniagentos', 'OmniAgentOS', 'active', "
        "'2026-08-14T00:00:00Z')"
    )
    goals_store.create_goal(
        org_company_id="co_omniagentos",
        title="Automate 100% of the operator's tasks",
        horizon="long_term",
        goal_id=AUTOMATION_PARENT_GOAL_ID,
    )
    created: dict[str, str] = {}
    for name in _CATEGORIES:
        goal = goals_store.create_goal(
            org_company_id="co_omniagentos",
            title=f"Automations — {name}",
            horizon="short_term",
            parent_goal_id=AUTOMATION_PARENT_GOAL_ID,
        )
        created[name] = str(goal["id"])
    return created


def _cmd(text: str) -> Command:
    command = parse_command(text)
    assert command is not None, f"expected a command from {text!r}"
    return command


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


# ==========================================================================
# grammar
# ==========================================================================


class TestGrammar:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/task propose draft the weekly digest", ("draft the weekly digest", None, None, "")),
            (
                "/task propose draft the weekly digest #email",
                ("draft the weekly digest", "email", None, ""),
            ),
            (
                "/task propose draft the weekly digest for ai",
                ("draft the weekly digest", None, "ai", ""),
            ),
            (
                "/task propose draft the digest #dev-tooling for bob | ac: it runs nightly",
                ("draft the digest", "dev-tooling", "bob", "it runs nightly"),
            ),
            (
                "/TASK PROPOSE draft the weekly digest FOR AI",
                ("draft the weekly digest", None, "ai", ""),
            ),
        ],
    )
    def test_propose_parses_its_title_category_hint_and_criteria(
        self, text: str, expected: tuple[str, str | None, str | None, str]
    ) -> None:
        command = _cmd(text)
        assert command.verb == "task_propose"
        assert (command.title, command.category, command.assignee_hint, command.note) == expected

    def test_a_title_containing_for_keeps_it(self) -> None:
        """The hint is a TAIL: only a trailing ``for <who>`` is a hint, so a
        title may talk about doing something for somebody."""
        command = _cmd("/task propose a script for the weekly digest")
        assert command.title == "a script for the weekly digest"
        assert command.assignee_hint is None

    def test_no_deadline_grammar_on_a_proposal(self) -> None:
        """A proposal is not scheduled work — it has no owner and no start until
        the operator approves it, so a deadline here would be a promise on a card nobody
        has agreed to do. The phrase stays part of the title."""
        command = _cmd("/task propose draft the weekly digest tomorrow")
        assert command.deadline is None
        assert command.title == "draft the weekly digest tomorrow"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("/task approve Q1", ("Q1", None)),
            ("/task approve Q1 for alice", ("Q1", "alice")),
            ("/task approve Q1 for ai", ("Q1", "ai")),
        ],
    )
    def test_approve_parses_ref_and_optional_hint(
        self, text: str, expected: tuple[str, str | None]
    ) -> None:
        command = _cmd(text)
        assert command.verb == "task_approve"
        assert (command.ref, command.assignee_hint) == expected

    def test_reject_takes_a_free_reason(self) -> None:
        command = _cmd("/task reject Q1 we already have one")
        assert (command.verb, command.ref, command.note) == (
            "task_reject",
            "Q1",
            "we already have one",
        )

    @pytest.mark.parametrize(
        "text",
        [
            "/task propose",
            "/task propose #email",
            "/task approve",
            "/task approve not-a-ref",
            "/task approve Q1 for the ads team",
            "/task reject",
        ],
    )
    def test_malformed_bodies_answer_rather_than_guess(self, text: str) -> None:
        """``/task approve Q1 for the ads team`` is the important one: a
        trailing phrase that is not a recognised hint is a typo'd hint, and
        guessing which teammate was meant is the mistake this refuses."""
        assert _cmd(text).verb == "task_unknown"

    def test_ordinary_chatter_is_still_silence(self) -> None:
        assert parse_command("we should propose an automation for this") is None


# ==========================================================================
# category resolution
# ==========================================================================


class TestCategoryResolution:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("ads", "ads"),
            ("Ads", "ads"),
            ("email", "email & comms"),
            ("comms", "email & comms"),
            ("finance", "finance & ops"),
            ("content", "content & marketing"),
            ("dev-tooling", "dev tooling"),
            ("dev_tooling", "dev tooling"),
            ("customer-service", "customer service"),
        ],
    )
    def test_a_token_resolves_to_its_category_goal(
        self, store: SqliteStore, automation_goals: dict[str, str], token: str, expected: str
    ) -> None:
        assert team_tasks.resolve_automation_category(store, token) == automation_goals[expected]

    @pytest.mark.parametrize("token", ["nosuchthing", "mail", "", None, "serv"])
    def test_an_unknown_token_resolves_to_nothing(
        self, store: SqliteStore, automation_goals: dict[str, str], token: str | None
    ) -> None:
        """``mail`` and ``serv`` are the interesting refusals: a mid-word
        fragment must NOT match, because a category is where work goes to be
        found later and a fuzzy match files it where nobody will look."""
        assert team_tasks.resolve_automation_category(store, token) is None

    def test_an_unknown_category_falls_back_to_the_parent_goal(
        self, store: SqliteStore, automation_goals: dict[str, str]
    ) -> None:
        """Never goal-less: a pool card with no goal is not pool-eligible, so a
        proposal that failed to resolve a category would approve into a card
        nobody can claim."""
        assert team_tasks.automation_goal_for(store, "nosuchthing") == AUTOMATION_PARENT_GOAL_ID
        assert team_tasks.automation_goal_for(store, None) == AUTOMATION_PARENT_GOAL_ID

    def test_a_database_without_the_parent_goal_degrades_quietly(self, store: SqliteStore) -> None:
        """The day before the goals are created, the command still works — the
        card simply lands without a goal rather than raising."""
        assert team_tasks.automation_categories(store) == []
        assert team_tasks.resolve_automation_category(store, "ads") is None
        assert team_tasks.automation_goal_for(store, "ads") is None


# ==========================================================================
# propose
# ==========================================================================


class TestPropose:
    def test_any_active_teammate_may_propose(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        automation_goals: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task propose draft the weekly digest #email for ai",
            employees["bob"],
        )
        assert reply.startswith("✓ proposed ")
        assert "awaiting the operator's approval" in reply
        assert "[#email]" in reply and "for ai" in reply

        task_id = reply.split()[2].rstrip(":")
        row = collab_store.get_board_task(task_id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.AWAITING_APPROVAL.value
        assert row["source"] == "automation-proposal"
        assert row["owner_employee_id"] is None
        assert row["goal_id"] == automation_goals["email & comms"]
        assert row["acceptance_criteria"] == "draft the weekly digest"
        assert row["org"]["proposal"] == {
            "proposed_by": employees["bob"],
            "assignee_hint": "ai",
        }

    def test_the_proposal_dms_owner(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        automation_goals: dict[str, str],
    ) -> None:
        """A backlog nobody is told about is a backlog nobody reads."""
        _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task propose draft the weekly digest",
            employees["bob"],
        )
        assert len(notifier.dms) == 1
        recipient, text = notifier.dms[0]
        assert recipient == "U0TEAM"
        assert "Bob proposed an automation" in text
        assert "/task approve" in text and "/task reject" in text

    def test_someone_off_the_active_roster_is_refused(
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
            "/task propose draft the weekly digest",
            "emp_stranger",
        )
        assert "not on the active roster" in reply
        assert collab_store.list_board_tasks() == []

    def test_an_explicit_ac_suffix_overrides_the_title_default(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        automation_goals: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task propose draft the digest | ac: it posts every Monday at 09:00",
            employees["alice"],
        )
        row = collab_store.get_board_task(reply.split()[2].rstrip(":"))
        assert row is not None
        assert row["acceptance_criteria"] == "it posts every Monday at 09:00"

    def test_the_org_envelope_is_merged_never_clobbered(
        self,
        collab_store: CollabStore,
        employees: dict[str, str],
        automation_goals: dict[str, str],
    ) -> None:
        """``org_json`` is a SHARED envelope — the orgdims classifier writes
        company/product keys into it. A proposal that replaced it would silently
        delete another subsystem's classification, and nothing would report the
        loss."""
        task = team_tasks.propose_automation(
            collab_store,
            title="draft the weekly digest",
            proposed_by=employees["bob"],
            category="ads",
        )
        collab_store.update_board_task(
            task.id, {"org_json": json.dumps({"company": "globex", "risk": "low"})}
        )
        decision = team_tasks.approve_automation(
            collab_store, task.id, actor=employees["owner"], assignee_hint="ai"
        )
        assert decision.outcome == "applied"
        row = collab_store.get_board_task(task.id)
        assert row is not None
        assert row["org"]["company"] == "globex"
        assert row["org"]["risk"] == "low"
        assert row["org"]["dispatch"]["target"] == COMPUTE_POOL_TARGET


# ==========================================================================
# approve / reject
# ==========================================================================


@pytest.fixture
def proposal(
    collab_store: CollabStore,
    employees: dict[str, str],
    automation_goals: dict[str, str],
) -> BoardTask:
    """One filed proposal, hinted at nobody, proposed by Bob."""
    return team_tasks.propose_automation(
        collab_store,
        title="draft the weekly digest",
        proposed_by=employees["bob"],
        category="email",
    )


class TestTheOwnerOnlyGate:
    @pytest.mark.parametrize("verb", ["approve", "reject"])
    @pytest.mark.parametrize("who", ["alice", "bob", "andy"])
    def test_only_owner_decides(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        proposal: BoardTask,
        verb: str,
        who: str,
    ) -> None:
        """Adding to the queue IS approval — the reason ``/task add`` is
        the operator-only is exactly the reason these are."""
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task {verb} {proposal.id}",
            employees[who],
        )
        assert "only the operator approves or rejects" in reply
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.AWAITING_APPROVAL.value
        assert notifier.dms == []

    @pytest.mark.parametrize("verb", ["approve", "reject"])
    def test_the_store_seam_refuses_a_non_operator_too(
        self, collab_store: CollabStore, employees: dict[str, str], proposal: BoardTask, verb: str
    ) -> None:
        """The gate lives in the primitive, not only in the Slack layer: the
        dashboard and any future caller get the same refusal."""
        call = team_tasks.approve_automation if verb == "approve" else team_tasks.reject_automation
        decision = call(collab_store, proposal.id, actor=employees["alice"])
        assert decision.outcome == "forbidden"
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.AWAITING_APPROVAL.value


class TestApproveOutcomes:
    def test_a_person_hint_lands_an_owned_open_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task approve {proposal.id} for bob",
            employees["owner"],
        )
        assert "assigned to Bob" in reply
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.OPEN.value
        assert row["owner_employee_id"] == employees["bob"]
        assert row["org"].get("dispatch") is None, "a person's card is never dispatched"
        events = [event["event"] for event in team_store.list_events(proposal.id)]
        assert "status_change" in events and "assign" in events

    def test_the_ai_hint_writes_the_EXACT_compute_pool_envelope(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        """Asserted against ``dispatch.COMPUTE_POOL_TARGET`` and the key path
        ``dispatch._dispatch_envelope`` actually reads — a copy of the string
        here would pass while the daemon ignored the card forever."""
        from omniagentos.team.dispatch import _dispatch_envelope

        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task approve {proposal.id} for ai",
            employees["owner"],
        )
        assert "marked for the AI pool" in reply
        assert "executable spec" in reply, "the reply must not claim work is in flight"
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert _dispatch_envelope(row).get("target") == COMPUTE_POOL_TARGET
        # Ownerless is not an oversight: the dispatcher only reads POOL cards,
        # so an owner would hide the card from the daemon the hint asks for.
        assert row["owner_employee_id"] is None
        assert row["status"] == BoardTaskStatus.OPEN.value
        assert proposal.id in {card.id for card in team_store.pool_cards()}

    def test_the_stored_hint_is_used_when_the_verb_gives_none(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        automation_goals: dict[str, str],
    ) -> None:
        """The proposer's opinion is kept and read back — it is just not the
        proposer's decision."""
        task = team_tasks.propose_automation(
            collab_store,
            title="draft the digest",
            proposed_by=employees["bob"],
            assignee_hint="alice",
        )
        _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task approve {task.id}",
            employees["owner"],
        )
        row = collab_store.get_board_task(task.id)
        assert row is not None
        assert row["owner_employee_id"] == employees["alice"]

    def test_the_verb_hint_overrides_the_stored_one(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        automation_goals: dict[str, str],
    ) -> None:
        task = team_tasks.propose_automation(
            collab_store,
            title="draft the digest",
            proposed_by=employees["bob"],
            assignee_hint="alice",
        )
        _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task approve {task.id} for ai",
            employees["owner"],
        )
        row = collab_store.get_board_task(task.id)
        assert row is not None
        assert row["owner_employee_id"] is None
        assert row["org"]["dispatch"]["target"] == COMPUTE_POOL_TARGET

    def test_no_hint_lands_a_claimable_pool_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task approve {proposal.id}",
            employees["owner"],
        )
        assert "in the shared queue (claimable)" in reply
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert row["owner_employee_id"] is None
        assert row["org"].get("dispatch") is None
        assert proposal.id in {card.id for card in team_store.pool_cards()}

    def test_approval_dms_the_proposer(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task approve {proposal.id} for alice",
            employees["owner"],
        )
        recipients = {recipient for recipient, _text in notifier.dms}
        assert recipients == {"U0BOB", "U0ALICE"}, "the proposer AND the new owner"
        assert any("approved" in text for _who, text in notifier.dms)


class TestRejectAndTheNarrowGuard:
    def test_reject_cancels_and_records_the_reason(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task reject {proposal.id} we already have one",
            employees["owner"],
        )
        assert reply.startswith("✓ rejected")
        assert "we already have one" in reply
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.CANCELLED.value
        notes = [
            event["note"]
            for event in team_store.list_events(proposal.id)
            if event["event"] == "comment"
        ]
        assert "we already have one" in notes
        assert notifier.dms[0][0] == "U0BOB"
        assert "declined" in notifier.dms[0][1]

    @pytest.mark.parametrize("verb", ["approve", "reject"])
    def test_a_card_that_is_not_a_proposal_is_refused(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        verb: str,
    ) -> None:
        """The narrow guard: the review bucket holds cards that got there for
        other reasons (a swarm awaiting a human call, a promoted plan), and
        these verbs must not resurrect one of those into the open pool."""
        card = make_card(
            title="Swarm work awaiting a call",
            ref="S1",
            status=BoardTaskStatus.AWAITING_APPROVAL.value,
        )
        reply = _apply(
            collab_store, team_store, slack_map, notifier, f"/task {verb} S1", employees["owner"]
        )
        assert "isn't an automation proposal" in reply
        row = collab_store.get_board_task(card.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.AWAITING_APPROVAL.value

    @pytest.mark.parametrize("verb", ["approve", "reject"])
    def test_an_unknown_ref_answers_rather_than_raises(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        verb: str,
    ) -> None:
        # Ref-SHAPED but absent: an unref-shaped token is task_unknown at the
        # grammar layer, which is a different (also answered) refusal.
        reply = _apply(
            collab_store, team_store, slack_map, notifier, f"/task {verb} Q99", employees["owner"]
        )
        assert reply == "no matching proposal"

    @pytest.mark.parametrize("second", ["approve", "reject"])
    def test_a_second_decision_reports_already_decided(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        proposal: BoardTask,
        second: str,
    ) -> None:
        """Two people deciding the same proposal in a channel is an ordinary
        race — the loser gets an answer, not a stack trace and not a second,
        contradictory decision."""
        _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task approve {proposal.id} for bob",
            employees["owner"],
        )
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task {second} {proposal.id}",
            employees["owner"],
        )
        assert "already decided" in reply
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.OPEN.value
        assert row["owner_employee_id"] == employees["bob"]


# ==========================================================================
# review round 2 — the sibling paths the first round did not bind
# ==========================================================================


class TestTheGenericPatchCannotDecide:
    """BLOCKER (round 2): the Slack gate is not the only way to move a card.

    ``PATCH /api/collab/board/{id}`` derives ANY authenticated principal and
    calls ``update_board_task``, so a gate that lives only in the /task handler
    is a gate with a door beside it. The refusal is at the STORE, which is what
    every path — Slack, HTTP, a future script — goes through.
    """

    @pytest.mark.parametrize("status", ["open", "cancelled", "in_progress"])
    def test_a_generic_status_patch_out_of_awaiting_approval_is_refused(
        self, collab_store: CollabStore, proposal: BoardTask, status: str
    ) -> None:
        with pytest.raises(ValueError, match="automation_proposal_decision_required"):
            collab_store.update_board_task(proposal.id, {"status": status}, actor="emp_alice")
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.AWAITING_APPROVAL.value

    def test_the_operator_is_refused_too(
        self, collab_store: CollabStore, proposal: BoardTask, employees: dict[str, str]
    ) -> None:
        """the operator's OWN decisions go through the verbs: that is where the hint, the
        envelope, the rejection comment and the DMs live. A PATCH would produce
        a decided card with none of them and no record that a decision was made."""
        with pytest.raises(ValueError, match="automation_proposal_decision_required"):
            collab_store.update_board_task(proposal.id, {"status": "open"}, actor=employees["owner"])

    def test_stripping_the_source_first_is_refused(
        self, collab_store: CollabStore, proposal: BoardTask
    ) -> None:
        """The two-step bypass: relabel, then move. The source is immutable in
        both directions, so the first step fails and the second never applies."""
        for source in ("decision", "", "task-adhoc"):
            with pytest.raises(ValueError, match="source_boundary_immutable"):
                collab_store.update_board_task(proposal.id, {"source": source}, actor="emp_alice")
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert row["source"] == "automation-proposal"

    def test_harmless_patches_still_work(
        self, collab_store: CollabStore, proposal: BoardTask
    ) -> None:
        """The guard is narrow: only the decision itself is reserved. Fixing a
        typo in a proposal's title must not need the operator."""
        assert collab_store.update_board_task(
            proposal.id, {"title": "draft the weekly digest (v2)"}, actor="emp_bob"
        )
        assert collab_store.update_board_task(
            proposal.id, {"status": BoardTaskStatus.AWAITING_APPROVAL.value}, actor="emp_bob"
        )

    def test_a_decided_card_is_a_normal_card_again(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        """The guard binds the awaiting_approval WINDOW, not the card forever:
        once approved, the card takes part in the ordinary board lifecycle."""
        _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task approve {proposal.id} for bob",
            employees["owner"],
        )
        assert collab_store.update_board_task(
            proposal.id, {"status": "in_progress"}, actor=employees["bob"]
        )


class TestTheDecisionGuardIsInsideTheTransaction:
    def test_a_source_change_between_read_and_write_cannot_be_acted_on(
        self, collab_store: CollabStore, employees: dict[str, str], automation_goals: dict[str, str]
    ) -> None:
        """The guard and the UPDATE must agree about the SAME row. With the
        check outside the transaction, a card that stopped being a proposal
        between the two was still approved."""
        task = team_tasks.propose_automation(
            collab_store, title="draft the digest", proposed_by=employees["bob"]
        )
        # Simulate the racing writer landing between the read and the write.
        collab_store._connection.execute(
            "UPDATE board_tasks SET source = 'decision' WHERE id = ?", (task.id,)
        )
        collab_store._connection.commit()
        decision = team_tasks.approve_automation(
            collab_store, task.id, actor=employees["owner"], assignee_hint="bob"
        )
        assert decision.outcome == "not_a_proposal"
        row = collab_store.get_board_task(task.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.AWAITING_APPROVAL.value

    def test_an_archived_proposal_is_not_decidable(
        self, collab_store: CollabStore, employees: dict[str, str], automation_goals: dict[str, str]
    ) -> None:
        task = team_tasks.propose_automation(
            collab_store, title="draft the digest", proposed_by=employees["bob"]
        )
        collab_store.update_board_task(task.id, {"archived_at": "2026-08-14T00:00:00Z"})
        decision = team_tasks.approve_automation(collab_store, task.id, actor=employees["owner"])
        # ``not_found``: the ref lookup already excludes archived cards, and the
        # in-transaction archival check behind it is the belt for a caller that
        # reaches the id another way. Either refusal is correct; approving is not.
        assert decision.outcome != "applied"
        row = collab_store.get_board_task(task.id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.AWAITING_APPROVAL.value

    def test_the_losing_receipt_quotes_the_FRESH_row(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        """The loser of a race must be told what the board actually holds — a
        receipt quoting the pre-race snapshot reports 'awaiting_approval' for a
        card that was decided a millisecond ago."""
        team_tasks.approve_automation(
            collab_store, proposal.id, actor=employees["owner"], assignee_hint="bob"
        )
        decision = team_tasks.reject_automation(
            collab_store, proposal.id, actor=employees["owner"], reason="too late"
        )
        assert decision.outcome == "already_decided"
        assert decision.task is not None
        assert decision.task["status"] == BoardTaskStatus.OPEN.value
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task reject {proposal.id} too late",
            employees["owner"],
        )
        assert "already decided (status: open)" in reply


class TestTheAiEnvelopeIsHonest:
    def test_the_envelope_says_it_is_not_ready(
        self, collab_store: CollabStore, employees: dict[str, str], proposal: BoardTask
    ) -> None:
        team_tasks.approve_automation(
            collab_store, proposal.id, actor=employees["owner"], assignee_hint="ai"
        )
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert row["org"]["dispatch"] == {"target": COMPUTE_POOL_TARGET, "ready": False}

    def test_the_dispatcher_names_the_gap_rather_than_passing_over_it(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        """Driven through the real pass. A silent pass-over would leave the card
        sitting in the pool forever with nobody able to say why; the named skip
        is what makes the missing spec findable."""
        from omniagentos.team.dispatch import dispatch_once

        team_tasks.approve_automation(
            collab_store, proposal.id, actor=employees["owner"], assignee_hint="ai"
        )
        actions = dispatch_once(collab_store, team_store, dry_run=True)
        mine = [action for action in actions if action.task_id == proposal.id]
        assert len(mine) == 1, "the card is seen, not skipped silently"
        assert mine[0].kind == "skip"
        assert "dispatch.ready=false" in mine[0].detail
        assert "acceptance_cmd" in mine[0].detail and "owned_paths" in mine[0].detail

    def test_a_completed_spec_dispatches_normally(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        """...and the flag is a GATE, not a wall: once a coordinator writes the
        spec, the same card enqueues."""
        from omniagentos.team.dispatch import dispatch_once

        team_tasks.approve_automation(
            collab_store, proposal.id, actor=employees["owner"], assignee_hint="ai"
        )
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        collab_store.update_board_task(
            proposal.id,
            {
                "org_json": json.dumps(
                    {
                        **row["org"],
                        "dispatch": {
                            "target": COMPUTE_POOL_TARGET,
                            "ready": True,
                            "acceptance_cmd": "pytest -q",
                            "owned_paths": ["omniagentos/team/"],
                        },
                    }
                )
            },
        )
        actions = dispatch_once(collab_store, team_store, dry_run=True)
        mine = [action for action in actions if action.task_id == proposal.id]
        assert [action.kind for action in mine] == ["enqueue"]


class TestTheBacklogRefusesToRunUnconfigured:
    def test_a_missing_parent_goal_refuses_the_proposal(
        self, collab_store: CollabStore, employees: dict[str, str]
    ) -> None:
        """No ``automation_goals`` fixture here on purpose. A goal-less card is
        not pool-eligible, so approving one would mint work nobody can ever
        claim — with a success receipt on the way in and no backfill when the
        goal is created later."""
        with pytest.raises(ValueError, match="automation_backlog_unconfigured"):
            team_tasks.propose_automation(
                collab_store, title="draft the digest", proposed_by=employees["bob"]
            )
        assert collab_store.list_board_tasks() == []

    def test_the_slack_verb_reports_the_configuration_fault(
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
            "/task propose draft the weekly digest",
            employees["bob"],
        )
        assert "automation_backlog_unconfigured" in reply
        assert collab_store.list_board_tasks() == []
        assert notifier.dms == [], "nothing happened; nobody is told it did"


class TestCategoryAmbiguity:
    @pytest.fixture
    def sibling_categories(
        self, store: SqliteStore, goals_store: CompanyGoalsStore, automation_goals: dict[str, str]
    ) -> dict[str, str]:
        """A second ``customer …`` category, which is what makes ``#customer``
        ambiguous — the exact shape that used to resolve to whichever goal was
        created first."""
        goal = goals_store.create_goal(
            org_company_id="co_omniagentos",
            title="Automations — customer success",
            horizon="short_term",
            parent_goal_id=AUTOMATION_PARENT_GOAL_ID,
        )
        return {**automation_goals, "customer success": str(goal["id"])}

    def test_an_ambiguous_prefix_names_the_candidates(
        self, store: SqliteStore, sibling_categories: dict[str, str]
    ) -> None:
        match = team_tasks.match_automation_category(store, "customer")
        assert match.goal_id is None
        assert set(match.ambiguous) == {"customer service", "customer success"}

    def test_an_exact_name_still_wins_over_its_siblings(
        self, store: SqliteStore, sibling_categories: dict[str, str]
    ) -> None:
        """Precedence is not cosmetic: an exact name must never lose to a longer
        category that merely starts with it."""
        match = team_tasks.match_automation_category(store, "customer service")
        assert match.goal_id == sibling_categories["customer service"]
        assert match.ambiguous == ()

    def test_the_slack_verb_refuses_and_lists_them(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        sibling_categories: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            "/task propose triage the inbox #customer",
            employees["bob"],
        )
        assert "matches 2 categories" in reply
        assert "customer service" in reply and "customer success" in reply
        assert collab_store.list_board_tasks() == []

    @pytest.mark.parametrize("token", ["dev-tooling", "dev_tooling", "DEV_TOOLING"])
    def test_the_underscore_and_hyphen_spellings_are_one_category(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        automation_goals: dict[str, str],
        token: str,
    ) -> None:
        """The token grammar is shared with the dashboard route: a surface that
        swallowed only ``#dev`` would file the work under a different category
        while showing the user the one they typed."""
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task propose ship the linter #{token}",
            employees["bob"],
        )
        row = collab_store.get_board_task(reply.split()[2].rstrip(":"))
        assert row is not None
        assert row["goal_id"] == automation_goals["dev tooling"]


class TestDmFailureIsReported:
    class DeadNotifier:
        """A notifier whose delivery fails — the ordinary Slack outage."""

        def post_dm(self, slack_user_id: str, text: str, **_: Any) -> bool:
            return False

    def test_a_proposal_says_the_dm_did_not_land(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
        automation_goals: dict[str, str],
    ) -> None:
        """The card IS filed — the board write never depends on Slack — but the
        reply must not claim the notification. Silently swallowing it is how the operator
        never learns a proposal is waiting while the proposer believes he was
        told."""
        reply = apply(
            _cmd("/task propose draft the weekly digest"),
            employees["bob"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
            notifier=self.DeadNotifier(),
        )
        assert reply.startswith("✓ proposed ")
        assert "⚠ DM to Owner not delivered" in reply
        assert len(collab_store.list_board_tasks()) == 1

    def test_a_decision_names_every_recipient_that_missed_out(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        reply = apply(
            _cmd(f"/task approve {proposal.id} for alice"),
            employees["owner"],
            PERMALINK,
            collab=collab_store,
            team=team_store,
            slack_map=slack_map,
            notifier=self.DeadNotifier(),
        )
        assert reply.startswith("✓ approved ")
        assert "⚠ DM to Alice, Bob not delivered" in reply

    def test_a_delivered_dm_adds_no_noise(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        notifier: FakeNotifier,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            notifier,
            f"/task approve {proposal.id}",
            employees["owner"],
        )
        assert "⚠" not in reply


class TestClassificationPreservesTheEnvelopes:
    """MAJOR (round 2): ``org_json`` is a SHARED envelope and orgdims used to
    replace the whole column, so a reclassification silently deleted the
    proposal's stored hint and an approved AI card's dispatch routing."""

    def _classify(self, store: SqliteStore, task_id: str) -> None:
        from omniagentos.orgdims.contracts import DimensionBundle, OrganizationContext
        from omniagentos.orgdims.store import OrgDimsStore

        bundle = DimensionBundle(
            organization_context=OrganizationContext(company_slug="globex")
        )
        OrgDimsStore(store._connection).set_board_org(task_id, bundle)

    def test_classifying_AFTER_a_proposal_keeps_the_proposal_envelope(
        self,
        collab_store: CollabStore,
        store: SqliteStore,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        self._classify(store, proposal.id)
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert row["org"]["proposal"] == {
            "proposed_by": employees["bob"],
            "assignee_hint": None,
        }
        assert row["org"]["organization_context"]["company_slug"] == "globex"

    def test_classifying_AFTER_an_ai_approval_keeps_the_card_dispatchable(
        self,
        collab_store: CollabStore,
        store: SqliteStore,
        employees: dict[str, str],
        proposal: BoardTask,
    ) -> None:
        """The consequence that matters: without the merge, a reclassification
        stops an approved AI card from ever being dispatchable, and nothing
        anywhere reports the loss."""
        from omniagentos.team.dispatch import _dispatch_envelope

        team_tasks.approve_automation(
            collab_store, proposal.id, actor=employees["owner"], assignee_hint="ai"
        )
        self._classify(store, proposal.id)
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        assert _dispatch_envelope(row).get("target") == COMPUTE_POOL_TARGET
        assert row["org"]["proposal"]["proposed_by"] == employees["bob"]
        assert row["org"]["organization_context"]["company_slug"] == "globex"

    def test_orgdims_still_owns_its_own_keys_wholesale(
        self, collab_store: CollabStore, store: SqliteStore, proposal: BoardTask
    ) -> None:
        """The merge preserves FOREIGN keys; it must not leave half of a
        previous classification behind."""
        from omniagentos.orgdims.contracts import DimensionBundle, OrganizationContext
        from omniagentos.orgdims.store import OrgDimsStore

        dal = OrgDimsStore(store._connection)
        dal.set_board_org(
            proposal.id,
            DimensionBundle(
                organization_context=OrganizationContext(
                    company_slug="globex", product_slug="ads"
                )
            ),
        )
        dal.set_board_org(
            proposal.id,
            DimensionBundle(organization_context=OrganizationContext(company_slug="initech")),
        )
        row = collab_store.get_board_task(proposal.id)
        assert row is not None
        context = row["org"]["organization_context"]
        assert context["company_slug"] == "initech"
        assert not context.get("product_slug"), "the stale product must not survive"
        assert row["org"]["proposal"] is not None
