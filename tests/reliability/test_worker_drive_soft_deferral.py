"""H-07 repair: soft deferral accounting is separate from hard failure accounting.

The blocker these tests pin down: ``overlap_with_monitoring`` is a governance WAIT, not
a failure, but the re-driver spent the same 3-attempt budget on it. With ``watch``
running every 600 s and observation windows of 24–72 h, a perfectly healthy improvement
false-failed with a critical alert in ~30 minutes and could never survive long enough
for the overlap to clear.

Everything here is deterministic: temp SQLite, an injected clock, and fake apply/spawn
callables. No live providers, no real subprocesses, no wall-clock sleeps.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate_connection
from omniagentos.reliability.pipeline import ApplyResult
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.reliability.taxonomy import ImprovementStatus
from omniagentos.reliability.worker_drive import (
    APPLY_LEASE_KEY,
    DEFAULT_MAX_ATTEMPTS,
    ESCALATION_LEASE_CONTENTION_EXHAUSTED,
    ESCALATION_OVERLAP_HORIZON_EXPIRED,
    ESCALATION_SOFT_STATE_INVALID,
    LEASE_CONTENTION_BUDGET_SECONDS,
    MAX_LEASE_CONTENTION_SECONDS,
    OUTCOME_APPLIED,
    OUTCOME_DEFERRED,
    OUTCOME_ESCALATED,
    OUTCOME_EXITED,
    OVERLAP_HORIZON_SLACK_SECONDS,
    SOFT_LEASE_CONTENTION,
    SOFT_MONITORING_OVERLAP,
    capture_exit_evidence,
    classify_deferral,
    get_worker_state,
    hard_attempt_count,
    put_worker_state,
    redrive_stranded_approvals,
)

WATCH_INTERVAL_SECONDS = 600  # the real launchd cadence
OBSERVATION_WINDOW_SECONDS = 48 * 3600  # a mid-range governance window


@pytest.fixture()
def store(tmp_path: Path) -> SqliteReliabilityStore:
    db = tmp_path / "rel.sqlite3"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    migrate_connection(conn)
    conn.close()
    return SqliteReliabilityStore(str(db))


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _approved(store: SqliteReliabilityStore, *, at: datetime, title: str = "fix") -> str:
    """An improvement sitting in ``approved`` with a stage anchor of *at*."""
    imp_id = store.create_improvement("audit", "fix", title)
    store._connection.execute(
        "UPDATE improvements SET status=?, version=version+1, updated_at=?, "
        "stage_started_at=? WHERE id=?",
        (ImprovementStatus.APPROVED.value, _iso(at), _iso(at), imp_id),
    )
    store._connection.commit()
    return imp_id


def _monitoring(
    store: SqliteReliabilityStore, *, monitor_until: datetime, title: str = "blocker"
) -> str:
    """A sibling improvement inside its observation window — the overlap blocker."""
    imp_id = store.create_improvement("audit", "fix", title)
    store._connection.execute(
        "UPDATE improvements SET status=?, version=version+1, monitor_until=? WHERE id=?",
        (ImprovementStatus.MONITORING.value, _iso(monitor_until), imp_id),
    )
    store._connection.commit()
    return imp_id


class _Clock:
    """Injected clock — no sleeps, exact cycle boundaries."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def tick(self, seconds: int = WATCH_INTERVAL_SECONDS) -> None:
        self.t += timedelta(seconds=seconds)


class _Applier:
    """apply_fn double: returns a scripted result, records every call.

    On a scripted success it performs the same CAS walk the real pipeline does
    (``approved → applying → applied``), because the re-driver's "exactly once"
    guarantee comes from the row leaving ``approved`` — a double that skipped the
    transition would prove nothing.
    """

    def __init__(
        self,
        *results: ApplyResult,
        default: ApplyResult | None = None,
        store: SqliteReliabilityStore | None = None,
    ) -> None:
        self.results = list(results)
        self.default = default
        self.store = store
        self.calls: list[str] = []

    def __call__(self, imp_id: str) -> ApplyResult:
        self.calls.append(imp_id)
        if self.results:
            result = self.results.pop(0)
        elif self.default is not None:
            result = self.default
        else:
            raise AssertionError("apply_fn called more times than the test scripted")
        if result.applied and self.store is not None:
            self.store.transition_improvement(
                imp_id,
                ImprovementStatus.APPROVED.value,
                ImprovementStatus.APPLYING.value,
                actor="test:apply",
            )
            self.store.transition_improvement(
                imp_id,
                ImprovementStatus.APPLYING.value,
                ImprovementStatus.APPLIED.value,
                actor="test:apply",
            )
        return result


def _deferring(reason: str) -> _Applier:
    return _Applier(default=ApplyResult(deferred=True, reason=reason))


class _Notes:
    """notify_fn double — the real callback is keyword-only, not a single dict."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def __call__(self, **kwargs) -> None:
        self.sent.append(kwargs)

    def __len__(self) -> int:
        return len(self.sent)

    def __getitem__(self, index: int) -> dict:
        return self.sent[index]


def _err(imp) -> dict:
    """``last_error_json`` comes back already decoded on some store paths."""
    raw = imp.last_error_json
    return raw if isinstance(raw, dict) else json.loads(raw or "{}")


def _anchor(store: SqliteReliabilityStore, imp_id: str, *, at: datetime) -> None:
    put_worker_state(store, imp_id, last_spawn_at=_iso(at), spawn_attempts=1, running=False)


def _cycle(
    store: SqliteReliabilityStore,
    *,
    clock: _Clock,
    apply_fn=None,
    spawn_fn=None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    notify_fn=None,
    owner: str = "sched",
) -> dict:
    """One scheduled re-drive pass at the current clock instant."""
    return redrive_stranded_approvals(
        store,
        apply_fn=apply_fn,
        spawn_fn=spawn_fn,
        grace_seconds=0,
        max_attempts=max_attempts,
        owner=owner,
        clock=clock,
        notify_fn=notify_fn,
    )


# --- the blocker itself -----------------------------------------------------


def test_overlap_survives_far_more_than_max_attempts_scheduled_cycles(
    store: SqliteReliabilityStore,
) -> None:
    """Ten watch cycles of legitimate overlap under max_attempts=3 must not fail the row.

    This is the regression: pre-repair, cycle 3 escalated approved→failed with a
    critical alert while the blocking observation window still had ~47 hours to run.
    """
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    monitor_until = start + timedelta(seconds=OBSERVATION_WINDOW_SECONDS)
    _monitoring(store, monitor_until=monitor_until)
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box = _deferring("overlap_with_monitoring")
    cycles = 10
    assert cycles > DEFAULT_MAX_ATTEMPTS

    for _ in range(cycles):
        summary = _cycle(store, clock=clock, apply_fn=box, max_attempts=DEFAULT_MAX_ATTEMPTS)
        assert summary["escalated"] == 0
        assert summary["soft_escalated"] == 0
        assert summary["deferred"] == 1
        assert summary["soft_deferred"] == 1
        assert store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value
        clock.tick()

    assert len(box.calls) == cycles
    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_DEFERRED
    assert hard_attempt_count(state) == 0
    assert int(store.get_improvement(imp_id).attempt or 0) == 0

    soft = state["soft_deferral"]
    assert soft["class"] == SOFT_MONITORING_OVERLAP
    assert soft["reason"] == "overlap_with_monitoring"
    assert soft["count"] == cycles
    # first_at is pinned to the FIRST deferral so the absolute cap cannot be reset.
    assert soft["first_at"] == _iso(start)
    assert soft["horizon_source"] == "monitor_until"
    assert soft["horizon"] == _iso(monitor_until + timedelta(seconds=OVERLAP_HORIZON_SLACK_SECONDS))


def test_soft_deferral_state_survives_restart(
    store: SqliteReliabilityStore, tmp_path: Path
) -> None:
    """The wait is durable: a fresh store handle on the same db keeps the same horizon."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    monitor_until = start + timedelta(seconds=OBSERVATION_WINDOW_SECONDS)
    _monitoring(store, monitor_until=monitor_until)
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box = _deferring("overlap_with_monitoring")
    _cycle(store, clock=clock, apply_fn=box)
    before = get_worker_state(store, imp_id)["soft_deferral"]

    # Restart: brand-new connection/handle, nothing carried in memory.
    db_path = str(tmp_path / "rel.sqlite3")
    restarted = SqliteReliabilityStore(db_path)
    after = get_worker_state(restarted, imp_id)["soft_deferral"]
    assert after == before

    clock.tick()
    summary = _cycle(restarted, clock=clock, apply_fn=box, owner="sched-after-restart")
    assert summary["escalated"] == 0
    assert summary["soft_deferred"] == 1
    resumed = get_worker_state(restarted, imp_id)["soft_deferral"]
    assert resumed["first_at"] == before["first_at"]  # budget not restarted by the restart
    assert resumed["count"] == before["count"] + 1
    assert restarted.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value


def test_overlap_release_applies_exactly_once(store: SqliteReliabilityStore) -> None:
    """When the blocker clears, the next cycle applies once and stops re-driving."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    blocker = _monitoring(
        store, monitor_until=start + timedelta(seconds=OBSERVATION_WINDOW_SECONDS)
    )
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box = _Applier(
        ApplyResult(deferred=True, reason="overlap_with_monitoring"),
        ApplyResult(deferred=True, reason="overlap_with_monitoring"),
        ApplyResult(applied=True, applied_sha="deadbeef", reason="ok"),
        store=store,
    )

    for _ in range(2):
        assert _cycle(store, clock=clock, apply_fn=box)["soft_deferred"] == 1
        clock.tick()

    # Blocker leaves its observation window.
    store.transition_improvement(
        blocker,
        ImprovementStatus.MONITORING.value,
        ImprovementStatus.CONFIRMED.value,
        actor="test",
    )

    summary = _cycle(store, clock=clock, apply_fn=box)
    assert summary["applied"] == 1
    assert summary["soft_deferred"] == 0
    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_APPLIED
    assert state["applied_sha"] == "deadbeef"
    assert state["soft_deferral"] is None  # wait record cleared on success

    # Exactly once: the row is no longer approved, so later cycles do not re-apply.
    finished_at = state["finished_at"]
    for _ in range(3):
        clock.tick()
        again = _cycle(store, clock=clock, apply_fn=box)
        assert again["applied"] == 0
        assert again["redriven"] == 0
    assert len(box.calls) == 3
    assert get_worker_state(store, imp_id)["finished_at"] == finished_at


# --- hard failures must still exhaust --------------------------------------


def test_hard_spawn_failures_still_exhaust_the_budget(store: SqliteReliabilityStore) -> None:
    """Separating soft accounting must not blunt real failures: 3 bad spawns still fail."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    def _spawn(command: str, improvement_id: str) -> dict:
        raise OSError("No such file or directory: /no/such/python")

    notes = _Notes()
    escalated_on = None
    for cycle in range(1, DEFAULT_MAX_ATTEMPTS + 1):
        summary = _cycle(store, clock=clock, spawn_fn=_spawn, notify_fn=notes, owner=f"s{cycle}")
        assert summary["spawn_failed"] == 1
        if summary["escalated"]:
            escalated_on = cycle
            break
        assert store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value
        clock.tick()

    assert escalated_on == DEFAULT_MAX_ATTEMPTS
    assert store.get_improvement(imp_id).status == ImprovementStatus.FAILED.value
    state = get_worker_state(store, imp_id)
    assert hard_attempt_count(state) == DEFAULT_MAX_ATTEMPTS
    assert state["outcome"] == OUTCOME_ESCALATED
    assert notes and notes[0]["severity"] == "critical"


def test_unrecognised_deferral_reason_is_accounted_hard(store: SqliteReliabilityStore) -> None:
    """An unknown "not yet" has no derivable horizon, so it must exhaust, not stall."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)
    assert classify_deferral("waiting_on_something_new") is None

    box = _deferring("waiting_on_something_new")
    for _ in range(DEFAULT_MAX_ATTEMPTS):
        summary = _cycle(store, clock=clock, apply_fn=box)
        assert summary["soft_deferred"] == 0
        clock.tick()

    assert hard_attempt_count(get_worker_state(store, imp_id)) == DEFAULT_MAX_ATTEMPTS
    final = _cycle(store, clock=clock, apply_fn=box)
    assert final["escalated"] == 1
    assert store.get_improvement(imp_id).status == ImprovementStatus.FAILED.value


# --- bounded horizon: expiry and malformed state ---------------------------


def test_overlap_horizon_expiry_escalates_visibly(store: SqliteReliabilityStore) -> None:
    """A blocker stuck past its own window fails VISIBLY — never an infinite silent wait."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    window = 3600
    _monitoring(store, monitor_until=start + timedelta(seconds=window))
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box = _deferring("overlap_with_monitoring")
    notes = _Notes()
    horizon = start + timedelta(seconds=window + OVERLAP_HORIZON_SLACK_SECONDS)

    escalated = None
    for _ in range(40):
        summary = _cycle(store, clock=clock, apply_fn=box, notify_fn=notes)
        if summary["escalated"]:
            escalated = clock.t
            assert summary["soft_escalated"] == 1
            break
        assert store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value
        clock.tick()

    assert escalated is not None, "horizon must be bounded — it never expired"
    assert escalated >= horizon
    # It waited the real window, not three cycles.
    assert (escalated - start).total_seconds() >= window

    imp = store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.FAILED.value
    err = _err(imp)
    assert err["reason"] == ESCALATION_OVERLAP_HORIZON_EXPIRED
    assert err["detail"]["class"] == SOFT_MONITORING_OVERLAP
    assert err["detail"]["horizon_source"] == "monitor_until"
    assert notes[-1]["severity"] == "critical"
    assert notes[-1]["payload"]["reason"] == ESCALATION_OVERLAP_HORIZON_EXPIRED
    assert "monitoring-overlap horizon" in notes[-1]["title"]
    assert get_worker_state(store, imp_id)["outcome"] == OUTCOME_ESCALATED


@pytest.mark.parametrize(
    ("record", "defect"),
    [
        ("not-a-dict", "not_a_mapping"),
        ({"class": "something_else", "horizon": "2999-01-01T00:00:00Z"}, "unknown_class"),
        (
            {"class": SOFT_MONITORING_OVERLAP, "first_at": "nonsense", "horizon": "nonsense"},
            "unparseable_timestamps",
        ),
        (
            {
                "class": SOFT_LEASE_CONTENTION,
                "first_at": "2026-01-01T00:00:00Z",
                "horizon": "2029-01-01T00:00:00Z",  # far beyond the 2 h class cap
            },
            "horizon_exceeds_cap",
        ),
    ],
)
def test_malformed_or_unbounded_soft_state_escalates(
    store: SqliteReliabilityStore, record: object, defect: str
) -> None:
    """A deferral record we cannot trust is an escalation, never an unbounded wait."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)
    put_worker_state(store, imp_id, soft_deferral=record)

    box = _Applier()  # must NOT be probed — the state is escalated before any apply
    notes = _Notes()
    summary = _cycle(store, clock=clock, apply_fn=box, notify_fn=notes)

    assert summary["escalated"] == 1
    assert summary["soft_escalated"] == 1
    assert box.calls == []
    imp = store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.FAILED.value
    err = _err(imp)
    assert err["reason"] == ESCALATION_SOFT_STATE_INVALID
    assert err["detail"]["defect"] == defect
    assert notes[-1]["severity"] == "critical"
    assert get_worker_state(store, imp_id)["soft_deferral"] is None


# --- lease contention is decided on its own, much shorter, budget ----------


def test_lease_contention_uses_short_budget_not_the_monitoring_horizon(
    store: SqliteReliabilityStore,
) -> None:
    """``apply_lease_held`` must never inherit a 48 h monitoring wait."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    # A long observation window exists but is irrelevant to lease contention.
    _monitoring(store, monitor_until=start + timedelta(seconds=OBSERVATION_WINDOW_SECONDS))
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box = _deferring("apply_lease_held")
    summary = _cycle(store, clock=clock, apply_fn=box)
    assert summary["soft_deferred"] == 1
    soft = get_worker_state(store, imp_id)["soft_deferral"]
    assert soft["class"] == SOFT_LEASE_CONTENTION
    assert soft["horizon_source"] == "contention_budget"
    assert soft["horizon"] == _iso(start + timedelta(seconds=LEASE_CONTENTION_BUDGET_SECONDS))

    escalated = None
    for _ in range(200):
        clock.tick()
        if _cycle(store, clock=clock, apply_fn=box)["escalated"]:
            escalated = clock.t
            break

    assert escalated is not None
    waited = (escalated - start).total_seconds()
    assert waited >= LEASE_CONTENTION_BUDGET_SECONDS
    assert waited <= MAX_LEASE_CONTENTION_SECONDS + WATCH_INTERVAL_SECONDS
    assert waited < OBSERVATION_WINDOW_SECONDS  # decided separately, not conflated
    imp = store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.FAILED.value
    assert _err(imp)["reason"] == ESCALATION_LEASE_CONTENTION_EXHAUSTED


def test_lease_contention_prefers_authoritative_lease_expiry(
    store: SqliteReliabilityStore,
) -> None:
    """The live ``reliability:apply`` lease row is the authoritative timestamp."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)
    store.acquire_lease(APPLY_LEASE_KEY, owner="other-apply", duration_seconds=1800)

    box = _deferring("apply_lease_held")
    _cycle(store, clock=clock, apply_fn=box)
    soft = get_worker_state(store, imp_id)["soft_deferral"]
    assert soft["horizon_source"] == "apply_lease_expiry"
    # Bounded by the contention floor and the 2 h cap either way.
    horizon = datetime.fromisoformat(soft["horizon"].replace("Z", "+00:00"))
    assert (horizon - start).total_seconds() <= MAX_LEASE_CONTENTION_SECONDS


# --- exit evidence is recorded once per spawn generation -------------------


def test_dead_worker_exit_recorded_once_not_every_sweep(
    store: SqliteReliabilityStore, tmp_path: Path
) -> None:
    """``exited`` is hard evidence, so re-recording it each sweep would fake a budget burn."""
    start = datetime.now(UTC).replace(microsecond=0)
    imp_id = _approved(store, at=start)
    log = tmp_path / "worker.log"
    log.write_text("worker vanished\n", encoding="utf-8")
    put_worker_state(
        store, imp_id, pid=424242, log=str(log), outcome="spawned", running=True, exit_code=None
    )

    for _ in range(5):
        state = capture_exit_evidence(store, imp_id, pid_alive_fn=lambda _pid: False)

    assert state["outcome"] == OUTCOME_EXITED
    assert state["running"] is False
    assert hard_attempt_count(state) == 1


def test_soft_deferred_worker_exit_is_not_re_recorded_as_exited(
    store: SqliteReliabilityStore,
) -> None:
    """A detached worker that deferred keeps its soft record across later sweeps."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    _monitoring(store, monitor_until=start + timedelta(seconds=OBSERVATION_WINDOW_SECONDS))
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box = _deferring("overlap_with_monitoring")
    _cycle(store, clock=clock, apply_fn=box)
    # Pretend the deferral came from a detached worker whose PID is now gone.
    put_worker_state(store, imp_id, pid=424242, running=True)
    soft_before = get_worker_state(store, imp_id)["soft_deferral"]

    for _ in range(4):
        capture_exit_evidence(store, imp_id, pid_alive_fn=lambda _pid: False)

    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_DEFERRED
    assert hard_attempt_count(state) == 0
    assert state["soft_deferral"] == soft_before
    assert store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value


def test_alternating_soft_deferrals_do_not_reset_first_at_and_expire_on_cap(
    store: SqliteReliabilityStore,
) -> None:
    """Alternating overlap and lease deferrals must not reset per-class first_at.

    Simulates alternating deferrals over time. Lease contention cap is 2 hours (7200s).
    Even when alternating with overlap_with_monitoring, lease contention MUST expire
    when 2 hours from its first occurrence pass.
    """
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    _monitoring(store, monitor_until=start + timedelta(days=5))
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    reasons = ["overlap_with_monitoring", "apply_lease_held"]
    box = _Applier(
        *[ApplyResult(deferred=True, reason=reasons[i % 2]) for i in range(1440)],
        store=store,
    )

    escalated = None
    for _ in range(1440):
        summary = _cycle(store, clock=clock, apply_fn=box)
        if summary["escalated"]:
            escalated = clock.t
            break
        assert store.get_improvement(imp_id).status == ImprovementStatus.APPROVED.value
        clock.tick()

    assert escalated is not None, "Alternating soft deferrals must not evade the cap indefinitely"
    waited = (escalated - start).total_seconds()
    assert waited >= LEASE_CONTENTION_BUDGET_SECONDS
    assert waited <= MAX_LEASE_CONTENTION_SECONDS + WATCH_INTERVAL_SECONDS * 2

    imp = store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.FAILED.value
    err = _err(imp)
    assert err["reason"] == ESCALATION_LEASE_CONTENTION_EXHAUSTED


def test_detached_worker_exit_persistence_atomic(
    store: SqliteReliabilityStore, tmp_path: Path
) -> None:
    """exit_recorded_pid is persisted atomically in the initial exit write.

    Eliminates crash gap between recording worker exit and writing exit_recorded_pid.
    """
    start = datetime.now(UTC).replace(microsecond=0)
    imp_id = _approved(store, at=start)
    log = tmp_path / "worker.log"
    log.write_text("worker crashed\n", encoding="utf-8")
    put_worker_state(
        store, imp_id, pid=999888, log=str(log), outcome="spawned", running=True, exit_code=None
    )

    # First capture: records exit AND persists exit_recorded_pid atomically
    state = capture_exit_evidence(store, imp_id, pid_alive_fn=lambda _pid: False)
    assert state["outcome"] == OUTCOME_EXITED
    assert state["running"] is False
    assert state.get("exit_recorded_pid") == 999888
    assert hard_attempt_count(state) == 1

    # Verify directly from DB store
    persisted = get_worker_state(store, imp_id)
    assert persisted.get("exit_recorded_pid") == 999888

    # Repeat capture 10 times (subsequent sweeps / restart)
    for _ in range(10):
        s = capture_exit_evidence(store, imp_id, pid_alive_fn=lambda _pid: False)
        assert hard_attempt_count(s) == 1


def test_expired_soft_horizon_probes_live_and_applies_when_blocker_cleared(
    store: SqliteReliabilityStore,
) -> None:
    """An expired persisted soft horizon performs a final probe and applies if cleared."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    _monitoring(store, monitor_until=start + timedelta(seconds=1800))
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box_defer = _deferring("overlap_with_monitoring")
    _cycle(store, clock=clock, apply_fn=box_defer)
    assert get_worker_state(store, imp_id)["soft_deferral"] is not None

    # Horizon is monitor_until (1800s) + OVERLAP_HORIZON_SLACK_SECONDS (600s) = 2400s
    clock.t += timedelta(seconds=3000)

    box_apply = _Applier(ApplyResult(applied=True, applied_sha="abc1234"), store=store)
    summary = _cycle(store, clock=clock, apply_fn=box_apply)

    assert summary["applied"] == 1
    assert summary["escalated"] == 0
    assert box_apply.calls == [imp_id]
    state = get_worker_state(store, imp_id)
    assert state["outcome"] == OUTCOME_APPLIED
    assert state["soft_deferral"] is None


def test_expired_soft_horizon_probes_live_and_escalates_when_overlap_persists(
    store: SqliteReliabilityStore,
) -> None:
    """An expired soft horizon performs a final probe; if still deferred, escalates."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    _monitoring(store, monitor_until=start + timedelta(seconds=1800))
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box = _deferring("overlap_with_monitoring")
    _cycle(store, clock=clock, apply_fn=box)

    # Horizon is monitor_until (1800s) + OVERLAP_HORIZON_SLACK_SECONDS (600s) = 2400s
    clock.t += timedelta(seconds=3000)

    summary = _cycle(store, clock=clock, apply_fn=box)

    assert summary["escalated"] == 1
    assert summary["soft_escalated"] == 1
    assert len(box.calls) == 2

    imp = store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.FAILED.value
    err = _err(imp)
    assert err["reason"] == ESCALATION_OVERLAP_HORIZON_EXPIRED
    assert err["detail"]["final_probe"] == "deferred"


def test_expired_soft_horizon_probes_live_and_escalates_when_lease_persists(
    store: SqliteReliabilityStore,
) -> None:
    """Expired lease contention performs a final live probe before escalating."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box = _deferring("apply_lease_held")
    _cycle(store, clock=clock, apply_fn=box)

    clock.t += timedelta(seconds=7300)

    summary = _cycle(store, clock=clock, apply_fn=box)

    assert summary["escalated"] == 1
    assert summary["soft_escalated"] == 1
    assert len(box.calls) == 2

    imp = store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.FAILED.value
    err = _err(imp)
    assert err["reason"] == ESCALATION_LEASE_CONTENTION_EXHAUSTED
    assert err["detail"]["final_probe"] == "deferred"


def test_restart_preserves_alternating_class_anchors(
    store: SqliteReliabilityStore,
) -> None:
    """Alternating deferral class history survives DB reload/restart."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)

    _monitoring(store, monitor_until=start + timedelta(days=5))
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box1 = _deferring("overlap_with_monitoring")
    _cycle(store, clock=clock, apply_fn=box1)

    clock.t += timedelta(seconds=600)
    box2 = _deferring("apply_lease_held")
    _cycle(store, clock=clock, apply_fn=box2)

    restarted = SqliteReliabilityStore(store._db_path)
    state = get_worker_state(restarted, imp_id)
    soft = state.get("soft_deferral")
    assert soft is not None
    assert soft["first_at_by_class"][SOFT_MONITORING_OVERLAP] == _iso(start)
    assert soft["first_at_by_class"][SOFT_LEASE_CONTENTION] == _iso(start + timedelta(seconds=600))


def test_soft_deferrals_do_not_poison_hard_attempts_budget(
    store: SqliteReliabilityStore,
) -> None:
    """Repeated soft deferrals never increment hard_attempts or poison failure budget."""
    start = datetime.now(UTC).replace(microsecond=0)
    clock = _Clock(start)
    _monitoring(store, monitor_until=start + timedelta(days=5))
    imp_id = _approved(store, at=start)
    _anchor(store, imp_id, at=start)

    box = _deferring("overlap_with_monitoring")
    for _ in range(50):
        _cycle(store, clock=clock, apply_fn=box)
        clock.tick()
        state = get_worker_state(store, imp_id)
        assert hard_attempt_count(state) == 0

    imp = store.get_improvement(imp_id)
    assert imp.status == ImprovementStatus.APPROVED.value
