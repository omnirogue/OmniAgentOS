from __future__ import annotations

import io
import json
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from omniagentos.sessions import hook_client, install
from omniagentos.sessions.steering_marker import (
    mark_steering_pending,
    steering_marker_path,
)


def test_hook_client_denies_when_api_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO('{"session_id":"ses_1","tool_name":"Write","tool_input":{},"cwd":"/p"}'),
    )
    monkeypatch.setattr(hook_client, "_post", lambda *_: (_ for _ in ()).throw(OSError()))
    hook_client.main()
    result = json.loads(capsys.readouterr().out)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert result["hookSpecificOutput"]["permissionDecisionReason"] == "api-error"


def test_report_only_hook_allows_without_network(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OMNI_SESSION_HOOK_MODE", "report")
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"tool_name":"Read"}'))
    monkeypatch.setattr(
        hook_client, "_post", lambda *_: pytest.fail("read-only tool called network")
    )
    hook_client.main()
    assert (
        json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "allow"
    )


def test_read_only_fast_path_never_opens_http(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OMNI_SESSION_HOOK_MODE", raising=False)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("read-only fast path opened HTTP"),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            '{"hook_event_name":"PreToolUse","session_id":"ses_1",'
            '"tool_name":"Read","tool_input":{"file_path":"/tmp/x"}}'
        ),
    )
    hook_client.main()
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["permissionDecision"] == "allow"


def test_post_tool_without_marker_never_opens_http(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # This path is only reached when report mode is OFF; an ambient
    # OMNI_SESSION_HOOK_MODE would otherwise decide the test for us.
    monkeypatch.delenv("OMNI_SESSION_HOOK_MODE", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("OMNIAGENTOS_BRIDGE_SESSION_ID", "ses_1")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("marker-free PostToolUse opened HTTP"),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            '{"hook_event_name":"PostToolUse","session_id":"provider-ref",'
            '"tool_name":"Bash","tool_input":{"command":"pytest"}}'
        ),
    )
    hook_client.main()
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse"
    assert "additionalContext" not in output


def test_post_tool_with_marker_posts_via_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # This path is only reached when report mode is OFF; an ambient
    # OMNI_SESSION_HOOK_MODE would otherwise decide the test for us.
    monkeypatch.delenv("OMNI_SESSION_HOOK_MODE", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("OMNIAGENTOS_BRIDGE_SESSION_ID", "ses_1")
    monkeypatch.setattr(hook_client, "_hook_eval_headers", lambda: {"X-Test": "yes"})
    mark_steering_pending("ses_1")
    requests: list[tuple[urllib.request.Request, float]] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"decision":"allow","additional_context":"Apply the steering."}'

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> Response:
        requests.append((request, timeout))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            '{"hook_event_name":"PostToolUse","session_id":"provider-ref",'
            '"tool_name":"Bash","tool_input":{"command":"pytest"}}'
        ),
    )
    hook_client.main()

    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["additionalContext"] == "Apply the steering."
    assert len(requests) == 1
    request, timeout = requests[0]
    assert request.full_url.endswith("/api/sessions/hook-eval")
    assert timeout == 3.0
    assert json.loads(request.data or b"{}")["eval_kind"] == "steering"
    assert steering_marker_path("ses_1").is_file()


def test_post_tool_with_marker_fails_open_when_api_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    # This path is only reached when report mode is OFF; an ambient
    # OMNI_SESSION_HOOK_MODE would otherwise decide the test for us.
    monkeypatch.delenv("OMNI_SESSION_HOOK_MODE", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("OMNIAGENTOS_BRIDGE_SESSION_ID", "ses_1")
    monkeypatch.setattr(hook_client, "_hook_eval_headers", lambda: {"X-Test": "yes"})
    mark_steering_pending("ses_1")
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline")),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            '{"hook_event_name":"PostToolUse","session_id":"provider-ref",'
            '"tool_name":"Bash","tool_input":{}}'
        ),
    )
    hook_client.main()
    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse"
    assert "permissionDecisionReason" not in output


def _report_events_the_installer_registers(project: Path) -> list[str]:
    """Every hook event ``install.install`` wires to the report-only command.

    Derived from the file the installer actually writes rather than from a hand
    written list, so a new event added to ``install.install`` is automatically
    covered by the test below instead of silently escaping it.
    """
    settings: dict[str, Any] = json.loads(install.install(project).read_text(encoding="utf-8"))
    return sorted(
        event
        for event, entries in settings["hooks"].items()
        if isinstance(entries, list) and any(install._is_report_entry(e) for e in entries)
    )


def test_report_mode_reports_every_event_its_own_installer_registers(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Report mode must survive every per-event branch in ``main``.

    The regression this pins: the PostToolUse steering branch was added ABOVE the
    report_only check, so the one per-tool event the report-only installer
    registers returned before reaching ``_send_report`` and the mode reported
    nothing but session end.
    """
    events = _report_events_the_installer_registers(tmp_path / "proj")
    assert events, "the report-only installer registered no hook events"

    monkeypatch.setenv("OMNI_SESSION_HOOK_MODE", "report")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.delenv("OMNIAGENTOS_BRIDGE_SESSION_ID", raising=False)

    for event in events:
        posted: list[tuple[str, dict[str, Any]]] = []
        arrived = threading.Event()

        def fake_post(
            path: str,
            payload: dict[str, Any],
            headers: dict[str, str] | None = None,
            _posted: list[tuple[str, dict[str, Any]]] = posted,
            _arrived: threading.Event = arrived,
        ) -> dict[str, Any]:
            _posted.append((path, payload))
            _arrived.set()
            return {}

        monkeypatch.setattr(hook_client, "_post", fake_post)
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": event,
                        "session_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301",
                        "cwd": "/foreign/project",
                        "tool_name": "Write",
                        "tool_input": {"file_path": "/foreign/project/a.txt", "content": "x"},
                    }
                )
            ),
        )
        hook_client.main()

        # _send_report runs on a daemon thread; wait for it rather than sleeping.
        assert arrived.wait(5.0), f"report mode sent nothing for the {event} hook"
        assert posted[0][0] == "/api/sessions/ingest"
        assert posted[0][1]["tool_name"] == "Write"
        output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
        assert output["hookEventName"] == event


def test_report_mode_never_steers_the_session_it_only_observes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Report mode watches a foreign CLI; it must not write to or steer it."""
    var_dir = tmp_path / "var"
    monkeypatch.setenv("OMNI_SESSION_HOOK_MODE", "report")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(var_dir))
    monkeypatch.delenv("OMNIAGENTOS_BRIDGE_SESSION_ID", raising=False)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: pytest.fail("report mode opened HTTP for a read-only tool"),
    )
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            '{"hook_event_name":"PostToolUse","session_id":"3f2504e0-4f89-41d3-9a0c-0305e82c3301",'
            '"cwd":"/foreign/project","tool_name":"Read",'
            '"tool_input":{"file_path":"/foreign/project/src/billing.py"}}'
        ),
    )
    hook_client.main()

    output = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse"
    assert "additionalContext" not in output
    assert not list(var_dir.rglob("chain-read/*.json"))


def test_installer_preserves_settings_and_removes_only_its_hooks(tmp_path: Path) -> None:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "permissions": {"allow": ["Read"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other"}]}]},
            }
        ),
        encoding="utf-8",
    )

    install.install(tmp_path)
    install.install(tmp_path)
    installed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    assert installed["permissions"] == {"allow": ["Read"]}
    assert len(installed["hooks"]["PostToolUse"]) == 1
    assert len(installed["hooks"]["Stop"]) == 2

    install.uninstall(tmp_path)
    removed = json.loads(path.read_text(encoding="utf-8"))
    assert removed["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "other"}]}]
    assert "PostToolUse" not in removed["hooks"]
