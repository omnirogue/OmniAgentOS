"""Tests for media generation tools with fake provider injection.

Production code calls the real provider seam. These tests inject a fake provider
to verify artifact verification, path containment, and error handling WITHOUT
making live provider calls.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from unittest import mock

from omniagentos.toolplane.manifest import load_manifest
from omniagentos.toolplane.tools import dispatch


@dataclass(frozen=True, slots=True)
class FakeStudioResult:
    """Fake result matching StudioResult interface."""

    ok: bool
    status_code: int
    body: dict[str, Any] | str | None
    error: str | None = None


def _make_manifest(tmp_path, **overrides):
    """Create a test manifest with standard defaults."""
    data = {
        "run_id": "run-1",
        "session_id": "session-1",
        "holder_generation": 3,
        "read_roots": [str(tmp_path)],
        "write_roots": [str(tmp_path)],
        "allowed_ops": ["globex_generate_image", "globex_generate_video"],
    }
    data.update(overrides)
    return load_manifest(data)


class TestGlobexGenerateImageWithFakeProvider:
    """Test image generation with injected fake provider."""

    def test_success_with_artifact_verification(self, tmp_path):
        """Real provider path: provider writes file, toolplane verifies it."""
        manifest = _make_manifest(tmp_path)
        target = tmp_path / "output.png"

        def fake_generate_image(payload: dict[str, Any]) -> FakeStudioResult:
            # Fake provider writes the artifact to the output_path
            output_path = payload.get("output_path")
            if output_path:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "wb") as f:
                    f.write(b"FAKE_PNG_DATA_12345")
            return FakeStudioResult(ok=True, status_code=200, body={"status": "success"})

        with mock.patch(
            "omniagentos.connectors.globex_studio.generate_image",
            side_effect=fake_generate_image,
        ):
            result = dispatch("globex_generate_image", manifest, {"output_path": str(target)})

        assert result["ok"] is True
        assert result["result"]["path"] == str(target)
        assert result["result"]["artifact_bytes"] == 19
        assert "artifact_sha256" in result["result"]
        assert os.path.exists(target)

    def test_missing_artifact_failure(self, tmp_path):
        """Provider returns ok but doesn't write file -> missing_artifact error."""
        manifest = _make_manifest(tmp_path)
        target = tmp_path / "missing.png"

        def fake_generate_no_file(payload: dict[str, Any]) -> FakeStudioResult:
            # Provider says ok but doesn't create file
            return FakeStudioResult(ok=True, status_code=200, body={"status": "success"})

        with mock.patch(
            "omniagentos.connectors.globex_studio.generate_image",
            side_effect=fake_generate_no_file,
        ):
            result = dispatch("globex_generate_image", manifest, {"output_path": str(target)})

        assert result["ok"] is False
        assert result["error"] == "missing_artifact"
        assert not os.path.exists(target)

    def test_empty_artifact_failure(self, tmp_path):
        """Provider writes empty file -> empty_artifact error."""
        manifest = _make_manifest(tmp_path)
        target = tmp_path / "empty.png"

        def fake_generate_empty(payload: dict[str, Any]) -> FakeStudioResult:
            output_path = payload.get("output_path")
            if output_path:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "wb") as f:
                    f.write(b"")  # Empty file
            return FakeStudioResult(ok=True, status_code=200, body={"status": "success"})

        with mock.patch(
            "omniagentos.connectors.globex_studio.generate_image",
            side_effect=fake_generate_empty,
        ):
            result = dispatch("globex_generate_image", manifest, {"output_path": str(target)})

        assert result["ok"] is False
        assert result["error"] == "empty_artifact"

    def test_provider_error_failure(self, tmp_path):
        """Provider returns error -> generation_failed."""
        manifest = _make_manifest(tmp_path)
        target = tmp_path / "failed.png"

        def fake_generate_error(payload: dict[str, Any]) -> FakeStudioResult:
            return FakeStudioResult(
                ok=False, status_code=500, body=None, error="internal_server_error"
            )

        with mock.patch(
            "omniagentos.connectors.globex_studio.generate_image",
            side_effect=fake_generate_error,
        ):
            result = dispatch("globex_generate_image", manifest, {"output_path": str(target)})

        assert result["ok"] is False
        assert result["error"] == "generation_failed"

    def test_missing_output_path(self, tmp_path):
        """No output_path provided -> missing_output error."""
        manifest = _make_manifest(tmp_path)

        result = dispatch("globex_generate_image", manifest, {})

        assert result["ok"] is False
        assert result["error"] == "missing_output"

    def test_path_outside_scope(self, tmp_path):
        """Output path outside write_roots -> out_of_scope error."""
        manifest = _make_manifest(tmp_path, write_roots=[str(tmp_path / "allowed")])
        (tmp_path / "allowed").mkdir()
        target = tmp_path / "not_allowed" / "image.png"

        result = dispatch("globex_generate_image", manifest, {"output_path": str(target)})

        assert result["ok"] is False
        assert result["error"] == "out_of_scope"

    def test_not_in_allowed_ops(self, tmp_path):
        """Tool not in allowed_ops -> not_allowed error."""
        manifest = _make_manifest(tmp_path, allowed_ops=["read_file"])
        target = tmp_path / "image.png"

        result = dispatch("globex_generate_image", manifest, {"output_path": str(target)})

        assert result["ok"] is False
        assert result["error"] == "not_allowed"


class TestGlobexGenerateVideoWithFakeProvider:
    """Test video generation with injected fake provider."""

    def test_success_with_artifact_verification(self, tmp_path):
        """Real provider path: provider writes file, toolplane verifies it."""
        manifest = _make_manifest(tmp_path)
        target = tmp_path / "output.mp4"

        def fake_generate_video(payload: dict[str, Any]) -> FakeStudioResult:
            output_path = payload.get("output_path")
            if output_path:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "wb") as f:
                    f.write(b"FAKE_MP4_VIDEO_DATA")
            return FakeStudioResult(ok=True, status_code=200, body={"status": "success"})

        with mock.patch(
            "omniagentos.connectors.globex_studio.generate_video",
            side_effect=fake_generate_video,
        ):
            result = dispatch("globex_generate_video", manifest, {"output_path": str(target)})

        assert result["ok"] is True
        assert result["result"]["path"] == str(target)
        assert result["result"]["artifact_bytes"] == 19
        assert "artifact_sha256" in result["result"]

    def test_missing_artifact_failure(self, tmp_path):
        """Provider returns ok but doesn't write file -> missing_artifact error."""
        manifest = _make_manifest(tmp_path)
        target = tmp_path / "missing.mp4"

        def fake_generate_no_file(payload: dict[str, Any]) -> FakeStudioResult:
            return FakeStudioResult(ok=True, status_code=200, body={"status": "success"})

        with mock.patch(
            "omniagentos.connectors.globex_studio.generate_video",
            side_effect=fake_generate_no_file,
        ):
            result = dispatch("globex_generate_video", manifest, {"output_path": str(target)})

        assert result["ok"] is False
        assert result["error"] == "missing_artifact"


class TestSpoofedArtifactDetection:
    """Test detection of spoofed artifacts (wrong path, hash mismatch, etc.)."""

    def test_artifact_at_wrong_path_detected(self, tmp_path):
        """Provider writes to wrong path -> missing_artifact at expected path."""
        manifest = _make_manifest(tmp_path)
        target = tmp_path / "expected.png"
        wrong_path = tmp_path / "wrong.png"

        def fake_generate_wrong_path(payload: dict[str, Any]) -> FakeStudioResult:
            # Write to wrong path instead of output_path
            with open(wrong_path, "wb") as f:
                f.write(b"WRONG_PATH_DATA")
            return FakeStudioResult(ok=True, status_code=200, body={"status": "success"})

        with mock.patch(
            "omniagentos.connectors.globex_studio.generate_image",
            side_effect=fake_generate_wrong_path,
        ):
            result = dispatch("globex_generate_image", manifest, {"output_path": str(target)})

        assert result["ok"] is False
        assert result["error"] == "missing_artifact"
        # Wrong path file exists but expected doesn't
        assert os.path.exists(wrong_path)
        assert not os.path.exists(target)


class TestUnknownCapabilityDenial:
    """Test that unknown capabilities are denied."""

    def test_unknown_tool_denied(self, tmp_path):
        """Unknown tool -> unknown_capability error."""
        manifest = _make_manifest(tmp_path, allowed_ops=["nonexistent_tool"])

        result = dispatch("nonexistent_tool", manifest, {})

        assert result["ok"] is False
        assert result["error"] == "unknown_capability"

    def test_known_tool_not_allowed(self, tmp_path):
        """Known tool but not in allowed_ops -> not_allowed error."""
        manifest = _make_manifest(tmp_path, allowed_ops=[])

        result = dispatch("read_file", manifest, {"path": str(tmp_path / "test.txt")})

        assert result["ok"] is False
        assert result["error"] == "not_allowed"
