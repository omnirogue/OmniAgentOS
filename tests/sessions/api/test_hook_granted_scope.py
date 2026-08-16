"""P3 -- hook-eval honors a session's FULL persisted granted scope.

The session's granted roots (project root_dirs + allowed_dirs, frozen on the row
at spawn) are looked up SERVER-SIDE here and relax ONLY the in-project write
boundary: a write inside a granted root auto-approves instead of parking, while
deletes / secret reads / out-of-scope writes still hard-stop. A session with no
granted roots is unchanged (pre-P3).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.routes import sessions
from omniagentos.contracts import utc_now_iso
from omniagentos.sessions import token
from omniagentos.sessions.dal import SessionsDal


@pytest.fixture
def token_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return token.load_or_create_token()


@pytest.fixture
def scoped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    project = tmp_path / "project"
    granted = tmp_path / "granted"
    for directory in (project, granted):
        directory.mkdir()
    dal = SessionsDal(str(tmp_path / "scope.db"))
    now = utc_now_iso()

    def _seed(session_id: str, granted_roots: list[str] | None) -> None:
        dal.create_session(
            {
                "id": session_id,
                "source": "bridge",
                "project_dir": str(project),
                "provider": "claude",
                "session_ref": f"ref-{session_id}",
                "state": "running",
                "model": "haiku",
                "last_activity_at": now,
                "created_at": now,
                "updated_at": now,
                "granted_roots": json.dumps(granted_roots) if granted_roots else None,
            }
        )

    _seed("ses_scoped", [str(granted)])
    _seed("ses_unscoped", None)
    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: dal)
    ctx = {"project": project, "granted": granted, "dal": dal}
    yield ctx
    dal.close()


def _post(
    client: httpx.AsyncClient,
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    cwd: str,
    token_value: str,
) -> dict[str, Any]:
    response = asyncio.run(
        client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Token": token_value},
            json={
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "cwd": cwd,
            },
        )
    )
    assert response.status_code == 200
    return response.json()


def test_write_into_granted_root_auto_allows(
    asgi_client: httpx.AsyncClient, scoped: dict[str, Any], token_value: str
) -> None:
    result = _post(
        asgi_client,
        "ses_scoped",
        "Write",
        {"file_path": str(scoped["granted"] / "out.txt"), "content": "hi"},
        str(scoped["project"]),
        token_value,
    )
    assert result["decision"] == "allow"


def test_bash_write_into_granted_root_auto_allows(
    asgi_client: httpx.AsyncClient, scoped: dict[str, Any], token_value: str
) -> None:
    result = _post(
        asgi_client,
        "ses_scoped",
        "Bash",
        {"command": f"echo hi > {scoped['granted']}/note.txt"},
        str(scoped["project"]),
        token_value,
    )
    assert result["decision"] == "allow"


def test_provable_delete_in_granted_root_is_denied(
    asgi_client: httpx.AsyncClient, scoped: dict[str, Any], token_value: str
) -> None:
    """M-37: a granted root widens writes, never destructive authority.

    This is end-to-end through the real hook route so stale session-layer
    expectations cannot silently turn a write grant into delete permission.
    """
    result = _post(
        asgi_client,
        "ses_scoped",
        "Bash",
        {"command": f"rm -rf {scoped['granted']}/build"},
        str(scoped["project"]),
        token_value,
    )
    assert result["decision"] == "deny"


def test_unprovable_delete_still_parks(
    asgi_client: httpx.AsyncClient, scoped: dict[str, Any], token_value: str
) -> None:
    """A delete behind an UNBOUND variable cannot be proven -> still a hard stop."""
    result = _post(
        asgi_client,
        "ses_scoped",
        "Bash",
        {"command": 'rm -rf "$UNSET_TARGET/build"'},
        str(scoped["project"]),
        token_value,
    )
    assert result["decision"] == "deny"


def test_delete_outside_scope_still_parks(
    asgi_client: httpx.AsyncClient, scoped: dict[str, Any], token_value: str, tmp_path: Path
) -> None:
    result = _post(
        asgi_client,
        "ses_scoped",
        "Bash",
        {"command": f"rm -rf {tmp_path / 'elsewhere'}"},
        str(scoped["project"]),
        token_value,
    )
    assert result["decision"] == "deny"


def test_write_outside_scope_still_parks(
    asgi_client: httpx.AsyncClient, scoped: dict[str, Any], token_value: str, tmp_path: Path
) -> None:
    result = _post(
        asgi_client,
        "ses_scoped",
        "Write",
        {"file_path": str(tmp_path / "elsewhere" / "x.txt"), "content": "hi"},
        str(scoped["project"]),
        token_value,
    )
    assert result["decision"] == "deny"


def test_widened_cwd_into_granted_root_is_allowed(
    asgi_client: httpx.AsyncClient, scoped: dict[str, Any], token_value: str
) -> None:
    """A cwd inside a granted root (not the project dir) is in-scope, not forced-hard."""
    result = _post(
        asgi_client,
        "ses_scoped",
        "Write",
        {"file_path": str(scoped["granted"] / "out.txt"), "content": "hi"},
        str(scoped["granted"]),
        token_value,
    )
    assert result["decision"] == "allow"


def test_unscoped_session_unchanged(
    asgi_client: httpx.AsyncClient, scoped: dict[str, Any], token_value: str
) -> None:
    """Invariant (3): a session with no granted roots parks the same write."""
    result = _post(
        asgi_client,
        "ses_unscoped",
        "Write",
        {"file_path": str(scoped["granted"] / "out.txt"), "content": "hi"},
        str(scoped["project"]),
        token_value,
    )
    assert result["decision"] == "deny"
