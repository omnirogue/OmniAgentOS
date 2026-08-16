"""POST /api/session-events/hook — attributed attention routing."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.routes import session_events
from omniagentos.contracts import utc_now_iso
from omniagentos.sessions import hook_token, token
from omniagentos.sessions.dal import SessionsDal
from tests.sessions.api.test_routes import FakeSessionsDal


class AttentionFakeDal(FakeSessionsDal):
    def set_session_attention(
        self,
        session_id: str,
        *,
        attention_state: str | None,
        attention_reason: str | None,
        attention_since: str | None,
    ) -> bool:
        if session_id not in self.sessions:
            return False
        self.sessions[session_id]["attention_state"] = attention_state
        self.sessions[session_id]["attention_reason"] = attention_reason
        self.sessions[session_id]["attention_since"] = attention_since
        if attention_since is not None:
            self.sessions[session_id]["updated_at"] = attention_since
        return True

    def terminalize_session(
        self, session_id: str, target: str, *, killed_by: str | None = None, void_note: str = ""
    ) -> bool:
        del killed_by, void_note
        if session_id not in self.sessions:
            return False
        self.sessions[session_id]["state"] = target
        self.sessions[session_id]["attention_state"] = None
        self.sessions[session_id]["attention_reason"] = None
        self.sessions[session_id]["attention_since"] = None
        return True


@pytest.fixture
def token_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return token.load_or_create_token()


@pytest.fixture
def hook_token_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[[str], str]:
    monkeypatch.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")
    return hook_token.issue_hook_token


@pytest.fixture
def dal(monkeypatch: pytest.MonkeyPatch) -> AttentionFakeDal:
    fake = AttentionFakeDal()
    monkeypatch.setattr(session_events, "get_sessions_dal", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def _isolate_ntfy_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Never read the operator's real connections.env from a test."""
    monkeypatch.setattr(session_events, "_CONNECTIONS_ENV_PATH", tmp_path / "connections.env")
    session_events._NTFY_DEDUP.clear()
    session_events._ATTENTION_COLUMNS_READY.clear()


def _post(
    asgi_client: httpx.AsyncClient,
    headers: dict[str, str] | None,
    **body: Any,
) -> httpx.Response:
    payload = {
        "session_id": "ses_1",
        "event": "notification",
        "cwd": "/project",
        "message": "waiting for you",
        **body,
    }
    return asyncio.run(
        asgi_client.post("/api/session-events/hook", headers=headers or {}, json=payload)
    )


def test_hook_event_requires_a_credential(
    asgi_client: httpx.AsyncClient, dal: AttentionFakeDal
) -> None:
    del dal
    response = _post(asgi_client, None)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_hook_event_rejects_a_garbage_scoped_credential(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    hook_token_value: Callable[[str], str],
) -> None:
    hook_token_value("ses_1")
    response = _post(asgi_client, {"X-Session-Hook-Token": "not-the-real-token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_hook_event_accepts_the_scoped_session_credential_alone(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    hook_token_value: Callable[[str], str],
) -> None:
    scoped = hook_token_value("ses_1")
    response = _post(asgi_client, {"X-Session-Hook-Token": scoped})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "session_id": "ses_1"}
    row = dal.get_session("ses_1")
    assert row is not None
    assert row["attention_state"] == "needs_input"
    assert row["attention_reason"] == "waiting for you"
    assert row["attention_since"]


def test_hook_event_accepts_the_full_token(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
) -> None:
    response = _post(asgi_client, {"X-Session-Token": token_value}, message="ping")
    assert response.status_code == 200
    row = dal.get_session("ses_1")
    assert row is not None
    assert row["attention_reason"] == "ping"


@pytest.mark.parametrize(
    ("event", "expected_state", "expected_reason"),
    [
        ("notification", "needs_input", "reason-notification"),
        ("permission_request", "needs_input", "reason-permission_request"),
        ("stop", None, None),
        ("session_end", None, None),
    ],
)
def test_hook_event_sets_attention_for_each_event_type(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
    event: str,
    expected_state: str | None,
    expected_reason: str | None,
) -> None:
    response = _post(
        asgi_client,
        {"X-Session-Token": token_value},
        event=event,
        message=f"reason-{event}",
    )
    assert response.status_code == 200
    row = dal.get_session("ses_1")
    assert row is not None
    assert row.get("attention_state") == expected_state
    assert row.get("attention_reason") == expected_reason
    if expected_state is None:
        assert row.get("attention_since") is None
    else:
        assert isinstance(row["attention_since"], str) and row["attention_since"]


def test_stop_clears_attention_last_write_wins(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
) -> None:
    first = _post(
        asgi_client,
        {"X-Session-Token": token_value},
        event="notification",
        message="need input",
    )
    assert first.status_code == 200
    first_row = dal.get_session("ses_1")
    assert first_row is not None
    assert first_row["attention_state"] == "needs_input"

    second = _post(
        asgi_client,
        {"X-Session-Token": token_value},
        event="stop",
        message="turn done",
    )
    assert second.status_code == 200
    row = dal.get_session("ses_1")
    assert row is not None
    assert row["attention_state"] is None
    assert row["attention_reason"] is None
    assert row["attention_since"] is None


def test_hook_event_bumps_updated_at_so_session_updated_synthesizes(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
) -> None:
    """session.updated is SSE-synthesized from updated_at (never persisted)."""
    dal.sessions["ses_1"]["updated_at"] = "2020-01-01T00:00:00Z"
    response = _post(asgi_client, {"X-Session-Token": token_value})
    assert response.status_code == 200
    row = dal.get_session("ses_1")
    assert row is not None
    assert row["updated_at"] != "2020-01-01T00:00:00Z"
    assert row["updated_at"] == row["attention_since"]


def test_attention_write_does_not_touch_last_activity_at(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
) -> None:
    """Idle reaper keys on last_activity_at (and updated_at as a floor)."""
    dal.sessions["ses_1"]["last_activity_at"] = "2020-01-01T00:00:00Z"
    response = _post(asgi_client, {"X-Session-Token": token_value})
    assert response.status_code == 200
    row = dal.get_session("ses_1")
    assert row is not None
    assert row["last_activity_at"] == "2020-01-01T00:00:00Z"


def test_unknown_session_with_full_token_creates_new_row_not_reuse_by_cwd(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
) -> None:
    """Bare-cwd fallback is gone: same cwd as ses_1 must still mint a new row."""
    existing = dal.get_session("ses_1")
    assert existing is not None
    assert existing["project_dir"] == "/project"
    response = asyncio.run(
        asgi_client.post(
            "/api/session-events/hook",
            headers={"X-Session-Token": token_value},
            json={
                "event": "notification",
                "cwd": "/project",
                "message": "blocked",
                "title": "foreign work",
            },
        )
    )
    assert response.status_code == 200
    created_id = response.json()["session_id"]
    assert created_id.startswith("ses_")
    assert created_id != "ses_1"
    row = dal.get_session(created_id)
    assert row is not None
    assert row["source"] == "external"
    assert row["project_dir"] == "/project"
    assert row["title"] == "foreign work"
    assert row["attention_state"] == "needs_input"


def test_hook_event_scoped_credential_cannot_create_an_unknown_session(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    hook_token_value: Callable[[str], str],
) -> None:
    scoped = hook_token_value("ses_1")
    response = asyncio.run(
        asgi_client.post(
            "/api/session-events/hook",
            headers={"X-Session-Hook-Token": scoped},
            json={"event": "notification", "cwd": "/other", "session_id": "ses_missing"},
        )
    )
    assert response.status_code == 401


def test_hook_event_resolves_by_session_ref(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
) -> None:
    dal.sessions["ses_1"]["session_ref"] = "provider-uuid"
    response = asyncio.run(
        asgi_client.post(
            "/api/session-events/hook",
            headers={"X-Session-Token": token_value},
            json={
                "session_ref": "provider-uuid",
                "event": "permission_request",
                "cwd": "/project",
                "message": "allow bash?",
            },
        )
    )
    assert response.status_code == 200
    assert response.json()["session_id"] == "ses_1"
    row = dal.get_session("ses_1")
    assert row is not None
    assert row["attention_state"] == "needs_input"
    assert row["attention_reason"] == "allow bash?"


def test_ntfy_unset_still_updates_the_row(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNI_NTFY_URL", raising=False)
    response = _post(asgi_client, {"X-Session-Token": token_value}, message="no push")
    assert response.status_code == 200
    row = dal.get_session("ses_1")
    assert row is not None
    assert row["attention_reason"] == "no push"


def test_ntfy_refused_connection_still_updates_the_row(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNI_NTFY_URL", "http://127.0.0.1:1")
    started = time.monotonic()
    response = _post(asgi_client, {"X-Session-Token": token_value}, message="dead ntfy")
    elapsed_ms = (time.monotonic() - started) * 1000
    assert response.status_code == 200
    assert elapsed_ms < 100, elapsed_ms
    row = dal.get_session("ses_1")
    assert row is not None
    assert row["attention_state"] == "needs_input"
    assert row["attention_reason"] == "dead ntfy"


def test_session_end_terminalizes_five_open_end_cycles(
    asgi_client: httpx.AsyncClient,
    token_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = AttentionFakeDal()
    store.sessions.clear()
    monkeypatch.setattr(session_events, "get_sessions_dal", lambda: store)
    for n in range(5):
        uuid = f"9f0b21c{n}-1111-2222-3333-444444444444"
        opened = asyncio.run(
            asgi_client.post(
                "/api/session-events/hook",
                headers={"X-Session-Token": token_value},
                json={"session_id": uuid, "event": "notification", "cwd": "/p", "message": "hi"},
            )
        )
        assert opened.status_code == 200
        ended = asyncio.run(
            asgi_client.post(
                "/api/session-events/hook",
                headers={"X-Session-Token": token_value},
                json={"session_id": uuid, "event": "session_end", "cwd": "/p"},
            )
        )
        assert ended.status_code == 200
    rows = list(store.sessions.values())
    assert len(rows) == 5
    assert all(row["state"] == "completed" for row in rows)
    assert not any(row["state"] == "running" for row in rows)


def test_dropped_attention_write_returns_409_and_does_not_push(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        session_events, "_schedule_attention_push", lambda *a, **k: pushes.append(("x", "y"))
    )

    def miss(_sid: str, **kwargs: Any) -> bool:
        del _sid, kwargs
        return False

    dal.set_session_attention = miss  # type: ignore[method-assign]
    response = _post(asgi_client, {"X-Session-Token": token_value})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state"
    assert pushes == []


def test_noop_stop_skips_write_when_already_cleared(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
) -> None:
    writes = {"n": 0}
    real = dal.set_session_attention

    def counted(**kwargs: Any) -> bool:
        writes["n"] += 1
        return real(**kwargs)

    dal.set_session_attention = counted  # type: ignore[method-assign]
    dal.sessions["ses_1"]["attention_state"] = None
    response = _post(asgi_client, {"X-Session-Token": token_value}, event="stop")
    assert response.status_code == 200
    assert writes["n"] == 0


def test_unauthenticated_invalid_event_is_401_not_400(
    asgi_client: httpx.AsyncClient, dal: AttentionFakeDal
) -> None:
    del dal
    response = _post(asgi_client, None, event="not-a-real-event")
    assert response.status_code == 401


def test_unknown_event_returns_400(
    asgi_client: httpx.AsyncClient, dal: AttentionFakeDal, token_value: str
) -> None:
    del dal
    response = _post(asgi_client, {"X-Session-Token": token_value}, event="turn_end")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_event"


def test_message_and_title_are_truncated_to_2000(
    asgi_client: httpx.AsyncClient, dal: AttentionFakeDal, token_value: str
) -> None:
    huge = "x" * 5000
    response = _post(
        asgi_client,
        {"X-Session-Token": token_value},
        message=huge,
        title=huge,
    )
    assert response.status_code == 200
    row = dal.get_session("ses_1")
    assert row is not None
    assert row["attention_reason"] == "x" * 2000


def test_duplicate_ntfy_is_suppressed_for_same_session_state(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del dal
    pushes: list[tuple[str, str]] = []
    monkeypatch.setattr(
        session_events,
        "_push_attention_ntfy",
        lambda title, body: pushes.append((title, body)),
    )
    monkeypatch.setenv("OMNI_NTFY_URL", "http://127.0.0.1:9")
    first = _post(asgi_client, {"X-Session-Token": token_value}, message="one")
    second = _post(asgi_client, {"X-Session-Token": token_value}, message="two")
    assert first.status_code == 200
    assert second.status_code == 200
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline and len(pushes) < 1:
        time.sleep(0.01)
    assert len(pushes) == 1


def test_unicode_title_is_latin1_safe_for_urlopen() -> None:
    raw = "プロジェクト — 会话"
    safe = session_events._latin1_header(raw)
    safe.encode("latin-1")
    assert "—" not in safe or safe.encode("latin-1")


def test_create_retries_once_after_integrity_error(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    token_value: str,
) -> None:
    real_create = dal.create_session

    def flaky(row: dict[str, Any]) -> None:
        real_create(row)
        raise sqlite3.IntegrityError("UNIQUE constraint failed: sessions.id")

    dal.create_session = flaky  # type: ignore[method-assign]
    response = asyncio.run(
        asgi_client.post(
            "/api/session-events/hook",
            headers={"X-Session-Token": token_value},
            json={"event": "notification", "cwd": "/race", "session_ref": "race-ref"},
        )
    )
    assert response.status_code == 200
    created = response.json()["session_id"]
    assert dal.get_session(created) is not None


@pytest.mark.real_auth
def test_hook_event_scoped_credential_reaches_handler_with_real_gate(
    asgi_client: httpx.AsyncClient,
    dal: AttentionFakeDal,
    hook_token_value: Callable[[str], str],
) -> None:
    """App-level gate must not reject hook-token-only POSTs to this route."""
    unauthenticated = _post(asgi_client, None)
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["error"]["message"] == "invalid session token"

    scoped = hook_token_value("ses_1")
    authorized = _post(asgi_client, {"X-Session-Hook-Token": scoped})
    assert authorized.status_code == 200
    assert authorized.json()["ok"] is True


def test_real_dal_persists_attention_columns(tmp_path: Path) -> None:
    dal = SessionsDal(str(tmp_path / "attention.db"))
    try:
        now = utc_now_iso()
        dal.create_session(
            {
                "id": "ses_real",
                "source": "bridge",
                "project_dir": "/project",
                "provider": "claude",
                "session_ref": "ses_real",
                "state": "running",
                "created_at": now,
                "updated_at": now,
            }
        )
        assert dal.set_session_attention(
            "ses_real",
            attention_state="needs_input",
            attention_reason="hi",
            attention_since=now,
        )
        row = dal.get_session("ses_real")
        assert row is not None
        assert row["attention_state"] == "needs_input"
        assert row["attention_reason"] == "hi"
        assert row["attention_since"] == now
        listed = dal.list_sessions()
        assert listed[0]["attention_state"] == "needs_input"
    finally:
        dal.close()


def test_terminalize_clears_ghost_attention(tmp_path: Path) -> None:
    dal = SessionsDal(str(tmp_path / "ghost.db"))
    try:
        now = utc_now_iso()
        dal.create_session(
            {
                "id": "ses_ghost",
                "source": "external",
                "project_dir": "/project",
                "provider": "claude",
                "session_ref": "extproc:1",
                "state": "running",
                "created_at": now,
                "updated_at": now,
            }
        )
        dal.set_session_attention(
            "ses_ghost",
            attention_state="needs_input",
            attention_reason="waiting",
            attention_since=now,
        )
        assert dal.terminalize_session(
            "ses_ghost", "completed", void_note="external process no longer running"
        )
        row = dal.get_session("ses_ghost")
        assert row is not None
        assert row["state"] == "completed"
        assert row["attention_state"] is None
        assert row["attention_reason"] is None
        assert row["attention_since"] is None
    finally:
        dal.close()


def test_update_session_state_to_terminal_clears_attention(tmp_path: Path) -> None:
    dal = SessionsDal(str(tmp_path / "term.db"))
    try:
        now = utc_now_iso()
        dal.create_session(
            {
                "id": "ses_term",
                "source": "bridge",
                "project_dir": "/project",
                "provider": "claude",
                "session_ref": "ses_term",
                "state": "running",
                "created_at": now,
                "updated_at": now,
            }
        )
        dal.set_session_attention(
            "ses_term",
            attention_state="needs_input",
            attention_reason="parked",
            attention_since=now,
        )
        assert dal.update_session_state("ses_term", "completed", expect="running")
        row = dal.get_session("ses_term")
        assert row is not None
        assert row["attention_state"] is None
    finally:
        dal.close()
