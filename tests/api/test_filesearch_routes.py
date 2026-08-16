"""Filesearch HTTP surface: catalog-filter params, the semantic endpoint's envelope,
and the reveal endpoint's index-membership floor (never trust a client path).

Hermetic — tmp catalog DB; Finder launches and platform are faked; semantic_query is
stubbed at the route seam.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from omniagentos.api.main import app
from omniagentos.api.routes import filesearch as filesearch_routes
from omniagentos.filesearch import catalog


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")


@pytest.fixture
def seeded_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tmp catalog with one REAL indexed file plus one gdrive metadata row."""
    monkeypatch.setattr(catalog, "catalog_path", lambda: str(tmp_path / "catalog.db"))
    real = tmp_path / "report.md"
    real.write_text("quarterly report")
    conn = catalog._connect()
    conn.execute(
        "INSERT INTO files (path, source, name, ext, size, mtime, indexed_at, doc, "
        "category, root) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (str(real), "local", "report.md", ".md", 16, 200.0, 0.0, "report", "documents", "desktop"),
    )
    conn.execute(
        "INSERT INTO files (path, source, name, ext, size, mtime, indexed_at, doc, "
        "category, root) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "/gdrive/budget.xlsx",
            "gdrive",
            "budget.xlsx",
            ".xlsx",
            9,
            100.0,
            0.0,
            "budget",
            "spreadsheets",
            "gdrive",
        ),
    )
    conn.commit()
    conn.close()
    return real


@pytest.fixture
def fake_open(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Capture the reveal argv WITHOUT launching Finder; pin the host to Darwin."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> Any:
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(filesearch_routes.subprocess, "run", fake_run)
    monkeypatch.setattr(filesearch_routes.platform, "system", lambda: "Darwin")
    monkeypatch.delenv(filesearch_routes._DISABLE_REVEAL_ENV, raising=False)
    return calls


# --- GET /api/filesearch (catalog-filter params) --------------------------------


def test_filesearch_requires_token(client: httpx.AsyncClient, seeded_catalog: Path) -> None:
    resp = _run(client.get("/api/filesearch", params={"q": "x"}))
    assert resp.status_code == 401


def test_filesearch_bare_request_still_422(
    client: httpx.AsyncClient, auth_headers: dict[str, str], seeded_catalog: Path
) -> None:
    resp = _run(client.get("/api/filesearch", headers=auth_headers))
    assert resp.status_code == 422


def test_filesearch_filter_params_return_rows(
    client: httpx.AsyncClient, auth_headers: dict[str, str], seeded_catalog: Path
) -> None:
    resp = _run(
        client.get(
            "/api/filesearch",
            params={"root": "desktop", "category": "documents", "sort": "name"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "catalog"
    assert body["count"] == 1
    row = body["rows"][0]
    assert row["path"] == str(seeded_catalog)
    assert row["root"] == "desktop"
    assert row["category"] == "documents"
    assert row["mtime"] == 200.0  # epoch seconds


def test_filesearch_filter_rejects_unknown_root(
    client: httpx.AsyncClient, auth_headers: dict[str, str], seeded_catalog: Path
) -> None:
    resp = _run(client.get("/api/filesearch", params={"root": "everything"}, headers=auth_headers))
    assert resp.status_code == 422


def test_filesearch_sort_recency_across_roots(
    client: httpx.AsyncClient, auth_headers: dict[str, str], seeded_catalog: Path
) -> None:
    resp = _run(client.get("/api/filesearch", params={"sort": "recency"}, headers=auth_headers))
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    assert [r["mtime"] for r in rows] == [200.0, 100.0]


# --- GET /api/filesearch/semantic ------------------------------------------------


def test_semantic_endpoint_returns_rows(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    canned = [
        {
            "path": "/a.md",
            "root": "icloud",
            "category": "documents",
            "mtime": 5.0,
            "score": 0.91,
            "excerpt": "the part that matched",
        }
    ]
    seen: dict[str, Any] = {}

    def fake_semantic_query(q: str, root=None, category=None, limit=20) -> list[dict[str, Any]]:
        seen.update({"q": q, "root": root, "category": category, "limit": limit})
        return canned

    monkeypatch.setattr(filesearch_routes, "semantic_query", fake_semantic_query)
    resp = _run(
        client.get(
            "/api/filesearch/semantic",
            params={"q": "the doc about pricing", "root": "icloud", "limit": 5},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"] == canned
    assert body["count"] == 1
    assert seen == {"q": "the doc about pricing", "root": "icloud", "category": None, "limit": 5}


def test_semantic_endpoint_503_when_backend_down(
    client: httpx.AsyncClient, auth_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(q: str, root=None, category=None, limit=20) -> list[dict[str, Any]]:
        raise filesearch_routes.SemanticUnavailable("ollama is down")

    monkeypatch.setattr(filesearch_routes, "semantic_query", boom)
    resp = _run(client.get("/api/filesearch/semantic", params={"q": "x"}, headers=auth_headers))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "semantic_unavailable"


def test_semantic_endpoint_requires_token(client: httpx.AsyncClient) -> None:
    resp = _run(client.get("/api/filesearch/semantic", params={"q": "x"}))
    assert resp.status_code == 401


# --- POST /api/filesearch/reveal (index-membership floor) ------------------------


def test_reveal_happy_path_uses_indexed_path(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded_catalog: Path,
    fake_open: list[list[str]],
) -> None:
    resp = _run(
        client.post(
            "/api/filesearch/reveal",
            json={"path": str(seeded_catalog), "root": "desktop", "app": "finder"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["revealed"] is True and body["app"] == "finder"
    assert fake_open[-1] == ["/usr/bin/open", "-R", str(seeded_catalog)]


def test_reveal_refuses_path_not_in_index(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded_catalog: Path,
    fake_open: list[list[str]],
    tmp_path: Path,
) -> None:
    outside = tmp_path / "not-indexed.txt"
    outside.write_text("exists on disk but NOT in the index")
    resp = _run(
        client.post("/api/filesearch/reveal", json={"path": str(outside)}, headers=auth_headers)
    )
    assert resp.status_code == 403
    assert fake_open == []  # never reached the launcher


def test_reveal_refuses_traversal_spelling_of_indexed_path(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded_catalog: Path,
    fake_open: list[list[str]],
) -> None:
    """Same file, different spelling → not byte-for-byte an index row → 403."""
    sneaky = str(seeded_catalog.parent) + "/../" + seeded_catalog.parent.name + "/report.md"
    assert os.path.exists(sneaky)
    resp = _run(client.post("/api/filesearch/reveal", json={"path": sneaky}, headers=auth_headers))
    assert resp.status_code == 403
    assert fake_open == []


def test_reveal_kill_switch(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded_catalog: Path,
    fake_open: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(filesearch_routes._DISABLE_REVEAL_ENV, "1")
    resp = _run(
        client.post(
            "/api/filesearch/reveal", json={"path": str(seeded_catalog)}, headers=auth_headers
        )
    )
    assert resp.status_code == 403
    assert fake_open == []


def test_reveal_root_mismatch_403(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded_catalog: Path,
    fake_open: list[list[str]],
) -> None:
    resp = _run(
        client.post(
            "/api/filesearch/reveal",
            json={"path": str(seeded_catalog), "root": "gdrive"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 403
    assert fake_open == []


def test_reveal_unknown_app_422_and_non_darwin_501(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    seeded_catalog: Path,
    fake_open: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resp = _run(
        client.post(
            "/api/filesearch/reveal",
            json={"path": str(seeded_catalog), "app": "vscode"},
            headers=auth_headers,
        )
    )
    assert resp.status_code == 422

    monkeypatch.setattr(filesearch_routes.platform, "system", lambda: "Linux")
    resp = _run(
        client.post(
            "/api/filesearch/reveal", json={"path": str(seeded_catalog)}, headers=auth_headers
        )
    )
    assert resp.status_code == 501
    assert fake_open == []


def test_reveal_requires_token(client: httpx.AsyncClient, seeded_catalog: Path) -> None:
    resp = _run(client.post("/api/filesearch/reveal", json={"path": str(seeded_catalog)}))
    assert resp.status_code == 401
