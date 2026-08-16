"""Tool reach: media + workmodes bridges + assess.py verify hook."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from omniagentos.execution.assess import assess
from omniagentos.toolplane.exposure import ExposureContext
from omniagentos.toolplane.mcp_client import McpClient
from omniagentos.toolplane.mcp_mode import MCP_BRIDGE_ENV
from omniagentos.toolplane.media_bridge import (
    MEDIA_TOOL_NAMES,
    media_tool_descriptors,
    register_media_handlers,
    run_media_tool,
)
from omniagentos.toolplane.workmodes_bridge import (
    VALIDATOR_TOOL_NAMES,
    run_workmodes_tool,
    workmodes_tool_descriptors,
)


def test_media_and_workmodes_descriptors_present() -> None:
    media = {d.name for d in media_tool_descriptors()}
    work = {d.name for d in workmodes_tool_descriptors()}
    assert media == set(MEDIA_TOOL_NAMES)
    assert work == set(VALIDATOR_TOOL_NAMES)


def test_bridged_tools_invocable_with_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "shadow")
    write_root = tmp_path / "w"
    write_root.mkdir()
    target = write_root / "a.png"

    def fake_gen(payload: Mapping[str, Any]) -> SimpleNamespace:
        Path(str(payload["output_path"])).write_bytes(b"PNG")
        return SimpleNamespace(ok=True, status_code=200, body={}, error=None)

    monkeypatch.setattr(
        "omniagentos.connectors.globex_studio.generate_image",
        fake_gen,
    )
    client = McpClient(
        ctx=ExposureContext(session_id="s1", run_id="r1", holder_generation=1),
        write_roots=[str(write_root)],
        env=dict(os.environ),
    )
    names = {t.name for t in client.list_tools()}
    for name in MEDIA_TOOL_NAMES:
        assert name in names
    for name in VALIDATOR_TOOL_NAMES:
        assert name in names

    img = client.call_tool(
        "globex_generate_image",
        {
            "output_path": str(target),
            "prompt": "logo",
        },
    )
    assert img.ok is True

    val = run_workmodes_tool(
        "validate_ad_copy",
        {"platform": "meta", "variants": {"headline": ["ok"], "primary_text": ["ok body"]}},
    )
    assert "ok" in val


def test_media_stub_no_live_api() -> None:
    register_media_handlers()
    with mock.patch(
        "omniagentos.connectors.globex_studio.generate_image",
        side_effect=AssertionError("live API must not be called"),
    ):
        register_media_handlers(
            {
                "globex_generate_image": lambda args: {
                    "ok": True,
                    "result": {"stub": True},
                }
            }
        )
        out = run_media_tool("globex_generate_image", {"output_path": "x.png"})
    assert out["ok"] is True


def test_assess_hook_records_validator_shadow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "shadow")
    (tmp_path / "out.txt").write_text("x")
    result = assess(
        working_dir=tmp_path,
        expected_files=("out.txt",),
        artifact_manifest={
            "workmodes_checks": [
                {
                    "tool": "validate_ad_copy",
                    "platform": "meta",
                    "variants": {
                        "headline": ["x" * 200],
                        "primary_text": ["y" * 200],
                    },
                }
            ]
        },
    )
    wm = result["mechanical"].get("workmodes_verify")
    assert wm is not None
    assert wm["any_fail"] is True
    assert wm["mode"] == "shadow"
    assert wm["verdict"] == "pass"
    assert result["verdict"] == "pass"


def test_assess_hook_off_unchanged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "off")
    (tmp_path / "out.txt").write_text("x")
    result = assess(
        working_dir=tmp_path,
        expected_files=("out.txt",),
        artifact_manifest={
            "workmodes_checks": [
                {
                    "tool": "validate_ad_copy",
                    "platform": "meta",
                    "variants": {"headline": ["x" * 200], "primary_text": ["y"]},
                }
            ]
        },
    )
    assert "workmodes_verify" not in result["mechanical"]
    assert result["verdict"] == "pass"


def test_assess_hook_enforce_can_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(MCP_BRIDGE_ENV, "enforce")
    (tmp_path / "out.txt").write_text("x")
    result = assess(
        working_dir=tmp_path,
        expected_files=("out.txt",),
        artifact_manifest={
            "workmodes_checks": [
                {
                    "tool": "validate_ad_copy",
                    "platform": "meta",
                    "variants": {"headline": ["x" * 200], "primary_text": ["y" * 200]},
                }
            ]
        },
    )
    wm = result["mechanical"]["workmodes_verify"]
    assert wm["verdict"] == "fail"
    assert result["verdict"] == "fail"
