"""The 07:00 report: ranking, bottleneck precedence, roll-up, and the write path.

The write path has three properties worth more than the numbers: the file lands
before the post is attempted, a re-run for the same day replaces rather than
appends, and a day already computed by another score version is REFUSED instead
of silently mixed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from omniagentos.collab.contracts import BoardTaskStatus
from omniagentos.team import report as team_report
from omniagentos.team.report import gather, main, render
from omniagentos.team.scoring import BASELINE_SOURCE
from omniagentos.team.store import TeamStore

from .conftest import DAY, IN_WINDOW, YESTERDAY


def _person(gathered: dict[str, object], employee_id: str) -> dict[str, object]:
    people = gathered["people"]
    assert isinstance(people, list)
    return next(person for person in people if person["employee_id"] == employee_id)


def _ready(make_task: Callable[..., str], owner: str, count: int, prefix: str) -> None:
    """Enough Ready cards to clear the grooming floor, so other rules can fire."""
    for index in range(count):
        make_task(owner=owner, size="S", ref=f"{prefix}-{index}", title=f"ready {index}")


# ---------------------------------------------------------------------------
# ranking and roll-up
# ---------------------------------------------------------------------------


def test_people_are_ranked_by_verified_points(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    make_task(
        owner=employees["bob"],
        size="M",
        ref="R-1",
        title="Three points",
        evidence=[("test_run", "tr-r1", "pass")],
        verified_at=IN_WINDOW,
    )
    make_task(
        owner=employees["alice"],
        size="L",
        ref="R-2",
        title="Eight points",
        evidence=[("test_run", "tr-r2", "pass")],
        verified_at=IN_WINDOW,
    )
    gathered = gather(team_store, DAY)

    assert [person["employee_id"] for person in gathered["people"]] == [
        employees["alice"],
        employees["bob"],
        employees["owner"],
    ]
    assert [person["rank"] for person in gathered["people"]] == [1, 2, 3]
    # A person with nothing still appears — absent and zero are different answers.
    assert _person(gathered, employees["owner"])["points"] == 0


def test_the_team_number_is_total_points_over_total_baselines(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    for employee_id, size in ((employees["alice"], "L"), (employees["bob"], "S")):
        make_task(
            owner=employee_id,
            size=size,
            ref=f"BASE-{employee_id}",
            title="Baseline week",
            source=BASELINE_SOURCE,
            acceptance="",
            evidence=[("doc", f"baseline-{employee_id}", "pass")],
            verified_at=IN_WINDOW,
        )
    make_task(
        owner=employees["alice"],
        size="M",
        ref="T-1",
        title="Alice's week",
        evidence=[("test_run", "tr-t1", "pass")],
        verified_at=IN_WINDOW,
    )
    make_task(
        owner=employees["bob"],
        size="M",
        ref="T-2",
        title="Bob's week",
        evidence=[("test_run", "tr-t2", "pass")],
        verified_at=IN_WINDOW,
    )
    gathered = gather(team_store, DAY)

    assert gathered["team"]["points"] == 6
    assert gathered["team"]["baseline_points"] == 9  # L + S
    assert gathered["team"]["production_x"] == 6 / 9


def test_the_major_contribution_is_the_biggest_counted_card(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    alice = employees["alice"]
    make_task(
        owner=alice,
        size="S",
        ref="MC-1",
        title="A small thing",
        evidence=[("test_run", "tr-mc1", "pass")],
        verified_at=IN_WINDOW,
    )
    make_task(
        owner=alice,
        size="L",
        ref="MC-2",
        title="The big thing",
        evidence=[("test_run", "tr-mc2", "pass")],
        verified_at=IN_WINDOW,
    )
    person = _person(gather(team_store, DAY), alice)
    major = person["major_contribution"]
    assert isinstance(major, dict)
    assert major["ref"] == "MC-2"
    assert person["display"]["major_contribution"] == "MC-2 — The big thing"


def test_a_person_with_no_counted_card_reports_no_major_contribution(
    team_store: TeamStore, employees: dict[str, str]
) -> None:
    person = _person(gather(team_store, DAY), employees["owner"])
    assert person["major_contribution"] is None
    assert person["display"]["major_contribution"] == "none"


# ---------------------------------------------------------------------------
# bottleneck precedence — one fixture per rule, in priority order
# ---------------------------------------------------------------------------


def test_rule_1_two_blocked_cards_win_over_everything_else(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    """Blocked beats a stale review AND a starved queue, both of which are also true."""
    bob = employees["bob"]
    for index in range(2):
        make_task(
            owner=bob,
            size="M",
            ref=f"B-{index}",
            title=f"blocked {index}",
            blocked_reason="waiting on Alice",
            status=BoardTaskStatus.BLOCKED.value,
        )
    make_task(
        owner=bob,
        size="M",
        ref="B-REVIEW",
        title="waiting on review",
        status=BoardTaskStatus.AWAITING_APPROVAL.value,
        updated_at="2026-08-10T00:00:00Z",
    )
    person = _person(gather(team_store, DAY), bob)

    assert person["bottleneck"]["class"] == "blocked"
    assert person["bottleneck"]["text"] == "blocked: B-0, B-1"
    assert person["recommendation"] == team_report.RECOMMENDATIONS["blocked"]


def test_rule_1_needs_two_a_single_blocked_card_is_not_the_bottleneck(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    bob = employees["bob"]
    make_task(
        owner=bob,
        size="M",
        ref="B-ONLY",
        title="one blocked card",
        blocked_reason="waiting on Alice",
        status=BoardTaskStatus.BLOCKED.value,
    )
    _ready(make_task, bob, 5, "RDY")
    make_task(
        owner=bob,
        size="M",
        ref="RECENT",
        title="finished yesterday",
        evidence=[("test_run", "tr-recent", "pass")],
        verified_at=YESTERDAY,
    )
    person = _person(gather(team_store, DAY), bob)
    assert person["bottleneck"]["class"] == "none"


def test_rule_2_a_review_older_than_24h_beats_a_starved_queue(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    alice = employees["alice"]
    make_task(
        owner=alice,
        size="M",
        ref="RV-1",
        title="waiting since Monday",
        status=BoardTaskStatus.AWAITING_APPROVAL.value,
        updated_at="2026-08-10T00:00:00Z",
    )
    person = _person(gather(team_store, DAY), alice)

    assert person["queue"]["ready"] == 0  # rule 3 is ALSO true, and loses
    assert person["bottleneck"]["class"] == "review_latency"
    assert person["bottleneck"]["text"] == "review latency: RV-1"


def test_rule_2_a_fresh_review_item_is_not_latency(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    alice = employees["alice"]
    make_task(
        owner=alice,
        size="M",
        ref="RV-2",
        title="waiting since this morning",
        status=BoardTaskStatus.AWAITING_APPROVAL.value,
        updated_at="2026-08-14T20:00:00Z",
    )
    _ready(make_task, alice, 5, "RDY")
    make_task(
        owner=alice,
        size="M",
        ref="RECENT-2",
        title="finished yesterday",
        evidence=[("test_run", "tr-recent2", "pass")],
        verified_at=YESTERDAY,
    )
    person = _person(gather(team_store, DAY), alice)
    assert person["bottleneck"]["class"] == "none"


def test_rule_3_a_thin_ready_queue_beats_having_produced_nothing(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    bob = employees["bob"]
    _ready(make_task, bob, 4, "RDY")
    person = _person(gather(team_store, DAY), bob)

    assert person["recent_verified_outcomes"] == 0  # rule 4 is ALSO true, and loses
    assert person["bottleneck"]["class"] == "queue_starvation"
    assert person["bottleneck"]["text"] == "queue starvation (4 ready)"


def test_rule_4_a_groomed_queue_with_nothing_verified_in_48h(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    bob = employees["bob"]
    _ready(make_task, bob, 5, "RDY")
    make_task(
        owner=bob,
        size="M",
        ref="OLD-48",
        title="verified three days ago",
        evidence=[("test_run", "tr-old48", "pass")],
        verified_at=IN_WINDOW,  # inside the 7d window, outside the 48h one
    )
    person = _person(gather(team_store, DAY), bob)

    assert person["points"] == 3
    assert person["recent_verified_outcomes"] == 0
    assert person["bottleneck"]["class"] == "no_verified_output"
    assert person["bottleneck"]["text"] == "no verified output 48h"


def test_rule_5_a_healthy_person_has_no_bottleneck(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    alice = employees["alice"]
    _ready(make_task, alice, 6, "RDY")
    make_task(
        owner=alice,
        size="M",
        ref="FRESH",
        title="verified yesterday",
        evidence=[("test_run", "tr-fresh", "pass")],
        verified_at=YESTERDAY,
    )
    person = _person(gather(team_store, DAY), alice)

    assert person["bottleneck"] == {"class": "none", "text": "none"}
    assert person["recommendation"] == team_report.RECOMMENDATIONS["none"]


def test_the_team_bottleneck_is_the_most_common_person_class(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    """Two starved queues and one blocked person: the team problem is starvation.

    Severity only breaks ties — it does not let one person's worse problem
    outvote what is actually happening to the team.
    """
    for owner, prefix in ((employees["alice"], "U"), (employees["bob"], "S")):
        _ready(make_task, owner, 3, prefix)
    owner = employees["owner"]
    for index in range(2):
        make_task(
            owner=owner,
            size="M",
            ref=f"SB-{index}",
            title=f"blocked {index}",
            blocked_reason="waiting on a decision",
            status=BoardTaskStatus.BLOCKED.value,
        )
    _ready(make_task, owner, 5, "SR")
    gathered = gather(team_store, DAY)

    assert gathered["team"]["bottleneck"]["class"] == "queue_starvation"
    assert gathered["team"]["bottleneck"]["people"] == 2
    assert gathered["team"]["recommendation"] == team_report.RECOMMENDATIONS["queue_starvation"]


def test_a_team_with_no_bottleneck_says_so(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    for key, prefix in (("alice", "U"), ("bob", "S"), ("owner", "A")):
        owner = employees[key]
        _ready(make_task, owner, 5, prefix)
        make_task(
            owner=owner,
            size="S",
            ref=f"{prefix}-DONE",
            title="verified yesterday",
            evidence=[("test_run", f"tr-{prefix}", "pass")],
            verified_at=YESTERDAY,
        )
    gathered = gather(team_store, DAY)
    assert gathered["team"]["bottleneck"] == {"class": "none", "text": "none", "people": 0}


# ---------------------------------------------------------------------------
# the write path
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing_at_all(
    db_path: str,
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_task(
        owner=employees["alice"],
        size="M",
        ref="DR-1",
        title="Work",
        evidence=[("test_run", "tr-dr", "pass")],
        verified_at=IN_WINDOW,
    )
    out_dir = tmp_path / "reports"
    code = main(["--day", DAY, "--db", db_path, "--out-dir", str(out_dir), "--dry-run"])

    assert code == 0
    assert "DAILY PRODUCTION" in capsys.readouterr().out
    assert not out_dir.exists()
    assert team_store.list_snapshots(day=DAY) == []


def test_a_run_writes_the_file_before_it_posts(
    db_path: str,
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slack is the delivery, not the record. A failed post still leaves the day's
    report on disk — and reports the failure with exit 1 rather than exit 0."""
    make_task(
        owner=employees["alice"],
        size="M",
        ref="WR-1",
        title="Work",
        evidence=[("test_run", "tr-wr", "pass")],
        verified_at=IN_WINDOW,
    )
    out_dir = tmp_path / "reports"
    seen: dict[str, object] = {}

    def refuse(
        text: str,
        *,
        blocks: list[dict[str, object]] | None = None,
        channel: str | None = None,
        timeout: int = 30,
    ) -> bool:
        seen["file_existed_at_post_time"] = (out_dir / f"{DAY}.md").exists()
        seen["text"] = text
        seen["blocks"] = blocks
        return False

    monkeypatch.setattr(team_report, "post", refuse)
    code = main(["--day", DAY, "--db", db_path, "--out-dir", str(out_dir)])

    assert code == 1
    assert seen["file_existed_at_post_time"] is True
    written = (out_dir / f"{DAY}.md").read_text(encoding="utf-8")
    assert written.startswith(f"DAILY PRODUCTION — {DAY}")
    assert written.rstrip("\n") == seen["text"]
    assert len(team_store.list_snapshots(day=DAY)) == 3


def test_snapshots_upsert_rather_than_append(
    db_path: str,
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    tmp_path: Path,
) -> None:
    make_task(
        owner=employees["alice"],
        size="L",
        ref="UP-SNAP",
        title="Work",
        evidence=[("test_run", "tr-snap", "pass")],
        verified_at=IN_WINDOW,
    )
    out_dir = tmp_path / "reports"
    arguments = ["--day", DAY, "--db", db_path, "--out-dir", str(out_dir), "--no-post"]

    assert main(arguments) == 0
    first = team_store.list_snapshots(day=DAY)
    assert main(arguments) == 0
    second = team_store.list_snapshots(day=DAY)

    assert len(first) == len(second) == 3
    assert [row["verified_points"] for row in first] == [row["verified_points"] for row in second]
    alice_row = next(row for row in second if row["employee_id"] == employees["alice"])
    assert alice_row["verified_points"] == 8
    breakdown = json.loads(str(alice_row["breakdown_json"]))
    assert breakdown["score_version"] == "v1"
    assert [entry["ref"] for entry in breakdown["counted"]] == ["UP-SNAP"]


def test_a_day_computed_by_another_score_version_is_refused(
    db_path: str,
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exit 2, nothing written, nothing posted.

    Two rulesets inside one day's table would make the table's own numbers
    incomparable, and the person reading the Slack message would have no way to
    know which question each row answers.
    """
    make_task(
        owner=employees["alice"],
        size="M",
        ref="VP-1",
        title="Work",
        evidence=[("test_run", "tr-vp", "pass")],
        verified_at=IN_WINDOW,
    )
    team_store.upsert_snapshot(
        day=DAY,
        employee_id=employees["alice"],
        verified_points=99,
        breakdown={"score_version": "v0", "counted": []},
    )

    def explode(*args: object, **kwargs: object) -> bool:
        raise AssertionError("a version-pinned refusal must never post")

    monkeypatch.setattr(team_report, "post", explode)
    out_dir = tmp_path / "reports"
    code = main(["--day", DAY, "--db", db_path, "--out-dir", str(out_dir)])

    assert code == 2
    assert not out_dir.exists()
    # The pre-existing row is untouched: refusing must not half-write the day.
    row = team_store.get_snapshot(DAY, employees["alice"])
    assert row is not None and row["verified_points"] == 99


def test_an_unversioned_snapshot_is_not_a_conflict(
    db_path: str,
    team_store: TeamStore,
    employees: dict[str, str],
    tmp_path: Path,
) -> None:
    """A row that predates versioning can be recomputed; only a DIFFERENT version blocks."""
    team_store.upsert_snapshot(day=DAY, employee_id=employees["alice"], verified_points=1)
    assert team_report.version_conflicts(team_store, DAY) == []
    code = main(
        ["--day", DAY, "--db", db_path, "--out-dir", str(tmp_path / "reports"), "--no-post"]
    )
    assert code == 0
    row = team_store.get_snapshot(DAY, employees["alice"])
    assert row is not None and row["verified_points"] == 0


def test_render_is_stable_for_the_same_input(
    team_store: TeamStore, employees: dict[str, str], make_task: Callable[..., str]
) -> None:
    make_task(
        owner=employees["alice"],
        size="M",
        ref="ST-1",
        title="Work",
        evidence=[("test_run", "tr-st", "pass")],
        verified_at=IN_WINDOW,
    )
    gathered = gather(team_store, DAY)
    assert render(gathered) == render(gathered)
