"""Tests for PreToolUse lifecycle/experience capture (no migration)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.memory.store import ConversationStore
from omniagentos.sessions.hook_client import _maybe_capture_pretool
from omniagentos.sessions.lifecycle_capture import (
    DEFAULT_MAX_CAPTURES_PER_SESSION,
    ENVELOPE_VERSION,
    SESSION_MEMORY_CAPTURE_ENV,
    build_envelope,
    capture_pretool_use,
    redact_secrets,
    session_memory_capture_mode,
)


class _Store:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def append_turn(
        self,
        scope_type: str,
        scope_id: str,
        role: str,
        content: str,
        *,
        model: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        row = {
            "scope_type": scope_type,
            "scope_id": scope_id,
            "role": role,
            "content": content,
            "meta": meta or {},
        }
        self.turns.append(row)
        return row


def test_mode_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SESSION_MEMORY_CAPTURE_ENV, raising=False)
    assert session_memory_capture_mode({}) == "off"


def test_redaction_strips_planted_secret() -> None:
    raw = {
        "api_key": "sk-supersecretvalue",
        "nested": {"Authorization": "Bearer abc.def"},
        "note": "password=hunter2 keep this",
        "safe": "hello",
    }
    redacted = redact_secrets(raw)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["Authorization"] == "[REDACTED]"
    assert "hunter2" not in json.dumps(redacted)
    assert redacted["safe"] == "hello"


def test_inplace_redaction_preserves_quotes_and_covers_jwt() -> None:
    """R2: redaction replaces only credential values; quotes and bare JWTs covered."""
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    command = (
        f'export TOKEN="sk-live-supersecretvalue123"; '
        f"curl -H 'Authorization: Bearer {jwt}' "
        f"-H \"password='hunter2'\" "
        f"--token {jwt} "
        f"https://api.example"
    )
    redacted = redact_secrets(command)
    assert "sk-live-supersecretvalue123" not in redacted
    assert jwt not in redacted
    assert "hunter2" not in redacted
    # Surrounding syntax / quotes stay intact (in-place value redaction).
    assert 'TOKEN="[REDACTED]"' in redacted
    assert "Bearer [REDACTED]" in redacted
    assert "password='[REDACTED]'" in redacted
    # Double-quoted shell fragment survives as a quoted form.
    assert redacted.count('"') >= 2


def test_bearer_token_value_never_persisted() -> None:
    """BLOCKER regression: free-text Authorization Bearer values must not leak."""
    store = _Store()
    secret = "super.secret.token.value.xyz"
    out = capture_pretool_use(
        {
            "session_id": "ses_sec",
            "tool_name": "Bash",
            "tool_input": {
                "command": f"curl -H 'Authorization: Bearer {secret}' https://api.example"
            },
        },
        store=store,
        env={SESSION_MEMORY_CAPTURE_ENV: "enforce"},
    )
    assert out is not None
    assert len(store.turns) == 1
    payload = store.turns[0]["content"]
    assert secret not in payload
    assert "Bearer " + secret not in payload
    assert "[REDACTED]" in payload
    # Envelope tool_input path too
    body = json.loads(payload)
    assert secret not in json.dumps(body)


def test_inplace_redaction_real_conversation_store(tmp_path: Path) -> None:
    """R2: the real SQLite capture path redacts values without eating syntax."""
    db_path = tmp_path / "capture-redact.db"
    migrate(str(db_path))
    sql_store = SqliteStore(str(db_path))
    store = ConversationStore(sql_store)
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiJ1c2VyIn0."
        "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    )
    secret_cmd = (
        '-H "Authorization: Bearer sk-live-1234567890" '
        f"curl -H 'Authorization: Bearer {jwt}' "
        f'-d "api_key=sk-prod-abcdef123456" '
        f"--header 'token=\"leakme\"' "
        f"naked-jwt={jwt} naked-key=sk-naked-abcdef123456 "
        "https://api.example"
    )
    out = capture_pretool_use(
        {
            "session_id": "ses_real_redact",
            "tool_name": "Bash",
            "tool_input": {"command": secret_cmd},
        },
        store=store,
        env={SESSION_MEMORY_CAPTURE_ENV: "enforce"},
    )
    assert out is not None
    rows = sql_store._connection.execute(
        "SELECT content FROM conversations "
        "WHERE scope_type = 'session' AND scope_id = 'ses_real_redact'"
    ).fetchall()
    assert len(rows) == 1
    content = str(rows[0]["content"])
    assert jwt not in content
    assert "sk-live-1234567890" not in content
    assert "sk-prod-abcdef123456" not in content
    assert "sk-naked-abcdef123456" not in content
    assert "leakme" not in content
    assert "[REDACTED]" in content
    # In-place forms preserve surrounding syntax through the SQLite sink.
    assert r"-H \"Authorization: Bearer [REDACTED]\"" in content
    assert "Bearer [REDACTED]" in content
    assert r"token=\"[REDACTED]\"" in content
    assert "naked-jwt=[REDACTED]" in content
    assert "naked-key=[REDACTED]" in content


def test_flag_off_writes_nothing() -> None:
    store = _Store()
    out = capture_pretool_use(
        {"session_id": "ses_1", "tool_name": "Read", "tool_input": {"path": "a"}},
        store=store,
        env={SESSION_MEMORY_CAPTURE_ENV: "off"},
    )
    assert out is None
    assert store.turns == []


def test_shadow_builds_without_write() -> None:
    store = _Store()
    out = capture_pretool_use(
        {
            "session_id": "ses_1",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "hook_event_name": "PreToolUse",
        },
        store=store,
        env={SESSION_MEMORY_CAPTURE_ENV: "shadow"},
    )
    assert out is not None
    assert out["version"] == ENVELOPE_VERSION
    assert store.turns == []


def test_enforce_round_trips_into_store() -> None:
    store = _Store()
    out = capture_pretool_use(
        {
            "session_id": "ses_9",
            "tool_name": "Edit",
            "tool_input": {"path": "f.py", "api_key": "sk-leakme"},
            "cwd": "/tmp/work",
        },
        store=store,
        env={SESSION_MEMORY_CAPTURE_ENV: "enforce"},
    )
    assert out is not None
    assert len(store.turns) == 1
    turn = store.turns[0]
    assert turn["scope_type"] == "session"
    assert turn["scope_id"] == "ses_9"
    assert turn["meta"]["kind"] == "lifecycle_capture"
    body = json.loads(turn["content"])
    assert body["tool_name"] == "Edit"
    assert body["tool_input"]["api_key"] == "[REDACTED]"
    assert "sk-leakme" not in turn["content"]


def test_conversations_growth_bounded_and_purged() -> None:
    """MAJOR: PreToolUse capture purges older lifecycle rows beyond keep-count."""
    store = _Store()
    keep = 5
    for i in range(keep + 8):
        capture_pretool_use(
            {
                "session_id": "ses_grow",
                "tool_name": f"Tool{i}",
                "tool_input": {"n": i},
            },
            store=store,
            env={SESSION_MEMORY_CAPTURE_ENV: "enforce"},
            max_captures=keep,
        )
    lifecycle = [
        t
        for t in store.turns
        if t.get("scope_id") == "ses_grow"
        and (t.get("meta") or {}).get("kind") == "lifecycle_capture"
    ]
    assert len(lifecycle) == keep
    assert keep <= DEFAULT_MAX_CAPTURES_PER_SESSION or keep == 5


def test_conversations_growth_bounded_in_real_store(tmp_path: Path) -> None:
    """The production SQLite store uses the serialized purge seam."""
    db_path = tmp_path / "capture.db"
    migrate(str(db_path))
    sql_store = SqliteStore(str(db_path))
    store = ConversationStore(sql_store)
    keep = 3
    for i in range(keep + 4):
        capture_pretool_use(
            {
                "session_id": "ses_real",
                "tool_name": f"Tool{i}",
                "tool_input": {"n": i},
            },
            store=store,
            env={SESSION_MEMORY_CAPTURE_ENV: "enforce"},
            max_captures=keep,
        )
    rows = sql_store._connection.execute(
        "SELECT seq, meta_json FROM conversations "
        "WHERE scope_type = 'session' AND scope_id = 'ses_real' ORDER BY seq"
    ).fetchall()
    assert len(rows) == keep
    assert [int(row["seq"]) for row in rows] == [4, 5, 6]
    assert all(json.loads(row["meta_json"])["kind"] == "lifecycle_capture" for row in rows)


def test_writer_exception_does_not_propagate() -> None:
    class Boom:
        def append_turn(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("db down")

    out = capture_pretool_use(
        {"session_id": "ses_1", "tool_name": "Read", "tool_input": {}},
        store=Boom(),
        env={SESSION_MEMORY_CAPTURE_ENV: "enforce"},
    )
    # Fail-open: exception swallowed; may still return envelope from before write
    # or None if exception wrapped outer try — either is acceptable.
    assert out is None or out.get("version") == ENVELOPE_VERSION


def test_hook_client_capture_path_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake(payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(payload)
        raise RuntimeError("should not escape")

    monkeypatch.setattr(
        "omniagentos.sessions.lifecycle_capture.capture_pretool_use",
        fake,
    )
    # Import path used by hook_client re-imports; call wrapper directly.
    _maybe_capture_pretool({"session_id": "s", "tool_name": "Read"})
    # When import uses real module, monkeypatch on lifecycle_capture is enough
    # only if hook re-imports each time — _maybe_capture_pretool imports inside.
    # So call capture path through real import after patching the module attr.
    import omniagentos.sessions.lifecycle_capture as lc

    monkeypatch.setattr(lc, "capture_pretool_use", fake)
    _maybe_capture_pretool({"session_id": "s", "tool_name": "Read"})
    # No exception raised.


def test_build_envelope_version() -> None:
    env = build_envelope({"tool_name": "X", "tool_input": {"a": 1}})
    assert env["version"] == ENVELOPE_VERSION
    assert env["event"] == "tool_call"
