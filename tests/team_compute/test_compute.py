"""Compute-pool reader, suggestion, offloads, alerts, and balancer decisions."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from omniagentos.team import compute, decisions


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@pytest.fixture()
def repo_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "var").mkdir()
    # Pin the resolved var root to the fixture so gather_compute and the tests
    # read the same workqueue DB (OMNIAGENTOS_VAR is the canonical first key).
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path / "var"))
    return tmp_path


def _make_db(
    repo_root: Path,
    machines: list[dict[str, Any]],
    units: list[dict[str, Any]],
) -> Path:
    """A minimal workqueue DB with exactly the columns compute reads."""
    path = repo_root / "var" / "workqueue.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE wq_machines (machine_id TEXT PRIMARY KEY, ncpu INTEGER, "
            "max_concurrent INTEGER, drain INTEGER, last_seen_at TEXT, "
            "last_load1 REAL, labels TEXT)"
        )
        connection.execute(
            "CREATE TABLE wq_units (id TEXT PRIMARY KEY, state TEXT, labels TEXT, "
            "submitted_by TEXT, lease_owner TEXT, cancel_requested INTEGER DEFAULT 0)"
        )
        for machine in machines:
            connection.execute(
                "INSERT INTO wq_machines VALUES (?,?,?,?,?,?,?)",
                (
                    machine["machine_id"],
                    machine.get("ncpu"),
                    machine.get("max_concurrent", 1),
                    1 if machine.get("drain") else 0,
                    machine.get("last_seen_at"),
                    machine.get("last_load1"),
                    machine.get("labels", "[]"),
                ),
            )
        for unit in units:
            connection.execute(
                "INSERT INTO wq_units VALUES (?,?,?,?,?,?)",
                (
                    unit["id"],
                    unit["state"],
                    unit.get("labels", "[]"),
                    unit.get("submitted_by"),
                    unit.get("lease_owner"),
                    1 if unit.get("cancel_requested") else 0,
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return path


# -- reader: present / absent / locked -------------------------------------


def test_absent_db_degrades_to_local_fallback(repo_root: Path) -> None:
    result = compute.gather_compute(repo_root)
    assert result.source == "local"
    assert len(result.machines) == 1
    assert result.machines[0].ncpu is not None  # os.cpu_count() row
    assert any("not deployed" in note for note in result.notes)


def test_present_db_reads_capacity_and_queue(repo_root: Path) -> None:
    fresh = _iso(time.time())
    _make_db(
        repo_root,
        machines=[
            {"machine_id": "mac-studio", "ncpu": 20, "max_concurrent": 4,
             "last_seen_at": fresh, "last_load1": 3.2, "labels": '["mac"]'},
            {"machine_id": "mini-2", "ncpu": 8, "max_concurrent": 2,
             "last_seen_at": fresh, "last_load1": 1.0, "labels": '["mac"]'},
        ],
        units=[
            {"id": "u1", "state": "running", "submitted_by": "emp_owner",
             "lease_owner": "mac-studio:w1", "labels": '["mac"]'},
            {"id": "u2", "state": "queued", "submitted_by": "emp_alice", "labels": '["mac"]'},
            {"id": "u3", "state": "queued", "submitted_by": "emp_alice", "labels": '["mac"]'},
        ],
    )
    result = compute.gather_compute(repo_root)
    assert result.source == "pool"
    assert result.total_slots == 6
    assert result.in_flight == 1
    assert result.free_slots == 5
    assert result.queued == 2
    studio = next(m for m in result.machines if m.machine_id == "mac-studio")
    assert studio.in_flight == 1 and studio.free_slots == 3 and studio.fresh


def test_cancel_requested_queued_unit_is_not_counted(repo_root: Path) -> None:
    fresh = _iso(time.time())
    _make_db(
        repo_root,
        machines=[{"machine_id": "m1", "ncpu": 4, "max_concurrent": 2,
                   "last_seen_at": fresh, "labels": "[]"}],
        units=[
            {"id": "u1", "state": "queued", "submitted_by": "emp_owner", "cancel_requested": 1},
            {"id": "u2", "state": "queued", "submitted_by": "emp_owner"},
        ],
    )
    result = compute.gather_compute(repo_root)
    assert result.queued == 1


def test_locked_db_degrades_silently(repo_root: Path) -> None:
    path = _make_db(
        repo_root,
        machines=[{"machine_id": "m1", "ncpu": 4, "max_concurrent": 1,
                   "last_seen_at": _iso(time.time()), "labels": "[]"}],
        units=[],
    )
    blocker = sqlite3.connect(path)
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        blocker.execute(
            "INSERT INTO wq_units VALUES ('x','queued','[]','emp_owner',NULL,0)"
        )
        result = compute.gather_compute(repo_root)  # SELECT blocks then times out
    finally:
        blocker.rollback()
        blocker.close()
    assert result.source == "local"  # never raised, degraded to the local row


# -- offloads attribution ---------------------------------------------------


def test_offloads_group_by_person_with_machine_attribution(repo_root: Path) -> None:
    fresh = _iso(time.time())
    _make_db(
        repo_root,
        machines=[{"machine_id": "mac-studio", "ncpu": 20, "max_concurrent": 8,
                   "last_seen_at": fresh, "labels": "[]"}],
        units=[
            {"id": "a", "state": "running", "submitted_by": "emp_owner",
             "lease_owner": "mac-studio:w1"},
            {"id": "b", "state": "queued", "submitted_by": "emp_owner"},
            {"id": "c", "state": "review", "submitted_by": "emp_alice",
             "lease_owner": "mini-2:w9"},
            {"id": "d", "state": "running", "submitted_by": "", "lease_owner": "mac-studio:w3"},
        ],
    )
    result = compute.gather_compute(repo_root)
    by_person = {o.person: o for o in result.offloads}
    assert by_person["emp_owner"].running == 1 and by_person["emp_owner"].queued == 1
    assert by_person["emp_owner"].machines == ["mac-studio"]
    assert by_person["emp_alice"].in_review == 1
    assert by_person["emp_alice"].machines == ["mini-2"]
    assert by_person["(unattributed)"].running == 1


# -- suggestion first-match table ------------------------------------------


def _pool(**kwargs: Any) -> compute.Compute:
    base: dict[str, Any] = {"source": "pool"}
    base.update(kwargs)
    return compute.Compute(**base)


def test_suggestion_first_match_table() -> None:
    # free slots + proposal/no-session bottleneck -> proposal-generation
    assert "proposal-generation" in compute.suggestion(
        _pool(free_slots=3), "proposals waiting, no active sessions"
    )
    assert "proposal-generation" in compute.suggestion(_pool(free_slots=3), "no active sessions")
    # free slots + gate/merge bottleneck -> gate runs
    assert "gate runs" in compute.suggestion(
        _pool(free_slots=2), "gate throughput (7 in merge queue)"
    )
    assert "gate runs" in compute.suggestion(
        _pool(free_slots=2), "merge gate red (failures with zero merges this hour)"
    )
    # unclaimable -> name the missing label (beats the saturated rule)
    unclaimable = _pool(
        free_slots=0,
        queued=1,
        total_slots=4,
        unclaimable=[{"labels": ["gpu"]}],
        machines=[compute.PoolMachine("m1", 4, 1.0, 4, 0, False, True, 0.0, labels=["mac"])],
    )
    assert "gpu" in compute.suggestion(unclaimable, "none")
    # saturated + queued -> enroll / raise max_concurrent
    assert "raise max_concurrent" in compute.suggestion(
        _pool(free_slots=0, queued=5, total_slots=4), "none"
    )
    # nothing matches -> balanced
    assert compute.suggestion(_pool(free_slots=2), "none") == "balanced"


# -- alert detectors --------------------------------------------------------


def test_box_down_alert(repo_root: Path) -> None:
    stale = _iso(time.time() - 20 * 60)  # 20 min ago (> 10 min horizon)
    _make_db(
        repo_root,
        machines=[{"machine_id": "mini-2", "ncpu": 8, "max_concurrent": 2,
                   "last_seen_at": stale, "labels": "[]"}],
        units=[],
    )
    result = compute.gather_compute(repo_root)
    alerts = compute.detect_pool_alerts(result)
    kinds = {a.kind for a in alerts}
    assert "box_down" in kinds
    down = next(a for a in alerts if a.kind == "box_down")
    assert down.color == compute.RED and down.machine_id == "mini-2"


def test_capacity_exhausted_alert(repo_root: Path) -> None:
    fresh = _iso(time.time())
    _make_db(
        repo_root,
        machines=[{"machine_id": "m1", "ncpu": 4, "max_concurrent": 1,
                   "last_seen_at": fresh, "labels": "[]"}],
        units=[
            {"id": "run", "state": "running", "submitted_by": "emp_owner", "lease_owner": "m1:w1"},
            {"id": "q1", "state": "queued", "submitted_by": "emp_owner"},
            {"id": "q2", "state": "queued", "submitted_by": "emp_owner"},
        ],
    )
    result = compute.gather_compute(repo_root)
    assert result.free_slots == 0 and result.queued == 2
    alerts = compute.detect_pool_alerts(result)
    exhausted = next(a for a in alerts if a.kind == "capacity_exhausted")
    assert exhausted.color == compute.RED  # queued (2) >= total_slots (1)


def test_starving_alert(repo_root: Path) -> None:
    fresh = _iso(time.time())
    _make_db(
        repo_root,
        machines=[{"machine_id": "m1", "ncpu": 8, "max_concurrent": 4,
                   "last_seen_at": fresh, "labels": '["mac"]'}],
        units=[{"id": "q1", "state": "queued", "submitted_by": "emp_owner", "labels": '["mac"]'}],
    )
    result = compute.gather_compute(repo_root)
    alerts = compute.detect_pool_alerts(result)
    starving = [a for a in alerts if a.kind == "starving"]
    assert starving and starving[0].color == compute.AMBER
    assert starving[0].machine_id == "m1"


def test_local_source_yields_no_alerts(repo_root: Path) -> None:
    result = compute.gather_compute(repo_root)  # no DB -> local
    assert compute.detect_pool_alerts(result) == []


# -- balancer decision round-trip ------------------------------------------


class _Notifier:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post_channel(self, text: str, **kwargs: Any) -> bool:
        self.posts.append(text)
        return True


def _alert() -> compute.PoolAlert:
    return compute.PoolAlert(
        kind="box_down", color=compute.RED, text="mini-2 down",
        remediation="drain mini-2 and re-route its in-flight work", machine_id="mini-2",
    )


def test_balancer_register_is_numbered_deduped_and_shares_number_space(
    repo_root: Path,
) -> None:
    # A prior repair takes number 1 from the shared allocator.
    from omniagentos.team.session_tracker import Overall

    decisions.register_repair_proposals(
        repo_root, Overall(bottleneck="merge gate red (x)", failed_merges_last_hour=1)
    )
    first = compute.register_balancer_proposals(repo_root, [_alert()])
    assert [d["number"] for d in first] == [2]  # shares the one number space
    again = compute.register_balancer_proposals(repo_root, [_alert()])
    assert [d["number"] for d in again] == [2]  # deduped while pending
    assert all(d["kind"] == "balancer" for d in again)


def test_balancer_reply_prefix_parses() -> None:
    roster = {"UOWNER": "emp_owner"}
    # collect_replies treats the list as Slack's newest-first and returns
    # oldest-first, so the "balancer"-prefixed reply lands last here. The
    # fourth element is the reply's own kind prefix (None for a bare "N yes"),
    # which process_replies checks against the stored kind before authorizing.
    messages = [
        {"user": "UOWNER", "text": "5 no"},
        {"user": "UOWNER", "text": "balancer 4 yes"},
    ]
    assert decisions.collect_replies(messages, roster) == [
        (4, True, "emp_owner", "balancer"),
        (5, False, "emp_owner", None),
    ]


def test_balancer_yes_authorizes_without_auto_executing(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compute.register_balancer_proposals(repo_root, [_alert()])
    # A balancer yes must NEVER fall through to the repair finding filer.
    monkeypatch.setattr(
        decisions, "_file_repair_finding",
        lambda root, d: (_ for _ in ()).throw(AssertionError("balancer must not file findings")),
    )
    monkeypatch.setattr(
        decisions, "_slack_history",
        lambda token, channel, oldest: [{"user": "UOWNER", "text": "1 yes"}],
    )
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    outbound = _Notifier()
    stats = decisions.process_replies(
        repo_root, notifier=outbound, token="t", channel="C", dry_run=False
    )
    assert stats == {"approved": 1, "declined": 0, "executed": 1, "failed": 0}
    assert "authorized" in outbound.posts[0] and "drain mini-2" in outbound.posts[0]
    state = decisions.load_state(repo_root)
    executed = next(d for d in state["decisions"] if d["number"] == 1)
    assert executed["status"] == "executed"
    assert executed["result"].startswith("authorized:")


def test_balancer_no_declines_with_kind_glyph(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compute.register_balancer_proposals(repo_root, [_alert()])
    monkeypatch.setattr(
        decisions, "_slack_history",
        lambda token, channel, oldest: [{"user": "UOWNER", "text": "1 no"}],
    )
    monkeypatch.setattr(decisions, "load_slack_map", lambda: {"UOWNER": "emp_owner"})
    outbound = _Notifier()
    stats = decisions.process_replies(repo_root, notifier=outbound, token="t", channel="C")
    assert stats["declined"] == 1 and stats["executed"] == 0
    assert "⚖️ balancer 1. declined by emp_owner" in outbound.posts[0]


# -- notes seam -------------------------------------------------------------


def test_compute_notes_carry_offloads_and_suggestion(repo_root: Path) -> None:
    fresh = _iso(time.time())
    _make_db(
        repo_root,
        machines=[{"machine_id": "mac-studio", "ncpu": 20, "max_concurrent": 4,
                   "last_seen_at": fresh, "last_load1": 2.0, "labels": '["mac"]'}],
        units=[
            {"id": "u1", "state": "queued", "submitted_by": "emp_owner", "labels": '["mac"]'},
        ],
    )
    result = compute.gather_compute(repo_root)
    notes = compute.compute_notes(result, "gate throughput (1 in merge queue)")
    joined = "\n".join(notes)
    # No COMPUTE summary line for a healthy pool — #342's capacity block is the
    # single authoritative summary (review finding, #355).
    assert "COMPUTE —" not in joined
    assert "offload emp_owner" in joined
    assert "suggestion — enqueue gate runs" in joined


# -- alert posting rides the tracker tick -----------------------------------


def test_tracker_tick_posts_pool_alerts(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hourly tick detects pool alerts and posts them via post_alerts."""
    from omniagentos.team import session_tracker as st

    stale = _iso(time.time() - 20 * 60)  # box_down: last beat 20 min ago
    _make_db(repo_root, machines=[{"machine_id": "mini-2", "ncpu": 8,
             "max_concurrent": 2, "last_seen_at": stale, "labels": "[]"}], units=[])
    posts: list[str] = []

    class _TickNotifier:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.token = "t"
            self.last_error = ""

        def post_channel(self, text: str, **kwargs: Any) -> bool:
            posts.append(text)
            return True

    monkeypatch.setattr(st, "load_slack_env", lambda: None)
    monkeypatch.setattr(st, "_fetch_pool_status", lambda: None)
    monkeypatch.setattr(st, "gather_overall", lambda *a: st.Overall(bottleneck="none"))
    monkeypatch.setattr(st, "gather_balances", lambda reports: "")
    monkeypatch.setattr(st, "SlackNotifier", _TickNotifier)
    assert st.main(["--repo-root", str(repo_root), "--channel", "C", "--no-slack-ingest"]) == 0
    assert any("box may be DOWN" in text for text in posts)  # posted, not just detected
