"""The OVERALL 10x score: humans + the AI fleet, each against its own baseline.

Same integrity bar as everything else in scoring: a missing input is ``None``
(never a fabricated zero), the blend weights are declared data, and the roster
a report speaks for is the ACTIVE roster — plus anyone who left live cards
behind, because a card no view can show is a card nobody finishes.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.team.report import gather, render, render_slack
from omniagentos.team.scoring import (
    BASELINE_WEEK_END,
    BASELINE_WEEK_START,
    OVERALL_WEIGHTS,
    fleet_production,
    overall_production_x,
)
from omniagentos.team.store import TeamStore

from .conftest import DAY, IN_WINDOW


def _ledger(path: Path, events: list[dict[str, object]]) -> str:
    lines = [json.dumps(event) for event in events]
    lines.insert(1, "{torn json line")  # malformed lines are skipped, not fatal
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_fleet_production_counts_unique_merged_events_in_window(tmp_path: Path) -> None:
    path = _ledger(
        tmp_path / "ledger.jsonl",
        [
            {"event": "merged", "id": "a", "ts": "2026-08-14T10:00:00Z"},
            {"event": "merged", "id": "a", "ts": "2026-08-14T11:00:00Z"},  # duplicate id
            {"event": "merged", "id": "b", "ts": "2026-08-14T12:00:00Z"},
            {"event": "merged", "id": "out", "ts": "2026-07-01T00:00:00Z"},  # out of window
            {"event": "found", "id": "c", "ts": "2026-08-14T13:00:00Z"},  # not a merge
        ],
    )
    assert fleet_production(path, "2026-08-08T00:00:00Z", "2026-08-15T00:00:00Z") == 2


def test_fleet_production_is_none_when_the_ledger_is_unreadable(tmp_path: Path) -> None:
    assert fleet_production(str(tmp_path / "absent.jsonl"), "2026-08-08", "2026-08-15") is None


def test_overall_blend_and_renormalization() -> None:
    weights = OVERALL_WEIGHTS
    assert weights == {"humans": 0.5, "fleet": 0.5}
    assert overall_production_x(2.0, 4.0) == pytest.approx(3.0)
    # A missing component renormalizes onto the other — never counted as 0x.
    assert overall_production_x(2.0, None) == pytest.approx(2.0)
    assert overall_production_x(None, 4.0) == pytest.approx(4.0)
    assert overall_production_x(None, None) is None


def test_gather_carries_overall_and_render_prints_it(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omniagentos.team.scoring import BASELINE_SOURCE

    make_task(
        owner=employees["alice"],
        size="M",
        ref="BASE-U",
        title="Baseline week",
        source=BASELINE_SOURCE,
        acceptance="",
        evidence=[("doc", "base-u", "pass")],
        verified_at=IN_WINDOW,
    )
    make_task(
        owner=employees["alice"],
        size="M",
        ref="FW-1",
        title="This week's work",
        evidence=[("test_run", "tr-fw", "pass")],
        verified_at=IN_WINDOW,
    )
    path = _ledger(
        tmp_path / "fleet.jsonl",
        [
            # Baseline week (outside the 7d window): 2 landings. This week: 4 → 2.0x.
            {"event": "merged", "id": "b1", "ts": "2026-08-03T10:00:00Z"},
            {"event": "merged", "id": "b2", "ts": "2026-08-04T10:00:00Z"},
            {"event": "merged", "id": "w1", "ts": f"{DAY}T01:00:00Z"},
            {"event": "merged", "id": "w2", "ts": f"{DAY}T02:00:00Z"},
            {"event": "merged", "id": "w3", "ts": f"{DAY}T03:00:00Z"},
            {"event": "merged", "id": "w4", "ts": f"{DAY}T04:00:00Z"},
        ],
    )
    monkeypatch.setenv("OMNI_TEAM_FLEET_LEDGER", path)

    gathered = gather(team_store, DAY)
    overall = gathered["overall"]
    assert overall["fleet_baseline_merged"] == 2
    assert overall["fleet_merged"] == 4
    assert overall["fleet_x"] == pytest.approx(2.0)
    assert overall["humans_x"] == pytest.approx(1.0)  # 3 pts vs 3-pt baseline
    assert overall["production_x"] == pytest.approx(1.5)

    text = render(gathered)
    assert "OVERALL — 1.5x (15% to 10x) · humans 1.0x · fleet 2.0x (4 landed)" in text

    _fallback, blocks = render_slack(gathered)
    flat = json.dumps(blocks, ensure_ascii=False)
    assert "OVERALL — 1.5x" in flat
    assert "▓" in flat  # the progress bar painted something


def test_inactive_employee_without_cards_leaves_every_view(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    goals = CompanyGoalsStore(team_store._store)
    goals.ensure_employee(employee_id="emp_frank", name="Frank", role=None)
    team_store._store._write(
        "UPDATE employees SET status = 'inactive' WHERE id = ?", ("emp_frank",)
    )

    gathered = gather(team_store, DAY)
    assert all(person["employee_id"] != "emp_frank" for person in gathered["people"])
    assert "emp_frank" not in team_store.team_queues(today=DAY)
    assert "ANDY" not in render(gathered)


def test_inactive_employee_with_a_live_card_stays_visible(
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    """Leaving the company does not make your open cards invisible — they stay
    in the views until someone reassigns them."""
    goals = CompanyGoalsStore(team_store._store)
    goals.ensure_employee(employee_id="emp_frank", name="Frank", role=None)
    make_task(owner="emp_frank", size="S", ref="AN-1", title="Orphaned work")
    team_store._store._write(
        "UPDATE employees SET status = 'inactive' WHERE id = ?", ("emp_frank",)
    )

    gathered = gather(team_store, DAY)
    assert any(person["employee_id"] == "emp_frank" for person in gathered["people"])
    assert "emp_frank" in team_store.team_queues(today=DAY)


def test_baseline_week_bounds_are_the_recorded_week() -> None:
    assert BASELINE_WEEK_START == "2026-08-03T00:00:00Z"
    assert BASELINE_WEEK_END == "2026-08-10T00:00:00Z"
