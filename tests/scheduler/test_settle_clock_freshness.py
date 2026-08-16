"""A gate that runs longer than the freshness tolerance settles on its EXIT CODE.

Settlement executes the gate (``settle_pending`` is the only code that runs
``gate_config.command``), so real time — minutes of it, up to the runner's 900s
timeout — passes between the batch's reference instant and the moment its
receipt is judged. ``PytestGateRunner`` stamps that receipt with wall-clock at
completion. Judging it against the instant the batch STARTED therefore makes
every gate slower than the 60s anti-forgery window look future-dated, i.e.
forged, and a passing gate is settled ``gate_passed=0 / accepted=0 / adverse``
with "gate evidence is dated in the future".

Live proof this was not theoretical (production DB, 2026-08-01): routine_runs
741/742/744-749 on both always-on routines, all adverse on that note, while the
receipts they were judging say ``exit_code=0`` with 20 of 20 checks passed —
run_03c9eea448424cc5beea's gate ran 23:16:30Z→23:19:27Z (177s) and was judged
against 23:16:22Z.

The tests below are deterministic: nothing sleeps. Time is a `_WallClock` the
slow gate moves forward by exactly as long as it claims to have run, which is
what running for five minutes really does to every clock read after it.

The 60s window and the anti-forgery DIRECTION are correct and are pinned here
too: `test_a_receipt_dated_after_the_gate_finished_is_still_refused_as_forged`
fails if the future-check is ever loosened away.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omniagentos.contracts import RunState
from omniagentos.db.store import SqliteStore
from omniagentos.policy import load_policy
from omniagentos.scheduler.gate_evidence import GateEvidence, GateEvidenceStore
from omniagentos.scheduler.gate_runner import GateRunRequest
from omniagentos.scheduler.routines_settle import settle_pending
from omniagentos.scheduler.routines_tick import tick
from omniagentos.scheduler.store import RoutinesStore
from tests.routines.conftest import valid_routine_payload

# The evidence builder is deliberately shared with the settlement suite rather
# than copied: a receipt schema change must break both files at once, never one.
from tests.scheduler.test_routines_settle import NOW, _evidence_for
from tests.support.db_template import make_store

FINISHED_AT = "2026-01-01T09:01:00Z"
# Comfortably past the 60s future window in `gate_evidence.evidence_rejections`,
# and well inside `DEFAULT_TIMEOUT_SECONDS` (900) — this is an ordinary gate, not
# a pathological one. Live gates on main take 90-180s.
SLOW_GATE_SECONDS = 300


@pytest.fixture
def database(tmp_path: Path) -> SqliteStore:
    return make_store(SqliteStore, tmp_path / "settle_clock.db")


@pytest.fixture
def routines(database: SqliteStore) -> RoutinesStore:
    return RoutinesStore(database)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Only its absolute path matters here — the gate runner is a stub."""
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def evidence_store(tmp_path: Path) -> GateEvidenceStore:
    return GateEvidenceStore(tmp_path / "gate-evidence")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class _WallClock:
    """Real time, made deterministic: it moves only when work takes time."""

    def __init__(self, start: datetime) -> None:
        self._instant = start

    def __call__(self) -> datetime:
        return self._instant

    def advance(self, seconds: float) -> None:
        self._instant += timedelta(seconds=seconds)


class _SlowGateRunner:
    """A gate that takes `seconds` of wall-clock time to run.

    Stamps its receipt the way ``PytestGateRunner`` does — the clock when it
    started, the clock when it finished — and moves the wall clock forward by
    its own duration, because that is what a long gate does to every clock read
    downstream of it.
    """

    def __init__(
        self,
        store: GateEvidenceStore,
        wall: _WallClock,
        *,
        seconds: float = SLOW_GATE_SECONDS,
        **overrides: Any,
    ) -> None:
        self.store = store
        self.wall = wall
        self.seconds = seconds
        self.overrides = overrides
        self.calls = 0

    def run(self, request: GateRunRequest) -> GateEvidence:
        self.calls += 1
        started = _stamp(self.wall())
        self.wall.advance(self.seconds)
        return self.store.record(
            _evidence_for(
                request,
                started_at=started,
                finished_at=_stamp(self.wall()),
                **self.overrides,
            )
        )


class _ForgedFutureGateRunner:
    """A gate that finishes NOW and swears it finished five minutes from now.

    The clock does not move: no time passed, the receipt just says it did. This
    is the case the future-check exists for, and it must stay refused.
    """

    def __init__(self, store: GateEvidenceStore, wall: _WallClock, *, seconds: float) -> None:
        self.store = store
        self.wall = wall
        self.seconds = seconds
        self.calls = 0

    def run(self, request: GateRunRequest) -> GateEvidence:
        self.calls += 1
        ahead = _stamp(self.wall() + timedelta(seconds=self.seconds))
        return self.store.record(_evidence_for(request, started_at=ahead, finished_at=ahead))


def _fire_and_complete(database: SqliteStore, routines: RoutinesStore) -> dict[str, Any]:
    """One fired routine whose dispatched run has reached COMPLETED."""
    routine = routines.create_routine(
        valid_routine_payload(
            trigger_config={"cron": "* * * * *"},
            task_template={"title": "Slow gate", "harness": "mock"},
        )
    )
    fired = tick(database, load_policy(), now=NOW)["fired"][0]
    assert database.update_run(
        fired["run_id"],
        {
            "state": RunState.COMPLETED.value,
            "finished_at": FINISHED_AT,
            "output_json": json.dumps({"exit_code": 0}),
        },
    )
    return routine


def test_a_gate_slower_than_the_freshness_window_settles_on_its_real_exit_code(
    database: SqliteStore,
    routines: RoutinesStore,
    evidence_store: GateEvidenceStore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole defect in one assertion: a PASSING 5-minute gate is accepted.

    The batch's reference instant is NOW; the gate finishes 300s later and says
    so. Judged against NOW that receipt is 300s "in the future" and the routine
    is condemned for having a slow test suite. Judged against the clock at the
    moment of judgement — which is what the fix reads — it is 0s old.
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    routine = _fire_and_complete(database, routines)
    wall = _WallClock(NOW)
    runner = _SlowGateRunner(evidence_store, wall)

    result = settle_pending(
        database,
        now=NOW,
        clock=wall,
        evidence_store=evidence_store,
        gate_runner=runner,
        workspace=workspace,
    )

    settled = result["settled"][0]
    assert runner.calls == 1
    assert wall() - NOW == timedelta(seconds=SLOW_GATE_SECONDS), "the gate must have taken time"
    assert "dated in the future" not in str(settled["notes"]), (
        "a gate that ran longer than the tolerance was judged against a stale batch clock: "
        f"{settled['notes']}"
    )
    assert settled["gate_passed"] is True
    assert settled["accepted"] is True
    assert settled["stop_reason"] == "gate_passed"

    updated = routines.get_routine(routine["id"])
    assert updated is not None
    assert updated["accepted_runs"] == 1


def test_a_slow_gate_that_really_failed_still_settles_adverse_on_its_exit_code(
    database: SqliteStore,
    routines: RoutinesStore,
    evidence_store: GateEvidenceStore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: fixing the clock must not launder a red gate.

    A slow gate that exits non-zero is still adverse, and the recorded reason
    must name the EXIT CODE — the fact about the candidate — never the clock.
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    _fire_and_complete(database, routines)
    wall = _WallClock(NOW)
    runner = _SlowGateRunner(
        evidence_store,
        wall,
        exit_code=1,
        checks_collected=3,
        checks_passed=2,
        checks_failed=1,
    )

    settled = settle_pending(
        database,
        now=NOW,
        clock=wall,
        evidence_store=evidence_store,
        gate_runner=runner,
        workspace=workspace,
    )["settled"][0]

    assert settled["gate_passed"] is False
    assert settled["accepted"] is False
    assert settled["stop_reason"] == "gate_failed"
    assert "gate exited 1, expected 0" in str(settled["notes"])
    assert "dated in the future" not in str(settled["notes"])


def test_a_receipt_dated_after_the_gate_finished_is_still_refused_as_forged(
    database: SqliteStore,
    routines: RoutinesStore,
    evidence_store: GateEvidenceStore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading the clock LATER must not become "trust any timestamp".

    Here no time passes — the gate returns immediately and post-dates itself by
    five minutes. That is forgery, the anti-forgery check is what catches it,
    and it must keep catching it after the fix.
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    _fire_and_complete(database, routines)
    wall = _WallClock(NOW)
    runner = _ForgedFutureGateRunner(evidence_store, wall, seconds=SLOW_GATE_SECONDS)

    settled = settle_pending(
        database,
        now=NOW,
        clock=wall,
        evidence_store=evidence_store,
        gate_runner=runner,
        workspace=workspace,
    )["settled"][0]

    assert wall() == NOW, "the forged case must not advance the clock"
    assert settled["gate_passed"] is False
    assert settled["accepted"] is False
    assert "gate evidence is dated in the future" in str(settled["notes"])


def test_the_batch_stamp_is_not_the_judging_clock(
    database: SqliteStore,
    routines: RoutinesStore,
    evidence_store: GateEvidenceStore,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two clocks stay two clocks.

    ``now`` remains the batch's RECORD stamp — stable across the batch, and the
    fallback finish time for a row with none of its own — while freshness is
    judged against the clock at judgement time. A settled row therefore still
    carries the run's own ``finished_at``, unaffected by how long its gate ran.
    """
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    _fire_and_complete(database, routines)
    wall = _WallClock(NOW)

    settled = settle_pending(
        database,
        now=NOW,
        clock=wall,
        evidence_store=evidence_store,
        gate_runner=_SlowGateRunner(evidence_store, wall),
        workspace=workspace,
    )["settled"][0]

    assert settled["finished_at"] == FINISHED_AT
    assert settled["gate_passed"] is True
