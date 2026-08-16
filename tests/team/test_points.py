"""Point floors, the ratchet schedule, and Friday pace (policy over scoring).

Everything here passes explicit dates and configs — the schedule is arithmetic
and must be provable without touching the wall clock.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.team import points
from omniagentos.team.points import (
    PaceStatus,
    PointsConfig,
    active_dev_ids,
    floor_for_week,
    friday_announcement,
    load_points_config,
    pace_line,
    pace_statuses,
    week_index,
    week_start,
)
from omniagentos.team.store import TeamStore

_CONFIG = PointsConfig(program_start=date(2026, 8, 11))  # Tuesday; week 1 = Mon 2026-08-10


class TestFloorSchedule:
    def test_configured_first_two_weeks(self) -> None:
        assert floor_for_week(1, _CONFIG) == 10
        assert floor_for_week(2, _CONFIG) == 15

    def test_ratchet_every_two_weeks_rounded(self) -> None:
        assert floor_for_week(3, _CONFIG) == 18  # 15 * 1.2
        assert floor_for_week(4, _CONFIG) == 18  # same ratchet window
        assert floor_for_week(5, _CONFIG) == 22  # 21.6 rounded
        assert floor_for_week(6, _CONFIG) == 22
        assert floor_for_week(7, _CONFIG) == 26  # 25.92 rounded

    def test_week_index_anchors_on_the_program_monday(self) -> None:
        assert week_start(date(2026, 8, 13)) == date(2026, 8, 10)
        assert week_index(date(2026, 8, 13), _CONFIG) == 1
        assert week_index(date(2026, 8, 17), _CONFIG) == 2
        # Days before the program count as week 1, never week 0 or negative.
        assert week_index(date(2026, 8, 3), _CONFIG) == 1


class TestConfigLoading:
    def test_shipped_yaml_matches_the_coded_defaults(self) -> None:
        loaded = load_points_config()
        assert loaded.week1_floor == 10
        assert loaded.week2_floor == 15
        assert loaded.ratchet_pct == 20
        assert loaded.ratchet_every_weeks == 2
        assert loaded.announce_day == "friday"

    def test_missing_file_degrades_to_defaults(self, tmp_path: Path) -> None:
        assert load_points_config(tmp_path / "absent.yaml") == PointsConfig()

    def test_malformed_file_degrades_to_defaults(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- just\n- a list\n", encoding="utf-8")
        assert load_points_config(bad) == PointsConfig()


class TestPace:
    def test_verified_points_this_week_count_toward_pace(
        self, collab_store, team_store: TeamStore, make_card, employees: dict[str, str]
    ) -> None:
        # Verified today (inside this week): an L card = 8 points. No
        # acceptance criteria, so the done-gate does not demand evidence; a
        # second person verifies, so the human path is admissible.
        card = make_card(title="Big", owner_employee_id=employees["bob"], size="L")
        collab_store.update_board_task(card.id, {"status": "done"}, actor=employees["bob"])
        team_store.verify_task(card.id, employees["alice"])

        today = date.today()
        config = PointsConfig(program_start=week_start(today))
        statuses = pace_statuses(
            team_store, [employees["bob"], employees["alice"]], config=config, today=today
        )
        assert statuses[employees["bob"]].points == 8
        assert statuses[employees["alice"]].points == 0
        assert statuses[employees["alice"]].on_pace is False  # 0 < any prorated target

    def test_prorated_target_walks_monday_to_friday(self, team_store: TeamStore) -> None:
        week1 = PointsConfig(program_start=date(2026, 8, 10))
        by_day = {
            date(2026, 8, 10): 2.0,  # Monday: 10 * 1/5
            date(2026, 8, 12): 6.0,  # Wednesday: 10 * 3/5
            date(2026, 8, 14): 10.0,  # Friday: the whole floor
            date(2026, 8, 16): 10.0,  # Sunday still owes the whole floor
        }
        for day, expected in by_day.items():
            status = pace_statuses(team_store, ["emp_bob"], config=week1, today=day)[
                "emp_bob"
            ]
            assert status.prorated_target == expected, day

    def test_pace_line_shapes(self) -> None:
        short = PaceStatus(
            employee_id="emp_bob", points=4, floor=15, prorated_target=9.0, on_pace=False
        )
        ok = PaceStatus(
            employee_id="emp_alice", points=9, floor=15, prorated_target=9.0, on_pace=True
        )
        assert pace_line(short) == "⚠ emp_bob 4/15 pts, Friday pace short"
        assert pace_line(ok) == "✓ emp_alice 9/15 pts, on pace"

    def test_operator_is_not_a_floor_bearing_dev(self) -> None:
        assert active_dev_ids(["emp_owner", "emp_bob", "emp_alice"]) == [
            "emp_bob",
            "emp_alice",
        ]


class TestFridayAnnouncement:
    def test_announces_on_friday_before_a_raise(self) -> None:
        config = PointsConfig(program_start=date(2026, 8, 10))
        line = friday_announcement(config, date(2026, 8, 14))  # Friday of week 1
        assert line == "📈 Point floor rises Monday: 10 → 15 verified pts/week (+20% ratchet)"

    def test_quiet_on_other_days_and_flat_weeks(self) -> None:
        config = PointsConfig(program_start=date(2026, 8, 10))
        assert friday_announcement(config, date(2026, 8, 13)) is None  # Thursday
        # Friday of week 3: week 4 keeps the same 18 floor -> no announcement.
        assert friday_announcement(config, date(2026, 8, 28)) is None


class TestScoringIsUntouched:
    def test_points_module_does_not_shadow_scoring_rules(self) -> None:
        """points.py consumes compute_scores; the sacred constants stay where
        they are and keep their values."""
        from omniagentos.team import scoring

        assert scoring.POINTS_BY_SIZE == {"S": 1, "M": 3, "L": 8}
        assert scoring.TARGET_X == 10
        assert not hasattr(points, "POINTS_BY_SIZE")


def test_roster_fixture_is_wired(goals_store: CompanyGoalsStore, employees: dict[str, str]) -> None:
    assert goals_store.get_employee(employees["bob"]) is not None
