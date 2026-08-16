"""Hermetic tests for the optional Google Drive API search backend."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

from omniagentos.filesearch import gdrive_api


def _install_google_stubs(monkeypatch: pytest.MonkeyPatch, build: Any, default: Any = None) -> None:
    """Provide the small optional Google module surface used by gdrive_api."""
    google = types.ModuleType("google")
    auth = types.ModuleType("google.auth")
    discovery = types.ModuleType("googleapiclient.discovery")
    client = types.ModuleType("googleapiclient")
    auth.default = default or (lambda *, scopes: (object(), "project"))
    discovery.build = build
    google.auth = auth
    client.discovery = discovery
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.auth", auth)
    monkeypatch.setitem(sys.modules, "googleapiclient", client)
    monkeypatch.setitem(sys.modules, "googleapiclient.discovery", discovery)


def test_is_available_gracefully_degrades_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean machine (no stored OAuth token) needs neither live auth nor an exception."""
    monkeypatch.setattr(gdrive_api, "_TOKEN_PATH", tmp_path / "missing-token.json")
    assert gdrive_api.is_available() is False


def test_search_drive_normalises_drive_results(monkeypatch: pytest.MonkeyPatch) -> None:
    build_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    list_calls: list[dict[str, object]] = []
    payload = {
        "files": [
            {
                "id": "folder-1",
                "name": "Projects",
                "mimeType": "application/vnd.google-apps.folder",
                "modifiedTime": "2026-07-21T12:00:00.000Z",
            },
            {
                "id": "pdf-1",
                "name": "invoice.pdf",
                "mimeType": "application/pdf",
                "size": "1234",
                "modifiedTime": "2026-07-20T08:00:00Z",
                "webViewLink": "https://drive.google.com/file/d/pdf-1/view",
            },
            {
                "id": "doc-1",
                "name": "Plan",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-07-19T00:00:00Z",
            },
        ]
    }

    class Request:
        def execute(self, *, num_retries: int) -> dict[str, object]:
            assert num_retries == 0
            return payload

    class Files:
        def list(self, **kwargs: object) -> Request:
            list_calls.append(kwargs)
            return Request()

    class Service:
        def files(self) -> Files:
            return Files()

    def build(*args: object, **kwargs: object) -> Service:
        build_calls.append((args, kwargs))
        return Service()

    _install_google_stubs(monkeypatch, build)
    monkeypatch.setattr(gdrive_api, "_load_credentials", lambda: object())
    results = gdrive_api.search_drive("quarterly")

    expected_keys = {"path", "name", "source", "kind", "size", "modified", "snippet", "score"}
    assert build_calls[0][0] == ("drive", "v3")
    assert build_calls[0][1]["cache_discovery"] is False
    assert "credentials" in build_calls[0][1]
    assert list_calls[0]["pageSize"] == 20
    assert list_calls[0]["supportsAllDrives"] is False
    assert list_calls[0]["q"] == "fullText contains 'quarterly' or name contains 'quarterly'"
    assert all(set(result) == expected_keys for result in results)
    assert [result["kind"] for result in results] == ["folder", "pdf", "doc"]
    assert all(result["source"] == "gdrive" for result in results)
    assert all(result["snippet"] == "" and result["score"] == 0.0 for result in results)
    assert results[0]["path"] == "gdrive:folder-1"
    assert results[1]["path"] == "https://drive.google.com/file/d/pdf-1/view"
    assert results[1]["size"] == 1234
    assert results[1]["modified"] == "2026-07-20"


def test_search_drive_returns_empty_list_when_build_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_build(*args: object, **kwargs: object) -> object:
        raise ImportError("client unavailable")

    _install_google_stubs(monkeypatch, broken_build)
    monkeypatch.setattr(gdrive_api, "_load_credentials", lambda: object())
    assert gdrive_api.search_drive("anything") == []
