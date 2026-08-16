"""NSG-025 residual: goal-level limbo detector over board_tasks.

Covers the three owned surfaces end to end:
- ``omniagentos.intake.board_sweep.goal_limbo_candidates`` (the predicate)
- ``omniagentos.steward.alerts.rules.goal_limbo`` (the alert shaper)
- ``omniagentos.steward.alerts.monitor.monitor_once`` (the wiring + the
  existing store-level (rule, cooldown_key) dedup, reused rather than
  reinvented -- see rules.py's ``goal_limbo`` docstring).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.intake.board_sweep import (
    goal_limbo_candidates,
    limbo_recommended_action,
)
from omniagentos.steward.alerts import monitor
from omniagentos.steward.alerts.rules import goal_limbo
from omniagentos.steward.config import AlertsConfig, StewardConfig
from tests.support.db_template import make_store

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


def _set_updated(collab: CollabStore, task_id: str, value: datetime) -> None:
    collab._store._write(
        "UPDATE board_tasks SET updated_at = ? WHERE id = ?",
        (value.strftime("%Y-%m-%dT%H:%M:%SZ"), task_id),
    )


def _collab(tmp_path: Path) -> CollabStore:
    return make_store(CollabStore, tmp_path / "goal_limbo.db")


def _make_open_untouched(collab: CollabStore, *, days_old: int = 10) -> str:
    task = BoardTask(title="ten day old open card", status=BoardTaskStatus.OPEN)
    collab.create_board_task(task)
    _set_updated(collab, task.id, NOW - timedelta(days=days_old))
    return task.id


def _make_blocked_dead_reason(collab: CollabStore, *, days_old: int = 10) -> str:
    task = BoardTask(title="blocked with dead reason", status=BoardTaskStatus.OPEN)
    collab.create_board_task(task)
    collab.update_board_task(task.id, {"status": BoardTaskStatus.BLOCKED.value, "blocked_reason": ""})
    _set_updated(collab, task.id, NOW - timedelta(days=days_old))
    return task.id


# ---------------------------------------------------------------------------
# goal_limbo_candidates (the predicate)
# ---------------------------------------------------------------------------


def test_candidates_selects_untouched_cards_and_skips_fresh(tmp_path: Path) -> None:
    collab = _collab(tmp_path)
    stale_id = _make_open_untouched(collab, days_old=10)

    fresh = BoardTask(title="fresh open card", status=BoardTaskStatus.OPEN)
    collab.create_board_task(fresh)
    _set_updated(collab, fresh.id, NOW - timedelta(days=1))

    rows = goal_limbo_candidates(collab, now=NOW, limbo_days=7)

    assert [row["id"] for row in rows] == [stale_id]


def test_candidates_skips_longhaul_and_swarm_lanes(tmp_path: Path) -> None:
    """A generic limbo detector must not fight an authoritative lifecycle owner."""
    collab = _collab(tmp_path)
    longhaul = BoardTask(title="longhaul", status=BoardTaskStatus.OPEN)
    swarm = BoardTask(title="swarm", status=BoardTaskStatus.OPEN)
    collab.create_board_task(longhaul)
    collab.create_board_task(swarm)
    collab._store._write(
        "UPDATE board_tasks SET lane = 'longhaul' WHERE id = ?", (longhaul.id,)
    )
    collab._store._write(
        "UPDATE board_tasks SET swarm_run_id = 'swr_test' WHERE id = ?", (swarm.id,)
    )
    for task_id in (longhaul.id, swarm.id):
        _set_updated(collab, task_id, NOW - timedelta(days=30))

    rows = goal_limbo_candidates(collab, now=NOW, limbo_days=7)

    assert rows == []


def test_candidates_skips_terminal_and_archived(tmp_path: Path) -> None:
    collab = _collab(tmp_path)
    done = BoardTask(title="done", status=BoardTaskStatus.DONE)
    collab.create_board_task(done)
    _set_updated(collab, done.id, NOW - timedelta(days=30))

    archived = BoardTask(title="archived open", status=BoardTaskStatus.OPEN)
    collab.create_board_task(archived)
    collab.update_board_task(archived.id, {"archived_at": "2026-01-01T00:00:00Z"})
    _set_updated(collab, archived.id, NOW - timedelta(days=30))

    rows = goal_limbo_candidates(collab, now=NOW, limbo_days=7)

    assert rows == []


# ---------------------------------------------------------------------------
# limbo_recommended_action -- every human-facing item names one (estate rule)
# ---------------------------------------------------------------------------


def test_recommended_action_dead_blocked_reason() -> None:
    action = limbo_recommended_action({"status": "blocked", "blocked_reason": ""})
    assert action.startswith("unblock:")


def test_recommended_action_live_blocked_reason() -> None:
    action = limbo_recommended_action({"status": "blocked", "blocked_reason": "waiting on the operator"})
    assert action.startswith("unblock-hint:")
    assert "waiting on the operator" in action


def test_recommended_action_awaiting_approval() -> None:
    action = limbo_recommended_action({"status": "awaiting_approval"})
    assert action.startswith("escalate:")


def test_recommended_action_open_no_owner() -> None:
    action = limbo_recommended_action({"status": "open", "owner_employee_id": None})
    assert action.startswith("reassign:")


# ---------------------------------------------------------------------------
# rules.goal_limbo -- shapes rows into alert candidates
# ---------------------------------------------------------------------------


def test_goal_limbo_rule_fires_one_candidate_per_card_with_recommendation(
    tmp_path: Path,
) -> None:
    collab = _collab(tmp_path)
    open_id = _make_open_untouched(collab, days_old=10)
    blocked_id = _make_blocked_dead_reason(collab, days_old=10)

    rows = goal_limbo_candidates(collab, now=NOW, limbo_days=7)
    candidates = goal_limbo(rows)

    assert len(candidates) == 2
    by_id = {c.evidence["task_id"]: c for c in candidates}
    assert set(by_id) == {open_id, blocked_id}
    for candidate in candidates:
        assert candidate.rule == "goal_limbo"
        assert candidate.evidence["recommended_action"]
        assert "Recommended action:" in candidate.body
    assert by_id[blocked_id].evidence["recommended_action"].startswith("unblock:")
    assert by_id[open_id].evidence["recommended_action"].startswith("reassign:")
    # Stable keys: distinct cards never collapse into one cooldown case.
    assert candidates[0].cooldown_key != candidates[1].cooldown_key


# ---------------------------------------------------------------------------
# monitor.monitor_once -- end-to-end wiring + dedup (reused store pattern)
# ---------------------------------------------------------------------------


def _steward_config() -> StewardConfig:
    return StewardConfig(alerts=AlertsConfig(cooldown_minutes=240))


def test_monitor_fires_exactly_one_alert_per_limbo_card_and_suppresses_repeat(
    tmp_path: Path,
) -> None:
    collab = _collab(tmp_path)
    _make_open_untouched(collab, days_old=10)
    _make_blocked_dead_reason(collab, days_old=10)
    database = collab._store
    cfg = _steward_config()
    marker = tmp_path / "triaged.json"

    first = monitor.monitor_once(database, cfg=cfg, now=NOW, triaged_marker_path=marker)
    assert first["created"] == 2

    # Card state is untouched between cycles -- the SAME condition is still
    # true, so the rule reports the SAME cooldown_key again. store.create_alert's
    # (rule, cooldown_key) case identity collapses this into the still-open
    # case (suppressed), never a second row for the same unresolved card.
    second = monitor.monitor_once(
        database, cfg=cfg, now=NOW + timedelta(minutes=15), triaged_marker_path=marker
    )
    assert second["created"] == 0
    assert second["suppressed"] >= 2


def test_monitor_re_fires_after_card_recovers_then_relapses(tmp_path: Path) -> None:
    """The dedup window: touching the card resolves the case; a later relapse
    into limbo is a genuinely new incident and gets a fresh alert -- this is
    the store's existing auto-resolve/case-identity pattern, not a bespoke
    timer invented for this rule (see rules.goal_limbo's docstring)."""
    collab = _collab(tmp_path)
    task_id = _make_open_untouched(collab, days_old=10)
    database = collab._store
    cfg = _steward_config()
    marker = tmp_path / "triaged.json"

    first = monitor.monitor_once(database, cfg=cfg, now=NOW, triaged_marker_path=marker)
    assert first["created"] == 1

    # The card is touched (a real state transition) inside the limbo window --
    # the rule no longer reports this cooldown_key, so the open case
    # auto-resolves as "recovered".
    collab.update_board_task(task_id, {"description": "touched by an owner"})
    touched_cycle = monitor.monitor_once(
        database, cfg=cfg, now=NOW + timedelta(minutes=30), triaged_marker_path=marker
    )
    assert touched_cycle["created"] == 0

    open_alert = collab._store  # sanity: same underlying db
    assert open_alert is database

    # It relapses into limbo (goes untouched again past the threshold).
    _set_updated(collab, task_id, NOW - timedelta(days=3))
    relapse_now = NOW + timedelta(days=8)
    relapsed = monitor.monitor_once(
        database, cfg=cfg, now=relapse_now, triaged_marker_path=marker
    )
    assert relapsed["created"] == 1


# ---------------------------------------------------------------------------
# C-MAJ-001 -- unknown updated_at must surface as limbo, never read as fresh
# ---------------------------------------------------------------------------


def test_candidates_surface_unknown_updated_at_as_limbo(tmp_path: Path) -> None:
    """Missing/empty/unparseable updated_at is an instrument problem, not a
    fresh card: it must be returned annotated, so an open limbo alert can
    never be falsely auto-resolved by a timestamp the store lost."""
    # NULL is refused by the board_tasks schema itself (NOT NULL), so the
    # storable instrument-problem shapes are empty and unparseable strings.
    for bad_value in ("", "not-a-timestamp"):
        collab = _collab(tmp_path / f"case-{bad_value[:4] or 'empty'}")
        task = BoardTask(title="unknown touch", status=BoardTaskStatus.OPEN)
        collab.create_board_task(task)
        collab._store._write(
            "UPDATE board_tasks SET updated_at = ? WHERE id = ?",
            (bad_value, task.id),
        )

        rows = goal_limbo_candidates(collab, now=NOW, limbo_days=7)

        assert [row["id"] for row in rows] == [task.id], f"bad_value={bad_value!r}"
        assert rows[0]["updated_at_unknown"] is True


def test_rule_names_unreadable_timestamp_in_alert(tmp_path: Path) -> None:
    collab = _collab(tmp_path)
    task = BoardTask(title="unknown touch", status=BoardTaskStatus.OPEN)
    collab.create_board_task(task)
    collab._store._write(
        "UPDATE board_tasks SET updated_at = '' WHERE id = ?", (task.id,)
    )

    rows = goal_limbo_candidates(collab, now=NOW, limbo_days=7)
    candidates = goal_limbo(rows)

    assert len(candidates) == 1
    assert "UNREADABLE last-touch timestamp" in candidates[0].body
    assert candidates[0].evidence["updated_at_unknown"] is True
