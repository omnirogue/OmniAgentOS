"""GROUP A / SEC-006 / T-DESIGN-001: the approve->resume one-shot authorization.

Drives BOTH the initial PreToolUse (park) AND the resumed PreToolUse (allow-once)
through the real hook-eval + SessionsDal, proving the gated action is allowed
EXACTLY once and that a different action after approval is still denied.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.routes import sessions
from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.sessions import token
from omniagentos.sessions.dal import SessionsDal


@pytest.fixture
def token_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return token.load_or_create_token()


@pytest.fixture
def real_dal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SessionsDal:
    db_path = str(tmp_path / "oneshot.db")
    dal = SessionsDal(db_path)
    now = utc_now_iso()
    dal.create_session(
        {
            "id": "ses_one",
            "source": "bridge",
            "project_dir": str(tmp_path),
            "provider": "claude",
            "session_ref": "ref-one",
            "state": "awaiting_approval",
            "model": "haiku",
            "last_activity_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: dal)
    yield dal
    dal.close()


def _post(
    client: httpx.AsyncClient, tool_input: dict[str, Any], token_value: str
) -> dict[str, Any]:
    response = asyncio.run(
        client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Token": token_value},
            json={
                "session_id": "ses_one",
                "tool_name": "Bash",
                "tool_input": tool_input,
                "cwd": "/project",
            },
        )
    )
    assert response.status_code == 200
    return response.json()


def test_approved_action_allowed_exactly_once_then_reparks(
    asgi_client: httpx.AsyncClient, real_dal: SessionsDal, token_value: str, tmp_path: Path
) -> None:
    action_a = {"command": "rm -rf build"}
    action_b = {"command": "rm -rf dist"}

    # 1. Initial PreToolUse for A parks a new approval (consequential -> deny).
    parked = _post(asgi_client, action_a, token_value)
    assert parked["decision"] == "deny"
    approval_id = parked["approval_id"]
    assert approval_id
    row = real_dal.get_approval_by_id(approval_id)
    assert row is not None and row["state"] == "pending" and row["consumed_at"] is None

    # 2. A human approves it (dashboard decide path).
    store = SqliteStore(str(tmp_path / "oneshot.db"))
    assert store.decide_approval(approval_id, "approved", "owner")

    # 3. Resumed PreToolUse for the SAME action A is allowed exactly once (consumed).
    allowed = _post(asgi_client, action_a, token_value)
    assert allowed["decision"] == "allow"
    assert allowed["approval_id"] == approval_id
    assert real_dal.get_approval_by_id(approval_id)["consumed_at"] is not None  # type: ignore[index]

    # 4. A second identical call finds it consumed -> re-classify -> new park (no replay).
    replay = _post(asgi_client, action_a, token_value)
    assert replay["decision"] == "deny"
    assert replay["approval_id"] != approval_id

    # 5. A DIFFERENT action after approval is denied (approval binds action_hash).
    other = _post(asgi_client, action_b, token_value)
    assert other["decision"] == "deny"
    assert other["approval_id"] not in {approval_id, replay["approval_id"]}
