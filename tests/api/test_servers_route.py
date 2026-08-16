"""GET /api/access/servers -- the server fleet feeder for the dashboard's Servers section.

The repo's known failure mode here is client-path <-> route drift (dashboard/src/
features/capabilities/client.ts calling a path the backend does not actually serve),
so this asserts the LITERAL path resolves, not just that some route under the router
prefix works.

Finding 5: this route returns live hosts/users/ports/purposes/status and SSH-key
*paths* for the whole fleet -- recon-sensitive even though it never returns key
material -- so, unlike ``GET /api/access/capabilities`` (a static catalogue), it
must be gated behind the local session token. The gate-specific probes below run
with the REAL app-level gate active (``@pytest.mark.real_auth`` opts out of the
suite-wide bypass in ``tests/conftest.py``), mirroring
``tests/api/test_mounts_routes.py``'s ``TestMountsRoutesAreGated``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omniagentos.sessions import token


def test_servers_route_resolves_at_the_literal_path(asgi_client: httpx.AsyncClient) -> None:
    response = asyncio.run(asgi_client.get("/api/access/servers"))
    assert response.status_code == 200


def test_servers_route_returns_the_documented_active_fleet(asgi_client: httpx.AsyncClient) -> None:
    response = asyncio.run(asgi_client.get("/api/access/servers"))
    body = response.json()
    assert "servers" in body
    servers = body["servers"]
    assert isinstance(servers, list)
    assert servers, "the real inventory must yield at least one server"

    aliases = {row["alias"] for row in servers}
    assert "initech-roi-calculator" in aliases
    assert "agentproacademy" in aliases

    for row in servers:
        assert set(row) == {"alias", "host", "user", "key", "status", "purpose", "sites"}
        # Never key material -- only a filename/path.
        assert row["key"].startswith("~/.ssh/")

    statuses = {row["alias"]: row["status"] for row in servers}
    assert any(s.startswith("ACTIVE") for s in statuses.values())
    assert any(s.startswith("EPHEMERAL") for s in statuses.values())
    assert any(s.startswith("LEGACY-STALE") for s in statuses.values())


# --- app-level session-token gate (real_auth) ----------------------------------


@pytest.mark.real_auth
class TestServersRouteIsGated:
    @pytest.fixture
    def token_header(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
        monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
        return {"X-Session-Token": token.load_or_create_token()}

    def test_servers_401_without_token(self, asgi_client: httpx.AsyncClient) -> None:
        resp = asyncio.run(asgi_client.get("/api/access/servers"))
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_servers_reachable_with_non_system_principal(
        self, asgi_client: httpx.AsyncClient, token_header: dict[str, str]
    ) -> None:
        resp = asyncio.run(
            asgi_client.get(
                "/api/access/servers",
                headers={**token_header, "X-Omni-Authenticated-Principal": "owner@example.test"},
            )
        )
        assert resp.status_code == 200
        assert "servers" in resp.json()

    def test_capabilities_catalogue_stays_public_under_the_real_gate(
        self, asgi_client: httpx.AsyncClient
    ) -> None:
        """Only the exact /api/access/servers path is gated -- the rest of
        /api/access/* (e.g. the static capability catalogue) must stay
        reachable without a token, proving this is a literal-path match and
        not a blanket gate over the whole /api/access/* tree."""
        resp = asyncio.run(asgi_client.get("/api/access/capabilities"))
        assert resp.status_code == 200
