"""HTTP unit tests — mount workfs router on a throwaway FastAPI app."""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omniagentos.api.routes import workfs as workfs_routes
from omniagentos.api.services import ApiError
from omniagentos.workfs.root import WORK_ROOT_ENV

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="workfs requires Darwin kernel atomic operations (renameatx_np)"
)


def _throwaway_app() -> FastAPI:
    app = FastAPI()
    app.include_router(workfs_routes.router)

    @app.exception_handler(ApiError)
    async def _api_error(_: Any, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "detail": exc.detail,
                }
            },
        )

    return app


@pytest.fixture
def client(work_tmp: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv(WORK_ROOT_ENV, str(work_tmp))
    with TestClient(_throwaway_app()) as c:
        yield c


def test_route_set_is_exactly_tree_and_ensure() -> None:
    """WORKFS-E2: mounted path set == {GET /api/workfs/tree, POST /api/workfs/ensure}."""
    routes = []
    for r in workfs_routes.router.routes:
        methods = sorted(getattr(r, "methods", []) or [])
        path = getattr(r, "path", None)
        if path is None:
            continue
        for m in methods:
            if m in ("HEAD", "OPTIONS"):
                continue
            routes.append((m, path))
    assert sorted(routes) == [
        ("GET", "/api/workfs/tree"),
        ("POST", "/api/workfs/ensure"),
    ]


def test_get_tree_empty(client: TestClient) -> None:
    r = client.get("/api/workfs/tree")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entries"] == []
    assert body["max_depth"] == 3


def test_get_tree_shows_oob_folder(client: TestClient, work_tmp: Path) -> None:
    (work_tmp / "LiveCo" / "Sales").mkdir(parents=True)
    r = client.get("/api/workfs/tree")
    assert r.status_code == 200
    names = {e["name"] for e in r.json()["entries"]}
    assert "LiveCo" in names


def test_post_ensure_creates_and_idempotent(client: TestClient, work_tmp: Path) -> None:
    r1 = client.post(
        "/api/workfs/ensure",
        json={"company": "RouteCo", "department": "Ops", "subfolder": "Meetings"},
    )
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert body1["created"] is True
    assert Path(body1["path"]).is_dir()
    assert (work_tmp / "RouteCo" / "Ops" / "Meetings").is_dir()

    r2 = client.post(
        "/api/workfs/ensure",
        json={"company": "RouteCo", "department": "Ops", "subfolder": "Meetings"},
    )
    assert r2.status_code == 200
    assert r2.json()["created"] is False
    assert r2.json()["path"] == body1["path"]


def test_post_ensure_traversal_refused(client: TestClient, work_tmp: Path) -> None:
    r = client.post("/api/workfs/ensure", json={"company": ".."})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_scope_component"

    r2 = client.post(
        "/api/workfs/ensure",
        json={"company": "Ok", "department": ".."},
    )
    assert r2.status_code == 400


def test_post_ensure_absent_company_refused(client: TestClient, work_tmp: Path) -> None:
    r = client.post("/api/workfs/ensure", json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "scope_required"


def test_post_ensure_dangling_symlink_is_400_not_500(client: TestClient, work_tmp: Path) -> None:
    """Defect 2: broken link under WORK must not 500 the ensure route."""
    (work_tmp / "DangleRouteCo").symlink_to(work_tmp / "missing-route-target")
    r = client.post(
        "/api/workfs/ensure",
        json={"company": "DangleRouteCo", "department": "Ops"},
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert "error" in body
    assert body["error"]["code"] in {
        "target_not_directory",
        "ensure_failed",
        "path_outside_root",
        "containment_lost",
    }
