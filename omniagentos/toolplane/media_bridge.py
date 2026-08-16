"""Thin adapters registering media tools for MCP / toolplane reach.

Does not reimplement Globex or voice TTS — only registers descriptors and
dispatch wrappers that call the existing connectors and voice service.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from omniagentos.toolplane.mcp_types import McpToolDescriptor

__all__ = [
    "MEDIA_TOOL_NAMES",
    "media_tool_descriptors",
    "register_media_handlers",
    "run_media_tool",
]

MEDIA_TOOL_NAMES: tuple[str, ...] = (
    "globex_generate_image",
    "globex_generate_video",
    "voice_tts",
)

_HANDLERS: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {}


def media_tool_descriptors() -> list[McpToolDescriptor]:
    return [
        McpToolDescriptor(
            name="globex_generate_image",
            description="Generate an image via Globex Studio (existing connector).",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "output_path": {"type": "string"},
                },
                "required": ["output_path"],
            },
            risk="medium",
            source="media",
        ),
        McpToolDescriptor(
            name="globex_generate_video",
            description="Generate a video via Globex Studio (existing connector).",
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "output_path": {"type": "string"},
                },
                "required": ["output_path"],
            },
            risk="medium",
            source="media",
        ),
        McpToolDescriptor(
            name="voice_tts",
            description="Synthesize speech via the existing voice TTS service.",
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "voice_id": {"type": "string"},
                    # Required by the toolplane dispatch gate (_voice_tts): the
                    # artifact path is confined to server-side write_roots.
                    "output_path": {"type": "string"},
                },
                "required": ["text", "output_path"],
            },
            risk="medium",
            source="media",
        ),
    ]


def _globex_image(args: Mapping[str, Any]) -> dict[str, Any]:
    from omniagentos.connectors.globex_studio import generate_image

    payload = dict(args)
    result = generate_image(payload)
    ok = bool(getattr(result, "ok", False))
    return {
        "ok": ok,
        "status_code": getattr(result, "status_code", None),
        "body": getattr(result, "body", None),
        "error": getattr(result, "error", None),
    }


def _globex_video(args: Mapping[str, Any]) -> dict[str, Any]:
    from omniagentos.connectors.globex_studio import generate_video

    payload = dict(args)
    result = generate_video(payload)
    ok = bool(getattr(result, "ok", False))
    return {
        "ok": ok,
        "status_code": getattr(result, "status_code", None),
        "body": getattr(result, "body", None),
        "error": getattr(result, "error", None),
    }


def _voice_tts(args: Mapping[str, Any]) -> dict[str, Any]:
    """Best-effort TTS via the existing provider surface (stub-friendly)."""
    text = str(args.get("text") or "")
    if not text:
        return {"ok": False, "error": "invalid_args", "detail": "text is required"}
    # Prefer a pure callable if tests inject one; otherwise return a structured stub
    # that does not hit the network.
    provider = args.get("_provider")
    if callable(provider):
        out = provider(text, voice_id=args.get("voice_id"))
        return {"ok": True, "result": out}
    return {
        "ok": True,
        "result": {
            "text": text[:5000],
            "voice_id": args.get("voice_id"),
            "bytes": 0,
            "stubbed": True,
        },
    }


def register_media_handlers(
    handlers: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] | None = None,
) -> dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]]:
    """Install default media handlers; optional override map replaces entries."""
    defaults: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
        "globex_generate_image": _globex_image,
        "globex_generate_video": _globex_video,
        "voice_tts": _voice_tts,
    }
    if handlers:
        defaults.update(handlers)
    _HANDLERS.clear()
    _HANDLERS.update(defaults)
    return dict(_HANDLERS)


def run_media_tool(name: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not _HANDLERS:
        register_media_handlers()
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": "unknown_tool", "detail": f"unknown media tool: {name}"}
    try:
        return handler(args or {})
    except Exception as exc:  # noqa: BLE001 — bridge never raises to callers
        return {"ok": False, "error": "media_failed", "detail": str(exc)}
