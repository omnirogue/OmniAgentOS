"""WP7 wiring: the scheduler coordinator calls the summary hook at every
terminal run status (completed/failed/cancelled), exactly once per run.

Uses an injected ``summary_writer`` (the scheduler's constructor seam) so
these tests exercise ONLY the wiring — ``test_summary.py`` covers
``write_summary``'s own behavior in isolation.
"""

from __future__ import annotations

from pathlib import Path

from omniagentos.swarm.scheduler import SwarmScheduler
from tests.swarm.scheduler_fakes import FakeGit, make_harness, make_scheduler, wait_until


class _RecordingWriter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, run_id: str) -> None:
        self.calls.append(run_id)


class TestSummaryHookWiring:
    def test_fires_once_on_normal_completion(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "a"}], target_n=1)
        writer = _RecordingWriter()
        try:
            scheduler = make_scheduler(h, summary_writer=writer)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.dal.get_run(h.run_id)["status"] == "completed"
            assert writer.calls == [h.run_id]
        finally:
            h.close()

    def test_fires_once_on_failure(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "a"}], target_n=1)
        h.git = FakeGit(checkout=False)  # forces the "not a git checkout" fail_run path
        writer = _RecordingWriter()
        try:
            scheduler = make_scheduler(h, summary_writer=writer, git=h.git)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert h.dal.get_run(h.run_id)["status"] == "failed"
            assert writer.calls == [h.run_id]
        finally:
            h.close()

    def test_fires_once_on_stall_failure(self, tmp_path: Path) -> None:
        """A run where no slot can ever start anything (claims never land)
        still gets exactly one summary write when the stall guard fails it —
        mirrors ``TestStallGuard`` in ``test_scheduler.py``."""
        h = make_harness(tmp_path, [{"id": "stuck"}], max_concurrency=1, fake_clock=True)
        writer = _RecordingWriter()
        try:
            scheduler = make_scheduler(h, summary_writer=writer)
            scheduler._try_claim = lambda state, index, worker_id: None  # type: ignore[method-assign]
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert wait_until(lambda: h.emitter.of("run_started"), timeout=10)
            h.clock.advance(31 * 60)  # past the 30-minute stall window
            assert wait_until(
                lambda: (h.dal.get_run(h.run_id) or {}).get("status") == "failed", timeout=10
            )
            assert handle.join(timeout=10)
            assert writer.calls == [h.run_id]
        finally:
            h.close()

    def test_writer_exception_never_raises_into_coordinator(self, tmp_path: Path) -> None:
        h = make_harness(tmp_path, [{"id": "a"}], target_n=1)

        def boom(run_id: str) -> None:
            raise RuntimeError("summary writer exploded")

        try:
            scheduler = make_scheduler(h, summary_writer=boom)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)  # coordinator must still finish cleanly

            assert h.dal.get_run(h.run_id)["status"] == "completed"
        finally:
            h.close()

    def test_default_writer_resolves_real_write_summary(self, tmp_path: Path, monkeypatch) -> None:
        """No injected ``summary_writer``: the scheduler's default lazily calls
        the real ``omniagentos.swarm.summary.write_summary`` exactly once,
        pointed at a throwaway vault dir so the test never touches the real
        repo vault."""
        calls: list[tuple] = []

        def fake_write_summary(run_id, *, dal, emitter=None, **kwargs):
            calls.append((run_id, dal, emitter))
            return {"score": 42.0}

        monkeypatch.setattr("omniagentos.swarm.summary.write_summary", fake_write_summary)
        h = make_harness(tmp_path, [{"id": "a"}], target_n=1)
        try:
            # make_scheduler fakes summary_writer by default (like every other
            # collaborator) so ordinary scheduler tests never touch a real
            # Fable CLI or the real vault; explicitly clear it here to reach
            # SwarmScheduler's own lazy-import default path.
            scheduler = make_scheduler(h, summary_writer=None)
            handle = scheduler.start_run(h.run_id)
            assert handle is not None
            assert handle.join(timeout=20)

            assert len(calls) == 1
            assert calls[0][0] == h.run_id
            assert calls[0][1] is h.dal
        finally:
            h.close()


class _StubDal:
    """Just enough DAL for ``_write_summary_best_effort``'s post-summary
    re-read: ``get_run`` returns the scripted row (or None)."""

    def __init__(self, run: dict | None) -> None:
        self.run = run
        self.calls: list[str] = []

    def get_run(self, run_id: str) -> dict | None:
        self.calls.append(run_id)
        return self.run


def _bare_scheduler(run: dict | None) -> SwarmScheduler:
    """A scheduler wired with stubs, sufficient to call the terminal-status
    notification region of ``_write_summary_best_effort`` directly."""
    return SwarmScheduler(
        dal=_StubDal(run),  # type: ignore[arg-type]
        collab=None,  # type: ignore[arg-type]
        spawner=None,  # type: ignore[arg-type]
        summary_writer=lambda run_id: None,
    )


class _Recorder:
    def __init__(self, raise_error: bool = False) -> None:
        self.calls: list[dict] = []
        self._raise = raise_error

    def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if self._raise:
            raise RuntimeError("notify exploded")


class TestTerminalNotifications:
    """The C0 done-bell + failed/cancelled alert emitted from
    ``_write_summary_best_effort`` (the sole swarm terminal emitter --
    ``reconcile_board`` skips swarm cards by design)."""

    def _patch(self, monkeypatch, done: _Recorder, failed: _Recorder) -> None:
        import omniagentos.notifications.service as notif_service

        monkeypatch.setattr(notif_service, "notify_task_done", done)
        monkeypatch.setattr(notif_service, "notify_run_terminal_failure", failed)

    def test_completed_run_emits_done_once_with_right_args(self, monkeypatch) -> None:
        done, failed = _Recorder(), _Recorder()
        self._patch(monkeypatch, done, failed)
        scheduler = _bare_scheduler(
            {
                "status": "completed",
                "board_task_id": "btk_x",
                "goal": "Ship it",
                "working_dir": "/w",
            }
        )
        scheduler._write_summary_best_effort("swr_x")
        assert len(done.calls) == 1
        assert done.calls[0]["board_task_id"] == "btk_x"
        assert done.calls[0]["run_id"] == "swr_x"
        assert done.calls[0]["task_title"] == "Ship it"
        assert done.calls[0]["workspace"] == "/w"
        assert failed.calls == []

    def test_failed_run_emits_swarm_failed_alert_not_done(self, monkeypatch) -> None:
        done, failed = _Recorder(), _Recorder()
        self._patch(monkeypatch, done, failed)
        scheduler = _bare_scheduler(
            {
                "status": "failed",
                "board_task_id": "btk_y",
                "goal": "Doomed goal",
                "working_dir": "/w2",
            }
        )
        scheduler._write_summary_best_effort("swr_y")
        assert done.calls == []
        assert len(failed.calls) == 1
        assert failed.calls[0]["run_id"] == "swr_y"
        assert failed.calls[0]["status"] == "failed"
        assert failed.calls[0]["goal"] == "Doomed goal"
        assert failed.calls[0]["board_task_id"] == "btk_y"

    def test_cancelled_run_emits_swarm_failed_alert(self, monkeypatch) -> None:
        done, failed = _Recorder(), _Recorder()
        self._patch(monkeypatch, done, failed)
        scheduler = _bare_scheduler(
            {"status": "cancelled", "board_task_id": None, "goal": "g", "working_dir": None}
        )
        scheduler._write_summary_best_effort("swr_z")
        assert done.calls == []
        assert len(failed.calls) == 1
        assert failed.calls[0]["status"] == "cancelled"
        assert failed.calls[0]["board_task_id"] is None  # run-ref fallback path

    def test_completed_without_board_task_emits_nothing(self, monkeypatch) -> None:
        done, failed = _Recorder(), _Recorder()
        self._patch(monkeypatch, done, failed)
        scheduler = _bare_scheduler(
            {"status": "completed", "board_task_id": None, "goal": "g", "working_dir": "/w"}
        )
        scheduler._write_summary_best_effort("swr_n")  # must not crash
        assert done.calls == []
        assert failed.calls == []

    def test_missing_run_row_emits_nothing(self, monkeypatch) -> None:
        done, failed = _Recorder(), _Recorder()
        self._patch(monkeypatch, done, failed)
        scheduler = _bare_scheduler(None)
        scheduler._write_summary_best_effort("swr_gone")  # must not crash
        assert done.calls == []
        assert failed.calls == []

    def test_done_notify_raising_is_swallowed(self, monkeypatch) -> None:
        done, failed = _Recorder(raise_error=True), _Recorder()
        self._patch(monkeypatch, done, failed)
        scheduler = _bare_scheduler(
            {"status": "completed", "board_task_id": "btk_b", "goal": "g", "working_dir": "/w"}
        )
        scheduler._write_summary_best_effort("swr_b")  # best-effort: never raises
        assert len(done.calls) == 1

    def test_failure_notify_raising_is_swallowed(self, monkeypatch) -> None:
        done, failed = _Recorder(), _Recorder(raise_error=True)
        self._patch(monkeypatch, done, failed)
        scheduler = _bare_scheduler(
            {"status": "failed", "board_task_id": None, "goal": "g", "working_dir": None}
        )
        scheduler._write_summary_best_effort("swr_c")  # best-effort: never raises
        assert len(failed.calls) == 1
