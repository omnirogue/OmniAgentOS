"""The v4 Work-vs-Tasks split (the operator's ruling 2026-08-13).

Four surfaces, one discriminator: ``board_tasks.source = 'task-adhoc'`` is
stamped at creation by BOTH ad-hoc paths (bare ``task`` verb, ``/task assign
@name <free title>``) and by nothing else; wherever a person's load renders,
Tasks sit ABOVE Work with their deadlines front-and-center, and the ``🔧 Work
x/5`` line makes the five-ongoing expectation visible (⚠ below floor — supply
visibility, never a block).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

import omniagentos.team.notify as notify
from omniagentos.collab.contracts import BoardTask
from omniagentos.collab.store import CollabStore
from omniagentos.team import tasks as team_tasks
from omniagentos.team.contracts import TASK_ADHOC_SOURCE, QueueCard, TeamQueueBuckets
from omniagentos.team.slack_updates import apply, parse_command
from omniagentos.team.store import TeamStore

PERMALINK = "https://slack.com/archives/C0000EXAMPLE/p1700000000000100"
TODAY = "2026-08-13"


class FakeNotifier:
    def __init__(self) -> None:
        self.dms: list[tuple[str, str]] = []

    def post_dm(self, slack_user_id: str, text: str, **_: Any) -> bool:
        self.dms.append((slack_user_id, text))
        return True


def _apply(
    collab: CollabStore,
    team: TeamStore,
    slack_map: dict[str, str],
    text: str,
    employee_id: str,
) -> str:
    command = parse_command(text)
    assert command is not None, f"expected a command from {text!r}"
    return apply(
        command,
        employee_id,
        PERMALINK,
        collab=collab,
        team=team,
        slack_map=slack_map,
        notifier=FakeNotifier(),
    )


# ==========================================================================
# the discriminator — stamped on exactly the two ad-hoc paths
# ==========================================================================


class TestStamping:
    def test_the_bare_task_verb_stamps_task_adhoc(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
    ) -> None:
        reply = _apply(
            collab_store,
            team_store,
            slack_map,
            "<@U0BOB> task fix the login bug",
            employees["owner"],
        )
        card = collab_store.list_board_tasks()[0]
        assert card["source"] == TASK_ADHOC_SOURCE
        assert team_tasks.is_adhoc_task(card)
        # The reply grammar is byte-identical to the pre-v4 shape.
        assert reply.startswith(f"Created {card['id']}: fix the login bug")
        assert f"Track with: done {card['id']}" in reply

    def test_slash_assign_with_a_free_title_stamps_task_adhoc(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
    ) -> None:
        _apply(
            collab_store,
            team_store,
            slack_map,
            "/task assign <@U0BOB> review the pricing page tomorrow",
            employees["owner"],
        )
        card = collab_store.list_board_tasks()[0]
        assert card["source"] == TASK_ADHOC_SOURCE
        assert card["owner_employee_id"] == employees["bob"]
        assert card["due_date"] is not None

    def test_queue_delegation_does_not_stamp(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
        initech_goal: str,
    ) -> None:
        make_card(
            title="Queue work", ref="Q1", goal_id=initech_goal, acceptance_criteria="works"
        )
        reply = _apply(
            collab_store, team_store, slack_map, "/task assign <@U0BOB> Q1", employees["owner"]
        )
        assert reply.startswith("✓ Q1 → emp_bob")
        card = collab_store.list_board_tasks()[0]
        assert card["source"] == ""  # a delegated queue card stays Work
        assert not team_tasks.is_adhoc_task(card)

    def test_task_add_does_not_stamp(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
        initech_goal: str,
    ) -> None:
        _apply(
            collab_store,
            team_store,
            slack_map,
            "/task add Fix checkout #initech",
            employees["owner"],
        )
        assert collab_store.list_board_tasks()[0]["source"] == ""


# ==========================================================================
# morning brief — Tasks above Work, floor warning, deadline glyphs
# ==========================================================================


@pytest.fixture
def bob_mapped(
    employees: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    mapping = {"U0BOB": employees["bob"]}
    monkeypatch.setattr(notify, "load_slack_map", lambda: mapping)
    return mapping


class TestBriefTasksOnTop:
    def test_tasks_render_above_work_with_deadline_glyphs(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        bob_mapped: dict[str, str],
    ) -> None:
        make_card(
            title="Buy the domain",
            ref="T1",
            owner_employee_id=employees["bob"],
            source=TASK_ADHOC_SOURCE,
            due_date="2026-08-10",  # overdue on TODAY
        )
        make_card(title="Ship the fix", ref="W1", owner_employee_id=employees["bob"])
        make_card(title="Write the doc", ref="W2", owner_employee_id=employees["bob"])

        text, _blocks, color = notify.daybrief_payload(team_store, today=TODAY)

        assert "📌 Tasks (1)" in text
        assert "▫️ T1 Buy the domain 🔴⏰2026-08-10" in text
        assert "🔧 Work 2/5 ⚠ below floor" in text
        # Exact order: Tasks subsection above the Work line, Work cards below.
        assert (
            text.index("📌 Tasks (1)")
            < text.index("🔧 Work 2/5")
            < text.index("▫️ W1 Ship the fix")
        )
        # The Task is NOT double-listed among the Work cards.
        assert text.count("T1 Buy the domain") == 1
        # The overdue Task turns the side-bar amber.
        from omniagentos.team import slack_blocks

        assert color == slack_blocks.AMBER

    def test_zero_tasks_omits_the_subsection(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        bob_mapped: dict[str, str],
    ) -> None:
        make_card(title="Only work", ref="W1", owner_employee_id=employees["bob"])
        text, _blocks, _color = notify.daybrief_payload(team_store, today=TODAY)
        assert "📌 Tasks" not in text
        assert "🔧 Work 1/5 ⚠ below floor" in text

    def test_at_or_above_the_floor_the_warning_disappears(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        bob_mapped: dict[str, str],
    ) -> None:
        for index in range(5):
            make_card(
                title=f"Work {index}", ref=f"W{index}", owner_employee_id=employees["bob"]
            )
        text, _blocks, _color = notify.daybrief_payload(team_store, today=TODAY)
        assert "🔧 Work 5/5" in text
        assert "below floor" not in text

    def test_tasks_do_not_move_the_work_counts(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
        bob_mapped: dict[str, str],
    ) -> None:
        make_card(
            title="An errand",
            ref="T1",
            owner_employee_id=employees["bob"],
            source=TASK_ADHOC_SOURCE,
        )
        make_card(title="Real work", ref="W1", owner_employee_id=employees["bob"])
        text, _blocks, _color = notify.daybrief_payload(team_store, today=TODAY)
        # Header counts are Work-only: 0 in progress, 1 queued — not 2.
        assert "👤 Bob — in progress 0 · queued 1" in text
        assert "🔧 Work 1/5 ⚠ below floor" in text
        # A deadline-less Task renders without ⏰ (and without 🔴).
        assert "▫️ T1 An errand\n" in text + "\n"
        assert "T1 An errand ⏰" not in text


# ==========================================================================
# hourly pulse — compressed person line, task deadline second line
# ==========================================================================


def _bucket(cards: list[QueueCard]) -> dict[str, TeamQueueBuckets]:
    bucket = TeamQueueBuckets(employee_id="emp_bob")
    bucket.ready.extend(cards)
    return {"emp_bob": bucket}


def _task_card(ref: str, due: str | None) -> QueueCard:
    return QueueCard(
        id=f"btk_{ref.lower()}",
        ref=ref,
        title=f"Task {ref}",
        status="open",
        source=TASK_ADHOC_SOURCE,
        due_date=due,
    )


def _work_card(ref: str) -> QueueCard:
    return QueueCard(id=f"btk_{ref.lower()}", ref=ref, title=f"Work {ref}", status="open")


class TestPulseTasksSegment:
    _MAP = {"U0BOB": "emp_bob"}

    def test_the_compressed_line_and_the_due_second_line(self) -> None:
        queues = _bucket(
            [
                _task_card("T1", "2026-08-13"),  # due today
                _task_card("T2", "2026-08-10"),  # overdue
                _work_card("W1"),
                _work_card("W2"),
                _work_card("W3"),
            ]
        )
        text = notify.render_pulse_message(queues, self._MAP, [], 10, today=TODAY)
        assert "👤 Bob — 📌 2 tasks · 🔧 Work 3/5 ⚠" in text
        # Task refs + deadlines ride a second line when one is due/overdue.
        assert "📌 T1 ⏰2026-08-13 · T2 🔴⏰2026-08-10" in text

    def test_zero_tasks_renders_no_task_segment(self) -> None:
        text = notify.render_pulse_message(
            _bucket([_work_card("W1")]), self._MAP, [], 10, today=TODAY
        )
        assert "📌" not in text.splitlines()[1]
        assert "🔧 Work 1/5 ⚠" in text

    def test_a_far_future_task_deadline_adds_no_second_line(self) -> None:
        text = notify.render_pulse_message(
            _bucket([_task_card("T9", "2026-09-01")]), self._MAP, [], 10, today=TODAY
        )
        assert "👤 Bob — 📌 1 task · 🔧 Work 0/5 ⚠" in text
        assert "⏰" not in text  # nothing due today, no deadline line

    def test_tasks_never_earn_urgent_markers_or_move_the_work_floor(self) -> None:
        urgent_task = QueueCard(
            id="btk_t",
            ref="T1",
            title="Urgent errand",
            status="open",
            priority="urgent",
            source=TASK_ADHOC_SOURCE,
        )
        text = notify.render_pulse_message(
            _bucket([urgent_task]), self._MAP, [], 10, today=TODAY
        )
        assert "🔥" not in text  # urgent markers are a Work signal
        assert "🔧 Work 0/5 ⚠" in text

    def test_the_pulse_reads_the_split_through_the_real_store(
        self,
        team_store: TeamStore,
        make_card: Callable[..., BoardTask],
        employees: dict[str, str],
    ) -> None:
        make_card(
            title="An errand",
            ref="T1",
            owner_employee_id=employees["bob"],
            source=TASK_ADHOC_SOURCE,
            due_date="2026-08-01",
        )
        make_card(title="Real work", ref="W1", owner_employee_id=employees["bob"])
        text = notify.render_pulse_message(
            team_store.team_queues(),
            {"US": employees["bob"]},
            [],
            10,
            today=TODAY,
        )
        assert "👤 Bob — 📌 1 task · 🔧 Work 1/5 ⚠" in text
        assert "📌 T1 🔴⏰2026-08-01" in text


# ==========================================================================
# /task mine — split sections
# ==========================================================================


class TestMineSplit:
    def test_tasks_section_sits_above_the_work_buckets(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
    ) -> None:
        make_card(
            title="Buy the domain",
            ref="T1",
            owner_employee_id=employees["bob"],
            source=TASK_ADHOC_SOURCE,
            due_date="2020-01-01",  # always overdue, wall-clock-proof
        )
        make_card(title="Real work", ref="W1", owner_employee_id=employees["bob"])

        reply = _apply(collab_store, team_store, slack_map, "/task mine", employees["bob"])

        assert "📌 Tasks (1):" in reply
        assert "T1 Buy the domain 🔴⏰2020-01-01" in reply
        assert "🔧 Work 1/5 ⚠ below floor" in reply
        assert (
            reply.index("📌 Tasks (1):")
            < reply.index("🔧 Work 1/5")
            < reply.index("Ready:")
        )
        # The Task never doubles as a Ready Work card.
        ready_section = reply.split("Ready:")[1]
        assert "T1" not in ready_section.split("Active:")[0]
        assert "W1 Real work" in ready_section

    def test_no_tasks_renders_the_plain_buckets(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
    ) -> None:
        make_card(title="Only work", ref="W1", owner_employee_id=employees["bob"])
        reply = _apply(collab_store, team_store, slack_map, "/task mine", employees["bob"])
        assert "📌 Tasks" not in reply
        assert "🔧 Work 1/5 ⚠ below floor" in reply
        assert "W1 Only work" in reply


# ==========================================================================
# /task queue — deadline glyphs on queue cards
# ==========================================================================


class TestQueueDeadlineGlyphs:
    def test_queue_lines_carry_the_deadline_glyphs(
        self,
        collab_store: CollabStore,
        team_store: TeamStore,
        slack_map: dict[str, str],
        employees: dict[str, str],
        make_card: Callable[..., BoardTask],
        initech_goal: str,
    ) -> None:
        make_card(
            title="Overdue queue card",
            ref="Q1",
            goal_id=initech_goal,
            acceptance_criteria="works",
            due_date="2020-01-01",  # always overdue, wall-clock-proof
        )
        make_card(
            title="No deadline",
            ref="Q2",
            goal_id=initech_goal,
            acceptance_criteria="works",
        )
        reply = _apply(collab_store, team_store, slack_map, "/task queue", employees["bob"])
        assert "• Q1 Overdue queue card 🔴⏰2020-01-01" in reply
        lines = [line for line in reply.splitlines() if "Q2" in line]
        assert lines == ["• Q2 No deadline"]
