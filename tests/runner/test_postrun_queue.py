from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.runner import core as runner_core
from omniagentos.runner.core import Runner
from tests.runner.test_state_machine import (
    FinalizationSpy,
    TrackingAdapter,
    agent_step,
    create_run,
    dependencies,
)


@pytest.fixture
def store(tmp_path: Path) -> SqliteStore:
    db_path = tmp_path / "runner.db"
    migrate(str(db_path))
    return SqliteStore(str(db_path))


@pytest.fixture
def var_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    root = tmp_path / "var"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(root))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(root))
    return root


def _runner(
    store: SqliteStore,
    tmp_path: Path,
    worker_id: str = "w1",
    stale_s: int = 30,
) -> Runner:
    return Runner(
        store,
        worker_id,
        dependencies=dependencies(TrackingAdapter(), FinalizationSpy()),
        ledger_dir=str(tmp_path / "ledger"),
        vault_dir=str(tmp_path / "vault"),
        workspace_base=str(tmp_path / "workspace"),
        stale_s=stale_s,
    )


def _wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not met before timeout")


def test_finalize_does_not_block_on_slow_wiki(
    monkeypatch: pytest.MonkeyPatch,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    _, run_id = create_run(store, [agent_step("work")])
    wiki_started = Event()
    release_wiki = Event()

    # The hold this test proves finalize does NOT wait on. Named, so the
    # assertion below can be stated in terms of it instead of a constant
    # calibrated on one machine.
    wiki_hold_seconds = 2.0

    def slow_wiki(run: dict[str, Any], vault_dir: str, artifacts: list[Any] | None = None) -> None:
        wiki_started.set()
        release_wiki.wait(wiki_hold_seconds)

    monkeypatch.setattr("omniagentos.vault_wiki.maybe_update_wiki", slow_wiki)
    runner = _runner(store, tmp_path)
    queue_path = runner._postrun_queue_path()

    started_at = time.monotonic()
    assert runner.tick()
    finalize_elapsed = time.monotonic() - started_at

    # CAUSAL, not calibrated. The claim is "finalize did not block on the
    # background wiki update", and the only hardware-independent evidence for
    # that is finishing well inside the hold it would otherwise have waited out.
    # `< 0.5` was a 16-P-core Mac's number: on a 2-vCPU CI runner it fails for
    # being slow, which is a fact about the runner, not about the code. Half the
    # hold keeps the claim unambiguous (a blocked finalize takes >= 2.0s) while
    # tolerating a 4x slower host.
    assert finalize_elapsed < wiki_hold_seconds / 2
    assert wiki_started.wait(1.0)
    queued = json.loads(queue_path.read_text(encoding="utf-8").splitlines()[0])
    assert queued["run_id"] == run_id
    assert queued["kind"] == "wiki_update"
    assert queued["db"] == runner._store_identity()
    assert queued["enqueued_at"].endswith("Z")

    release_wiki.set()
    runner.shutdown(timeout=1.0)


def test_daemon_drains_and_marks_success_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    _, run_id = create_run(store, [])
    calls: list[str] = []

    def wiki(run: dict[str, Any], vault_dir: str, artifacts: list[Any] | None = None) -> None:
        calls.append(str(run["id"]))

    monkeypatch.setattr("omniagentos.vault_wiki.maybe_update_wiki", wiki)
    runner = _runner(store, tmp_path)
    runner._enqueue_postrun_job(run_id, "wiki_update")
    _wait_until(lambda: calls == [run_id])
    runner.shutdown(timeout=1.0)

    done_path = runner._postrun_done_path()
    markers = [json.loads(line) for line in done_path.read_text(encoding="utf-8").splitlines()]
    assert len(markers) == 1
    assert markers[0]["offset"] == 0
    assert markers[0]["processed"] is True
    assert markers[0]["status"] == "processed"
    assert markers[0]["worker_id"] == "w1"
    assert markers[0]["completed_at"].endswith("Z")

    restarted = _runner(store, tmp_path, worker_id="w2")
    restarted.shutdown(timeout=1.0)
    assert calls == [run_id]


def test_restart_skips_completed_job_even_with_garbage_entry(
    monkeypatch: pytest.MonkeyPatch,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    _, run_id = create_run(store, [])
    runner = _runner(store, tmp_path, worker_id="first")
    queue_path = runner._postrun_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "run_id": run_id,
                        "kind": "wiki_update",
                        "db": runner._store_identity(),
                        "enqueued_at": "2026-07-23T12:00:00Z",
                    }
                ),
                "garbage",
                "",
            ]
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    def wiki(run: dict[str, Any], vault_dir: str, artifacts: list[Any] | None = None) -> None:
        calls.append(str(run["id"]))

    monkeypatch.setattr("omniagentos.vault_wiki.maybe_update_wiki", wiki)
    # Queue was written after construction; wake the daemon explicitly.
    runner._postrun_wake.set()
    runner._ensure_postrun_daemon()
    _wait_until(lambda: calls == [run_id])
    runner.shutdown(timeout=1.0)

    restarted = _runner(store, tmp_path, worker_id="restarted")
    restarted.shutdown(timeout=1.0)
    assert calls == [run_id]


def test_malformed_line_is_logged_and_drain_continues(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    _, run_id = create_run(store, [])
    runner = _runner(store, tmp_path)
    queue_path = runner._postrun_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(
        "not-json\n"
        + json.dumps(
            {
                "run_id": run_id,
                "kind": "wiki_update",
                "db": runner._store_identity(),
                "enqueued_at": "2026-07-23T12:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def wiki(run: dict[str, Any], vault_dir: str, artifacts: list[Any] | None = None) -> None:
        calls.append(str(run["id"]))

    monkeypatch.setattr("omniagentos.vault_wiki.maybe_update_wiki", wiki)
    caplog.set_level(logging.WARNING, logger=runner_core.__name__)
    # Wake the daemon that was started on construction if the queue already
    # existed empty; re-enqueue wake by ensuring daemon runs.
    runner._postrun_wake.set()
    runner._ensure_postrun_daemon()
    _wait_until(lambda: calls == [run_id])
    runner.shutdown(timeout=1.0)

    assert "skipped malformed post-run queue line" in caplog.text
    statuses = [
        json.loads(line)["status"]
        for line in runner._postrun_done_path().read_text(encoding="utf-8").splitlines()
    ]
    assert statuses == ["skipped", "processed"]


def test_postrun_queue_is_scoped_per_database(
    monkeypatch: pytest.MonkeyPatch,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    """Concurrent runners on different databases must not share or drain each other."""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    migrate(str(db_a))
    migrate(str(db_b))
    store_a = SqliteStore(str(db_a))
    store_b = SqliteStore(str(db_b))
    _, run_a = create_run(store_a, [])
    _, run_b = create_run(store_b, [])

    calls: list[str] = []

    def wiki(run: dict[str, Any], vault_dir: str, artifacts: list[Any] | None = None) -> None:
        calls.append(str(run["id"]))

    monkeypatch.setattr("omniagentos.vault_wiki.maybe_update_wiki", wiki)

    runner_a = _runner(store_a, tmp_path / "a", worker_id="wa")
    runner_b = _runner(store_b, tmp_path / "b", worker_id="wb")

    assert runner_a._store_identity() != runner_b._store_identity()
    assert runner_a._postrun_queue_path() != runner_b._postrun_queue_path()

    runner_a._enqueue_postrun_job(run_a, "wiki_update")
    runner_b._enqueue_postrun_job(run_b, "wiki_update")
    _wait_until(lambda: set(calls) == {run_a, run_b})
    runner_a.shutdown(timeout=1.0)
    runner_b.shutdown(timeout=1.0)

    # Each runner only executed its own run (get_run from the other DB returns None,
    # so a cross-drain would silently skip rather than call wiki with the wrong run).
    assert sorted(calls) == sorted([run_a, run_b])
    assert calls.count(run_a) == 1
    assert calls.count(run_b) == 1


def test_postrun_jobs_are_claimed_exactly_once_across_workers(
    monkeypatch: pytest.MonkeyPatch,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    """Two workers draining the same DB-scoped queue execute each job once."""
    _, run_id = create_run(store, [])
    started = Event()
    release = Event()
    calls: list[str] = []

    def wiki(run: dict[str, Any], vault_dir: str, artifacts: list[Any] | None = None) -> None:
        calls.append(str(run["id"]))
        started.set()
        release.wait(2.0)

    monkeypatch.setattr("omniagentos.vault_wiki.maybe_update_wiki", wiki)
    seed = _runner(store, tmp_path, worker_id="seed")
    seed._enqueue_postrun_job(run_id, "wiki_update")
    # Stop the seed daemon so two fresh workers race the claim.
    seed._postrun_stop.set()
    seed._postrun_wake.set()
    if seed._postrun_thread is not None:
        seed._postrun_thread.join(timeout=1.0)

    # Clear the claim that the seed daemon may have already taken if it was fast.
    # Re-seed a single job with no claims/done markers for a clean race.
    queue_path = seed._postrun_queue_path()
    for path in (
        queue_path,
        seed._postrun_done_path(),
        seed._postrun_claim_path(),
    ):
        if path.is_file():
            path.unlink()
    calls.clear()
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    queue_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "kind": "wiki_update",
                "db": seed._store_identity(),
                "enqueued_at": "2026-07-23T12:00:00Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Drain in-process from two workers concurrently (O_EXCL claim fence).
    w1 = _runner(store, tmp_path, worker_id="w1")
    w2 = _runner(store, tmp_path, worker_id="w2")
    # Stop auto-daemons; drive drain explicitly from two threads for a hard race.
    for r in (w1, w2):
        r._postrun_stop.set()
        r._postrun_wake.set()
        if r._postrun_thread is not None:
            r._postrun_thread.join(timeout=1.0)
        r._postrun_stop.clear()

    t1 = Thread(target=w1._drain_postrun_queue, daemon=True)
    t2 = Thread(target=w2._drain_postrun_queue, daemon=True)
    t1.start()
    t2.start()
    assert started.wait(2.0)
    # Give the second worker a chance to race; claim must serialize them.
    time.sleep(0.2)
    release.set()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    w1.shutdown(timeout=1.0)
    w2.shutdown(timeout=1.0)

    assert calls == [run_id]


def test_compaction_cleans_up_claim_files(
    monkeypatch: pytest.MonkeyPatch,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    """Compaction must remove claim-{offset}.lock files for compacted offsets."""
    from omniagentos.runner.core import _POSTRUN_RETENTION

    _, run_id = create_run(store, [])
    runner = _runner(store, tmp_path)
    queue_path = runner._postrun_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    # Seed enough jobs to trigger compaction (> _POSTRUN_RETENTION).
    jobs: list[str] = []
    for i in range(_POSTRUN_RETENTION + 5):
        job_id = f"run-{i:04d}"
        jobs.append(job_id)
        runner._append_postrun_line(
            queue_path,
            {
                "run_id": job_id,
                "kind": "wiki_update",
                "db": runner._store_identity(),
                "enqueued_at": "2026-07-23T12:00:00Z",
            },
        )

    # Manually create claim files and mark all as completed to simulate drained state.
    done_path = runner._postrun_done_path()
    claim_path = runner._postrun_claim_path()
    with queue_path.open("rb") as handle:
        while True:
            offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            # Create claim file.
            claim_file = runner._claim_file_for_offset(offset)
            claim_file.parent.mkdir(parents=True, exist_ok=True)
            claim_file.write_text(f"test\n{offset}\n", encoding="utf-8")
            # Mark as claimed and done.
            runner._append_postrun_line(
                claim_path,
                {
                    "offset": offset,
                    "claimed": True,
                    "worker_id": "test",
                    "claimed_at": "2026-07-23T12:00:00Z",
                },
            )
            runner._append_postrun_line(
                done_path,
                {
                    "offset": offset,
                    "processed": True,
                    "status": "processed",
                    "worker_id": "test",
                    "completed_at": "2026-07-23T12:00:00Z",
                },
            )

    # Count claim files before compaction.
    claim_files_before = list(runner._postrun_queue_dir().glob("claim-*.lock"))
    assert len(claim_files_before) == _POSTRUN_RETENTION + 5

    # Trigger compaction.
    completed = runner._completed_postrun_offsets()
    assert len(completed) >= _POSTRUN_RETENTION
    runner._maybe_compact_postrun_queue(completed)

    # After compaction, claim files for compacted offsets should be removed.
    claim_files_after = list(runner._postrun_queue_dir().glob("claim-*.lock"))
    # All jobs were completed, so all claim files should be cleaned up.
    assert len(claim_files_after) == 0
    runner.shutdown(timeout=1.0)


def test_compaction_deferred_while_claims_outstanding(
    monkeypatch: pytest.MonkeyPatch,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    """Compaction must not run while any claims are outstanding (not yet done)."""
    from omniagentos.runner.core import _POSTRUN_RETENTION

    _, run_id = create_run(store, [])
    runner = _runner(store, tmp_path)
    queue_path = runner._postrun_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    # Seed enough jobs to trigger compaction.
    for i in range(_POSTRUN_RETENTION + 2):
        runner._append_postrun_line(
            queue_path,
            {
                "run_id": f"run-{i:04d}",
                "kind": "wiki_update",
                "db": runner._store_identity(),
                "enqueued_at": "2026-07-23T12:00:00Z",
            },
        )

    # Mark all but one as done; leave one claimed but not done.
    done_path = runner._postrun_done_path()
    claim_path = runner._postrun_claim_path()
    offsets: list[int] = []
    with queue_path.open("rb") as handle:
        while True:
            offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            offsets.append(offset)

    # First offset: claimed only (outstanding).
    runner._append_postrun_line(
        claim_path,
        {
            "offset": offsets[0],
            "claimed": True,
            "worker_id": "test",
            "claimed_at": "2026-07-23T12:00:00Z",
        },
    )
    # Create claim file for the outstanding claim.
    claim_file = runner._claim_file_for_offset(offsets[0])
    claim_file.parent.mkdir(parents=True, exist_ok=True)
    claim_file.write_text("outstanding\n", encoding="utf-8")

    # All other offsets: claimed AND done.
    for offset in offsets[1:]:
        runner._append_postrun_line(
            claim_path,
            {
                "offset": offset,
                "claimed": True,
                "worker_id": "test",
                "claimed_at": "2026-07-23T12:00:00Z",
            },
        )
        runner._append_postrun_line(
            done_path,
            {
                "offset": offset,
                "processed": True,
                "status": "processed",
                "worker_id": "test",
                "completed_at": "2026-07-23T12:00:00Z",
            },
        )

    completed = runner._completed_postrun_offsets()
    assert len(completed) >= _POSTRUN_RETENTION
    queue_size_before = queue_path.stat().st_size

    # Compaction should be deferred because of the outstanding claim.
    runner._maybe_compact_postrun_queue(completed)
    queue_size_after = queue_path.stat().st_size

    # Queue should NOT have been compacted (same size).
    assert queue_size_after == queue_size_before

    # Now mark the outstanding claim as done.
    runner._append_postrun_line(
        done_path,
        {
            "offset": offsets[0],
            "processed": True,
            "status": "processed",
            "worker_id": "test",
            "completed_at": "2026-07-23T12:00:00Z",
        },
    )
    completed = runner._completed_postrun_offsets()

    # Now compaction should proceed.
    runner._maybe_compact_postrun_queue(completed)
    queue_size_final = queue_path.stat().st_size

    # Queue should be compacted (smaller or empty).
    assert queue_size_final < queue_size_before
    runner.shutdown(timeout=1.0)


def test_compaction_triggers_again_after_new_jobs(
    monkeypatch: pytest.MonkeyPatch,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    """After compaction, new jobs reaching threshold should trigger compaction again."""
    from omniagentos.runner.core import _POSTRUN_RETENTION

    _, run_id = create_run(store, [])
    runner = _runner(store, tmp_path)
    queue_path = runner._postrun_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    def seed_and_drain(count: int) -> None:
        """Add count jobs, mark them all as claimed and done."""
        for i in range(count):
            runner._append_postrun_line(
                queue_path,
                {
                    "run_id": f"run-{i:04d}",
                    "kind": "wiki_update",
                    "db": runner._store_identity(),
                    "enqueued_at": "2026-07-23T12:00:00Z",
                },
            )
        done_path = runner._postrun_done_path()
        with queue_path.open("rb") as handle:
            while True:
                offset = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                runner._append_postrun_line(
                    done_path,
                    {
                        "offset": offset,
                        "processed": True,
                        "status": "processed",
                        "worker_id": "test",
                        "completed_at": "2026-07-23T12:00:00Z",
                    },
                )

    # First round: seed enough to trigger compaction.
    seed_and_drain(_POSTRUN_RETENTION + 10)
    completed = runner._completed_postrun_offsets()
    assert len(completed) >= _POSTRUN_RETENTION
    runner._maybe_compact_postrun_queue(completed)

    # After first compaction, queue should be empty (all were completed).
    queue_size_after_first = queue_path.stat().st_size
    assert queue_size_after_first == 0

    # Second round: add another batch of jobs.
    seed_and_drain(_POSTRUN_RETENTION + 5)
    completed = runner._completed_postrun_offsets()
    # New done markers should be counted.
    assert len(completed) >= _POSTRUN_RETENTION

    # Second compaction should also work.
    runner._maybe_compact_postrun_queue(completed)
    queue_size_after_second = queue_path.stat().st_size
    assert queue_size_after_second == 0
    runner.shutdown(timeout=1.0)


def test_successful_done_unlinks_claim_file(
    monkeypatch: pytest.MonkeyPatch,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    """When a job is marked done, its claim lock file is unlinked immediately (M-43)."""
    _, run_id = create_run(store, [])
    calls: list[str] = []

    def wiki(run: dict[str, Any], vault_dir: str, artifacts: list[Any] | None = None) -> None:
        calls.append(str(run["id"]))

    monkeypatch.setattr("omniagentos.vault_wiki.maybe_update_wiki", wiki)
    runner = _runner(store, tmp_path)
    runner._enqueue_postrun_job(run_id, "wiki_update")
    _wait_until(lambda: calls == [run_id])

    claim_file = runner._claim_file_for_offset(0)
    assert not claim_file.exists(), "claim file should be unlinked when marking done"
    runner.shutdown(timeout=1.0)


def test_orphan_claim_file_is_recovered_without_silent_work_loss(
    monkeypatch: pytest.MonkeyPatch,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    """An orphan claim-{offset}.lock file without a claim marker is recovered and executed (M-43)."""
    _, run_id = create_run(store, [])
    runner = _runner(store, tmp_path)
    queue_path = runner._postrun_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    # Seed one job at offset 0.
    runner._append_postrun_line(
        queue_path,
        {
            "run_id": run_id,
            "kind": "wiki_update",
            "db": runner._store_identity(),
            "enqueued_at": "2026-07-23T12:00:00Z",
        },
    )

    # Create orphan claim file (no corresponding entry in queue.jsonl.claims).
    claim_file = runner._claim_file_for_offset(0)
    claim_file.write_text("crashed-process\n", encoding="utf-8")
    assert claim_file.exists()

    calls: list[str] = []

    def wiki(run: dict[str, Any], vault_dir: str, artifacts: list[Any] | None = None) -> None:
        calls.append(str(run["id"]))

    monkeypatch.setattr("omniagentos.vault_wiki.maybe_update_wiki", wiki)

    # Wake daemon and drain queue.
    runner._postrun_wake.set()
    runner._ensure_postrun_daemon()
    _wait_until(lambda: calls == [run_id])
    runner.shutdown(timeout=1.0)

    # Prove orphan lock was unlinked and job was processed without work loss.
    assert not claim_file.exists()
    assert calls == [run_id]


def test_crashed_claim_recovers_as_abandoned_and_unblocks_compaction(
    monkeypatch: pytest.MonkeyPatch,
    store: SqliteStore,
    var_dir: Path,
    tmp_path: Path,
) -> None:
    """A crashed outstanding claim past TTL recovers as abandoned and allows compaction (M-43)."""
    from omniagentos.runner.core import _POSTRUN_RETENTION

    runner = _runner(store, tmp_path, stale_s=1)
    queue_path = runner._postrun_queue_path()
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    # Seed jobs: offset 0 (crashed claim) + _POSTRUN_RETENTION completed jobs.
    runner._append_postrun_line(
        queue_path,
        {
            "run_id": "crashed-run",
            "kind": "wiki_update",
            "db": runner._store_identity(),
            "enqueued_at": "2026-07-23T12:00:00Z",
        },
    )

    # Claim offset 0 with an old timestamp (expired TTL).
    claim_file = runner._claim_file_for_offset(0)
    claim_file.write_text("dead-worker\n2020-01-01T00:00:00Z\n", encoding="utf-8")
    runner._append_postrun_line(
        runner._postrun_claim_path(),
        {
            "offset": 0,
            "claimed": True,
            "worker_id": "dead-worker",
            "claimed_at": "2020-01-01T00:00:00Z",
        },
    )

    # Seed and mark done for subsequent jobs to reach compaction threshold.
    done_path = runner._postrun_done_path()
    for i in range(_POSTRUN_RETENTION + 5):
        runner._append_postrun_line(
            queue_path,
            {
                "run_id": f"run-{i:04d}",
                "kind": "wiki_update",
                "db": runner._store_identity(),
                "enqueued_at": "2026-07-23T12:00:00Z",
            },
        )

    # Mark all jobs except offset 0 as done.
    with queue_path.open("rb") as handle:
        while True:
            off = handle.tell()
            line = handle.readline()
            if not line:
                break
            if off != 0:
                runner._append_postrun_line(
                    done_path,
                    {
                        "offset": off,
                        "processed": True,
                        "status": "processed",
                        "worker_id": "test",
                        "completed_at": "2026-07-23T12:00:00Z",
                    },
                )

    queue_size_before = queue_path.stat().st_size

    # Direct recovery call checks marker before compaction prunes it.
    runner._recover_crashed_claims()

    # Offset 0 must now be marked done as 'abandoned' and claim file unlinked.
    done_markers = [json.loads(line) for line in done_path.read_text(encoding="utf-8").splitlines()]
    abandoned_markers = [m for m in done_markers if m["offset"] == 0 and m["status"] == "abandoned"]
    assert len(abandoned_markers) == 1
    assert not claim_file.exists()

    # Claim next / drain triggers compaction now that outstanding claim is cleared.
    runner._claim_next_postrun_job()

    # Queue size should be compacted (reduced).
    queue_size_after = queue_path.stat().st_size
    assert queue_size_after < queue_size_before
    runner.shutdown(timeout=1.0)
