"""GROUP F / DR-009 / T-DESIGN-004: decide an approval outside the newest-500 window."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import control
from omniagentos.db.store import SqliteStore
from omniagentos.sessions import token
from omniagentos.sessions.dal import SessionsDal


def _approval(
    approval_id: str, created_at: str, *, session_id: str | None = None
) -> dict[str, object]:
    return {
        "id": approval_id,
        "run_id": None,
        "task_id": None,
        "step_seq": None,
        "action_class": "consequential",
        "proposed_action": "x",
        "params_json": "{}",
        "risk": "high",
        "evidence": "",
        "state": "pending",
        "decided_by": None,
        "decision_note": None,
        "decided_at": None,
        "expires_at": None,
        "created_at": created_at,
        "session_id": session_id,
    }


def test_decide_approval_outside_newest_500_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db = str(tmp_path / "pt.db")
    store = SqliteStore(db)
    target_id = "apr_target"
    # Oldest -> falls outside the newest-500 scan window.
    store.create_approval(_approval(target_id, "2020-01-01T00:00:00Z", session_id="ses_pt"))
    for i in range(520):
        store.create_approval(_approval(f"apr_{i}", "2026-07-20T00:00:00Z"))

    newest = {row["id"] for row in store.list_approvals(None, limit=500)}
    assert target_id not in newest  # the scan alone cannot find it

    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    token_value = token.load_or_create_token()
    app.dependency_overrides[get_store] = lambda: store
    monkeypatch.setattr(control, "_open_sessions_reader", lambda: SessionsDal(db))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        response = asyncio.run(
            client.post(
                f"/api/approvals/{target_id}/decision",
                headers={"X-Session-Token": token_value},
                json={"decision": "approved", "decided_by": "owner"},
            )
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "approved"
    assert body["session_id"] == "ses_pt"
    # SEC-005: a session approval records the server-side operator identity.
    assert body["decided_by"] == "operator"
