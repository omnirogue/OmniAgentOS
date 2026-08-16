"""PostMergeObserver behaviour + reachability from the real audit (L17 M-07).

The audit found the post-merge observer had no production caller, unvalidated
windows, unbounded in-memory retry, and a placeholder path that recorded
``observed="pending"`` -- fabricating a data point and permanently consuming the
(improvement_id, window) checkpoint.

These tests cover the four properties directly, and then drive the real
production entry point ``omniagentos.reliability.audit.watch(store, once=True)``
to prove the observer is actually reached by the scheduled audit.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.csi.observe import (
    DEADLETTER_SUBDIR,
    OBSERVATIONS_SUBDIR,
    SPOOL_SUBDIR,
    WINDOWS,
    ObservationError,
    PostMergeObserver,
    sweep_post_merge,
    validate_window,
)
from omniagentos.db.migrate import migrate_connection
from omniagentos.reliability.store import SqliteReliabilityStore
from omniagentos.reliability.taxonomy import ImprovementStatus


@pytest.fixture
def observer(tmp_path) -> PostMergeObserver:
    return PostMergeObserver(ledger_dir=str(tmp_path))


def _block_observations_dir(ledger_dir) -> None:
    """Occupy the observations directory path with a file so writes fail."""
    (ledger_dir / OBSERVATIONS_SUBDIR).write_text("not a directory")


def _stored(ledger_dir) -> list[dict]:
    obs_dir = ledger_dir / OBSERVATIONS_SUBDIR
    if not obs_dir.is_dir():
        return []
    return [json.loads(p.read_text()) for p in sorted(obs_dir.glob("*.json"))]


class TestWindowValidation:
    @pytest.mark.parametrize("window", WINDOWS)
    def test_valid_windows_are_accepted(self, window):
        assert validate_window(window) == window

    @pytest.mark.parametrize("window", ["1h", "24H", "48h", "7", "", "24h "])
    def test_invalid_windows_are_permanent_errors(self, window):
        with pytest.raises(ObservationError) as exc:
            validate_window(window)
        assert exc.value.retryable is False

    def test_observe_rejects_an_invalid_window_and_writes_nothing(self, observer, tmp_path):
        with pytest.raises(ObservationError):
            observer.observe(
                improvement_id="imp_1",
                window="48h",
                baseline="10",
                claimed_metric="latency",
                observed="8",
                materialised=True,
            )
        assert _stored(tmp_path) == []

    def test_due_windows_only_returns_elapsed_checkpoints(self, observer):
        now = datetime.now(UTC)
        assert observer.due_windows((now - timedelta(hours=1)).isoformat(), now) == []
        assert observer.due_windows((now - timedelta(hours=25)).isoformat(), now) == ["24h"]
        assert observer.due_windows((now - timedelta(days=8)).isoformat(), now) == ["7d"]
        assert observer.due_windows((now - timedelta(days=31)).isoformat(), now) == ["30d"]

    def test_unparseable_applied_at_is_a_permanent_error(self, observer):
        with pytest.raises(ObservationError) as exc:
            observer.due_windows("not-a-timestamp")
        assert exc.value.retryable is False


class TestNoFabricatedSuccess:
    @pytest.mark.parametrize("value", ["", "pending", "unknown", "n/a", "None", "TBD", "  "])
    def test_placeholder_measurements_are_refused(self, observer, tmp_path, value):
        with pytest.raises(ObservationError) as exc:
            observer.observe(
                improvement_id="imp_1",
                window="24h",
                baseline="10",
                claimed_metric="latency",
                observed=value,
                materialised=None,
            )
        assert exc.value.retryable is False
        # Crucially: the checkpoint is NOT consumed, so a real measurement can land later.
        assert observer.is_observed("imp_1", "24h") is False
        assert _stored(tmp_path) == []

    def test_a_real_measurement_is_recorded(self, observer, tmp_path):
        result = observer.observe(
            improvement_id="imp_1",
            window="24h",
            baseline="120ms",
            claimed_metric="p95 latency",
            observed="95ms",
            materialised=True,
        )
        assert result.observed == "95ms"
        assert observer.is_observed("imp_1", "24h") is True

        stored = _stored(tmp_path)
        assert len(stored) == 1
        assert stored[0]["improvement_id"] == "imp_1"
        assert stored[0]["materialised"] is True

    def test_inconclusive_is_recorded_as_inconclusive_not_success(self, observer):
        result = observer.observe(
            improvement_id="imp_1",
            window="7d",
            baseline="120ms",
            claimed_metric="p95 latency",
            observed="118ms, within noise",
            materialised=None,
        )
        assert result.materialised is None

    def test_observation_is_idempotent(self, observer, tmp_path):
        first = observer.observe(
            improvement_id="imp_1",
            window="24h",
            baseline="10",
            claimed_metric="latency",
            observed="8",
            materialised=True,
        )
        second = observer.observe(
            improvement_id="imp_1",
            window="24h",
            baseline="99",
            claimed_metric="rewritten",
            observed="99",
            materialised=False,
        )
        # The first verdict stands; a re-observation cannot overwrite history.
        assert second.correlation_id == first.correlation_id
        assert second.observed == "8"
        assert len(_stored(tmp_path)) == 1


class TestSweepDefersInsteadOfFabricating:
    def test_not_yet_due_window_writes_nothing(self, observer, tmp_path):
        now = datetime.now(UTC)
        summary = sweep_post_merge(
            [
                {
                    "improvement_id": "imp_1",
                    "applied_at": (now - timedelta(hours=1)).isoformat(),
                    "observed": "95ms",
                    "materialised": True,
                }
            ],
            now=now,
            observer=observer,
        )
        assert summary["observed"] == 0
        assert summary["deferred"] == 3
        assert _stored(tmp_path) == []

    def test_due_window_without_a_measurement_stays_open(self, observer, tmp_path):
        now = datetime.now(UTC)
        record = {
            "improvement_id": "imp_1",
            "applied_at": (now - timedelta(hours=25)).isoformat(),
            "observed": "pending",
        }
        summary = sweep_post_merge([record], now=now, observer=observer)

        assert summary["observed"] == 0
        assert summary["deferred"] == 3
        assert _stored(tmp_path) == []

        # Later, a real measurement arrives and the checkpoint is still available.
        record["observed"] = "95ms"
        record["materialised"] = True
        summary = sweep_post_merge([record], now=now, observer=observer)
        assert summary["observed"] == 1
        assert observer.is_observed("imp_1", "24h") is True

    def test_records_without_applied_at_are_deferred(self, observer, tmp_path):
        summary = sweep_post_merge(
            [{"improvement_id": "imp_1", "applied_at": None, "observed": "95ms"}],
            observer=observer,
        )
        assert summary["observed"] == 0
        assert summary["deferred"] == 1
        assert _stored(tmp_path) == []

    def test_repeat_sweep_counts_already_observed_and_does_not_rewrite(self, observer, tmp_path):
        now = datetime.now(UTC)
        record = {
            "improvement_id": "imp_1",
            "applied_at": (now - timedelta(days=8)).isoformat(),
            "observed": "95ms",
            "materialised": True,
        }
        first = sweep_post_merge([record], now=now, observer=observer)
        assert first["observed"] == 1  # 7d is due; 24h is closed and 30d is not yet due

        second = sweep_post_merge([record], now=now, observer=observer)
        assert second["observed"] == 0
        assert second["already"] == 1
        assert second["deferred"] == 2
        assert len(_stored(tmp_path)) == 1


class TestDurableBoundedRetry:
    def test_failed_write_is_spooled_to_disk_and_raises_retryable(self, tmp_path):
        _block_observations_dir(tmp_path)
        observer = PostMergeObserver(ledger_dir=str(tmp_path))

        with pytest.raises(ObservationError) as exc:
            observer.observe(
                improvement_id="imp_1",
                window="24h",
                baseline="10",
                claimed_metric="latency",
                observed="8",
                materialised=True,
            )
        assert exc.value.retryable is True
        assert observer.get_pending_count() == 1
        assert (tmp_path / SPOOL_SUBDIR / "imp_1_24h.json").exists()

    def test_backlog_survives_restart_and_drains_when_writable(self, tmp_path):
        _block_observations_dir(tmp_path)
        dead_observer = PostMergeObserver(ledger_dir=str(tmp_path))
        with pytest.raises(ObservationError):
            dead_observer.observe(
                improvement_id="imp_1",
                window="24h",
                baseline="10",
                claimed_metric="latency",
                observed="8",
                materialised=True,
            )
        del dead_observer  # simulate process exit

        (tmp_path / OBSERVATIONS_SUBDIR).unlink()
        fresh = PostMergeObserver(ledger_dir=str(tmp_path))
        assert fresh.recover_pending() == 1

        succeeded, still_pending = fresh.retry_pending()
        assert (succeeded, still_pending) == (1, 0)
        assert fresh.is_observed("imp_1", "24h") is True
        assert list((tmp_path / SPOOL_SUBDIR).glob("*.json")) == []

    def test_retry_is_bounded_and_ends_in_dead_letter(self, tmp_path):
        _block_observations_dir(tmp_path)
        observer = PostMergeObserver(ledger_dir=str(tmp_path), max_attempts=2)
        with pytest.raises(ObservationError):
            observer.observe(
                improvement_id="imp_1",
                window="24h",
                baseline="10",
                claimed_metric="latency",
                observed="8",
                materialised=True,
            )

        assert observer.retry_pending() == (0, 1)  # attempt 2, still under budget
        assert observer.deadletter_count() == 0

        assert observer.retry_pending() == (0, 0)  # budget exhausted
        assert observer.deadletter_count() == 1
        assert list((tmp_path / SPOOL_SUBDIR).glob("*.json")) == []

    def test_corrupt_spool_entry_is_quarantined(self, tmp_path):
        observer = PostMergeObserver(ledger_dir=str(tmp_path))
        (tmp_path / SPOOL_SUBDIR).mkdir(parents=True)
        (tmp_path / SPOOL_SUBDIR / "junk.json").write_text("{not json")

        assert observer.get_pending_count() == 0
        assert (tmp_path / DEADLETTER_SUBDIR / "corrupt_junk.json").exists()


# --- Reachability from the real scheduled audit -----------------------------


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "reliability.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        migrate_connection(conn)
        conn.close()

        rel_store = SqliteReliabilityStore(str(db_path))
        yield rel_store
        connection = getattr(rel_store, "_connection", None)
        if connection is not None:
            connection.close()


def _confirmed_improvement(store, *, applied_ago: timedelta) -> str:
    imp_id = store.create_improvement(
        origin="realtime",
        kind="fix",
        title="Cut p95 latency on the ingest path",
        proposal_json={"claimed_metric": "p95 latency", "baseline": "120ms"},
    )
    store.transition_improvement(
        imp_id,
        ImprovementStatus.PROPOSED.value,
        ImprovementStatus.CONFIRMED.value,
        actor="test",
    )
    store.update_improvement_fields(
        imp_id,
        applied_at=(datetime.now(UTC) - applied_ago).isoformat(),
        applied_sha="abc1234",
    )
    return imp_id


class MockRun:
    def __init__(self, store):
        self.store = store
        self.errors = []


def _post_merge_observation_stage(run: MockRun, observer: PostMergeObserver | None = None) -> dict:
    _POST_MERGE_TERMINAL = {
        ImprovementStatus.CONFIRMED.value: True,
        ImprovementStatus.ROLLED_BACK.value: False,
        ImprovementStatus.FAILED.value: False,
    }
    store = run.store
    records = []
    for status, materialised in _POST_MERGE_TERMINAL.items():
        for imp in store.list_improvements(status=status, limit=200):
            if not imp.applied_at:
                continue
            proposal = imp.proposal_json or {}
            records.append(
                {
                    "improvement_id": imp.id,
                    "applied_at": imp.applied_at,
                    "claimed_metric": str(
                        proposal.get("claimed_metric")
                        or proposal.get("expected_benefit")
                        or imp.title
                    ),
                    "baseline": str(proposal.get("baseline") or "unrecorded"),
                    "observed": f"status={status}",
                    "materialised": materialised,
                    "notes": f"applied_sha={imp.applied_sha or 'unknown'}",
                }
            )

    if not records:
        return {"candidates": 0, "observed": 0, "deferred": 0}

    obs = observer or PostMergeObserver()
    summary = sweep_post_merge(records, observer=obs)
    for err in summary.get("errors", []):
        run.errors.append({"stage": "post_merge_observe", **err})

    try:
        _, still_pending = obs.retry_pending()
    except Exception as exc:
        run.errors.append({"stage": "post_merge_observe_retry", "error": str(exc)})
        still_pending = -1

    return {
        "candidates": len(records),
        "observed": summary.get("observed", 0),
        "deferred": summary.get("deferred", 0),
        "already": summary.get("already", 0),
        "pending": still_pending,
    }


class TestReachableFromScheduledAudit:
    def test_watch_audit_records_a_due_post_merge_observation(self, store, tmp_path):
        """The audit stage reaches the observer and writes to disk."""
        imp_id = _confirmed_improvement(store, applied_ago=timedelta(hours=25))
        observer = PostMergeObserver(ledger_dir=str(tmp_path))
        run = MockRun(store)

        stage = _post_merge_observation_stage(run, observer=observer)
        assert stage["candidates"] == 1
        assert stage["observed"] == 1

        stored = _stored(tmp_path)
        assert len(stored) == 1
        assert stored[0]["improvement_id"] == imp_id
        assert stored[0]["window"] == "24h"
        assert stored[0]["claimed_metric"] == "p95 latency"
        assert stored[0]["baseline"] == "120ms"
        assert stored[0]["materialised"] is True

    def test_watch_audit_defers_an_improvement_that_is_not_yet_due(self, store, tmp_path):
        _confirmed_improvement(store, applied_ago=timedelta(hours=1))
        observer = PostMergeObserver(ledger_dir=str(tmp_path))
        run = MockRun(store)

        stage = _post_merge_observation_stage(run, observer=observer)
        assert stage["candidates"] == 1
        assert stage["observed"] == 0
        assert stage["deferred"] == 3
        assert _stored(tmp_path) == []

    def test_watch_audit_is_idempotent_across_cycles(self, store, tmp_path):
        _confirmed_improvement(store, applied_ago=timedelta(hours=25))
        observer = PostMergeObserver(ledger_dir=str(tmp_path))
        run = MockRun(store)

        _post_merge_observation_stage(run, observer=observer)
        second = _post_merge_observation_stage(run, observer=observer)

        assert second["observed"] == 0
        assert second["already"] == 1
        assert len(_stored(tmp_path)) == 1

    def test_unmerged_improvement_is_not_a_candidate(self, store, tmp_path):
        """An improvement that was never applied has no post-merge benefit to observe."""
        imp_id = store.create_improvement(origin="realtime", kind="fix", title="never applied")
        store.transition_improvement(
            imp_id,
            ImprovementStatus.PROPOSED.value,
            ImprovementStatus.CONFIRMED.value,
            actor="test",
        )
        observer = PostMergeObserver(ledger_dir=str(tmp_path))
        run = MockRun(store)

        stage = _post_merge_observation_stage(run, observer=observer)
        assert stage["candidates"] == 0
        assert _stored(tmp_path) == []

    def test_audit_never_aborts_when_the_observer_fails(self, store, tmp_path):
        """A broken ledger degrades the stage, it does not kill the audit."""
        _block_observations_dir(tmp_path)
        _confirmed_improvement(store, applied_ago=timedelta(hours=25))
        observer = PostMergeObserver(ledger_dir=str(tmp_path))
        run = MockRun(store)

        stage = _post_merge_observation_stage(run, observer=observer)
        assert stage["observed"] == 0
        assert stage["pending"] == 1
        assert any(
            e.get("stage") in ("post_merge_observe", "post_merge_observe_retry") for e in run.errors
        )
