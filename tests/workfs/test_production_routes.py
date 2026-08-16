"""Production-app reachability and auth contract for mounted WorkFS routes."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.api.routes.workfs import router as workfs_router
from omniagentos.sessions import token
from omniagentos.workfs.root import WORK_ROOT_ENV

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="workfs requires Darwin kernel atomic operations (renameatx_np)"
)


def test_production_app_and_committed_openapi_include_workfs() -> None:
    """I1: both production routes are mounted and represented in the artifact."""
    # This FastAPI version retains included routers as entries in ``app.routes``.
    # Verify the production app owns this exact router, then its concrete paths.
    mounted = [
        entry for entry in app.routes if getattr(entry, "original_router", None) is workfs_router
    ]
    assert len(mounted) == 1
    routes = {
        (method, route.path)
        for route in workfs_router.routes
        for method in (getattr(route, "methods", set()) or set())
        if method not in {"HEAD", "OPTIONS"}
    }
    assert ("GET", "/api/workfs/tree") in routes
    assert ("POST", "/api/workfs/ensure") in routes

    schema = json.loads(Path("contracts/openapi.json").read_text(encoding="utf-8"))
    assert "/api/workfs/tree" in schema["paths"]
    assert "get" in schema["paths"]["/api/workfs/tree"]
    assert "/api/workfs/ensure" in schema["paths"]
    assert "post" in schema["paths"]["/api/workfs/ensure"]


@pytest.mark.real_auth
def test_production_workfs_routes_require_session_and_keep_400_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WorkFS rejects the raw machine bearer but admits an asserted principal."""
    root = tmp_path / "Work"
    root.mkdir()
    monkeypatch.setenv(WORK_ROOT_ENV, str(root))
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    headers = {"X-Session-Token": token.load_or_create_token()}
    operator_headers = {
        **headers,
        "X-Omni-Authenticated-Principal": "human:operator",
    }
    client = TestClient(app)

    assert client.get("/api/workfs/tree").status_code == 401
    assert client.post("/api/workfs/ensure", json={"company": "Acme"}).status_code == 401

    machine_tree = client.get("/api/workfs/tree", headers=headers)
    assert machine_tree.status_code == 403, machine_tree.text
    assert machine_tree.json()["error"]["code"] == "system_principal_forbidden"

    tree = client.get("/api/workfs/tree", headers=operator_headers)
    assert tree.status_code == 200, tree.text
    invalid = client.post(
        "/api/workfs/ensure", json={"company": ".."}, headers=operator_headers
    )
    assert invalid.status_code == 400, invalid.text
    assert invalid.json()["error"]["code"] == "invalid_scope_component"

    ensure_response = client.post(
        "/api/workfs/ensure",
        json={"company": "Acme", "department": "Operations"},
        headers=operator_headers,
    )
    assert ensure_response.status_code == 200, ensure_response.text
    assert (root / "Acme" / "Operations").is_dir()
