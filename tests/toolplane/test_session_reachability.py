"""Reachability tests: toolplane observation on the real session tool path (L17 M-17/M-08).

The audit's finding was that toolplane observation was only reachable from the
standalone CLI. These tests drive the *real* production seam instead -- the
PreToolUse gate ``POST /api/sessions/hook-eval``, mounted on the real ASGI app --
and assert that a durable observation lands on disk as a side effect of an
ordinary gate call. Nothing here asserts on source text.

They also pin the two safety properties of that adapter: the observation is
metadata-only (no reason text, tool input or cwd), and it can never change or
block a gate decision.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import sessions
from omniagentos.contracts import ActionClass
from omniagentos.sessions import token
from omniagentos.toolplane import observe as observe_module
from omniagentos.toolplane.observe import OBSERVATIONS_SUBDIR, ObservationSink
from omniagentos.toolplane.session import SESSION_SOURCE, bypass_inventory
from tests.api.fake_store import FakeStore
from tests.sessions.api.test_routes import FakeSessionsDal


@pytest.fixture
def asgi_client():
    app.dependency_overrides[get_store] = lambda: FakeStore()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def dal(monkeypatch: pytest.MonkeyPatch) -> FakeSessionsDal:
    fake = FakeSessionsDal()
    monkeypatch.setattr(sessions, "get_sessions_dal", lambda: fake)
    return fake


@pytest.fixture
def token_value(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    return token.load_or_create_token()


@pytest.fixture
def ledger(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Point the process-wide observation sink at an isolated ledger."""
    ledger_dir = tmp_path / "ledger"
    monkeypatch.setattr(
        observe_module, "_default_sink", ObservationSink(ledger_dir=str(ledger_dir))
    )
    return ledger_dir


def _records(ledger_dir) -> list[dict[str, Any]]:
    obs_dir = ledger_dir / OBSERVATIONS_SUBDIR
    if not obs_dir.is_dir():
        return []
    records = []
    for path in sorted(obs_dir.glob("*.json")):
        record = json.loads(path.read_text())
        if record.get("source") == SESSION_SOURCE:
            records.append(record)
    return records


def _post(asgi_client, headers: dict[str, str], **body: Any) -> httpx.Response:
    return asyncio.run(asgi_client.post("/api/sessions/hook-eval", headers=headers, json=body))


class TestRealSessionPathIsObserved:
    def test_allowed_tool_call_emits_a_durable_observation(
        self, asgi_client, dal, token_value, ledger, monkeypatch
    ):
        monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.READ_ONLY)

        response = _post(
            asgi_client,
            {"X-Session-Token": token_value},
            session_id="ses_1",
            tool_name="Read",
            tool_input={"file_path": "/project/a"},
            cwd="/project",
        )

        assert response.status_code == 200
        assert response.json()["decision"] == "allow"

        records = _records(ledger)
        assert len(records) == 1
        assert records[0]["tool"] == "Read"
        assert records[0]["session_id"] == "ses_1"
        assert records[0]["status"] == "allowed"
        assert records[0]["ok"] is True
        assert records[0]["decision"] == "allow"

    def test_denied_tool_call_emits_a_denied_observation(
        self, asgi_client, dal, token_value, ledger, monkeypatch
    ):
        monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)

        response = _post(
            asgi_client,
            {"X-Session-Token": token_value},
            session_id="ses_1",
            tool_name="Write",
            tool_input={"content": "x", "file_path": "/project/a"},
            cwd="/project",
        )

        assert response.status_code == 200
        assert response.json()["decision"] == "deny"

        records = _records(ledger)
        assert len(records) == 1
        assert records[0]["status"] == "denied"
        assert records[0]["ok"] is False
        assert records[0]["error"] == "approval_required"

    def test_rejected_request_is_observed_too(self, asgi_client, dal, token_value, ledger):
        """The 401 path is an outcome, and outcomes are observed (M-08)."""
        response = _post(
            asgi_client,
            {},
            session_id="ses_1",
            tool_name="Write",
            tool_input={},
            cwd="/project",
        )

        assert response.status_code == 401

        records = _records(ledger)
        assert len(records) == 1
        assert records[0]["status"] == "denied"
        assert records[0]["error"] == "unauthorized"

    def test_gate_failure_is_observed_as_failed_not_success(
        self, asgi_client, dal, token_value, ledger, monkeypatch
    ):
        monkeypatch.setattr(
            sessions, "classify_tool", lambda *_: (_ for _ in ()).throw(RuntimeError())
        )

        response = _post(
            asgi_client,
            {"X-Session-Token": token_value},
            session_id="ses_1",
            tool_name="Write",
            tool_input={},
            cwd="/project",
        )

        assert response.json()["decision"] == "deny"
        records = _records(ledger)
        assert len(records) == 1
        assert records[0]["ok"] is False
        assert records[0]["error"] == "api_error"

    def test_every_call_is_observed_separately(
        self, asgi_client, dal, token_value, ledger, monkeypatch
    ):
        """Two identical calls are two events; they must not deduplicate each other."""
        monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.READ_ONLY)
        for _ in range(3):
            _post(
                asgi_client,
                {"X-Session-Token": token_value},
                session_id="ses_1",
                tool_name="Read",
                tool_input={"file_path": "/project/a"},
                cwd="/project",
            )

        assert len(_records(ledger)) == 3


class TestObservationIsMetadataOnly:
    def test_tool_input_cwd_and_reason_text_never_reach_disk(
        self, asgi_client, dal, token_value, ledger, monkeypatch
    ):
        monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.IRREVERSIBLE)

        _post(
            asgi_client,
            {"X-Session-Token": token_value},
            session_id="ses_1",
            tool_name="Write",
            tool_input={"content": "SUPER-SENSITIVE-BODY", "file_path": "/project/secret.txt"},
            cwd="/project",
        )

        raw = json.dumps(_records(ledger))
        assert "SUPER-SENSITIVE-BODY" not in raw
        assert "/project/secret.txt" not in raw
        # The gate reason names the parked approval; only the short code is kept.
        assert "apr_1" not in raw
        assert "parked" not in raw


class TestObservationCannotAffectTheGate:
    def test_sink_failure_does_not_change_the_decision(
        self, asgi_client, dal, token_value, ledger, monkeypatch
    ):
        def _explode(_observation):
            raise RuntimeError("ledger on fire")

        monkeypatch.setattr(observe_module._default_sink, "emit", _explode)
        monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.READ_ONLY)

        response = _post(
            asgi_client,
            {"X-Session-Token": token_value},
            session_id="ses_1",
            tool_name="Read",
            tool_input={"file_path": "/project/a"},
            cwd="/project",
        )

        assert response.status_code == 200
        assert response.json()["decision"] == "allow"
        assert _records(ledger) == []


class TestCoverageInventory:
    def test_inventory_separates_covered_tools_from_bypasses(
        self, asgi_client, dal, token_value, ledger, monkeypatch
    ):
        monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.READ_ONLY)
        for tool_name, tool_input in (
            ("Read", {"file_path": "/project/a"}),
            ("Bash", {"command": "ls"}),
        ):
            _post(
                asgi_client,
                {"X-Session-Token": token_value},
                session_id="ses_1",
                tool_name=tool_name,
                tool_input=tool_input,
                cwd="/project",
            )

        inventory = bypass_inventory(ledger_dir=str(ledger))

        assert inventory["observed"] == 2
        assert inventory["tools"]["Read"]["coverage"] == "toolplane"
        assert inventory["tools"]["Read"]["capability"] == "read_file"
        assert inventory["tools"]["Bash"]["coverage"] == "bypass"
        assert inventory["tools"]["Bash"]["capability"] is None
        assert inventory["covered"] == 1
        assert inventory["bypassed"] == 1


class TestSpoolDrainOnRealSessionPath:
    def test_queued_spool_observations_drain_after_restart_through_real_session_path(
        self, asgi_client, dal, token_value, ledger, monkeypatch
    ):
        """M-28 / L17 repair: Queued spool observations from before restart must
        automatically drain when a real production session call executes on
        POST /api/sessions/hook-eval.
        """
        spool_dir = ledger / observe_module.SPOOL_SUBDIR
        spool_dir.mkdir(parents=True, exist_ok=True)

        # Queue an observation in the spool (as if left by a process before restart)
        queued_payload = {
            "attempts": 1,
            "observation": {
                "version": "1",
                "ts": "2026-07-26T00:00:00+00:00",
                "tool": "Read",
                "session_id": "ses_restart_prev",
                "correlation_id": "corr_queued_before_restart",
                "status": "allowed",
                "ok": True,
                "duration_ms": 10,
                "source": SESSION_SOURCE,
                "decision": "allow",
                "coverage": "toolplane",
                "capability": "read_file",
            },
        }
        spool_file = spool_dir / "corr_queued_before_restart.json"
        spool_file.write_text(json.dumps(queued_payload), encoding="utf-8")

        obs_dir = ledger / OBSERVATIONS_SUBDIR
        assert not obs_dir.exists() or len(list(obs_dir.glob("*.json"))) == 0
        assert len(list(spool_dir.glob("*.json"))) == 1

        # Execute a real call on the production path
        monkeypatch.setattr(sessions, "classify_tool", lambda *_: ActionClass.READ_ONLY)
        response = _post(
            asgi_client,
            {"X-Session-Token": token_value},
            session_id="ses_2",
            tool_name="Read",
            tool_input={"file_path": "/project/b"},
            cwd="/project",
        )

        assert response.status_code == 200

        # Spool must be drained through the real path
        assert len(list(spool_dir.glob("*.json"))) == 0

        # Observations directory must contain both the new observation and the drained queued observation
        records = list(obs_dir.glob("*.json"))
        assert len(records) == 2
        correlation_ids = {json.loads(r.read_text())["correlation_id"] for r in records}
        assert "corr_queued_before_restart" in correlation_ids
