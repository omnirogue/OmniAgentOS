from __future__ import annotations

import asyncio
import json
import stat
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from omniagentos.api.routes import sessions
from omniagentos.contracts import ActionClass, utc_now_iso
from omniagentos.sessions import hook_token, ssh_keys, token
from omniagentos.sessions.steering_marker import steering_marker_path


class FakeSessionsDal:
    def __init__(self) -> None:
        self.orchestrator_sessions: set[str] = set()
        self.sessions: dict[str, dict[str, Any]] = {
            "ses_1": {
                "id": "ses_1",
                "source": "bridge",
                "project_dir": "/project",
                "provider": "claude",
                "session_ref": "ses_1",
                "state": "running",
                "pid": None,
                "model": None,
                "title": None,
                "company_override": None,
                "agent_name": None,
                "agent_status": None,
                "agent_profile": None,
                "budget_usd_max": None,
                "cost_usd": 0.0,
                "kill_requested": 0,
                "last_activity_at": None,
                "created_at": utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
        }
        self.approvals: list[dict[str, Any]] = []

    def list_sessions(self, state: str | None = None) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.sessions.values() if state is None or row["state"] == state
        ]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.sessions.get(session_id)
        return dict(row) if row else None

    def get_session_by_ref(self, ref: str) -> dict[str, Any] | None:
        if ref in self.sessions:
            return dict(self.sessions[ref])
        for row in self.sessions.values():
            if row.get("session_ref") == ref:
                return dict(row)
        return None

    def terminalize_session(
        self, session_id: str, target: str, *, killed_by: str | None = None, void_note: str
    ) -> bool:
        del killed_by, void_note
        if session_id not in self.sessions:
            return False
        self.sessions[session_id]["state"] = target
        return True

    def create_session(self, row: dict[str, Any]) -> None:
        self.sessions[row["id"]] = dict(row)

    def mark_orchestrator_session(self, session_id: str, run_id: str | None = None) -> None:
        del run_id
        self.orchestrator_sessions.add(session_id)

    def is_orchestrator_session(self, session_id: str) -> bool:
        return session_id in self.orchestrator_sessions

    def touch_activity(self, session_id: str, timestamp: str) -> None:
        self.sessions[session_id]["last_activity_at"] = timestamp

    def request_kill(self, session_id: str) -> bool:
        if session_id not in self.sessions:
            return False
        self.sessions[session_id]["kill_requested"] = 1
        return True

    def update_session_details(
        self,
        session_id: str,
        *,
        title: Any = ...,
        company_override: Any = ...,
    ) -> bool:
        row = self.sessions.get(session_id)
        if row is None:
            return False
        if title is not ...:
            row["title"] = title
        if company_override is not ...:
            row["company_override"] = company_override
        return True

    def request_cancel(self, session_id: str) -> bool:
        if session_id not in self.sessions:
            return False
        row = self.sessions[session_id]
        if row["state"] == "cancelled":
            return True
        if row["state"] in {"completed", "failed", "killed"}:
            return False
        row["kill_requested"] = 1
        row["killed_by"] = "cancel_requested"
        return True

    def enqueue_message(
        self, session_id: str, message: str, *, created_by: str = "operator"
    ) -> dict[str, str]:
        del session_id, message, created_by
        return {"id": "smsg_1", "queued_at": utc_now_iso()}

    def list_session_messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        del session_id, limit
        return []

    def create_session_approval(self, **kwargs: Any) -> str:
        approval_id = f"apr_{len(self.approvals) + 1}"
        self.approvals.append({"id": approval_id, **kwargs})
        return approval_id

    def consume_authorized_approval(self, **kwargs: Any) -> str | None:
        del kwargs
        return None

    def approval_counts(self, session_id: str) -> dict[str, int]:
        rows = [a for a in self.approvals if a.get("session_id") == session_id]
        return {
            "approvals_requested": len(rows),
            "approvals_granted": sum(1 for a in rows if a.get("state") == "approved"),
            "approvals_denied": sum(1 for a in rows if a.get("state") == "rejected"),
        }


@pytest.fixture
def token_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> str:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return token.load_or_create_token()


@pytest.fixture
def dal(monkeypatch: pytest.MonkeyPatch) -> FakeSessionsDal:
    fake = FakeSessionsDal()
    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: fake)
    return fake


def test_hook_eval_requires_local_token(
    asgi_client: httpx.AsyncClient, dal: FakeSessionsDal
) -> None:
    del dal
    response = asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            json={"session_id": "ses_1", "tool_name": "Write", "tool_input": {}, "cwd": "/project"},
        )
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_session_token_is_owner_read_write_only(token_value: str) -> None:
    assert token_value
    assert stat.S_IMODE(token.TOKEN_PATH.stat().st_mode) == 0o600


def test_hook_eval_parks_approval(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    # AUTO mode: only the irreversible hard-stop class parks; consequential is auto.
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)
    response = asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Token": token_value},
            json={
                "session_id": "ses_1",
                "tool_name": "Write",
                "tool_input": {"content": "x", "file_path": "/project/a"},
                "cwd": "/project",
            },
        )
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    assert response.json()["reason"] == "parked: approval apr_1"
    assert dal.approvals[0]["session_id"] == "ses_1"


def test_hook_eval_fails_closed_when_classifier_breaks(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    del dal
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: (_ for _ in ()).throw(RuntimeError()))
    response = asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Token": token_value},
            json={"session_id": "ses_1", "tool_name": "Write", "tool_input": {}, "cwd": "/project"},
        )
    )
    assert response.status_code == 200
    assert response.json() == {
        "decision": "deny",
        "approval_id": None,
        "action_hash": "",
        "reason": "api-error",
    }


def _post_task_call(asgi_client: httpx.AsyncClient, token_value: str) -> httpx.Response:
    return asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Token": token_value},
            json={
                "session_id": "ses_1",
                "tool_name": "Task",
                "tool_input": {"prompt": "child"},
                "cwd": "/project",
            },
        )
    )


def test_hook_eval_task_denied_while_fanout_dark(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    """PKG-INSESSION-FANOUT: with the kill switch thrown, Task is denied
    outright — classification, approvals, and ownership marks never apply."""
    del dal
    monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "0")
    response = _post_task_call(asgi_client, token_value)
    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    assert response.json()["reason"] == "task-fanout-disabled"


def test_hook_eval_task_denied_without_grant(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
    tmp_path: Any,
) -> None:
    del dal
    monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "1")
    monkeypatch.setenv("OMNIAGENTOS_DB", str(tmp_path / "empty.db"))
    response = _post_task_call(asgi_client, token_value)
    assert response.status_code == 200
    assert response.json()["decision"] == "deny"
    assert response.json()["reason"] == "task-fanout:no_grant"


def test_hook_eval_task_allowed_by_live_grant_until_budget(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
    tmp_path: Any,
) -> None:
    """A live coordinator grant admits exactly max_children Task calls; the
    consume is server-side and atomic, so the budget cannot be talked past."""
    import sqlite3

    from omniagentos.collab.store import CollabStore
    from omniagentos.swarm import insession

    del dal
    db = str(tmp_path / "main.db")
    CollabStore(db)
    monkeypatch.setenv("OMNIAGENTOS_INSESSION_FANOUT", "1")
    monkeypatch.setenv("OMNIAGENTOS_DB", db)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, "
        "session_id, provider, model, tier, account_id, started_at) "
        "VALUES ('att-1', 'run-1', 'task-1', 1, 'ses_1', 'claude', 'sonnet', "
        "'standard', 'acct-1', '2026-07-27T11:00:00Z')"
    )
    conn.commit()
    conn.close()
    grant, deny = insession.create_grant(
        swarm_run_id="run-1",
        board_task_id="task-1",
        attempt_id="att-1",
        session_id="ses_1",
        provider="claude",
        account_id="acct-1",
        children=[
            {"title": "one", "description": "", "owned_paths": ["a"]},
            {"title": "two", "description": "", "owned_paths": ["b"]},
        ],
        db_path=db,
    )
    assert deny is None and grant is not None
    first = _post_task_call(asgi_client, token_value)
    second = _post_task_call(asgi_client, token_value)
    third = _post_task_call(asgi_client, token_value)
    assert first.json()["decision"] == "allow"
    assert second.json()["decision"] == "allow"
    assert third.json()["decision"] == "deny"
    assert third.json()["reason"] == "task-fanout:budget_exhausted"


def test_ingest_is_report_only_and_kill_updates_session(
    asgi_client: httpx.AsyncClient,
    store: Any,
    dal: FakeSessionsDal,
    token_value: str,
) -> None:
    ingest = asyncio.run(
        asgi_client.post(
            "/api/sessions/ingest",
            headers={"X-Session-Token": token_value},
            json={"session_id": "ses_external", "cwd": "/external", "tool_name": "Write"},
        )
    )
    assert ingest.status_code == 200
    assert ingest.json()["ok"] is True
    assert dal.sessions["ses_external"]["source"] == "external"
    assert store.events[-1]["action"] == "session.ingested"

    killed = asyncio.run(
        asgi_client.post("/api/sessions/ses_1/kill", headers={"X-Session-Token": token_value})
    )
    assert killed.status_code == 200
    assert killed.json()["kill_requested"] is True


def test_kill_requires_local_token(asgi_client: httpx.AsyncClient, dal: FakeSessionsDal) -> None:
    # SEC-005: the mutating kill route must reject an unauthenticated caller.
    del dal
    response = asyncio.run(asgi_client.post("/api/sessions/ses_1/kill"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_update_session_requires_local_token(asgi_client: httpx.AsyncClient, dal: FakeSessionsDal) -> None:
    del dal
    response = asyncio.run(asgi_client.post("/api/sessions/ses_1/update", json={"title": "x"}))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_update_session_title_company_clear_and_absent_fields(
    asgi_client: httpx.AsyncClient, dal: FakeSessionsDal, token_value: str
) -> None:
    headers = {"X-Session-Token": token_value}
    updated = asyncio.run(
        asgi_client.post(
            "/api/sessions/ses_1/update",
            headers=headers,
            json={"title": "Agent task", "company": "Manual Company"},
        )
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Agent task"
    assert updated.json()["company_override"] == "Manual Company"
    assert updated.json()["company"] == "Manual Company"
    assert {"agent_name", "agent_status", "agent_profile"} <= updated.json().keys()

    cleared = asyncio.run(
        asgi_client.post(
            "/api/sessions/ses_1/update", headers=headers, json={"company": None}
        )
    )
    assert cleared.status_code == 200
    assert cleared.json()["company_override"] is None
    assert cleared.json()["title"] == "Agent task"

    untouched = asyncio.run(
        asgi_client.post("/api/sessions/ses_1/update", headers=headers, json={})
    )
    assert untouched.status_code == 200
    assert untouched.json()["title"] == "Agent task"
    assert untouched.json()["company_override"] is None


def test_cancel_is_idempotent(
    asgi_client: httpx.AsyncClient, dal: FakeSessionsDal, token_value: str
) -> None:
    first = asyncio.run(
        asgi_client.post("/api/sessions/ses_1/cancel", headers={"X-Session-Token": token_value})
    )
    second = asyncio.run(
        asgi_client.post("/api/sessions/ses_1/cancel", headers={"X-Session-Token": token_value})
    )

    assert first.status_code == 200
    assert first.json() == {"session_id": "ses_1", "status": "cancel_requested"}
    assert second.status_code == 200
    assert dal.sessions["ses_1"]["kill_requested"] == 1
    assert dal.sessions["ses_1"]["killed_by"] == "cancel_requested"


def test_send_session_message_requires_active_session(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    token_value: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    sent = asyncio.run(
        asgi_client.post(
            "/api/sessions/ses_1/message",
            headers={"X-Session-Token": token_value},
            json={"message": "Prioritize the failing test before continuing."},
        )
    )
    assert sent.status_code == 200
    assert sent.json()["ok"] is True
    assert sent.json()["message_id"] == "smsg_1"
    assert steering_marker_path("ses_1").is_file()

    missing = asyncio.run(
        asgi_client.post(
            "/api/sessions/ses_missing/message",
            headers={"X-Session-Token": token_value},
            json={"message": "Continue"},
        )
    )
    assert missing.status_code == 404

    dal.sessions["ses_1"]["state"] = "completed"
    terminal = asyncio.run(
        asgi_client.post(
            "/api/sessions/ses_1/message",
            headers={"X-Session-Token": token_value},
            json={"message": "Continue"},
        )
    )
    assert terminal.status_code == 400


def test_list_and_get_session_require_token_and_carry_approval_counts(
    asgi_client: httpx.AsyncClient, dal: FakeSessionsDal, token_value: str
) -> None:
    # SEC-005: read routes are token-gated (consistent with hook-eval/ingest).
    assert asyncio.run(asgi_client.get("/api/sessions")).status_code == 401
    # T-CODE-005: the session row carries per-session approval totals.
    dal.approvals.append({"id": "apr_x", "session_id": "ses_1", "state": "approved"})
    dal.approvals.append({"id": "apr_y", "session_id": "ses_1", "state": "rejected"})
    dal.approvals.append({"id": "apr_z", "session_id": "ses_1", "state": "pending"})
    listed = asyncio.run(asgi_client.get("/api/sessions", headers={"X-Session-Token": token_value}))
    assert listed.status_code == 200
    row = listed.json()[0]
    assert row["approvals_requested"] == 3
    assert row["approvals_granted"] == 1
    assert row["approvals_denied"] == 1

    got = asyncio.run(
        asgi_client.get("/api/sessions/ses_1", headers={"X-Session-Token": token_value})
    )
    assert got.status_code == 200
    assert got.json()["approvals_requested"] == 3


def test_hook_eval_denies_unknown_session(
    asgi_client: httpx.AsyncClient, dal: FakeSessionsDal, token_value: str
) -> None:
    # SEC-004: an unknown session fails closed (deny), never classifies.
    del dal
    response = asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Token": token_value},
            json={
                "session_id": "ses_missing",
                "tool_name": "Read",
                "tool_input": {},
                "cwd": "/anywhere",
            },
        )
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "deny"
    assert body["reason"] == "unknown-session"


def test_hook_eval_widened_cwd_forces_consequential(
    asgi_client: httpx.AsyncClient, dal: FakeSessionsDal, token_value: str, tmp_path: Any
) -> None:
    # SEC-004: a Write INSIDE the recorded project_dir (would be internal_reversible
    # -> allow) is forced consequential -> parked when the client cwd is widened to
    # '/', so an out-of-tree write cannot ride a widened containment boundary.
    project = tmp_path / "proj"
    project.mkdir()
    dal.create_session(
        {
            "id": "ses_proj",
            "source": "bridge",
            "project_dir": str(project),
            "session_ref": "ses_proj",
            "state": "running",
        }
    )
    payload = {
        "session_id": "ses_proj",
        "tool_name": "Write",
        "tool_input": {"file_path": str(project / "a.txt"), "content": "x"},
    }
    # cwd == project_dir: an in-project write classifies internal_reversible -> allow.
    allowed = asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Token": token_value},
            json={**payload, "cwd": str(project)},
        )
    )
    assert allowed.status_code == 200
    assert allowed.json()["decision"] == "allow"

    # cwd widened to '/': the SAME write is an out-of-scope op, forced IRREVERSIBLE
    # and parked -- it cannot ride the widened boundary into auto-execution.
    denied = asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Token": token_value},
            json={**payload, "cwd": "/"},
        )
    )
    assert denied.status_code == 200
    assert denied.json()["decision"] == "deny"
    assert dal.approvals[-1]["action_class"] == ActionClass.IRREVERSIBLE


# ---------------------------------------------------------------------------
# The one live wire: orchestrator-owned sessions auto-resolve their approvals.
#
# A human-spawned bridge session parks EVERY gated action for a human (proven by
# test_hook_eval_parks_approval above). When the Orchestrator spawned the session it
# owns the run, so its gated actions route through orchestrator.approvals.resolve_approval:
# ordinary work, secret reads, and proven local-temp deletes auto-approve; money,
# customer, and production-delete actions park; bank writes refuse permanently.
# The classifier is pinned to IRREVERSIBLE so the action requires approval under EVERY
# policy mode; the wire's verdict then turns on the AD-15 decision.
# ---------------------------------------------------------------------------


def _hook_eval(asgi_client: httpx.AsyncClient, token_value: str, **body: Any) -> httpx.Response:
    return asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Token": token_value},
            json=body,
        )
    )


def test_orchestrator_session_auto_approves_safe_action(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)
    dal.mark_orchestrator_session("ses_1")
    response = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Write",
        tool_input={"content": "x", "file_path": "/project/a"},
        cwd="/project",
    )
    assert response.status_code == 200
    body = response.json()
    # Ordinary engineering work auto-approves with no human wait and nothing parked.
    assert body["decision"] == "allow"
    assert "auto-approved per finance-only policy" in (body["reason"] or "")
    assert dal.approvals == []


def test_orchestrator_session_auto_reason_is_truthful_for_risk_shaped_paths(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)
    dal.mark_orchestrator_session("ses_1")

    # H3(a): a credential read parks for a human on the live hook-eval wire, and
    # the deny carries a truthful trigger rather than an audit-only note.
    secret = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Read",
        tool_input={"file_path": "~/.ssh/id_rsa"},
        cwd="/project",
    ).json()
    assert secret["decision"] == "deny"
    assert "parked per finance-only policy" in (secret["reason"] or "")
    assert "trigger: secret" in secret["reason"]
    assert "safe action" not in secret["reason"].lower()
    assert len(dal.approvals) == 1
    dal.approvals.clear()

    target = tmp_path / "isolated-delete"
    local_delete = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Bash",
        tool_input={"command": f"rm -rf {target}"},
        cwd="/project",
    ).json()
    assert local_delete["decision"] == "allow"
    assert "hard_stop: delete" in local_delete["reason"]
    assert "scope: local_temp" in local_delete["reason"]
    assert "safe action" not in local_delete["reason"].lower()
    assert dal.approvals == []


def test_orchestrator_session_escalates_delete(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)
    dal.mark_orchestrator_session("ses_1")
    response = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Bash",
        tool_input={"command": "rm -rf /project/build"},
        cwd="/project",
    )
    assert response.status_code == 200
    body = response.json()
    # Production/unresolved deletes park for a human under AD-15.
    assert body["decision"] == "deny"
    assert "parked per finance-only policy" in (body["reason"] or "")
    assert "production-delete" in (body["reason"] or "")
    assert len(dal.approvals) == 1
    assert dal.approvals[0]["risk"] == "high"


def test_orchestrator_session_escalates_money(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)
    dal.mark_orchestrator_session("ses_1")
    response = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Bash",
        tool_input={"command": "stripe payment create --amount 5000"},
        cwd="/project",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "deny"
    assert "parked per finance-only policy" in (body["reason"] or "")
    assert "trigger: money" in (body["reason"] or "")
    assert len(dal.approvals) == 1


def test_orchestrator_session_escalates_customer_write(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)
    dal.mark_orchestrator_session("ses_1")
    response = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Bash",
        tool_input={"command": "broadcast message to all customers about the outage"},
        cwd="/project",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "deny"
    assert "parked per finance-only policy" in (body["reason"] or "")
    assert "trigger: customer" in (body["reason"] or "")
    assert len(dal.approvals) == 1


def test_orchestrator_session_refuses_bank_write(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)
    dal.mark_orchestrator_session("ses_1")
    response = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Bash",
        tool_input={"command": "bank transfer 500 to operating account"},
        cwd="/project",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "deny"
    assert "refused per finance-only policy" in (body["reason"] or "")
    assert "trigger: bank" in (body["reason"] or "")
    # Permanent refuse: never creates a parkable approval row.
    assert dal.approvals == []


def test_human_session_cannot_turn_bank_refusal_into_approval(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)
    response = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Bash",
        tool_input={"command": "bank transfer 500 to operating account"},
        cwd="/project",
    )
    body = response.json()
    assert response.status_code == 200
    assert body["decision"] == "deny"
    assert "refused per finance-only policy" in body["reason"]
    assert "trigger: bank" in body["reason"]
    assert dal.approvals == []


def test_orchestrator_ssh_read_auto_approves_but_remote_destroy_escalates(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr(ssh_keys, "SSH_KEYS_ROOT", tmp_path / "ssh-keys")
    ssh_keys.issue_ssh_key_grant("ses_1", ["host"])
    dal.mark_orchestrator_session("ses_1")

    read = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Bash",
        tool_input={"command": "ssh host 'ls'"},
        cwd="/project",
    )
    assert read.status_code == 200
    assert read.json()["decision"] == "allow"
    assert dal.approvals == []

    destructive = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Bash",
        tool_input={"command": "ssh host 'rm -rf /'"},
        cwd="/project",
    )
    assert destructive.status_code == 200
    body = destructive.json()
    assert body["decision"] == "deny"
    assert "parked per finance-only policy" in (body["reason"] or "")
    assert "production-delete" in (body["reason"] or "")
    assert len(dal.approvals) == 1


def test_human_ssh_destroy_parks_for_approval(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
    tmp_path: Any,
) -> None:
    monkeypatch.setattr(ssh_keys, "SSH_KEYS_ROOT", tmp_path / "ssh-keys")
    ssh_keys.issue_ssh_key_grant("ses_1", ["host"])
    response = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Bash",
        tool_input={"command": "ssh host 'rm -rf /'"},
        cwd="/project",
    )
    assert response.status_code == 200
    assert response.json()["reason"] == "parked: approval apr_1"


def test_human_session_is_unchanged_by_the_wire(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    # Same ordinary action as the auto-approve test, but this session is not orchestrator-owned:
    # it must park for a human exactly as before (the wire is behind the orchestrator path only).
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)
    assert dal.is_orchestrator_session("ses_1") is False
    response = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Write",
        tool_input={"content": "x", "file_path": "/project/a"},
        cwd="/project",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "deny"
    assert body["reason"] == "parked: approval apr_1"


# ---------------------------------------------------------------------------
# AC-policy hook-auth: the narrowly-scoped per-session hook-eval credential.
#
# A sandboxed session cannot read var/secrets/sessions-token (the full
# control-plane token), so hook-eval ALSO accepts a per-session credential bound
# to exactly that session's own row (sessions.hook_token). It must: (1) work on
# its own, with no X-Session-Token at all; (2) authorize ONLY the session it was
# minted for, never a sibling's; (3) preserve AD-15 parks/refusals, exactly
# like the full token; (4) never substitute for the full token on any OTHER
# mutating route.
# ---------------------------------------------------------------------------


@pytest.fixture
def hook_token_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Callable[[str], str]:
    monkeypatch.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")
    return hook_token.issue_hook_token


def test_hook_eval_accepts_the_scoped_session_credential_alone(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    hook_token_value: Callable[[str], str],
) -> None:
    """A sandboxed session authenticates hook-eval with ONLY its own scoped
    credential -- no X-Session-Token (the control-plane token) at all."""
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.INTERNAL_REVERSIBLE)
    scoped = hook_token_value("ses_1")
    response = asyncio.run(
        asgi_client.post(
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
    assert response.status_code == 200
    assert response.json()["decision"] == "allow"


def test_hook_eval_scoped_credential_does_not_authorize_a_different_session(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    hook_token_value: Callable[[str], str],
) -> None:
    """ses_1's own (real, currently valid) credential cannot be replayed to
    evaluate ANOTHER session -- it is bound to the row it was minted for."""
    dal.create_session(
        {
            "id": "ses_2",
            "source": "bridge",
            "project_dir": "/other",
            "session_ref": "ses_2",
            "state": "running",
        }
    )
    scoped_for_ses_1 = hook_token_value("ses_1")
    scoped_for_ses_2 = hook_token_value("ses_2")
    assert scoped_for_ses_1 != scoped_for_ses_2

    response = asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Hook-Token": scoped_for_ses_1},
            json={"session_id": "ses_2", "tool_name": "Read", "tool_input": {}, "cwd": "/other"},
        )
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_hook_eval_rejects_a_garbage_scoped_credential(
    asgi_client: httpx.AsyncClient, dal: FakeSessionsDal, hook_token_value: Callable[[str], str]
) -> None:
    del dal
    hook_token_value("ses_1")  # a real credential exists...
    response = asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Hook-Token": "not-the-real-token"},  # ...but this isn't it
            json={"session_id": "ses_1", "tool_name": "Read", "tool_input": {}, "cwd": "/project"},
        )
    )
    assert response.status_code == 401


def test_hook_eval_scoped_credential_still_escalates_delete_and_money(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    hook_token_value: Callable[[str], str],
) -> None:
    """The scoped credential authenticates the CALL, not the DECISION: an
    orchestrator session still cannot auto-approve its own money/production-delete action,
    exactly as with the full token (test_orchestrator_session_escalates_*)."""
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)
    dal.mark_orchestrator_session("ses_1")
    scoped = hook_token_value("ses_1")

    delete_response = asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Hook-Token": scoped},
            json={
                "session_id": "ses_1",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /project/build"},
                "cwd": "/project",
            },
        )
    )
    assert delete_response.status_code == 200
    delete_body = delete_response.json()
    assert delete_body["decision"] == "deny"
    assert "parked per finance-only policy" in (delete_body["reason"] or "")
    assert "production-delete" in (delete_body["reason"] or "")

    money_response = asyncio.run(
        asgi_client.post(
            "/api/sessions/hook-eval",
            headers={"X-Session-Hook-Token": scoped},
            json={
                "session_id": "ses_1",
                "tool_name": "Bash",
                "tool_input": {"command": "stripe payment create --amount 5000"},
                "cwd": "/project",
            },
        )
    )
    assert money_response.status_code == 200
    money_body = money_response.json()
    assert money_body["decision"] == "deny"
    assert "parked per finance-only policy" in (money_body["reason"] or "")
    assert "trigger: money" in (money_body["reason"] or "")
    assert len(dal.approvals) == 2  # both escalations parked for a human, none auto-approved


def test_hook_scoped_credential_cannot_reach_any_other_mutating_route(
    asgi_client: httpx.AsyncClient, dal: FakeSessionsDal, hook_token_value: Callable[[str], str]
) -> None:
    """The scoped credential is good for hook-eval ONLY. Every other mutating
    route (kill shown here) still demands the real control-plane token: the
    app-level gate does not even recognize X-Session-Hook-Token."""
    del dal
    scoped = hook_token_value("ses_1")
    response = asyncio.run(
        asgi_client.post("/api/sessions/ses_1/kill", headers={"X-Session-Hook-Token": scoped})
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_hook_eval_still_accepts_the_full_token_with_no_scoped_credential_issued(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    """Backward compatible: the full control-plane token authorizes hook-eval on
    its own, exactly as before -- no per-session credential needs to exist."""
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.INTERNAL_REVERSIBLE)
    response = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Write",
        tool_input={"content": "x", "file_path": "/project/a"},
        cwd="/project",
    )
    assert response.status_code == 200
    assert response.json()["decision"] == "allow"


# ── live-transcript resolution (chat-reply pipeline drift fix) ────────────────


def _live_session_row(session_id: str, **overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": session_id,
        "source": "bridge",
        "project_dir": "/work/live proj",
        "provider": "claude",
        "session_ref": f"ref-{session_id}",
        "account_id": "acct_x",
        "state": "running",
        "pid": None,
        "model": "haiku",
        "title": None,
        "budget_usd_max": None,
        "cost_usd": 0.0,
        "kill_requested": 0,
        "last_activity_at": None,
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "output_text": None,
        "error": None,
    }
    row.update(overrides)
    return row


def _assistant_line(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}
    )


def test_transcript_prefers_live_cli_transcript(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    token_value: str,
    tmp_path: Any,
) -> None:
    """The /transcript route reads the LIVE CLI transcript (account config_dir +
    slug + session_ref), not the ledger manifest — the drift that starved the
    chat reply pipeline."""
    config_dir = tmp_path / "claude-config"
    dal.sessions["ses_live"] = _live_session_row("ses_live")
    dal.get_claude_account = lambda account_id: (  # type: ignore[method-assign]
        {"id": account_id, "config_dir": str(config_dir)} if account_id == "acct_x" else None
    )
    transcript = config_dir / "projects" / "-work-live-proj" / "ref-ses_live.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(_assistant_line("SOLO-OK") + "\n", encoding="utf-8")

    response = asyncio.run(
        asgi_client.get(
            "/api/sessions/ses_live/transcript",
            headers={"X-Session-Token": token_value},
        )
    )
    assert response.status_code == 200
    entries = response.json()
    assert any(entry.get("type") == "assistant" for entry in entries)
    assert not any(entry.get("synthetic") for entry in entries)


def test_transcript_delta_reads_live_transcript(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    token_value: str,
    tmp_path: Any,
) -> None:
    config_dir = tmp_path / "claude-config"
    dal.sessions["ses_live"] = _live_session_row("ses_live")
    dal.get_claude_account = lambda account_id: (  # type: ignore[method-assign]
        {"id": account_id, "config_dir": str(config_dir)} if account_id == "acct_x" else None
    )
    transcript = config_dir / "projects" / "-work-live-proj" / "ref-ses_live.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(_assistant_line("chunk one") + "\n", encoding="utf-8")

    response = asyncio.run(
        asgi_client.get(
            "/api/sessions/ses_live/transcript/delta?offset=0",
            headers={"X-Session-Token": token_value},
        )
    )
    assert response.status_code == 200
    body = response.json()
    assert any(entry.get("type") == "assistant" for entry in body["entries"])
    assert body["new_offset"] > 0


def test_transcript_provider_session_still_synthesizes(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    token_value: str,
) -> None:
    """Provider sessions resolve no live file; the synthesis fallback stands."""
    dal.sessions["ses_prov"] = _live_session_row(
        "ses_prov",
        provider="codex",
        account_id=None,
        state="completed",
        output_text="provider reply",
    )
    response = asyncio.run(
        asgi_client.get(
            "/api/sessions/ses_prov/transcript",
            headers={"X-Session-Token": token_value},
        )
    )
    assert response.status_code == 200
    entries = response.json()
    assert entries
    assert all(entry.get("synthetic") for entry in entries)
    assert any(entry.get("message") == "provider reply" for entry in entries)


def test_transcript_manifest_is_last_resort(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    token_value: str,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No live file and not a provider session → the terminal audit manifest."""
    from omniagentos.sessions.manifest import SessionManifest as RealManifest

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sessions, "SessionManifest", lambda: RealManifest(tmp_path / "ledger"))
    dal.sessions["ses_old"] = _live_session_row(
        "ses_old", account_id=None, state="completed", session_ref="ref-gone"
    )
    manifest_path = RealManifest(tmp_path / "ledger").path_for("ses_old")
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"session_id": "ses_old", "final_state": "completed"}) + "\n",
        encoding="utf-8",
    )

    response = asyncio.run(
        asgi_client.get(
            "/api/sessions/ses_old/transcript",
            headers={"X-Session-Token": token_value},
        )
    )
    assert response.status_code == 200
    entries = response.json()
    assert any(entry.get("session_id") == "ses_old" for entry in entries)


def test_hook_eval_approval_banner_coalesces_per_approval(
    asgi_client: httpx.AsyncClient,
    dal: FakeSessionsDal,
    monkeypatch: pytest.MonkeyPatch,
    token_value: str,
) -> None:
    """LS-R2-004: this route pushes DIRECTLY rather than through
    record_notification, so it must supply the labels that seam supplies --
    otherwise a repeat approval stacks another banner in Notification Center
    instead of replacing the live one, and the banner loses its severity line.
    The group must be the SAME key the durable seam mints for this approval, so
    the two writers coalesce with each other and not merely with themselves.
    """
    from omniagentos.notifications import service as notifications_service
    from omniagentos.sessions import notify

    pushes: list[dict[str, Any]] = []

    def _capture(title: str, body: str, url: str | None = None, **kwargs: Any) -> Any:
        pushes.append({"title": title, "body": body, **kwargs})
        return notify.PushOutcome(banner=True, ntfy=True, slack=False)

    monkeypatch.setattr(notify, "push_outcome", _capture)
    monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)

    response = _hook_eval(
        asgi_client,
        token_value,
        session_id="ses_1",
        tool_name="Write",
        tool_input={"content": "x", "file_path": "/project/a"},
        cwd="/project",
    )

    assert response.status_code == 200
    approval_id = response.json()["approval_id"]
    assert len(pushes) == 1
    assert pushes[0]["group"] == notifications_service.push_group("approval", approval_id)
    assert pushes[0]["group"] is not None
    assert pushes[0]["subtitle"] == notifications_service.push_subtitle(pushes[0]["severity"])
    # The delivery carrier is still the remote legs only, never the local banner.
    assert dal.approvals[0].get("delivery_state") in {None, "delivered", "failed", "unattempted"}
