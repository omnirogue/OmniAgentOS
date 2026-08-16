"""Content-Type inerting regression tests for artifact preview endpoints."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest


def test_preview_raw_forces_text_plain_for_html(
    html_artifact: Path, monkeypatch: pytest.MonkeyPatch, asgi_client: httpx.AsyncClient
) -> None:
    """Verify that HTML artifacts are served as text/plain to prevent browser rendering."""
    import omniagentos.api.routes.artifacts_preview as mod

    monkeypatch.setattr(mod, "_repo_var", lambda: html_artifact.parent.parent.parent)
    response = asyncio.run(asgi_client.get("/api/artifacts/preview/raw", params={"path": str(html_artifact)}))
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert "Content-Disposition" in response.headers
    assert response.headers["Content-Disposition"] == "attachment"
    assert "X-Content-Type-Options" in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_preview_raw_forces_text_plain_for_javascript(
    javascript_artifact: Path, monkeypatch: pytest.MonkeyPatch, asgi_client: httpx.AsyncClient
) -> None:
    """Verify that JavaScript artifacts are served as text/plain."""
    import omniagentos.api.routes.artifacts_preview as mod

    monkeypatch.setattr(mod, "_repo_var", lambda: javascript_artifact.parent.parent.parent)
    response = asyncio.run(asgi_client.get("/api/artifacts/preview/raw", params={"path": str(javascript_artifact)}))
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["Content-Disposition"] == "attachment"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_preview_raw_forces_text_plain_for_svg(
    svg_artifact: Path, monkeypatch: pytest.MonkeyPatch, asgi_client: httpx.AsyncClient
) -> None:
    """Verify that SVG artifacts (which can execute scripts) are served as text/plain."""
    import omniagentos.api.routes.artifacts_preview as mod

    monkeypatch.setattr(mod, "_repo_var", lambda: svg_artifact.parent.parent.parent)
    response = asyncio.run(asgi_client.get("/api/artifacts/preview/raw", params={"path": str(svg_artifact)}))
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["Content-Disposition"] == "attachment"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_preview_raw_forces_text_plain_for_text_file(
    text_artifact: Path, monkeypatch: pytest.MonkeyPatch, asgi_client: httpx.AsyncClient
) -> None:
    """Verify that text artifacts are also served as text/plain with headers."""
    import omniagentos.api.routes.artifacts_preview as mod

    monkeypatch.setattr(mod, "_repo_var", lambda: text_artifact.parent.parent.parent)
    response = asyncio.run(asgi_client.get("/api/artifacts/preview/raw", params={"path": str(text_artifact)}))
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["Content-Disposition"] == "attachment"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_preview_raw_sends_content_disposition_attachment(
    html_artifact: Path, monkeypatch: pytest.MonkeyPatch, asgi_client: httpx.AsyncClient
) -> None:
    """Verify that Content-Disposition is set to attachment to trigger downloads."""
    import omniagentos.api.routes.artifacts_preview as mod

    monkeypatch.setattr(mod, "_repo_var", lambda: html_artifact.parent.parent.parent)
    response = asyncio.run(asgi_client.get("/api/artifacts/preview/raw", params={"path": str(html_artifact)}))
    assert response.status_code == 200
    assert "attachment" in response.headers.get("Content-Disposition", "")


def test_preview_raw_sends_nosniff_header(
    html_artifact: Path, monkeypatch: pytest.MonkeyPatch, asgi_client: httpx.AsyncClient
) -> None:
    """Verify that X-Content-Type-Options: nosniff prevents MIME-type sniffing."""
    import omniagentos.api.routes.artifacts_preview as mod

    monkeypatch.setattr(mod, "_repo_var", lambda: html_artifact.parent.parent.parent)
    response = asyncio.run(asgi_client.get("/api/artifacts/preview/raw", params={"path": str(html_artifact)}))
    assert response.status_code == 200
    assert response.headers.get("X-Content-Type-Options") == "nosniff"


def test_preview_json_endpoint_still_returns_json_response(
    html_artifact: Path, monkeypatch: pytest.MonkeyPatch, asgi_client: httpx.AsyncClient
) -> None:
    """Verify that the /preview JSON endpoint still returns JSON metadata (not affected by inerting)."""
    import omniagentos.api.routes.artifacts_preview as mod

    monkeypatch.setattr(mod, "_repo_var", lambda: html_artifact.parent.parent.parent)
    response = asyncio.run(asgi_client.get("/api/artifacts/preview", params={"path": str(html_artifact)}))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    data = response.json()
    assert data["kind"] == "text"
    # The JSON endpoint reports the guessed content type, but the raw endpoint inerting happens separately
    assert "content_type" in data
