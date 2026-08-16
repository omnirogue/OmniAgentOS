"""Tests for toolplane observation system.

L17 requirements tested:
- Status classification: attempted, denied, failed, success
- Correlation IDs for deduplication
- Trusted provenance fields
- Durable emission with retry support
- Idempotent observation recording
"""

from __future__ import annotations

import json

from omniagentos.toolplane.manifest import load_manifest
from omniagentos.toolplane.observe import (
    DENIAL_ERRORS,
    OBSERVE_VERSION,
    ObservationSink,
    emit_observation,
    get_sink,
    observe_call,
)


def _make_manifest(**overrides):
    """Create a test manifest with standard defaults."""
    data = {
        "run_id": "run-1",
        "session_id": "session-1",
        "holder_generation": 3,
        "read_roots": ["/tmp/read"],
        "write_roots": ["/tmp/write"],
        "allowed_ops": ["read_file"],
    }
    data.update(overrides)
    return load_manifest(data)


class TestObserveCallStatusClassification:
    """Test status classification for different result types."""

    def test_observe_success(self):
        """Result with ok:true -> status:success."""
        manifest = _make_manifest()
        result = observe_call("read_file", manifest, {"ok": True}, 150)

        assert result["status"] == "success"
        assert result["ok"] is True
        assert result["error"] is None

    def test_observe_attempted(self):
        """Result is None -> status:attempted."""
        manifest = _make_manifest()
        result = observe_call("read_file", manifest, None, 0)

        assert result["status"] == "attempted"
        assert result["ok"] is False
        assert result["error"] is None

    def test_observe_denied_for_all_denial_errors(self):
        """Each denial error -> status:denied."""
        manifest = _make_manifest()
        for err in DENIAL_ERRORS:
            result = observe_call("read_file", manifest, {"ok": False, "error": err}, 10)
            assert result["status"] == "denied", f"Failed for {err}"
            assert result["ok"] is False
            assert result["error"] == err

    def test_observe_failed(self):
        """Non-denial error -> status:failed."""
        manifest = _make_manifest()
        result = observe_call("read_file", manifest, {"ok": False, "error": "not_found"}, 10)

        assert result["status"] == "failed"
        assert result["ok"] is False
        assert result["error"] == "not_found"


class TestObserveCallCorrelationAndProvenance:
    """Test correlation ID and provenance fields."""

    def test_correlation_id_generated(self):
        """Correlation ID is generated when not provided."""
        manifest = _make_manifest()
        result = observe_call("read_file", manifest, {"ok": True}, 100)

        assert "correlation_id" in result
        assert len(result["correlation_id"]) == 16

    def test_correlation_id_passed_through(self):
        """Provided correlation ID is used."""
        manifest = _make_manifest()
        result = observe_call(
            "read_file", manifest, {"ok": True}, 100, correlation_id="my_correlation"
        )

        assert result["correlation_id"] == "my_correlation"

    def test_version_field_present(self):
        """Version field is included for forward compatibility."""
        manifest = _make_manifest()
        result = observe_call("read_file", manifest, {"ok": True}, 100)

        assert result["version"] == OBSERVE_VERSION

    def test_source_field_is_toolplane(self):
        """Source field indicates toolplane origin."""
        manifest = _make_manifest()
        result = observe_call("read_file", manifest, {"ok": True}, 100)

        assert result["source"] == "toolplane"

    def test_all_manifest_fields_propagated(self):
        """Manifest correlation fields are propagated."""
        manifest = _make_manifest(run_id="run-xyz", session_id="session-abc", holder_generation=42)
        result = observe_call("read_file", manifest, {"ok": True}, 100)

        assert result["run_id"] == "run-xyz"
        assert result["session_id"] == "session-abc"
        assert result["holder_generation"] == 42


class TestObservationSinkDurableEmission:
    """Test durable emission with atomic writes."""

    def test_emit_creates_file(self, tmp_path):
        """Emit creates observation file in ledger directory."""
        sink = ObservationSink(ledger_dir=str(tmp_path))
        manifest = _make_manifest()
        obs = observe_call("read_file", manifest, {"ok": True}, 100)

        result = sink.emit(obs)

        assert result is True
        obs_dir = tmp_path / "toolplane-observations"
        assert obs_dir.exists()
        files = list(obs_dir.glob("*.json"))
        assert len(files) == 1

    def test_emit_idempotent_same_correlation(self, tmp_path):
        """Emitting same correlation_id twice is idempotent."""
        sink = ObservationSink(ledger_dir=str(tmp_path))
        manifest = _make_manifest()
        obs = observe_call("read_file", manifest, {"ok": True}, 100, correlation_id="fixed_id")

        result1 = sink.emit(obs)
        result2 = sink.emit(obs)

        assert result1 is True
        assert result2 is True
        obs_dir = tmp_path / "toolplane-observations"
        files = list(obs_dir.glob("*.json"))
        assert len(files) == 1

    def test_emit_different_correlations_creates_multiple(self, tmp_path):
        """Different correlation IDs create separate files."""
        sink = ObservationSink(ledger_dir=str(tmp_path))
        manifest = _make_manifest()

        for i in range(3):
            obs = observe_call("read_file", manifest, {"ok": True}, 100, correlation_id=f"id_{i}")
            sink.emit(obs)

        obs_dir = tmp_path / "toolplane-observations"
        files = list(obs_dir.glob("*.json"))
        assert len(files) == 3

    def test_emitted_file_content_valid_json(self, tmp_path):
        """Emitted file contains valid JSON with all fields."""
        sink = ObservationSink(ledger_dir=str(tmp_path))
        manifest = _make_manifest()
        obs = observe_call("read_file", manifest, {"ok": True}, 100)
        sink.emit(obs)

        obs_dir = tmp_path / "toolplane-observations"
        files = list(obs_dir.glob("*.json"))
        with open(files[0]) as f:
            loaded = json.load(f)

        assert loaded["tool"] == "read_file"
        assert loaded["ok"] is True
        assert loaded["source"] == "toolplane"


class TestObservationSinkRetry:
    """Test retry mechanism for failed emissions."""

    def test_failed_emit_queues_for_retry(self, tmp_path):
        """Failed emission queues observation for retry."""
        # Use a read-only directory to force failure
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)

        try:
            sink = ObservationSink(ledger_dir=str(readonly_dir))
            manifest = _make_manifest()
            obs = observe_call("read_file", manifest, {"ok": True}, 100)

            result = sink.emit(obs)

            assert result is False
            assert len(sink._pending) == 1
        finally:
            readonly_dir.chmod(0o755)

    def test_retry_pending_succeeds_when_writable(self, tmp_path):
        """Retry succeeds when directory becomes writable."""
        sink = ObservationSink(ledger_dir=str(tmp_path))
        manifest = _make_manifest()
        obs = observe_call("read_file", manifest, {"ok": True}, 100)

        # Manually add to pending (simulating failed emit)
        sink._pending.append(obs)
        assert len(sink._pending) == 1

        # Retry should succeed
        still_pending = sink.retry_pending()

        assert still_pending == 0
        obs_dir = tmp_path / "toolplane-observations"
        files = list(obs_dir.glob("*.json"))
        assert len(files) == 1


class TestObservationSinkRestartRecovery:
    """Test that observations survive process restart."""

    def test_observations_persist_across_sink_instances(self, tmp_path):
        """Observations from one sink instance are visible to another."""
        manifest = _make_manifest()
        obs = observe_call("read_file", manifest, {"ok": True}, 100, correlation_id="persist_test")

        # First sink instance writes
        sink1 = ObservationSink(ledger_dir=str(tmp_path))
        sink1.emit(obs)

        # Second sink instance (simulating restart) sees the file
        sink2 = ObservationSink(ledger_dir=str(tmp_path))
        # Idempotent emit should succeed without creating duplicate
        result = sink2.emit(obs)

        assert result is True
        obs_dir = tmp_path / "toolplane-observations"
        files = list(obs_dir.glob("*.json"))
        assert len(files) == 1


class TestGlobalSinkAccess:
    """Test global sink access functions."""

    def test_get_sink_returns_same_instance(self):
        """get_sink returns the same instance on repeated calls."""
        # Reset global state for test isolation
        import omniagentos.toolplane.observe as obs_module

        obs_module._default_sink = None

        sink1 = get_sink()
        sink2 = get_sink()

        assert sink1 is sink2

    def test_emit_observation_uses_default_sink(self, tmp_path, monkeypatch):
        """emit_observation uses the default sink."""
        import omniagentos.toolplane.observe as obs_module

        # Reset and configure for test
        obs_module._default_sink = ObservationSink(ledger_dir=str(tmp_path))

        manifest = _make_manifest()
        obs = observe_call("read_file", manifest, {"ok": True}, 100)

        result = emit_observation(obs)

        assert result is True
        obs_dir = tmp_path / "toolplane-observations"
        assert obs_dir.exists()
