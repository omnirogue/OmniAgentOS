"""Session steering API integration coverage with the real SQLite DAL."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from omniagentos.api.routes import sessions
from omniagentos.api.routes import swarm as swarm_routes
from omniagentos.contracts import utc_now_iso
from omniagentos.sessions.dal import SessionsDal
from omniagentos.sessions.manifest import SessionManifest
from omniagentos.swarm import spawn as swarm_spawn
from omniagentos.swarm.dal import SwarmDal


def test_send_session_message(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    dal = SessionsDal(tmp_path / "sessions.db")
    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: dal)
    monkeypatch.setattr(sessions, "SessionManifest", lambda: SessionManifest(tmp_path / "ledger"))
    now = utc_now_iso()
    dal.create_session(
        {
            "id": "ses_message",
            "source": "bridge",
            "project_dir": str(tmp_path),
            "session_ref": "ses_message",
            "state": "running",
            "created_at": now,
            "updated_at": now,
        }
    )

    response = asyncio.run(
        asgi_client.post(
            "/api/sessions/ses_message/message",
            headers=auth_headers,
            json={"message": "Run the focused tests, then summarize the result."},
        )
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    stored = dal._connection.execute(  # noqa: SLF001 - validates durable API contract.
        "SELECT session_id, message, applied_at, created_by FROM session_messages WHERE id = ?",
        (body["message_id"],),
    ).fetchone()
    assert stored is not None
    assert dict(stored) == {
        "session_id": "ses_message",
        "message": "Run the focused tests, then summarize the result.",
        "applied_at": None,
        "created_by": "operator",
    }

    transcript = asyncio.run(
        asgi_client.get("/api/sessions/ses_message/transcript", headers=auth_headers)
    )
    assert transcript.status_code == 200
    assert transcript.json()[-1]["message"] == "Run the focused tests, then summarize the result."
    assert transcript.json()[-1]["actor"] == "operator"

    missing = asyncio.run(
        asgi_client.post(
            "/api/sessions/ses_missing/message",
            headers=auth_headers,
            json={"message": "Hi"},
        )
    )
    assert missing.status_code == 404

    dal.terminalize_session("ses_message", "completed", void_note="test complete")
    terminal = asyncio.run(
        asgi_client.post(
            "/api/sessions/ses_message/message",
            headers=auth_headers,
            json={"message": "Hi"},
        )
    )
    assert terminal.status_code == 400
    dal.close()


# ---------------------------------------------------------------------------
# Provider-session transcript synthesis (codex/grok/gemini/kimi sessions have
# no bridge JSONL activity file — the endpoint reconstructs a transcript from
# the durable sessions row + swarm attempt + WP5b workbook).
# ---------------------------------------------------------------------------


class _TranscriptEnv:
    def __init__(self, dal: SessionsDal, swarm_dal: SwarmDal, tmp_path: Path) -> None:
        self.dal = dal
        self.swarm_dal = swarm_dal
        self.tmp_path = tmp_path

    def create_session(self, session_id: str, **overrides: object) -> None:
        now = utc_now_iso()
        row: dict[str, object] = {
            "id": session_id,
            "source": "bridge",
            "project_dir": str(self.tmp_path),
            "provider": "codex",
            "state": "running",
            "model": "gpt-5-codex",
            "prompt": "Implement the widget end to end.",
            "created_at": now,
            "updated_at": now,
        }
        row.update(overrides)
        self.dal.create_session({k: v for k, v in row.items() if v is not None})

    def insert_attempt(self, session_id: str, **overrides: object) -> dict[str, object]:
        attempt: dict[str, object] = {
            "id": "swa_test1",
            "swarm_run_id": "swr_run1",
            "board_task_id": "tsk_alpha",
            "seq": 2,
            "session_id": session_id,
            "provider": "codex",
            "model": "gpt-5-codex",
            "tier": "fast",
            "account_id": None,
            "started_at": utc_now_iso(),
            "ended_at": None,
            "end_reason": None,
            "detail": "",
        }
        attempt.update(overrides)
        self.swarm_dal._connection.execute(  # noqa: SLF001 - direct seed row.
            "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, "
            "session_id, provider, model, tier, account_id, started_at, ended_at, "
            "end_reason, detail, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(
                attempt[key]
                for key in (
                    "id",
                    "swarm_run_id",
                    "board_task_id",
                    "seq",
                    "session_id",
                    "provider",
                    "model",
                    "tier",
                    "account_id",
                    "started_at",
                    "ended_at",
                    "end_reason",
                    "detail",
                )
            ) + ("test",),
        )
        self.swarm_dal._connection.commit()  # noqa: SLF001
        return attempt

    def write_workbook(self, run_id: str, task_id: str, content: str) -> Path:
        path = self.tmp_path / "swarm-var" / run_id / task_id / "WORKBOOK.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def write_activity(self, session_id: str, records: list[dict[str, object]]) -> None:
        path = SessionManifest(self.tmp_path / "ledger").path_for(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def get_transcript(
        self, client: httpx.AsyncClient, headers: dict[str, str], session_id: str
    ) -> list[dict[str, object]]:
        response = asyncio.run(
            client.get(f"/api/sessions/{session_id}/transcript", headers=headers)
        )
        assert response.status_code == 200
        return response.json()


@pytest.fixture
def transcript_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _TranscriptEnv:
    db_path = tmp_path / "sessions.db"
    dal = SessionsDal(db_path)  # migrates the full schema, incl. swarm_attempts
    swarm_dal = SwarmDal(db_path)
    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: dal)
    monkeypatch.setattr(sessions, "SessionManifest", lambda: SessionManifest(tmp_path / "ledger"))
    monkeypatch.setattr(swarm_routes, "_SWARM_DAL", swarm_dal)
    monkeypatch.setattr(swarm_spawn, "default_swarm_var_root", lambda: tmp_path / "swarm-var")
    yield _TranscriptEnv(dal, swarm_dal, tmp_path)
    swarm_dal.close()
    dal.close()


def test_provider_transcript_completed_with_attempt_and_workbook(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    transcript_env: _TranscriptEnv,
) -> None:
    env = transcript_env
    env.create_session(
        "ses_prov_done", state="completed", output_text="All acceptance tests green."
    )
    env.insert_attempt("ses_prov_done", ended_at=utc_now_iso(), end_reason="completed")
    workbook_text = "# Task alpha\n\n## Status\nDONE\n"
    env.write_workbook("swr_run1", "tsk_alpha", workbook_text)

    entries = env.get_transcript(asgi_client, auth_headers, "ses_prov_done")
    assert entries and all(entry.get("synthetic") is True for entry in entries)

    user_turns = [e for e in entries if e.get("actor") == "user"]
    assert [e["message"] for e in user_turns] == ["Implement the widget end to end."]

    assistant_turns = [e for e in entries if e.get("actor") == "assistant"]
    assert [e["message"] for e in assistant_turns] == ["All acceptance tests green."]

    attempt_turn = next(e for e in entries if "attempt" in e)
    assert "codex/gpt-5-codex" in attempt_turn["summary"]
    assert "tier fast" in attempt_turn["summary"]
    assert "end_reason completed" in attempt_turn["summary"]
    assert attempt_turn["attempt"]["board_task_id"] == "tsk_alpha"

    workbook_turn = next(e for e in entries if "workbook" in str(e.get("summary", "")).lower())
    assert workbook_text.strip() in workbook_turn["summary"]
    assert str(workbook_turn["file_path"]).endswith("swr_run1/tsk_alpha/WORKBOOK.md")


def test_provider_transcript_failed_surfaces_error_and_partial_output(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    transcript_env: _TranscriptEnv,
) -> None:
    env = transcript_env
    env.create_session(
        "ses_prov_fail",
        provider="kimi",
        state="failed",
        output_text="partial CLI output before crash",
        error="kimi exited 2: model overloaded",
    )

    entries = env.get_transcript(asgi_client, auth_headers, "ses_prov_fail")
    assert all(entry.get("synthetic") is True for entry in entries)
    # No swarm attempt row -> no attempt/workbook turns.
    assert not any("attempt" in entry for entry in entries)

    assistant_turns = [e for e in entries if e.get("actor") == "assistant"]
    assert [e["message"] for e in assistant_turns] == ["partial CLI output before crash"]

    error_turn = next(e for e in entries if e.get("actor") == "system")
    assert "failed" in error_turn["summary"]
    assert "kimi exited 2: model overloaded" in error_turn["summary"]


def test_provider_transcript_running_returns_prompt_and_placeholder(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    transcript_env: _TranscriptEnv,
) -> None:
    env = transcript_env
    env.create_session("ses_prov_live", state="running")

    entries = env.get_transcript(asgi_client, auth_headers, "ses_prov_live")
    assert all(entry.get("synthetic") is True for entry in entries)
    assert [e["message"] for e in entries if e.get("actor") == "user"] == [
        "Implement the widget end to end."
    ]
    placeholder = next(e for e in entries if e.get("actor") == "system")
    assert "arrives at completion" in placeholder["summary"]
    # Output has not been produced yet, so no assistant turn is invented.
    assert not any(e.get("actor") == "assistant" for e in entries)


def test_claude_bridge_transcript_unchanged(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    transcript_env: _TranscriptEnv,
) -> None:
    env = transcript_env
    env.create_session("ses_claude", provider="claude", state="completed", output_text="done")
    records = [
        {
            "ts": "2026-07-23T00:00:01Z",
            "type": "tool_call",
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/app.py"},
        },
        {"ts": "2026-07-23T00:00:02Z", "type": "event", "summary": "Stop"},
    ]
    env.write_activity("ses_claude", records)

    entries = env.get_transcript(asgi_client, auth_headers, "ses_claude")
    assert entries == records
    assert not any("synthetic" in entry for entry in entries)

    # A claude session WITHOUT an activity file stays empty (no synthesis).
    env.create_session("ses_claude_bare", provider="claude", state="running")
    assert env.get_transcript(asgi_client, auth_headers, "ses_claude_bare") == []


def test_provider_transcript_prefers_real_activity_file(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    transcript_env: _TranscriptEnv,
) -> None:
    """A provider session that somehow HAS activity records keeps them verbatim."""
    env = transcript_env
    env.create_session("ses_prov_real", state="completed", output_text="synth me not")
    records = [{"ts": "2026-07-23T01:00:00Z", "type": "event", "summary": "real record"}]
    env.write_activity("ses_prov_real", records)

    entries = env.get_transcript(asgi_client, auth_headers, "ses_prov_real")
    assert entries == records
    assert not any("synthetic" in entry for entry in entries)


def test_session_transcript_delta_basic_and_partial_lines(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    transcript_env: _TranscriptEnv,
) -> None:
    env = transcript_env
    env.create_session("ses_delta_1", state="running")

    # 1. Test when transcript file does not exist yet.
    response = asyncio.run(
        asgi_client.get(
            "/api/sessions/ses_delta_1/transcript/delta?offset=0",
            headers=auth_headers,
        )
    )
    assert response.status_code == 200
    data = response.json()
    assert data["raw"] == ""
    assert data["lines"] == []
    assert data["entries"] == []
    assert data["new_offset"] == 0

    # 2. Write partial data (no newline) to the transcript.
    path = SessionManifest(env.tmp_path / "ledger").path_for("ses_delta_1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"type": "event", "text": "start"}', encoding="utf-8")

    response = asyncio.run(
        asgi_client.get(
            "/api/sessions/ses_delta_1/transcript/delta?offset=0",
            headers=auth_headers,
        )
    )
    assert response.status_code == 200
    data = response.json()
    assert data["raw"] == ""
    assert data["lines"] == []
    assert data["new_offset"] == 0

    # 3. Complete the line and add another partial line.
    # Total written: '{"type": "event", "text": "start"}\n{"type": "event", "text": "partial"'
    line1 = '{"type": "event", "text": "start"}\n'
    line2_partial = '{"type": "event", "text": "partial"'
    path.write_text(line1 + line2_partial, encoding="utf-8")

    response = asyncio.run(
        asgi_client.get(
            "/api/sessions/ses_delta_1/transcript/delta?offset=0",
            headers=auth_headers,
        )
    )
    assert response.status_code == 200
    data = response.json()
    assert data["raw"] == line1
    assert data["lines"] == ['{"type": "event", "text": "start"}']
    assert len(data["entries"]) == 1
    assert data["entries"][0]["text"] == "start"
    expected_offset = len(line1.encode("utf-8"))
    assert data["new_offset"] == expected_offset

    # 4. Now read from the new offset. Since line2 is still partial, we should get nothing new.
    response2 = asyncio.run(
        asgi_client.get(
            f"/api/sessions/ses_delta_1/transcript/delta?offset={expected_offset}",
            headers=auth_headers,
        )
    )
    assert response2.status_code == 200
    data2 = response2.json()
    assert data2["raw"] == ""
    assert data2["lines"] == []
    assert data2["new_offset"] == expected_offset

    # 5. Complete line2 and append line3.
    line2 = '{"type": "event", "text": "partial"}\n'
    line3 = '{"type": "event", "text": "end"}\n'
    path.write_text(line1 + line2 + line3, encoding="utf-8")

    # Read from expected_offset
    response3 = asyncio.run(
        asgi_client.get(
            f"/api/sessions/ses_delta_1/transcript/delta?offset={expected_offset}",
            headers=auth_headers,
        )
    )
    assert response3.status_code == 200
    data3 = response3.json()
    assert data3["raw"] == line2 + line3
    assert data3["lines"] == [
        '{"type": "event", "text": "partial"}',
        '{"type": "event", "text": "end"}',
    ]
    assert len(data3["entries"]) == 2
    assert data3["entries"][0]["text"] == "partial"
    assert data3["entries"][1]["text"] == "end"
    final_offset = len((line1 + line2 + line3).encode("utf-8"))
    assert data3["new_offset"] == final_offset


def test_session_transcript_delta_rotation_guard(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    transcript_env: _TranscriptEnv,
) -> None:
    env = transcript_env
    env.create_session("ses_delta_rot", state="running")

    path = SessionManifest(env.tmp_path / "ledger").path_for("ses_delta_rot")
    path.parent.mkdir(parents=True, exist_ok=True)
    line1 = '{"type": "event", "text": "rotated"}\n'
    path.write_text(line1, encoding="utf-8")

    # Querying with offset larger than file size (which is len(line1)) should reset offset to 0 and read line1.
    response = asyncio.run(
        asgi_client.get(
            "/api/sessions/ses_delta_rot/transcript/delta?offset=99999",
            headers=auth_headers,
        )
    )
    assert response.status_code == 200
    data = response.json()
    assert data["raw"] == line1
    assert data["lines"] == ['{"type": "event", "text": "rotated"}']
    assert data["new_offset"] == len(line1.encode("utf-8"))


def test_session_transcript_delta_not_found(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    transcript_env: _TranscriptEnv,
) -> None:
    response = asyncio.run(
        asgi_client.get(
            "/api/sessions/ses_missing_delta/transcript/delta?offset=0",
            headers=auth_headers,
        )
    )
    assert response.status_code == 404
