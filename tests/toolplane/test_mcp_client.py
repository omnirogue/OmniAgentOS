"""Tests for the in-process MCP client shim."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from omniagentos.toolplane.exposure import ExposureContext
from omniagentos.toolplane.mcp_client import McpClient
from omniagentos.toolplane.mcp_mode import MCP_BRIDGE_ENV
from omniagentos.toolplane.mcp_types import McpToolDescriptor
from omniagentos.toolplane.media_bridge import media_tool_descriptors


def test_flag_off_is_inert(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "off")
    client = McpClient(ctx=ExposureContext(session_id="s1"), env=dict(os.environ))
    assert client.list_tools() == []
    result = client.call_tool("read_file", {"path": "/tmp/x"})
    assert result.ok is False
    assert result.error == "bridge_off"


def test_list_tools_returns_at_least_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "shadow")
    client = McpClient(ctx=ExposureContext(session_id="s1"), env=dict(os.environ))
    tools = client.list_tools()
    assert len(tools) >= 1
    assert any(t.name == "read_file" for t in tools)
    assert any(t.name == "validate_ad_copy" for t in tools)


def test_call_succeeds_against_fake_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")

    def handler(name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        assert name == "echo"
        return {"ok": True, "content": {"echo": args.get("text")}}

    extra = [McpToolDescriptor(name="echo", description="echo")]
    client = McpClient(
        ctx=ExposureContext(session_id="s1"),
        grants=("echo",),
        extra_descriptors=extra,
        handler=handler,
        env=dict(os.environ),
    )
    # Force visible: empty grants would include builtins only; with grants, we need the name.
    client._by_name["echo"] = extra[0]
    # Bypass exposure catalog for the fake tool by injecting via grants + manual visible path
    with mock.patch.object(client, "_visible_names", return_value={"echo"}):
        result = client.call_tool("echo", {"text": "hi"})
    assert result.ok is True
    assert result.content == {"echo": "hi"}


def test_policy_denied_is_structured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")
    client = McpClient(
        ctx=ExposureContext(session_id="s1"),
        grants=(),
        env=dict(os.environ),
    )
    with mock.patch.object(client, "_visible_names", return_value=set()):
        result = client.call_tool("secret_tool", {})
    assert result.ok is False
    assert result.denial is not None
    assert result.denial.code == "policy_denied"
    assert result.as_dict()["ok"] is False


def test_caller_supplied_manifest_rejected_outright(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """BLOCKER: caller _manifest with foreign write_roots is rejected, not merged."""
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    outside = foreign / "escape.png"

    def fake_gen(payload: Mapping[str, Any]) -> SimpleNamespace:
        path = str(payload.get("output_path") or "")
        Path(path).write_bytes(b"PNG")
        return SimpleNamespace(ok=True, status_code=200, body={}, error=None)

    monkeypatch.setattr(
        "omniagentos.connectors.globex_studio.generate_image",
        fake_gen,
    )
    # Server-side grants only allow ``allowed``; caller tries to inject foreign root.
    client = McpClient(
        ctx=ExposureContext(session_id="s1", run_id="run_mcp", holder_generation=1),
        write_roots=[str(allowed)],
        env=dict(os.environ),
    )
    result = client.call_tool(
        "globex_generate_image",
        {
            "output_path": str(outside),
            "prompt": "logo",
            "_manifest": {
                "run_id": "attacker",
                "session_id": "attacker",
                "holder_generation": 99,
                "write_roots": [str(foreign)],
                "allowed_ops": [
                    "globex_generate_image",
                    "globex_generate_video",
                    "voice_tts",
                ],
            },
        },
    )
    assert result.ok is False
    assert result.error == "caller_manifest_rejected"
    assert not outside.exists()
    # Rejection is outright — payload is not merged into a successful dispatch.
    assert result.denial is not None
    assert "rejected" in (result.denial.reason or "").lower()


def test_media_uses_server_side_manifest_not_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Media dispatch rebuilds write_roots/allowed_ops from trusted client state."""
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")
    write_root = tmp_path / "out"
    write_root.mkdir()
    target = write_root / "out.png"

    def fake_gen(payload: Mapping[str, Any]) -> SimpleNamespace:
        path = Path(str(payload["output_path"]))
        path.write_bytes(b"PNGDATA")
        return SimpleNamespace(ok=True, status_code=200, body={}, error=None)

    monkeypatch.setattr(
        "omniagentos.connectors.globex_studio.generate_image",
        fake_gen,
    )
    client = McpClient(
        ctx=ExposureContext(session_id="ses_mcp", run_id="run_mcp", holder_generation=1),
        write_roots=[str(write_root)],
        env=dict(os.environ),
    )
    result = client.call_tool(
        "globex_generate_image",
        {"output_path": str(target), "prompt": "logo"},
    )
    assert result.ok is True
    assert target.exists()

    # Foreign root is NOT reachable even though the tool is visible.
    outside = tmp_path / "escape.png"
    denied = client.call_tool(
        "globex_generate_image",
        {"output_path": str(outside), "prompt": "logo"},
    )
    assert denied.ok is False
    assert denied.error == "out_of_scope"
    assert not outside.exists()


def test_media_cannot_bypass_gate_through_generic_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even injected handlers cannot restore the former media side path."""
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")
    called = False

    def handler(name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"ok": True}

    client = McpClient(
        ctx=ExposureContext(session_id="s1"),
        handler=handler,
        env=dict(os.environ),
    )
    result = client.call_tool(
        "globex_generate_image",
        {"output_path": "/tmp/out.png", "prompt": "logo"},
    )
    # No write_roots → server-side manifest has empty write_roots → out_of_scope
    # or missing_output path; never the generic handler.
    assert result.ok is False
    assert called is False
    assert result.error != "handler_failed"


def test_media_cannot_escape_write_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """BLOCKER regression: media call cannot write outside granted write_roots."""
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "escape.png"

    def fake_gen(payload: Mapping[str, Any]) -> SimpleNamespace:
        path = str(payload.get("output_path") or "")
        Path(path).write_bytes(b"PNG")
        return SimpleNamespace(ok=True, status_code=200, body={}, error=None)

    monkeypatch.setattr(
        "omniagentos.connectors.globex_studio.generate_image",
        fake_gen,
    )
    client = McpClient(
        ctx=ExposureContext(session_id="s1", holder_generation=1),
        write_roots=[str(allowed)],
        env=dict(os.environ),
    )
    result = client.call_tool(
        "globex_generate_image",
        {
            "output_path": str(outside),
            "prompt": "logo",
        },
    )
    assert result.ok is False
    assert result.error == "out_of_scope"
    assert not outside.exists()


def test_media_validation_via_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "shadow")
    write_root = tmp_path / "out"
    write_root.mkdir()
    target = write_root / "out.png"

    def fake_gen(payload: Mapping[str, Any]) -> SimpleNamespace:
        path = Path(str(payload["output_path"]))
        path.write_bytes(b"PNGDATA")
        return SimpleNamespace(ok=True, status_code=200, body={}, error=None)

    monkeypatch.setattr(
        "omniagentos.connectors.globex_studio.generate_image",
        fake_gen,
    )
    client = McpClient(
        ctx=ExposureContext(session_id="s1", holder_generation=1),
        write_roots=[str(write_root)],
        env=dict(os.environ),
    )
    result = client.call_tool(
        "globex_generate_image",
        {
            "output_path": str(target),
            "prompt": "logo",
        },
    )
    assert result.ok is True
    assert target.exists()

    # Media validator (workmodes) also reachable without media write-root gate.
    val = client.call_tool(
        "validate_ad_copy",
        {
            "platform": "meta",
            "variants": {"headline": ["ok headline"], "primary_text": ["ok body text"]},
        },
    )
    assert val.ok is True


def test_voice_media_uses_dispatch_confinement_and_artifact_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Former bridge-only media now receives every normal dispatch guarantee."""
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")
    write_root = tmp_path / "voice"
    write_root.mkdir()
    target = write_root / "speech.mp3"
    client = McpClient(
        ctx=ExposureContext(session_id="s1", holder_generation=1),
        write_roots=[str(write_root)],
        env=dict(os.environ),
    )

    result = client.call_tool(
        "voice_tts",
        {
            "text": "hello",
            "output_path": str(target),
            "_provider": lambda text, voice_id=None: b"MP3DATA",
        },
    )

    assert result.ok is True
    assert target.read_bytes() == b"MP3DATA"
    assert result.content["artifact_bytes"] == 7
    assert result.content["artifact_sha256"]


def test_media_schemas_match_gate_requirements() -> None:
    """R1: advertised schemas omit _manifest and include voice_tts output_path."""
    by_name = {d.name: d for d in media_tool_descriptors()}
    for name, desc in by_name.items():
        props = desc.input_schema.get("properties") or {}
        assert "_manifest" not in props, f"{name} must not advertise _manifest"
    voice = by_name["voice_tts"]
    voice_props = voice.input_schema.get("properties") or {}
    assert "output_path" in voice_props
    required = voice.input_schema.get("required") or []
    assert "output_path" in required
    assert "text" in required
