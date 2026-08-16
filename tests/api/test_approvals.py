from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.sessions import token


@pytest.fixture
def token_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return token.load_or_create_token()


def _seed_approval(store: object, approval_id: str, **overrides: object) -> None:
    row = {
        "id": approval_id,
        "run_id": "run_test",
        "task_id": "tsk_test",
        "step_seq": 0,
        "action_class": "consequential",
        "proposed_action": "publish",
        "params_json": "{}",
        "risk": "high",
        "evidence": "",
        "state": "pending",
        "decided_by": None,
        "decision_note": None,
        "decided_at": None,
        "expires_at": None,
        "created_at": utc_now_iso(),
        "session_id": None,
    }
    row.update(overrides)
    store.create_approval(row)  # type: ignore[attr-defined]


def test_approval_decision_is_single_use(
    asgi_client: httpx.AsyncClient, store: object, token_value: str
) -> None:
    # The approval table is normally populated by the runner; use the test store
    # to arrange that boundary state.
    _seed_approval(store, "apr_test")
    headers = {"X-Session-Token": token_value}
    first = asyncio.run(
        asgi_client.post(
            "/api/approvals/apr_test/decision", headers=headers, json={"decision": "approved"}
        )
    )
    second = asyncio.run(
        asgi_client.post(
            "/api/approvals/apr_test/decision", headers=headers, json={"decision": "rejected"}
        )
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_decision_requires_local_token(asgi_client: httpx.AsyncClient, store: object) -> None:
    # SEC-005: the state-changing decide route rejects an unauthenticated caller.
    _seed_approval(store, "apr_noauth")
    response = asyncio.run(
        asgi_client.post("/api/approvals/apr_noauth/decision", json={"decision": "approved"})
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_session_approval_ignores_client_decided_by(
    asgi_client: httpx.AsyncClient, store: object, token_value: str
) -> None:
    # SEC-005: a session-linked approval stamps a server-side operator identity so a
    # forged client decided_by (e.g. impersonating a human) is ignored.
    _seed_approval(store, "apr_session", session_id="ses_1")
    response = asyncio.run(
        asgi_client.post(
            "/api/approvals/apr_session/decision",
            headers={"X-Session-Token": token_value},
            json={"decision": "approved", "decided_by": "owner-the-human"},
        )
    )
    assert response.status_code == 200
    assert response.json()["decided_by"] == "operator"


def test_run_approval_keeps_client_decided_by(
    asgi_client: httpx.AsyncClient, store: object, token_value: str
) -> None:
    # Run-scoped approvals keep the existing client-supplied identity behavior.
    _seed_approval(store, "apr_run", session_id=None)
    response = asyncio.run(
        asgi_client.post(
            "/api/approvals/apr_run/decision",
            headers={"X-Session-Token": token_value},
            json={"decision": "approved", "decided_by": "owner"},
        )
    )
    assert response.status_code == 200
    assert response.json()["decided_by"] == "owner"


def test_project_approvals_include_global_session_approvals_without_other_projects(
    asgi_client: httpx.AsyncClient, store: object
) -> None:
    # Session approvals are intentionally global when they have no task owned by
    # a project. They must remain visible in a project-scoped approvals view.
    store.create_task({"id": "tsk_project_a", "project_id": "prj_a"})  # type: ignore[attr-defined]
    store.create_task({"id": "tsk_project_b", "project_id": "prj_b"})  # type: ignore[attr-defined]
    _seed_approval(store, "apr_project_a", task_id="tsk_project_a")
    _seed_approval(store, "apr_project_b", task_id="tsk_project_b")
    # approvals.run_id and approvals.task_id are nullable in the SQLite schema.
    _seed_approval(store, "apr_session_global", run_id=None, task_id=None, session_id="ses_x")

    global_response = asyncio.run(asgi_client.get("/api/approvals", params={"state": "pending"}))
    assert global_response.status_code == 200
    assert "apr_session_global" in {row["id"] for row in global_response.json()}

    project_response = asyncio.run(
        asgi_client.get("/api/approvals", params={"state": "pending", "project_id": "prj_a"})
    )
    assert project_response.status_code == 200
    assert {row["id"] for row in project_response.json()} == {
        "apr_project_a",
        "apr_session_global",
    }
