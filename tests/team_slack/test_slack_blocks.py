"""Block Kit rendering: structure, sanitization, limits, and color rules."""

from __future__ import annotations

import json
from typing import Any

from omniagentos.team import slack_blocks
from omniagentos.team.session_tracker import Overall


def _texts(blocks: list[dict[str, Any]]) -> str:
    return json.dumps(blocks)


def test_tracker_blocks_structure_and_color() -> None:
    overall = Overall(proposals=5, candidates=3, merge_queue=1, merged_last_hour=2,
                      failed_merges_last_hour=0, active_sessions=4, bottleneck="none")
    reports = {
        "emp_owner": {
            "host": "mac", "_age_seconds": 60.0, "active_count": 2, "recent_count": 3,
            "sessions": [
                {"active": True, "description": "build tracker", "project": "Repo", "account": "account-2"},
            ],
        }
    }
    color, blocks = slack_blocks.tracker_blocks(
        overall, [("emp_owner", "the operator"), ("emp_alice", "Alice")], reports,
        stamp="t", fresh_seconds=7200,
    )
    assert color == slack_blocks.GREEN
    assert blocks[0]["type"] == "header"
    fields = next(b for b in blocks if "fields" in b)
    assert len(fields["fields"]) == 6
    body = _texts(blocks)
    assert "*2 active*" in body and "`@account-2`" in body
    assert "no session report received" in body  # Alice has no report


def test_tracker_color_red_on_gate_failures() -> None:
    assert slack_blocks.tracker_color("merge gate red (x)", 1) == slack_blocks.RED
    assert slack_blocks.tracker_color("none", 1) == slack_blocks.RED
    assert slack_blocks.tracker_color("gate throughput (6 in merge queue)", 0) == slack_blocks.AMBER
    assert slack_blocks.tracker_color("none", 0) == slack_blocks.GREEN


def test_blocks_sanitize_urls_and_tokens() -> None:
    overall = Overall(bottleneck="none")
    reports = {"emp_owner": {"host": "mac", "_age_seconds": 1.0, "sessions": [
        {"active": True, "description": "leak https://hooks.slack.com/secret and xoxb-123", "project": "", "account": ""},
    ]}}
    _, blocks = slack_blocks.tracker_blocks(overall, [("emp_owner", "the operator")], reports,
                                            stamp="t", fresh_seconds=7200)
    body = _texts(blocks)
    assert "hooks.slack.com" not in body and "xoxb-123" not in body
    assert "[link omitted]" in body


def test_blocks_clip_under_slack_limit() -> None:
    overall = Overall(bottleneck="none")
    roster = [(f"emp_{i}", f"P{i}") for i in range(90)]
    _, blocks = slack_blocks.tracker_blocks(overall, roster, {}, stamp="t", fresh_seconds=1)
    assert len(blocks) <= 48
    assert "more blocks trimmed" in _texts(blocks[-1:])


def test_pulse_blocks_amber_on_low_pool_and_overnight_title() -> None:
    color, blocks = slack_blocks.pulse_blocks(
        [("• emp_owner: capacity 1/5", False)], 3, True, stamp="t",
        overnight=["🌙 start a loop on U-7"],
    )
    assert color == slack_blocks.AMBER
    assert "overnight edition" in blocks[0]["text"]["text"]
    assert "Overnight suggestions" in _texts(blocks)


def test_morning_dm_blocks_sections() -> None:
    color, blocks = slack_blocks.morning_dm_blocks(
        "the operator", ["• U-1 fix"], [], "capacity: 1 of 5 active — room for more",
        ["• U-9 pool card claim U-9"], stamp="t",
    )
    assert color == slack_blocks.GREEN
    body = _texts(blocks)
    assert "In flight" in body and "Grab from the pool" in body and "capacity: 1 of 5" in body


def test_mentions_neutralized_everywhere() -> None:
    from omniagentos.team.notify import _safe_title

    hostile = "urgent <!channel> ping <@U123ABC> and <#C042>"
    cleaned = _safe_title(hostile)
    assert "<!channel>" not in cleaned and "<@U123ABC>" not in cleaned
    assert cleaned.count("[mention omitted]") == 3
    overall = Overall(bottleneck="none")
    reports = {"emp_owner": {"host": "m", "_age_seconds": 1.0, "sessions": [
        {"active": True, "description": hostile, "project": "", "account": ""}]}}
    _, blocks = slack_blocks.tracker_blocks(overall, [("emp_owner", "S")], reports,
                                            stamp="t", fresh_seconds=7200)
    assert "<!channel>" not in _texts(blocks)


def test_pulse_trims_visibly_at_scale() -> None:
    lines = [(f"• emp_{i}: capacity 1/5 " + "x" * 80, False) for i in range(90)]
    _, blocks = slack_blocks.pulse_blocks(lines, 12, False, stamp="t")
    body = _texts(blocks)
    assert "more people trimmed" in body
    section = next(b for b in blocks if b.get("type") == "section")
    assert len(section["text"]["text"]) <= 2900
