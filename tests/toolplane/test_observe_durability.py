"""Durability tests for toolplane observation emission (L17 M-08 / M-28).

These prove the properties the audit called out, by exercising real filesystem
behaviour rather than asserting on source text:

- Pending retry state lives on disk, so it survives process restart.
- Retry is bounded: an observation that keeps failing is dead-lettered, never
  retried forever and never silently dropped.
- Re-emission is idempotent, including across a simulated restart.
- Every durable record is scrubbed before it reaches disk.

The write failure is induced by occupying ``toolplane-observations`` with a
regular file, which makes ``os.makedirs`` on it fail while leaving the spool
directory writable. That is the exact split the durable spool exists for.
"""

from __future__ import annotations

import json
import os

from omniagentos.toolplane.manifest import load_manifest
from omniagentos.toolplane.observe import (
    DEADLETTER_SUBDIR,
    OBSERVATIONS_SUBDIR,
    SPOOL_SUBDIR,
    ObservationSink,
    observe_call,
)


def _manifest():
    return load_manifest(
        {
            "run_id": "run-durable",
            "session_id": "session-durable",
            "holder_generation": 7,
            "read_roots": ["/tmp/read"],
            "write_roots": ["/tmp/write"],
            "allowed_ops": ["read_file"],
        }
    )


def _block_observations_dir(ledger_dir) -> None:
    """Occupy the observations directory path with a file so writes fail."""
    (ledger_dir / OBSERVATIONS_SUBDIR).write_text("not a directory")


def _unblock_observations_dir(ledger_dir) -> None:
    (ledger_dir / OBSERVATIONS_SUBDIR).unlink()


def _observation_files(ledger_dir) -> list[str]:
    obs_dir = ledger_dir / OBSERVATIONS_SUBDIR
    if not obs_dir.is_dir():
        return []
    return sorted(p.name for p in obs_dir.glob("*.json"))


class TestDurableSpool:
    def test_failed_emit_is_spooled_to_disk_not_only_memory(self, tmp_path):
        _block_observations_dir(tmp_path)
        sink = ObservationSink(ledger_dir=str(tmp_path))
        obs = observe_call("read_file", _manifest(), {"ok": True}, 12, correlation_id="spool_1")

        assert sink.emit(obs) is False
        # The backlog is on disk, which is what makes it survive a restart.
        assert sink._pending == []
        spooled = sorted((tmp_path / SPOOL_SUBDIR).glob("*.json"))
        assert [p.name for p in spooled] == ["spool_1.json"]
        assert json.loads(spooled[0].read_text())["attempts"] == 1

    def test_spool_entry_carries_the_full_observation(self, tmp_path):
        _block_observations_dir(tmp_path)
        sink = ObservationSink(ledger_dir=str(tmp_path))
        obs = observe_call(
            "read_file",
            _manifest(),
            {"ok": False, "error": "not_allowed"},
            3,
            correlation_id="spool_2",
        )
        sink.emit(obs)

        payload = json.loads((tmp_path / SPOOL_SUBDIR / "spool_2.json").read_text())
        assert payload["observation"]["status"] == "denied"
        assert payload["observation"]["run_id"] == "run-durable"


class TestRestartRecovery:
    def test_new_sink_recovers_backlog_left_by_previous_process(self, tmp_path):
        _block_observations_dir(tmp_path)
        dead_sink = ObservationSink(ledger_dir=str(tmp_path))
        for i in range(3):
            dead_sink.emit(
                observe_call(
                    "read_file", _manifest(), {"ok": True}, 5, correlation_id=f"restart_{i}"
                )
            )
        del dead_sink  # simulate the process going away

        # A brand new sink, with no in-memory carry-over, sees the same backlog.
        fresh_sink = ObservationSink(ledger_dir=str(tmp_path))
        assert fresh_sink._pending == []
        assert fresh_sink.recover_pending() == 3
        assert fresh_sink.pending_count() == 3

    def test_recovered_backlog_drains_once_writes_succeed(self, tmp_path):
        _block_observations_dir(tmp_path)
        dead_sink = ObservationSink(ledger_dir=str(tmp_path))
        for i in range(3):
            dead_sink.emit(
                observe_call("read_file", _manifest(), {"ok": True}, 5, correlation_id=f"drain_{i}")
            )
        del dead_sink

        _unblock_observations_dir(tmp_path)
        fresh_sink = ObservationSink(ledger_dir=str(tmp_path))

        assert fresh_sink.retry_pending() == 0
        assert len(_observation_files(tmp_path)) == 3
        assert list((tmp_path / SPOOL_SUBDIR).glob("*.json")) == []

    def test_recovery_is_idempotent_across_restart(self, tmp_path):
        """A record already written before the crash is not duplicated on recovery."""
        sink = ObservationSink(ledger_dir=str(tmp_path))
        obs = observe_call("read_file", _manifest(), {"ok": True}, 5, correlation_id="idem_restart")
        assert sink.emit(obs) is True

        # Simulate a crash that left a stale spool entry for an already-written record.
        os.makedirs(tmp_path / SPOOL_SUBDIR, exist_ok=True)
        (tmp_path / SPOOL_SUBDIR / "idem_restart.json").write_text(
            json.dumps({"attempts": 1, "observation": obs})
        )

        fresh_sink = ObservationSink(ledger_dir=str(tmp_path))
        assert fresh_sink.recover_pending() == 1
        assert fresh_sink.retry_pending() == 0
        assert len(_observation_files(tmp_path)) == 1


class TestBoundedRetry:
    def test_persistently_failing_observation_is_dead_lettered(self, tmp_path):
        _block_observations_dir(tmp_path)
        sink = ObservationSink(ledger_dir=str(tmp_path), max_attempts=2)
        obs = observe_call("read_file", _manifest(), {"ok": True}, 5, correlation_id="doomed")

        sink.emit(obs)  # attempts=1
        assert sink.retry_pending() == 1  # attempts=2, still under budget
        assert sink.deadletter_count() == 0

        assert sink.retry_pending() == 0  # budget exhausted -> dead-lettered
        assert sink.deadletter_count() == 1
        assert list((tmp_path / SPOOL_SUBDIR).glob("*.json")) == []

        payload = json.loads((tmp_path / DEADLETTER_SUBDIR / "doomed.json").read_text())
        assert payload["observation"]["correlation_id"] == "doomed"

    def test_retry_does_not_lose_the_record_when_it_gives_up(self, tmp_path):
        """Giving up must leave evidence, not drop the observation."""
        _block_observations_dir(tmp_path)
        sink = ObservationSink(ledger_dir=str(tmp_path), max_attempts=1)
        sink.emit(observe_call("read_file", _manifest(), {"ok": True}, 5, correlation_id="gone"))

        sink.retry_pending()

        assert sink.pending_count() == 0
        assert sink.deadletter_count() == 1

    def test_corrupt_spool_entry_is_quarantined_not_retried_forever(self, tmp_path):
        sink = ObservationSink(ledger_dir=str(tmp_path))
        os.makedirs(tmp_path / SPOOL_SUBDIR, exist_ok=True)
        (tmp_path / SPOOL_SUBDIR / "corrupt.json").write_text("{not json")

        assert sink.pending_count() == 0
        assert list((tmp_path / SPOOL_SUBDIR).glob("*.json")) == []
        assert (tmp_path / DEADLETTER_SUBDIR / "corrupt_corrupt.json").exists()


class TestScrubbedBeforeDisk:
    def test_credential_in_error_never_reaches_the_durable_record(self, tmp_path):
        sink = ObservationSink(ledger_dir=str(tmp_path))
        leaked = "auth failed for Bearer abcd1234efgh5678"
        obs = observe_call(
            "read_file", _manifest(), {"ok": False, "error": leaked}, 9, correlation_id="scrub_1"
        )

        assert sink.emit(obs) is True

        on_disk = (tmp_path / OBSERVATIONS_SUBDIR / _observation_files(tmp_path)[0]).read_text()
        assert "abcd1234efgh5678" not in on_disk
        assert "[REDACTED]" in on_disk

    def test_spooled_record_is_also_scrubbed(self, tmp_path):
        _block_observations_dir(tmp_path)
        sink = ObservationSink(ledger_dir=str(tmp_path))
        obs = observe_call(
            "read_file",
            _manifest(),
            {"ok": False, "error": "denied using sk-ABCDEFGHIJKLMNOP"},
            9,
            correlation_id="scrub_2",
        )
        sink.emit(obs)

        spooled = (tmp_path / SPOOL_SUBDIR / "scrub_2.json").read_text()
        assert "sk-ABCDEFGHIJKLMNOP" not in spooled
        assert "[REDACTED]" in spooled
