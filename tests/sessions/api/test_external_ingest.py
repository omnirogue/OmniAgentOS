"""T-DESIGN-006 / T-OPS-004: external sessions ingest a normal UUID and can complete."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.routes import sessions
from omniagentos.sessions import token
from omniagentos.sessions.dal import SessionsDal


@pytest.fixture
def token_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return token.load_or_create_token()


@pytest.fixture
def real_dal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SessionsDal:
    dal = SessionsDal(str(tmp_path / "ext.db"))
    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: dal)
    yield dal
    dal.close()


def _ingest(client: httpx.AsyncClient, token_value: str, **body: Any) -> dict[str, Any]:
    response = asyncio.run(
        client.post("/api/sessions/ingest", headers={"X-Session-Token": token_value}, json=body)
    )
    assert response.status_code == 200
    return response.json()


def test_external_uuid_start_activity_stop(
    asgi_client: httpx.AsyncClient, real_dal: SessionsDal, token_value: str
) -> None:
    uuid_ref = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"

    # start: a plain provider UUID mints a canonical ses_ id, keeping the ref.
    started = _ingest(
        asgi_client,
        token_value,
        session_id=uuid_ref,
        cwd="/proj",
        hook_event_name="PostToolUse",
        tool_name="Read",
        provider="claude",
    )
    canonical = started["session_id"]
    assert canonical.startswith("ses_")
    row = real_dal.get_session_by_ref(uuid_ref)
    assert row is not None
    assert row["source"] == "external"
    assert row["session_ref"] == uuid_ref
    assert row["id"] == canonical

    # activity: the same UUID resolves to the same canonical session.
    again = _ingest(
        asgi_client,
        token_value,
        session_id=uuid_ref,
        cwd="/proj",
        hook_event_name="PostToolUse",
        tool_name="Grep",
    )
    assert again["session_id"] == canonical

    # stop: maps to a terminal transition.
    stopped = _ingest(
        asgi_client,
        token_value,
        session_id=uuid_ref,
        cwd="/proj",
        hook_event_name="Stop",
    )
    assert stopped["session_id"] == canonical
    assert real_dal.get_session(canonical)["state"] == "completed"  # type: ignore[index]
