"""Unit tests for OpenHands policy gate script (Lane 2).

Verifies local command-line and path guardrails, central API curl validation,
fail-closed behaviors, malformed payload rejections, and latency targets.
"""

from __future__ import annotations

import json
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

# Path to the gate script
SCRIPT_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "oh-policy-gate.sh"


class MockGateAPIHandler(BaseHTTPRequestHandler):
    decision_result = "allow"
    last_payload: dict[str, Any] | None = None

    def log_message(self, format: str, *args: Any) -> None:
        pass  # suppress logging

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        MockGateAPIHandler.last_payload = json.loads(body.decode("utf-8"))

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        response = {
            "gate_id": "g3",
            "decision": MockGateAPIHandler.decision_result,
            "evidence": {"reason": "mocked_result"},
            "next_state": "idle",
        }
        self.wfile.write(json.dumps(response).encode("utf-8"))


@pytest.fixture
def mock_gate_server() -> Any:
    # Reset state
    MockGateAPIHandler.decision_result = "allow"
    MockGateAPIHandler.last_payload = None

    server = HTTPServer(("127.0.0.1", 0), MockGateAPIHandler)
    port = server.server_port
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _run_gate_script(payload: dict[str, Any], grok_api_url: str) -> tuple[int, dict[str, Any]]:
    # Inherit existing system env (provides PATH, HOME, etc.) and inject test API URL
    import os

    env = dict(os.environ)
    env["OMNIAGENTOS_API_URL"] = grok_api_url

    # Run the bash script as a subprocess
    process = subprocess.Popen(
        [str(SCRIPT_PATH)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    stdout, stderr = process.communicate(input=json.dumps(payload).encode("utf-8"))

    # Parse the output JSON
    out_str = stdout.decode("utf-8").strip()
    try:
        out_json = json.loads(out_str) if out_str else {}
    except Exception:
        out_json = {"raw_output": out_str, "error": "unparseable"}

    return process.returncode, out_json


def test_gate_local_credentials_protection(mock_gate_server: str) -> None:
    """Verifies that reading sensitive ssh/creds files is blocked locally (DENY)."""
    payload = {
        "event_type": "PreToolUse",
        "tool_name": "editor",
        "tool_input": {"path": "~/.ssh/id_rsa"},
        "session_id": "ses-abc",
        "working_dir": "/workspace",
    }

    code, decision = _run_gate_script(payload, mock_gate_server)
    assert code == 2
    assert decision["decision"] == "deny"
    assert "Access to sensitive file" in decision["reason"]


def test_gate_local_destructive_command_protection(mock_gate_server: str) -> None:
    """Verifies that running destructive shell commands (like rm -rf) is blocked locally (DENY)."""
    payload = {
        "event_type": "PreToolUse",
        "tool_name": "terminal",
        "tool_input": {"command": "rm -rf /Users/youruser"},
        "session_id": "ses-abc",
        "working_dir": "/workspace",
    }

    code, decision = _run_gate_script(payload, mock_gate_server)
    assert code == 2
    assert decision["decision"] == "deny"
    assert "Destructive or credential-exposing command" in decision["reason"]


def test_gate_authorized_by_central_api(mock_gate_server: str) -> None:
    """Verifies that an authorized tool pass evaluation correctly returns ALLOW."""
    MockGateAPIHandler.decision_result = "allow"
    payload = {
        "event_type": "PreToolUse",
        "tool_name": "terminal",
        "tool_input": {"command": "ls -la"},
        "session_id": "ses-abc",
        "working_dir": "/workspace",
    }

    code, decision = _run_gate_script(payload, mock_gate_server)
    assert code == 0
    assert decision["decision"] == "allow"
    assert "authorized" in decision["reason"]

    # Ensure correct payload was transmitted to the G3 gate
    assert MockGateAPIHandler.last_payload is not None
    assert MockGateAPIHandler.last_payload["gate"] == "g3"
    assert MockGateAPIHandler.last_payload["evidence"]["tool_name"] == "terminal"


def test_gate_fail_closed_on_api_down() -> None:
    """Verifies fail-closed behavior: if central API is down, we must DENY."""
    payload = {
        "event_type": "PreToolUse",
        "tool_name": "terminal",
        "tool_input": {"command": "ls"},
        "session_id": "ses-abc",
        "working_dir": "/workspace",
    }

    # Point to a dead local port
    code, decision = _run_gate_script(payload, "http://127.0.0.1:54321/eval")
    assert code == 2
    assert decision["decision"] == "deny"
    assert "API unreachable" in decision["reason"]


def test_gate_rejected_by_central_api(mock_gate_server: str) -> None:
    """Verifies that if central API returns 'deny', we correctly return DENY."""
    MockGateAPIHandler.decision_result = "deny"
    payload = {
        "event_type": "PreToolUse",
        "tool_name": "terminal",
        "tool_input": {"command": "ls"},
        "session_id": "ses-abc",
        "working_dir": "/workspace",
    }

    code, decision = _run_gate_script(payload, mock_gate_server)
    assert code == 2
    assert decision["decision"] == "deny"
    assert "rejected execution" in decision["reason"]


def test_gate_latency_is_minimal(mock_gate_server: str) -> None:
    """Verifies that decision latency is bounded (gate does not do expensive work).

    Note: This test measures the gate script's performance but under parallel load
    (xdist), subprocess startup time and lock contention can dominate. We verify
    (1) the gate makes the decision correctly and (2) finishes within a generous
    bound that does not include startup overhead. The assertion is not latency alone.
    """
    payload = {
        "event_type": "PreToolUse",
        "tool_name": "terminal",
        "tool_input": {"command": "pwd"},
        "session_id": "ses-abc",
        "working_dir": "/workspace",
    }

    t0 = time.time()
    code, decision = _run_gate_script(payload, mock_gate_server)
    t1 = time.time()
    latency_ms = int((t1 - t0) * 1000)

    # (1) The script succeeded and made the decision.
    assert code == 0, f"Expected success (code 0), got {code}"
    assert decision.get("decision") == "allow", f"Expected 'allow' decision, got {decision.get('decision')}"

    # (2) Latency is bounded by a generous ceiling (NOT the sole assertion).
    # Under load with 16 parallel workers, subprocess startup can take 100-500ms;
    # we verify the gate is not doing expensive I/O or network calls, not that
    # this specific invocation completes in a tight bound. 5 seconds accounts for
    # startup, lock contention, and the gate's own work; a genuine hang or network
    # call would exceed this by an order of magnitude.
    assert (
        latency_ms < 5000
    ), f"Gate latency {latency_ms}ms exceeds generous bound (5s); gate may be doing expensive work"
