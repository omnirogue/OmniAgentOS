"""Pace lines ride the pulse (multi-company Work OS, 2026-08-13).

``render_pulse_message`` stays time-independent: pace lines are passed IN,
pre-rendered, and ``None`` reproduces the pre-pace pulse byte-for-byte.
"""

from __future__ import annotations

from omniagentos.team.contracts import QueueCard, TeamQueueBuckets
from omniagentos.team.notify import render_pulse_message

_SLACK_MAP = {"U0BOB": "emp_bob"}


def _queues() -> dict[str, TeamQueueBuckets]:
    bucket = TeamQueueBuckets(employee_id="emp_bob")
    bucket.ready.extend(
        QueueCard(id=f"btk_{index}", title=f"Card {index}", status="open") for index in range(5)
    )
    return {"emp_bob": bucket}


def test_pace_lines_render_between_people_and_pool() -> None:
    message = render_pulse_message(
        _queues(),
        _SLACK_MAP,
        pool=[],
        pool_depth=12,
        pace_lines=[
            "⚠ emp_bob 4/15 pts, Friday pace short",
            "✓ emp_alice 9/15 pts, on pace",
        ],
    )
    lines = message.split("\n")
    assert "• ⚠ emp_bob 4/15 pts, Friday pace short" in lines
    assert "• ✓ emp_alice 9/15 pts, on pace" in lines
    assert lines.index("• ⚠ emp_bob 4/15 pts, Friday pace short") < lines.index("Pool: 12")


def test_no_pace_lines_reproduces_the_pre_pace_pulse() -> None:
    without = render_pulse_message(_queues(), _SLACK_MAP, pool=[], pool_depth=12)
    explicit_none = render_pulse_message(
        _queues(), _SLACK_MAP, pool=[], pool_depth=12, pace_lines=None
    )
    empty = render_pulse_message(_queues(), _SLACK_MAP, pool=[], pool_depth=12, pace_lines=[])
    assert without == explicit_none == empty
    assert "pts" not in without


def test_friday_announcement_rides_as_a_pace_line() -> None:
    message = render_pulse_message(
        _queues(),
        _SLACK_MAP,
        pool=[],
        pool_depth=12,
        pace_lines=["📈 Point floor rises Monday: 15 → 18 verified pts/week (+20% ratchet)"],
    )
    assert "Point floor rises Monday" in message
