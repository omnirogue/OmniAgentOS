"""Adapter for Claude Code session transcripts (`<sessionId>.jsonl`).

Covers both the downloaded HF corpora (claude-fable-5-claude-code,
Fable-5-traces) and OmniAgentOS's own `~/.claude*/projects/**` transcripts —
they are the same format (line-typed records forming a parentUuid tree, with
sidecar metadata records keyed only by sessionId).

No ground-truth outcome exists in this format. The one mechanical failure we
label is session-limit truncation: a leaf assistant record with
``model == "<synthetic>"`` announcing the session limit means the run was cut
mid-task, which we record as a failure with ``outcome_field`` explaining why.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from omniagentos.tracelab.adapters.base import (
    TraceAdapter,
    iter_jsonl,
    text_of,
)
from omniagentos.tracelab.events import EventKind, Outcome, Trace, TraceEvent

_CONVERSATION_TYPES = {"user", "assistant", "system", "attachment"}
_SESSION_LIMIT_MARKER = "hit your session limit"


def _is_session_record(record: dict[str, Any]) -> bool:
    """Whether a record is sufficient evidence of a Claude Code session."""
    return ("parentUuid" in record and "uuid" in record) or (
        record.get("type") in _CONVERSATION_TYPES and "sessionId" in record
    )


def _safe_int(value: object) -> int:
    """Usage fields are occasionally non-numeric in the wild — never raise."""
    try:
        return int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def sniff(path: Path) -> bool:
    """Accept if any of the first few records is a Claude Code conversation
    record — session files often START with sidecar records (mode, ai-title,
    queue-operation), so peeking only line 1 misses real sessions."""
    if path.suffix != ".jsonl":
        return False
    checked = 0
    for record in iter_jsonl(path):
        if _is_session_record(record):
            return True
        if record.get("type") == "session" and "version" in record:
            return False  # pi-coding-agent session, not Claude Code
        checked += 1
        if checked >= 10:
            return False
    return False


def _tool_result_size(block: dict[str, Any], record: dict[str, Any]) -> int:
    content = block.get("content")
    if isinstance(content, str):
        return len(content)
    try:
        size = len(json.dumps(content, default=str)) if content is not None else 0
    except (TypeError, ValueError):
        size = 0
    tool_use_result = record.get("toolUseResult")
    if isinstance(tool_use_result, str):
        size = max(size, len(tool_use_result))
    return size


def _tool_result_is_error(block: dict[str, Any], excerpt: str) -> bool | None:
    """Preserve a missing source error flag as unknown.

    Claude Code sometimes omits ``is_error`` from otherwise valid
    ``tool_result`` blocks.  The explicit error envelope is still conclusive,
    but absence of both signals is not evidence of success.
    """
    if "<tool_use_error>" in excerpt[:200]:
        return True
    flag = block.get("is_error")
    return flag if isinstance(flag, bool) else None


def _collect_tool_names(path: Path) -> dict[str, str]:
    """First pass: tool_use id → tool name over the whole file.

    Transcripts are parentUuid trees, not guaranteed-causal flat lists — a
    tool_result can appear in file order before its issuing tool_use, so a
    single-pass map would drop its tool name."""
    tool_names: dict[str, str] = {}
    for record in iter_jsonl(path):
        if record.get("type") != "assistant":
            continue
        content = (record.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                tool_names[str(block["id"])] = str(block.get("name") or "")
    return tool_names


def iter_traces(path: Path) -> Iterator[Trace]:
    trace = Trace(
        trace_id=path.stem,
        dataset="claude-code",
        source_path=str(path),
    )
    seq = 0
    truncated = False
    saw_session_record = False
    tool_names = _collect_tool_names(path)

    for record in iter_jsonl(path):
        if _is_session_record(record):
            saw_session_record = True
        record_type = record.get("type")
        if record_type == "assistant":
            message = record.get("message") or {}
            model = message.get("model") or ""
            content = message.get("content")
            usage = message.get("usage") or {}
            if model == "<synthetic>":
                if _SESSION_LIMIT_MARKER in text_of(content):
                    truncated = True
                continue
            text = ""
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tool_name = str(block.get("name") or "")
                    tool_id = str(block.get("id") or "")
                    if tool_id:
                        tool_names[tool_id] = tool_name
                    try:
                        excerpt = json.dumps(block.get("input"), default=str)
                    except (TypeError, ValueError):
                        excerpt = ""
                    trace.events.append(
                        TraceEvent(
                            seq=seq,
                            kind=EventKind.TOOL_CALL,
                            tool_name=tool_name,
                            excerpt=excerpt,
                            content_chars=len(excerpt),
                            model=model,
                            ts=str(record.get("timestamp") or ""),
                        )
                    )
                    seq += 1
                elif block.get("type") in ("text", "thinking"):
                    text += text_of(block.get("text") or block.get("thinking") or "")
            trace.events.append(
                TraceEvent(
                    seq=seq,
                    kind=EventKind.ASSISTANT,
                    content_chars=len(text),
                    excerpt=text,
                    model=model,
                    tokens_in=_safe_int(usage.get("input_tokens")),
                    tokens_out=_safe_int(usage.get("output_tokens")),
                    ts=str(record.get("timestamp") or ""),
                )
            )
            seq += 1
        elif record_type == "user":
            message = record.get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    excerpt = text_of(block.get("content"))
                    tool_id = str(block.get("tool_use_id") or "")
                    trace.events.append(
                        TraceEvent(
                            seq=seq,
                            kind=EventKind.TOOL_RESULT,
                            tool_name=tool_names.get(tool_id, ""),
                            content_chars=_tool_result_size(block, record),
                            excerpt=excerpt,
                            is_error=_tool_result_is_error(block, excerpt),
                            ts=str(record.get("timestamp") or ""),
                        )
                    )
                    seq += 1
            else:
                text = text_of(content)
                trace.events.append(
                    TraceEvent(
                        seq=seq,
                        kind=EventKind.USER,
                        content_chars=len(text),
                        excerpt=text,
                        ts=str(record.get("timestamp") or ""),
                    )
                )
                seq += 1
        elif record_type == "queue-operation":
            trace.events.append(
                TraceEvent(
                    seq=seq,
                    kind=EventKind.META,
                    excerpt=f"queue:{record.get('operation', '')}",
                )
            )
            seq += 1

    if not trace.events and not saw_session_record:
        return
    if truncated:
        trace.outcome = Outcome.FAILURE
        trace.outcome_field = "synthetic_session_limit"
        trace.meta["end_state"] = "truncated_session_limit"
    yield trace


ADAPTER = TraceAdapter(name="claude-code", sniff=sniff, iter_traces=iter_traces)
