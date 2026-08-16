"""POST /api/team/nl-assign — the dashboard's owner-assigning path.

Deterministic grammar, no model call. The property that matters most is not the
parsing: it is that a card born from a sentence is still a card the board's
rules bind. Review S1 (BLOCKER): acceptance criteria default to the TITLE, so
the evidence-before-done gate applies to every card this route creates — an
empty acceptance_criteria would have made NL the one way to mint work that can
reach done with nothing behind it.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.team.store import TeamStore
from omniagentos.team.tasks import TASK_ADHOC_SOURCE


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


def _assign(api: httpx.AsyncClient, text: str) -> httpx.Response:
    return _run(api.post("/api/team/nl-assign", json={"text": text}))


@pytest.fixture
def omniagentos_goal(store: SqliteStore, goals_store: CompanyGoalsStore) -> str:
    store._connection.execute(
        "INSERT INTO org_companies (id, slug, name, status, created_at) VALUES (?, ?, ?, ?, ?)",
        ("co_omniagentos", "omniagentos", "OmniAgentOS", "active", utc_now_iso()),
    )
    goal = goals_store.create_goal(
        org_company_id="co_omniagentos",
        title="General engineering — OmniAgentOS",
        horizon="quarter",
    )
    return str(goal["id"])


# --- the grammar contract, as fixtures -------------------------------------
#
# The dashboard composer's client-side intercept runs against THESE SAME
# STRINGS (Sol review, item 6). The two halves must agree exactly: a client
# that is stricter drops assignments into an LLM turn, and a client that is
# looser swallows ordinary chat. Every accepted string below names Bob and
# titles the card "fix the login page" so one assertion covers all of them.
NL_ASSIGN_ACCEPTED: tuple[str, ...] = (
    # (a) the Slack /task spelling, with and without the @, any case
    "/task assign @bob fix the login page",
    "/task assign bob fix the login page",
    "/TASK ASSIGN @Bob fix the login page",
    # (b) "give <name> a task to ..." and its colon form
    "give bob a task to fix the login page",
    "give @Bob a task to fix the login page",
    "give bob a task: fix the login page",
    "GIVE BOB A TASK TO fix the login page",
    # (c) the terse form
    "assign bob fix the login page",
    "assign @bob fix the login page",
    "Assign Bob fix the login page",
)

# The PROPOSE half (automation backlog, 2026-08-14). Same contract, same
# mirroring obligation — and a FOLLOW-UP is owed on the client: the dashboard
# composer's intercept (dashboard/src/features/chats/nlAssignGrammar.ts) does
# not yet carry a 'propose' pattern, so these sentences currently reach the LLM
# instead of this route. The server half is landed first on purpose (a client
# that intercepted before the route existed would 404 every proposal); adding
# the two regexes there is the whole of the client change.
NL_PROPOSE_ACCEPTED: tuple[str, ...] = (
    "propose an automation to draft the weekly digest",
    "propose a automation to draft the weekly digest",
    "propose an automation: draft the weekly digest",
    "propose automation: draft the weekly digest",
    "PROPOSE AN AUTOMATION TO draft the weekly digest",
    "propose an automation to draft the weekly digest #email",
    "propose an automation to draft the weekly digest for ai",
    "propose an automation to draft the weekly digest #email for bob",
)

NL_ASSIGN_REJECTED: tuple[str, ...] = (
    # not an assignment at all
    "what is bob working on?",
    "hello there",
    "bob should fix the login page",
    "can you assign this to bob?",
    # right verb, no title
    "assign bob",
    "give bob a task to",
    "give bob a task:",
    "/task assign @bob",
    # right shape, wrong verb/prefix — near-misses are the dangerous class,
    # because a parser that "helpfully" accepts them files work under a name
    # the writer never confirmed
    "task assign bob fix the login page",
    "give bob a job to fix the login page",
    "/task claim GH-7",
    # propose near-misses: the verb without the noun, and the noun without a
    # title, are both ordinary chat until they say what to automate
    "propose an automation",
    "propose automation:",
    "propose an automation to",
    "we should propose an automation to draft the digest",
    "propose a meeting to discuss automation",
)

# Shape-ACCEPTED, roster-REJECTED. The split is deliberate and the dashboard
# mirrors it: the client intercepts on SHAPE (it does not know the roster), the
# server answers with the roster. "assign fix the login page" reads as an
# assignment to a person called "fix" — the client should send it, and the
# server should say there is nobody by that name rather than silently guessing.
NL_ASSIGN_UNKNOWN_NAME: tuple[str, ...] = (
    "assign fix the login page",
    "give bartholomew a task to fix the login page",
)


class TestTheGrammar:
    @pytest.mark.parametrize("text", NL_ASSIGN_ACCEPTED)
    def test_every_accepted_shape_lands_the_same_card(
        self, api: httpx.AsyncClient, employees: dict[str, str], text: str
    ) -> None:
        response = _assign(api, text)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["owner_employee_id"] == employees["bob"]
        assert body["title"] == "fix the login page"

    def test_a_trailing_deadline_becomes_a_due_date(
        self, api: httpx.AsyncClient, employees: dict[str, str]
    ) -> None:
        body = _assign(api, "give bob a task to fix the login page tomorrow").json()
        assert body["title"] == "fix the login page"
        assert body["due_date"] is not None
        # Local wall clock with an explicit offset — parse_deadline's contract.
        assert "T10:00" in body["due_date"]

    def test_a_company_flag_ladders_the_card_to_a_goal(
        self, api: httpx.AsyncClient, omniagentos_goal: str, employees: dict[str, str]
    ) -> None:
        body = _assign(api, "give bob a task to speed up the gate #omniagentos").json()
        assert body["goal_id"] == omniagentos_goal
        assert body["title"] == "speed up the gate"

    def test_an_unknown_company_is_a_400(
        self, api: httpx.AsyncClient, employees: dict[str, str]
    ) -> None:
        response = _assign(api, "give bob a task to do something #nosuchco")
        assert response.status_code == 400
        assert "nosuchco" in response.text

    @pytest.mark.parametrize("text", NL_ASSIGN_UNKNOWN_NAME)
    def test_a_shape_match_with_an_unknown_name_is_a_400_that_names_it(
        self, api: httpx.AsyncClient, employees: dict[str, str], text: str
    ) -> None:
        """Grammar accepted it; the ROSTER did not. The message must say which
        name failed, or the writer cannot tell a typo from a parse failure."""
        response = _assign(api, text)
        assert response.status_code == 400
        assert "no active teammate called" in response.text

    def test_an_inactive_teammate_is_not_assignable(
        self, api: httpx.AsyncClient, goals_store: CompanyGoalsStore, employees: dict[str, str]
    ) -> None:
        goals_store.ensure_employee(employee_id="emp_gone", name="Gone", status="inactive")
        assert _assign(api, "give gone a task to do the thing").status_code == 400

    @pytest.mark.parametrize("text", NL_ASSIGN_REJECTED)
    def test_everything_outside_the_grammar_is_a_helpful_400(
        self, api: httpx.AsyncClient, employees: dict[str, str], text: str
    ) -> None:
        """A parser that guesses files somebody else's work under your name."""
        response = _assign(api, text)
        assert response.status_code == 400, response.text
        assert "give <name> a task to" in response.text

    def test_the_accepted_and_rejected_sets_do_not_overlap(self) -> None:
        """The fixtures are a CONTRACT the dashboard mirrors; a string in both
        lists would let the two suites 'agree' while testing nothing."""
        assert not set(NL_ASSIGN_ACCEPTED) & set(NL_ASSIGN_REJECTED)

    def test_the_slack_spelling_takes_a_deadline_and_an_ac_suffix(
        self, api: httpx.AsyncClient, employees: dict[str, str]
    ) -> None:
        """The optional tails apply to every shape, not just to (b)."""
        body = _assign(
            api,
            "/task assign @bob fix the login page tomorrow | ac: it loads under 1s",
        ).json()
        assert body["title"] == "fix the login page"
        assert body["acceptance_criteria"] == "it loads under 1s"
        assert "T10:00" in str(body["due_date"])


class TestEvidenceAlwaysBinds:
    def test_acceptance_criteria_default_to_the_title(
        self, api: httpx.AsyncClient, employees: dict[str, str], automation_goals: dict[str, str]
    ) -> None:
        body = _assign(api, "give bob a task to fix the login page").json()
        assert body["acceptance_criteria"] == "fix the login page"
        assert "| ac:" in body["message"], "the response advertises the override (round-3 §10)"

    def test_an_explicit_ac_suffix_overrides_the_default(
        self, api: httpx.AsyncClient, employees: dict[str, str]
    ) -> None:
        body = _assign(
            api, "give bob a task to fix the login page | ac: the login page loads under 1s"
        ).json()
        assert body["title"] == "fix the login page"
        assert body["acceptance_criteria"] == "the login page loads under 1s"

    def test_an_nl_created_card_cannot_reach_done_without_evidence(
        self,
        api: httpx.AsyncClient,
        collab_store: CollabStore,
        team_store: TeamStore,
        employees: dict[str, str],
    ) -> None:
        """review S1 (BLOCKER), end to end: the store's done-gate binds because
        the card has BOTH an owner and acceptance criteria."""
        task_id = _assign(api, "give bob a task to fix the login page").json()["task_id"]
        with pytest.raises(ValueError, match="without evidence"):
            collab_store.update_board_task(
                task_id, {"status": BoardTaskStatus.DONE.value}, actor=employees["bob"]
            )
        team_store.add_evidence(kind="commit", ref="abc123", repo="omnios", task_id=task_id)
        assert collab_store.update_board_task(
            task_id, {"status": BoardTaskStatus.DONE.value}, actor=employees["bob"]
        )

    def test_the_card_is_a_zero_point_adhoc_task(
        self, api: httpx.AsyncClient, collab_store: CollabStore, employees: dict[str, str]
    ) -> None:
        """the operator's Work-vs-Tasks ruling: an ad-hoc assignment is a Task, not Work."""
        task_id = _assign(api, "give bob a task to fix the login page").json()["task_id"]
        row = collab_store.get_board_task(task_id)
        assert row is not None
        assert row["source"] == TASK_ADHOC_SOURCE
        assert row["owner_employee_id"] == employees["bob"]


class TestProposeGrammar:
    """The automation backlog's dashboard half. A proposal names no assignee —
    it lands in ``awaiting_approval`` for the operator — so it is emphatically NOT an
    assignment, and the two grammars must not overlap."""

    @pytest.mark.parametrize("text", NL_PROPOSE_ACCEPTED)
    def test_every_accepted_shape_files_a_proposal(
        self,
        api: httpx.AsyncClient,
        employees: dict[str, str],
        automation_goals: dict[str, str],
        text: str,
    ) -> None:
        response = _assign(api, text)
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "automation_proposal"
        assert body["title"] == "draft the weekly digest"
        assert body["status"] == BoardTaskStatus.AWAITING_APPROVAL.value
        assert "awaiting the operator's approval" in body["message"]

    def test_a_proposal_is_not_claimable_until_it_is_approved(
        self,
        api: httpx.AsyncClient,
        collab_store: CollabStore,
        employees: dict[str, str],
        automation_goals: dict[str, str],
    ) -> None:
        """The whole safety argument for letting anyone propose: the card
        cannot be worked, counted or dispatched until the operator decides."""
        task_id = _assign(api, "propose an automation to draft the weekly digest").json()["task_id"]
        row = collab_store.get_board_task(task_id)
        assert row is not None
        assert row["status"] == BoardTaskStatus.AWAITING_APPROVAL.value
        assert row["owner_employee_id"] is None
        assert row["source"] == "automation-proposal"

    def test_the_category_and_hint_tails_are_parsed(
        self,
        api: httpx.AsyncClient,
        collab_store: CollabStore,
        employees: dict[str, str],
        automation_goals: dict[str, str],
    ) -> None:
        body = _assign(api, "propose an automation to draft the weekly digest #email for ai").json()
        assert body["title"] == "draft the weekly digest"
        assert body["category"] == "email"
        assert body["goal_id"] == automation_goals["email & comms"]
        assert body["assignee_hint"] == "ai"
        row = collab_store.get_board_task(body["task_id"])
        assert row is not None
        assert row["org"]["proposal"] == {"proposed_by": "emp_owner", "assignee_hint": "ai"}

    def test_acceptance_criteria_default_to_the_title(
        self, api: httpx.AsyncClient, employees: dict[str, str], automation_goals: dict[str, str]
    ) -> None:
        plain = _assign(api, "propose an automation to draft the weekly digest").json()
        assert plain["acceptance_criteria"] == "draft the weekly digest"
        explicit = _assign(
            api,
            "propose an automation to draft the weekly digest | ac: it posts every Monday",
        ).json()
        assert explicit["acceptance_criteria"] == "it posts every Monday"

    def test_the_two_grammars_do_not_overlap(
        self, api: httpx.AsyncClient, employees: dict[str, str], automation_goals: dict[str, str]
    ) -> None:
        """A propose sentence must never produce an assignment, and the fixture
        lists the dashboard mirrors must stay disjoint."""
        assert not set(NL_PROPOSE_ACCEPTED) & set(NL_ASSIGN_ACCEPTED)
        assert not set(NL_PROPOSE_ACCEPTED) & set(NL_ASSIGN_REJECTED)
        for text in NL_PROPOSE_ACCEPTED:
            body = _assign(api, text).json()
            assert "owner_employee_id" not in body, "a proposal assigns nobody"


class TestProposeCategoryTokens:
    """Review round 2, item 6: ONE token grammar across both surfaces.

    The dashboard route and the Slack verb must consume the same ``#token`` or a
    person types ``#dev_tooling`` in one place, sees it accepted, and finds the
    work filed under a different category than the one they named.
    """

    @pytest.fixture
    def sibling_categories(
        self, store: SqliteStore, goals_store: CompanyGoalsStore, automation_goals: dict[str, str]
    ) -> dict[str, str]:
        from omniagentos.team.contracts import AUTOMATION_PARENT_GOAL_ID

        created = dict(automation_goals)
        for name in ("customer service", "customer success"):
            goal = goals_store.create_goal(
                org_company_id="co_omniagentos",
                title=f"Automations — {name}",
                horizon="short_term",
                parent_goal_id=AUTOMATION_PARENT_GOAL_ID,
            )
            created[name] = str(goal["id"])
        return created

    @pytest.mark.parametrize("token", ["dev-tooling", "dev_tooling", "DEV_TOOLING"])
    def test_underscored_and_hyphenated_tokens_are_one_category(
        self,
        api: httpx.AsyncClient,
        employees: dict[str, str],
        automation_goals: dict[str, str],
        token: str,
    ) -> None:
        """The regex used to stop at the underscore and consume ``#dev`` — a
        partial token that resolves somewhere the writer never named."""
        body = _run(
            api.post(
                "/api/team/nl-assign",
                json={"text": f"propose an automation to ship the linter #{token}"},
            )
        ).json()
        assert body["title"] == "ship the linter", "the whole token leaves the title"
        assert body["goal_id"] == automation_goals["dev tooling"]

    def test_an_ambiguous_prefix_is_a_400_naming_the_candidates(
        self, api: httpx.AsyncClient, employees: dict[str, str], sibling_categories: dict[str, str]
    ) -> None:
        """Silently taking the older of two ``customer …`` goals files the work
        where the person who typed it will not look for it."""
        response = _run(
            api.post(
                "/api/team/nl-assign",
                json={"text": "propose an automation to triage the inbox #customer"},
            )
        )
        assert response.status_code == 400, response.text
        assert "matches 2 categories" in response.text
        assert "customer service" in response.text and "customer success" in response.text

    def test_an_exact_name_beats_its_longer_siblings(
        self, api: httpx.AsyncClient, employees: dict[str, str], sibling_categories: dict[str, str]
    ) -> None:
        body = _run(
            api.post(
                "/api/team/nl-assign",
                json={"text": "propose an automation to triage the inbox #customer-service"},
            )
        ).json()
        assert body["goal_id"] == sibling_categories["customer service"]

    def test_an_unconfigured_backlog_refuses_rather_than_filing_a_goal_less_card(
        self, api: httpx.AsyncClient, employees: dict[str, str]
    ) -> None:
        """No ``automation_goals`` fixture: a goal-less proposal would approve
        into a card that is not pool-eligible and can never be claimed."""
        response = _run(
            api.post(
                "/api/team/nl-assign",
                json={"text": "propose an automation to draft the weekly digest"},
            )
        )
        assert response.status_code == 400, response.text
        assert "automation_backlog_unconfigured" in response.text
