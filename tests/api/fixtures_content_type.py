"""Fixtures for Content-Type security tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def html_artifact(tmp_path: Path) -> Path:
    """Create a test HTML artifact file."""
    artifact_dir = tmp_path / "var" / "artifacts" / "test-scope"
    artifact_dir.mkdir(parents=True)
    html_file = artifact_dir / "test.html"
    html_file.write_text(
        "<html><head><title>Test</title></head><body>Hello</body></html>",
        encoding="utf-8",
    )
    return html_file


@pytest.fixture
def javascript_artifact(tmp_path: Path) -> Path:
    """Create a test JavaScript artifact file."""
    artifact_dir = tmp_path / "var" / "artifacts" / "test-scope"
    artifact_dir.mkdir(parents=True)
    js_file = artifact_dir / "test.js"
    js_file.write_text("console.log('test');", encoding="utf-8")
    return js_file


@pytest.fixture
def svg_artifact(tmp_path: Path) -> Path:
    """Create a test SVG artifact file."""
    artifact_dir = tmp_path / "var" / "artifacts" / "test-scope"
    artifact_dir.mkdir(parents=True)
    svg_file = artifact_dir / "test.svg"
    svg_file.write_text(
        '<svg><script>alert("XSS")</script></svg>',
        encoding="utf-8",
    )
    return svg_file


@pytest.fixture
def text_artifact(tmp_path: Path) -> Path:
    """Create a test plain text artifact file."""
    artifact_dir = tmp_path / "var" / "artifacts" / "test-scope"
    artifact_dir.mkdir(parents=True)
    text_file = artifact_dir / "test.txt"
    text_file.write_text("Plain text content", encoding="utf-8")
    return text_file
