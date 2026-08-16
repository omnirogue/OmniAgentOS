"""Regression probe for the AC-policy fix7 control-plane confinement.

Proves, against the REAL app with the app-level session-token gate ACTIVE
(``@pytest.mark.real_auth`` opts out of the suite-wide bypass in
``tests/conftest.py``):

  1. Every mutating route family (projects / provision / routines / suggestions /
     intake / control) is 401 WITHOUT the local session token and reachable
     (never 401) WITH it.
  2. Reads stay public (no token required).
  3. The comms webhook is allowlisted off the session gate (it carries its own
     ``X-Comms-Token``), so it is NOT rejected by the session gate.
  4. A ``create_project`` / ``provision`` grant that targets a secret-registry dir
     (``~/.ssh``) is HARD-REJECTED (422), not accepted.

Together these close the Round-7 loopback-POST self-grant escape: an agent that
cannot read the 0600 token file cannot author scope OR runs, and even a
token-bearing caller cannot grant itself a credential directory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request

import omniagentos.api.main as api_main
from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.contracts import ActionClass
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectStore
from omniagentos.sessions import token

pytestmark = pytest.mark.real_auth


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


@pytest.fixture
def token_header(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return {"X-Session-Token": token.load_or_create_token()}


@pytest.fixture
def client(store: SqliteStore) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: store
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield c
    finally:
        app.dependency_overrides.clear()


# (method, path, json-body) — one representative mutating route per gated family.
_MUTATING_ROUTES = [
    ("POST", "/api/projects", {"name": "probe"}),
    ("POST", "/api/routines", {"name": "probe"}),
    ("POST", "/api/suggestions/sug_missing/approve", {"approved_by": "operator"}),
    ("POST", "/api/intake/dispatch", {"title": "probe"}),
    ("POST", "/api/tasks", {"title": "probe"}),
    ("PUT", "/api/pause", {"paused": True}),
    ("POST", "/api/disciplines", {"id": "d1", "name": "d1"}),
]


@pytest.mark.parametrize(("method", "path", "body"), _MUTATING_ROUTES)
def test_mutating_route_401_without_token(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any]
) -> None:
    resp = _run(client.request(method, path, json=body))
    assert resp.status_code == 401, f"{method} {path} was not gated: {resp.status_code}"
    assert resp.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(("method", "path", "body"), _MUTATING_ROUTES)
def test_mutating_route_reachable_with_token(
    client: httpx.AsyncClient,
    token_header: dict[str, str],
    method: str,
    path: str,
    body: dict[str, Any],
) -> None:
    resp = _run(client.request(method, path, json=body, headers=token_header))
    # With a valid token the gate lets the request through to the handler — the
    # outcome may be 200/201/400/404/422 depending on payload, but NEVER 401.
    assert resp.status_code != 401, f"{method} {path} rejected a valid token"


def test_provision_request_401_without_token(client: httpx.AsyncClient, store: SqliteStore) -> None:
    project = ProjectStore(store).create_project({"name": "p"})
    resp = _run(client.post(f"/api/provision/{project['id']}/request", json={"dir": "/x"}))
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_reads_stay_public(client: httpx.AsyncClient) -> None:
    for path in ("/api/health", "/api/projects", "/api/routines"):
        resp = _run(client.get(path))
        assert resp.status_code == 200, f"GET {path} should be public"


def test_machine_session_token_is_refused_on_cross_principal_read(
    client: httpx.AsyncClient, token_header: dict[str, str]
) -> None:
    """A raw machine bearer resolves to ``system``, not an operator identity."""
    response = _run(client.get("/api/access/servers", headers=token_header))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "system_principal_forbidden"


def test_cross_principal_read_counterfeit_passes_when_system_deny_list_is_removed(
    client: httpx.AsyncClient,
    token_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Counterfeit: deleting the policy recreates the machine-token bypass."""
    monkeypatch.setattr(api_main, "_SYSTEM_DENY_RULES", ())
    response = _run(client.get("/api/access/servers", headers=token_header))
    assert response.status_code == 200


def test_composition_sensitive_route_denied_without_the_real_hop_assertion(
    client: httpx.AsyncClient, token_header: dict[str, str]
) -> None:
    """The half of the composition check with no assertion at all.

    ``X-Omni-Authenticated-Principal`` is the SAME header name and derivation
    the dashboard's server-side proxy resolves from a signed browser-credential
    cookie elsewhere in the estate (see the constant's definition in
    ``omniagentos/api/main.py``) — never ``Tailscale-User-Login``, which is a
    different hop that never reaches this process. A request that carries
    nothing but the shared session token — no assertion of any kind — is the
    unauthenticated machine subject and stays denied on a sensitive route.
    """
    response = _run(client.get("/api/access/servers", headers=token_header))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "system_principal_forbidden"


def test_composition_sensitive_route_reachable_with_the_real_hop_assertion(
    client: httpx.AsyncClient, token_header: dict[str, str]
) -> None:
    """The other half: a well-formed assertion of the real header reaches the route."""
    response = _run(
        client.get(
            "/api/access/servers",
            headers={**token_header, "X-Omni-Authenticated-Principal": "owner@example.test"},
        )
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "malformed",
    [
        "   ",  # empty after stripping
        "a" * 257,  # over the 256-character shape limit
        "owner\x00@example.test",  # embedded control character (NUL)
        "owner\r\n@example.test",  # embedded control character (CRLF)
    ],
)
def test_a_malformed_principal_assertion_is_treated_as_system_not_human(
    client: httpx.AsyncClient, token_header: dict[str, str], malformed: str
) -> None:
    """A malformed assertion is not a softer form of "human".

    Mirrors the shape validation the dashboard-side producer applies before it
    ever mints this header. Accepting anything non-empty at face value would
    let a corrupted or truncated assertion silently pass as an operator on a
    sensitive route.
    """
    response = _run(
        client.get(
            "/api/access/servers",
            headers={**token_header, "X-Omni-Authenticated-Principal": malformed},
        )
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "system_principal_forbidden"


def _route_request(method: str, path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
        }
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/api/autonomy"),
        ("POST", "/api/improvements/imp_1/approve"),
        ("GET", "/api/access/servers"),
        ("GET", "/api/accounts"),
        ("POST", "/api/approvals/apr_1/decision"),
        ("POST", "/api/suggestions/sug_1/approve"),
        ("PUT", "/api/access/agents/agent_1"),
        ("POST", "/api/grok/grants"),
        ("POST", "/api/grok/grants/grant_1/revoke"),
    ],
)
def test_system_deny_list_covers_second_factor_cross_principal_and_parked_actions(
    method: str, path: str
) -> None:
    assert api_main._system_route_is_denied(_route_request(method, path))


def test_comms_webhook_allowlisted_off_session_gate(client: httpx.AsyncClient) -> None:
    """POST /api/comms/inbound is mutating but carries its own X-Comms-Token, so the
    session gate must NOT own its rejection — it must reach comms' own auth."""
    resp = _run(client.post("/api/comms/inbound?source=unknown", json={}))
    # comms' own auth rejects an unknown source; the point is we got PAST the
    # session gate to comms' handler (its message differs from the session gate's).
    assert resp.status_code == 401
    assert "session token" not in resp.json()["error"]["message"]
    assert "comms" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# AC-policy hook-auth: /api/sessions/hook-eval is the ONE deliberate exception
# to the gate proven above -- a sandboxed bridge session cannot read the full
# session token, so it authenticates with a narrowly-scoped per-session
# credential instead (sessions.hook_token). These probes run with the REAL
# app-level gate active (this whole module is @pytest.mark.real_auth), so they
# are the only tests that actually prove api.main._PUBLIC_MUTATION_PATHS is
# wired correctly -- tests/sessions/api/test_routes.py's hook-eval coverage
# runs under the suite-wide bypass and would pass even without that wiring.
# ---------------------------------------------------------------------------


def test_hook_eval_scoped_credential_reaches_the_handler_with_the_real_gate_active(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A request with NEITHER header reaches hook_eval's OWN auth (message
    "invalid session token"), never the app-level gate's blanket rejection
    ("invalid or missing session token") -- the same technique
    test_comms_webhook_allowlisted_off_session_gate uses for comms.inbound. A
    valid per-session credential (and NO X-Session-Token at all) IS authorized.
    """
    from omniagentos.api.routes import sessions as sessions_routes
    from omniagentos.sessions import hook_token
    from tests.sessions.api.test_routes import FakeSessionsDal

    monkeypatch.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")
    fake_dal = FakeSessionsDal()
    monkeypatch.setattr(sessions_routes, "get_sessions_dal", lambda: fake_dal)

    unauthenticated = _run(
        client.post(
            "/api/sessions/hook-eval",
            json={"session_id": "ses_1", "tool_name": "Read", "tool_input": {}, "cwd": "/project"},
        )
    )
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["message"] == "invalid session token"

    scoped = hook_token.issue_hook_token("ses_1")
    monkeypatch.setattr(
        sessions_routes, "classify_tool", lambda *_: ActionClass.INTERNAL_REVERSIBLE
    )
    authorized = _run(
        client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Hook-Token": scoped},
            json={
                "session_id": "ses_1",
                "tool_name": "Write",
                "tool_input": {"content": "x", "file_path": "/project/a"},
                "cwd": "/project",
            },
        )
    )
    assert authorized.status_code == 200
    assert authorized.json()["decision"] == "allow"


def test_hook_scoped_credential_does_not_open_any_other_route_under_the_real_gate(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only /api/sessions/hook-eval is allowlisted -- every OTHER mutating route
    (including every other /api/sessions/* route) still needs the real token
    under the ACTIVE app-level gate; X-Session-Hook-Token means nothing to it."""
    from omniagentos.sessions import hook_token

    monkeypatch.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")
    scoped = hook_token.issue_hook_token("ses_1")
    resp = _run(client.post("/api/sessions/ses_1/kill", headers={"X-Session-Hook-Token": scoped}))
    assert resp.status_code == 401
    assert resp.json()["error"]["message"] == "invalid or missing session token"


def test_create_project_secret_dir_grant_rejected(
    client: httpx.AsyncClient,
    token_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = str(tmp_path / ".ssh")
    resp = _run(
        client.post(
            "/api/projects",
            json={"name": "evil", "root_dirs": [ssh_dir]},
            headers=token_header,
        )
    )
    assert resp.status_code == 422
    assert "secret" in resp.json()["error"]["message"].lower()


def test_simgate_refusals_are_forbidden_for_project_and_provision_grants(
    client: httpx.AsyncClient,
    store: SqliteStore,
    token_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN", raising=False)

    project_response = _run(
        client.post("/api/projects", json={"name": "blocked"}, headers=token_header)
    )
    assert project_response.status_code == 403
    assert project_response.json()["error"]["code"] == "forbidden"

    project = ProjectStore(store).create_project({"name": "existing"})
    provision_response = _run(
        client.post(
            f"/api/provision/{project['id']}/request",
            json={"dir": "/etc"},
            headers=token_header,
        )
    )
    assert provision_response.status_code == 403
    assert provision_response.json()["error"]["code"] == "forbidden"


def test_provision_secret_dir_grant_rejected(
    client: httpx.AsyncClient,
    store: SqliteStore,
    token_header: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    project = ProjectStore(store).create_project({"name": "p"})
    ssh_dir = str(tmp_path / ".ssh")
    resp = _run(
        client.post(
            f"/api/provision/{project['id']}/request",
            json={"dir": ssh_dir},
            headers=token_header,
        )
    )
    assert resp.status_code == 422
    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert ssh_dir not in (row["root_dirs"] or [])
