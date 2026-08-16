"""Artifact preview path safety."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omniagentos.api.routes.artifacts_preview import _resolve_safe, preview_artifact
from omniagentos.api.services import ApiError


def test_preview_text_under_var_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asgi_client: httpx.AsyncClient
) -> None:
    import omniagentos.api.routes.artifacts_preview as mod

    var = tmp_path / "var"
    art = var / "artifacts" / "scope1"
    art.mkdir(parents=True)
    target = art / "script.md"
    target.write_text("# Hello\n", encoding="utf-8")
    monkeypatch.setattr(mod, "_repo_var", lambda: var)
    response = asyncio.run(asgi_client.get("/api/artifacts/preview", params={"path": str(target)}))
    assert response.status_code == 200
    out = response.json()
    assert out["kind"] == "text"
    assert out["content_type"] == "text/markdown"
    assert "Hello" in out["text"]


def test_preview_image_has_working_raw_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asgi_client: httpx.AsyncClient
) -> None:
    import omniagentos.api.routes.artifacts_preview as mod

    var = tmp_path / "var"
    target = var / "artifacts" / "scope1" / "preview.png"
    target.parent.mkdir(parents=True)
    png = b"\x89PNG\r\n\x1a\npreview"
    target.write_bytes(png)
    monkeypatch.setattr(mod, "_repo_var", lambda: var)

    response = asyncio.run(asgi_client.get("/api/artifacts/preview", params={"path": str(target)}))
    assert response.status_code == 200
    out = response.json()
    assert out["kind"] == "image"
    assert out["content_type"] == "image/png"
    assert out["url"].startswith("/api/artifacts/preview/raw?")
    raw = asyncio.run(asgi_client.get(out["url"]))
    assert raw.status_code == 200
    assert raw.headers["content-type"].startswith("image/png")
    assert raw.content == png


def test_preview_allows_board_outputs_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import omniagentos.api.routes.artifacts_preview as mod

    var = tmp_path / "var"
    target = var / "intake-workspace" / "board-42" / "outputs" / "result.txt"
    target.parent.mkdir(parents=True)
    target.write_text("board output", encoding="utf-8")
    monkeypatch.setattr(mod, "_repo_var", lambda: var)

    out = preview_artifact(str(target))
    assert out["kind"] == "text"
    assert out["text"] == "board output"


def test_preview_rejects_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import omniagentos.api.routes.artifacts_preview as mod

    var = tmp_path / "var"
    (var / "artifacts").mkdir(parents=True)
    monkeypatch.setattr(mod, "_repo_var", lambda: var)
    with pytest.raises(ApiError) as ei:
        _resolve_safe("/etc/passwd")
    assert ei.value.status_code == 403

    with pytest.raises(ApiError) as ei:
        _resolve_safe("artifacts/../outside.txt")
    assert ei.value.status_code == 403


def test_preview_rejects_var_file_outside_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.api.routes.artifacts_preview as mod

    var = tmp_path / "var"
    target = var / "projects" / "project-1" / "secret.txt"
    target.parent.mkdir(parents=True)
    target.write_text("not an output", encoding="utf-8")
    monkeypatch.setattr(mod, "_repo_var", lambda: var)

    with pytest.raises(ApiError) as ei:
        _resolve_safe(str(target))
    assert ei.value.status_code == 403


def test_preview_rejects_artifact_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.api.routes.artifacts_preview as mod

    var = tmp_path / "var"
    artifact_dir = var / "artifacts"
    artifact_dir.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    escaped_link = artifact_dir / "escaped.txt"
    escaped_link.symlink_to(outside)
    monkeypatch.setattr(mod, "_repo_var", lambda: var)

    with pytest.raises(ApiError) as ei:
        _resolve_safe(str(escaped_link))
    assert ei.value.status_code == 403
