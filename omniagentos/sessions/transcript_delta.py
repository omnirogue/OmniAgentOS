"""Shared byte-offset transcript delta read for session JSONL activity logs.

Extracted from ``GET /api/sessions/{id}/transcript/delta`` so there is ONE
implementation of the byte-offset + complete-lines-only + rotation-guard
protocol: the HTTP route and the chat turn bridge
(:class:`omniagentos.chats.bridge.ChatTurnBridge`) both read live session
transcripts through :func:`read_transcript_delta`.

NOTE: the spec named this module ``chain_read.py``, but that name was already
taken by the T6.4 chain-read protocol (PostToolUse context accumulation), so
the extraction lives here instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_transcript_delta(path: Path, offset: int) -> dict[str, Any]:
    """Return new complete lines/events from a session transcript since ``offset``.

    Byte-offset based delta read with complete-lines-only logic and a rotation
    guard that resets the offset to 0 if it exceeds the current file size.
    Never holds a file handle across calls.

    Returns ``{"raw", "lines", "entries", "new_offset"}`` — ``entries`` are the
    parsed JSON objects from the new complete lines (unparseable lines are
    skipped but still advance the offset).
    """
    empty = {"raw": "", "lines": [], "entries": [], "new_offset": 0}
    if not path.exists():
        return empty

    try:
        file_size = path.stat().st_size
    except OSError:
        return empty

    # Rotation guard: reset-to-0 if offset is greater than current file size
    if offset > file_size:
        offset = 0

    try:
        with open(path, "rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return {
            "raw": "",
            "lines": [],
            "entries": [],
            "new_offset": offset,
        }

    if not chunk:
        return {
            "raw": "",
            "lines": [],
            "entries": [],
            "new_offset": offset,
        }

    last_nl_idx = chunk.rfind(b"\n")
    if last_nl_idx == -1:
        # Complete-lines-only: no newline found, so no complete lines yet.
        return {
            "raw": "",
            "lines": [],
            "entries": [],
            "new_offset": offset,
        }

    complete_chunk = chunk[: last_nl_idx + 1]
    new_offset = offset + last_nl_idx + 1

    decoded = complete_chunk.decode("utf-8", errors="replace")
    # Split lines and remove the last empty element resulting from trailing \n
    lines = decoded.split("\n")
    if lines and lines[-1] == "":
        lines.pop()

    entries = []
    for line in lines:
        try:
            if line.strip():
                entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    return {
        "raw": decoded,
        "lines": lines,
        "entries": entries,
        "new_offset": new_offset,
    }


def assistant_text(entry: dict[str, Any]) -> str | None:
    """Extract assistant message text from one stream-json transcript entry.

    Claude bridge sessions write one ``{"type": "assistant"}`` event per
    assistant message; text lives in ``message.content[]`` text blocks.
    Returns ``None`` for non-assistant entries and empty messages.
    """
    if not isinstance(entry, dict):
        return None
    if entry.get("type") != "assistant":
        return None
    message = entry.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    texts: list[str] = []
    if isinstance(content, str):
        texts.append(content)
    elif isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ):
                texts.append(block["text"])
    joined = "".join(texts).strip()
    return joined or None


def result_text(entry: dict[str, Any]) -> str | None:
    """Extract the final result text from a ``{"type": "result"}`` entry."""
    if not isinstance(entry, dict):
        return None
    if entry.get("type") != "result":
        return None
    result = entry.get("result")
    if isinstance(result, str) and result.strip():
        return result.strip()
    return None


__all__ = ["assistant_text", "read_transcript_delta", "result_text"]
