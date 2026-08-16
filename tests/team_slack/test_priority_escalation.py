"""``!top <REF>``: escalate an EXISTING card to urgent, in the fixed grammar.

Mirrors ``test_parse_command.py`` (grammar) and ``test_apply.py`` (board
mutation + authorization) for the one new verb: same deterministic parsing,
same resolve/authorize path as ``done``/``progress``, one idempotent PATCH.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.team.slack_updates import Command, apply, parse_command
from omniagentos.team.store import TeamStore

PERMALINK = "https://slack.com/archives/C0000EXAMPLE/p1700000000000100"


class TestParse:
    def test_top_with_a_ref(self) -> None:
        assert parse_command("!top U3") == Command(verb="top", ref="U3", note="")

    def test_top_is_case_insensitive(self) -> None:
        command = parse_command("!TOP OPS-2")
        assert command is not None
        assert (command.verb, command.ref) == ("top", "OPS-2")

    def test_top_accepts_the_pool_id_printed_in_dms(self) -> None:
        assert parse_command("!top btk_ab12cd") == Command(verb="top", ref="btk_ab12cd", note="")

    def test_top_accepts_a_quoted_title_prefix(self) -> None:
        assert parse_command('!top "Fix login bug"') == Command(
            verb="top", title_prefix="Fix login bug", note=""
        )

    @pytest.mark.parametrize(
        "text",
        [
            "!top",  # no ref
            "top U3",  # bare 'top' is not on the grammar — chatter
            "!topmost U3",
            "!top widget",  # not ref-shaped, not quoted
        ],
    )
    def test_near_misses_are_chatter_not_errors(self, text: str) -> None:
        assert parse_command(text) is None

    def test_the_create_time_flag_still_belongs_to_the_task_verb(self) -> None:
        command = parse_command("<@U0BOB> task ship it !top")
        assert command is not None
        assert (command.verb, command.priority) == ("task", "urgent")


class TestApply:
    def _apply(
        self,
        collab: CollabStore,
        team: TeamStore,
        slack_map: dict[str, str],
        text: str,
        employee_id: str,
    ) -> str:
        command = parse_command(text)
        assert command is not None
        return apply(
            command, employee_id, PERMALINK, collab=collab, team=team, slack_map=slack_map
        )

    def test_escalates_the_senders_own_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Login bug", ref="S5", owner_employee_id=employees["bob"])
        reply = self._apply(collab_store, team_store, slack_map, "!top S5", employees["bob"])

        assert reply == "✓ S5 → priority urgent (recorded)"
        row = collab_store.get_board_task(card.id)
        assert row is not None and row["priority"] == "urgent"
        # The escalated card now leads the sender's ready bucket.
        ready = team_store.team_queues(employee_ids=[employees["bob"]])[
            employees["bob"]
        ].ready
        assert ready[0].id == card.id and ready[0].priority == "urgent"

    def test_someone_elses_card_is_refused(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Alice's card", ref="U3", owner_employee_id=employees["alice"])
        reply = self._apply(collab_store, team_store, slack_map, "!top U3", employees["bob"])

        assert reply == "not your task"
        row = collab_store.get_board_task(card.id)
        assert row is not None and row["priority"] == "normal"

    def test_the_operator_may_escalate_anyones_card(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        card = make_card(title="Alice's card", ref="U4", owner_employee_id=employees["alice"])
        reply = self._apply(collab_store, team_store, slack_map, "!top U4", employees["owner"])

        assert reply == "✓ U4 → priority urgent (recorded)"
        row = collab_store.get_board_task(card.id)
        assert row is not None and row["priority"] == "urgent"

    def test_an_ownerless_pool_card_is_escalatable(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        initech_goal: str,
    ) -> None:
        card = make_card(
            title="Pool card",
            ref="P1",
            goal_id=initech_goal,
            acceptance_criteria="the pool contract",
        )
        reply = self._apply(collab_store, team_store, slack_map, "!top P1", employees["bob"])

        assert reply == "✓ P1 → priority urgent (recorded)"
        pool = team_store.pool_cards(limit=1)
        assert pool and pool[0].id == card.id  # urgent now drains first

    def test_already_urgent_is_an_idempotent_reply(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        make_card(
            title="Fire",
            ref="F1",
            owner_employee_id=employees["bob"],
            priority="urgent",
        )
        reply = self._apply(collab_store, team_store, slack_map, "!top F1", employees["bob"])
        assert reply == "F1 is already urgent"

    def test_no_matching_task(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
    ) -> None:
        reply = self._apply(collab_store, team_store, slack_map, "!top ZZ9", employees["bob"])
        assert reply == "no matching task"


def test_the_event_trail_names_the_escalation(
    collab_store: CollabStore,
    team_store: TeamStore,
    slack_map: dict[str, str],
    make_card: Callable[..., BoardTask],
    employees: dict[str, str],
) -> None:
    card = make_card(title="Trail", ref="T1", owner_employee_id=employees["bob"])
    command = parse_command("!top T1")
    assert command is not None
    apply(
        command,
        employees["bob"],
        PERMALINK,
        collab=collab_store,
        team=team_store,
        slack_map=slack_map,
    )
    events: list[dict[str, Any]] = team_store.list_events(card.id)
    assert events[-1]["actor"] == employees["bob"]
    assert events[-1]["event"] == "comment"
    assert events[-1]["note"] == "priority"
