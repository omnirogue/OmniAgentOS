"""Traceability: nothing in the report text is allowed to come from nowhere.

Two properties, both mechanical:

1. **Every counted point is backed.** A counted card carries either evidence
   refs or a named verifier — so "where did this point come from" always has an
   answer that is not "the scorer said so".
2. **Every number in the TEXT exists in the gathered data.** ``render`` is a
   template fill, so a numeric token appearing in the message that is absent
   from the dict would mean the renderer computed (or invented) it. This is the
   same defense the briefing composer runs against an LLM narrative, applied to
   our own formatting code — because "no LLM is involved" is not by itself a
   guarantee that a number is real.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

from omniagentos.team.report import _targets_line, gather, render
from omniagentos.team.store import TeamStore

from .conftest import DAY, IN_WINDOW, YESTERDAY

_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _numbers(text: str) -> set[float]:
    return {round(float(token), 6) for token in _NUMBER.findall(text)}


def _populate(
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    """A board with every shape the renderer has to handle at once."""
    from omniagentos.collab.contracts import BoardTaskStatus
    from omniagentos.team.scoring import BASELINE_SOURCE

    alice, bob, owner = employees["alice"], employees["bob"], employees["owner"]

    for card_owner, size in ((alice, "L"), (bob, "S")):
        make_task(
            owner=card_owner,
            size=size,
            ref=f"BASE-{card_owner}",
            title="Baseline week",
            source=BASELINE_SOURCE,
            acceptance="",
            evidence=[("doc", f"baseline-{owner}", "pass")],
            verified_at=IN_WINDOW,
        )

    task = make_task(
        owner=alice,
        size="M",
        ref="UP-11",
        title="Shared queue spec",
        evidence=[("test_run", "tr-queue", "pass")],
        verified_at=YESTERDAY,
    )
    add_session(task_id=task, started_at="2026-08-13T09:00:00Z", ended_at="2026-08-13T12:00:00Z")
    bulk_evidence(
        task_id=task,
        kind="pr",
        count=1,
        prefix="merged-pr",
        meta_json=json.dumps({"state": "MERGED", "gate_attempts": 1}),
        created_at=YESTERDAY,
    )
    for index in range(6):
        make_task(owner=alice, size="S", ref=f"UR-{index}", title=f"ready {index}")

    # Bob: a verified card, a blocked pair, and a thin queue.
    make_task(
        owner=bob,
        size="S",
        ref="SP-11",
        title="Telemetry spike",
        evidence=[("test_run", "tr-spike", "pass")],
        verified_at=IN_WINDOW,
    )
    for index in range(2):
        make_task(
            owner=bob,
            size="M",
            ref=f"SB-{index}",
            title=f"blocked {index}",
            blocked_reason="waiting on a decision",
            status=BoardTaskStatus.BLOCKED.value,
        )

    # the operator: an item that has been sitting in review, and nothing verified.
    make_task(
        owner=owner,
        size="M",
        ref="OPSP-1",
        title="Awaiting approval",
        status=BoardTaskStatus.AWAITING_APPROVAL.value,
        updated_at="2026-08-10T00:00:00Z",
    )


def test_every_counted_point_is_backed_by_evidence_or_a_verifier(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    _populate(employees, make_task, add_session, bulk_evidence)
    gathered = gather(team_store, DAY)

    counted = [entry for person in gathered["people"] for entry in person["counted"]]
    assert counted, "the fixture must actually score something"
    for entry in counted:
        assert entry["evidence_refs"] or entry["verified_by"], entry
        assert entry["task_id"]
        assert entry["points"] > 0


def test_every_number_in_the_text_exists_in_the_gathered_data(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    _populate(employees, make_task, add_session, bulk_evidence)
    gathered = gather(team_store, DAY)
    text = render(gathered)

    allowed = _numbers(json.dumps(gathered, sort_keys=True, default=str))
    invented = _numbers(text) - allowed
    assert invented == set(), f"render introduced numbers absent from gather: {sorted(invented)}"


def test_the_rendered_report_reads_the_way_the_spec_says(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    """The golden shape. Pinned line by line, because this text IS the product."""
    _populate(employees, make_task, add_session, bulk_evidence)
    text = render(gather(team_store, DAY))
    lines = text.split("\n")

    assert lines[0] == f"DAILY PRODUCTION — {DAY}"
    # The standing targets line (the operator's 2026-08-14 ruling): once, at the top,
    # byte-identical to the morning DM's and the daybrief's.
    assert lines[1] == _targets_line()
    assert lines[2] == "1. ALICE — 0.4x (4% to 10x)"
    assert lines[3] == "   major contribution: UP-11 — Shared queue spec"
    assert lines[4] == "   verified outcomes: 1 · avg sessions: <0.1 · first-pass: 100%"
    assert lines[5] == "   queue: 0 active / 6 ready / 0 blocked / 0 in review"
    # No commitments were ever generated in this fixture (WP-B): the explicit
    # zero state, never a bare '0/0' that would misread as two misses.
    assert lines[6] == "   Yesterday: no commitments recorded"
    assert lines[7] == "   #1 bottleneck: none / recommended: keep going"
    assert lines[8] == "2. BOB — 1.0x (10% to 10x)"
    assert lines[9] == "   major contribution: SP-11 — Telemetry spike"
    assert lines[10] == "   verified outcomes: 1 · avg sessions: n/a · first-pass: n/a"
    assert lines[11] == "   queue: 0 active / 0 ready / 2 blocked / 0 in review"
    assert lines[12] == "   Yesterday: no commitments recorded"
    assert lines[13] == (
        "   #1 bottleneck: blocked: SB-0, SB-1 / recommended: unblock first — "
        "name the owner of each blocker and get a decision today"
    )
    assert lines[14] == "3. THE OPERATOR — no baseline (0 pts)"
    assert lines[15] == "   major contribution: none"
    assert lines[16] == "   verified outcomes: 0 · avg sessions: n/a · first-pass: n/a"
    assert lines[17] == "   queue: 0 active / 0 ready / 0 blocked / 1 in review"
    assert lines[18] == "   Yesterday: no commitments recorded"
    assert lines[19] == (
        "   #1 bottleneck: review latency: OPSP-1 / recommended: "
        "review the oldest card before starting anything new"
    )
    assert lines[20] == ""
    assert lines[21].startswith("TEAM — 0.4x (4% to 10x) · #1 bottleneck: ")


def test_an_empty_board_still_renders_a_report(
    team_store: TeamStore, employees: dict[str, str]
) -> None:
    """The 07:00 message on a dead-quiet day is still a message, not a crash."""
    text = render(gather(team_store, DAY))
    assert text.startswith(f"DAILY PRODUCTION — {DAY}")
    assert "no baseline (0 pts)" in text
    assert "recommended action: groom this queue back above 5 ready cards before standup" in text
    # The OVERALL line closes the report; with no baselines and no readable
    # fleet ledger it says so instead of inventing a ratio.
    assert text.rstrip().endswith("OVERALL — no baseline (0 pts) · humans no baseline · fleet no baseline (unreadable landed)")


# ---------------------------------------------------------------------------
# Slack rendering (render_slack) — the delivery skin obeys the same contract
# ---------------------------------------------------------------------------


def test_slack_render_numbers_trace_to_gathered(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    from omniagentos.team.report import render_slack

    _populate(employees, make_task, add_session, bulk_evidence)
    gathered = gather(team_store, DAY)
    fallback, blocks = render_slack(gathered)

    allowed = _numbers(json.dumps(gathered, sort_keys=True, default=str))
    # ensure_ascii=False keeps emoji as literal characters — the escaped form
    # (📊) would leak its OWN hex digits into the number scan.
    for label, text in (
        ("fallback", fallback),
        ("blocks", json.dumps(blocks, ensure_ascii=False)),
    ):
        invented = _numbers(text) - allowed
        assert invented == set(), f"{label} introduced numbers absent from gather: {sorted(invented)}"
    # The fallback is exactly the plain render — one durable text, two skins.
    assert fallback == render(gathered)
    # Block Kit payload must survive the wire.
    json.dumps(blocks)


def test_slack_medals_follow_rank_order(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    from omniagentos.team.report import render_slack

    _populate(employees, make_task, add_session, bulk_evidence)
    gathered = gather(team_store, DAY)
    _fallback, blocks = render_slack(gathered)

    section_texts = [
        block["text"]["text"] for block in blocks if block.get("type") == "section"
    ]
    person_sections = [text for text in section_texts if "bottleneck" in text and "TEAM" not in text]
    visible_names = [
        str(person["display"]["name"])
        for person in gathered["people"]
        if any(int(person["queue"][key]) for key in ("active", "ready", "blocked", "review", "done_today"))
        or int(person["points"]) > 0
        or person["baseline_points"] not in (None, 0)
    ]
    assert len(person_sections) == len(visible_names)
    medals = ["\U0001f947", "\U0001f948", "\U0001f949"]
    for index, (section, name) in enumerate(zip(person_sections, visible_names, strict=True)):
        assert name in section
        if index < len(medals):
            assert medals[index] in section


def test_slack_hides_all_zero_roster_members(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
    bulk_evidence: Callable[..., int],
) -> None:
    """A roster member with no cards, no points, and no baseline is omitted from
    the Slack skin (noise), while remaining in the gathered data and the plain
    file (the record hides nothing)."""
    from omniagentos.company_goals.store import CompanyGoalsStore
    from omniagentos.team.report import render_slack

    _populate(employees, make_task, add_session, bulk_evidence)
    CompanyGoalsStore(team_store._store).ensure_employee(
        employee_id="emp_frank", name="Frank", role=None
    )
    gathered = gather(team_store, DAY)

    assert any(person["employee_id"] == "emp_frank" for person in gathered["people"])
    assert "FRANK" in render(gathered)
    _fallback, blocks = render_slack(gathered)
    assert "ANDY" not in json.dumps(blocks)
