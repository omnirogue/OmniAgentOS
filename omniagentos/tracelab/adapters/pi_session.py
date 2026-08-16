"""Adapter for pi-coding-agent session JSONL (0xKobolds corpus, version 3).

Records form a linked list via ``parentId``; the payload lives in ``message``
records wrapping ``{role: user|assistant|toolResult}``. Assistant messages
carry ``stopReason`` (``toolUse|stop|error|aborted``) and inline ``toolCall``
content items; ``compaction`` records are context-compaction checkpoints —
a distinctive signal this format has that Claude Code transcripts lack.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from omniagentos.tracelab.adapters.base import (
    TraceAdapter,
    iter_jsonl,
    peek_jsonl_record,
    text_of,
)
from omniagentos.tracelab.events import EventKind, Trace, TraceEvent


def sniff(path: Path) -> bool:
    if path.suffix != ".jsonl":
        return False
    record = peek_jsonl_record(path)
    return bool(record and record.get("type") == "session" and "version" in record)


def iter_traces(path: Path) -> Iterator[Trace]:
    trace = Trace(trace_id=path.stem, dataset="pi-session", source_path=str(path))
    seq = 0
    model = ""

    for record in iter_jsonl(path):
        record_type = record.get("type")
        if record_type == "model_change":
            model = str(record.get("modelId") or "")
            continue
        if record_type == "compaction":
            summary = str(record.get("summary") or "")
            trace.events.append(
                TraceEvent(
                    seq=seq,
                    kind=EventKind.META,
                    excerpt=f"compaction:{summary[:200]}",
                    content_chars=len(summary),
                )
            )
            seq += 1
            continue
        if record_type != "message":
            continue

        message = record.get("message") or {}
        role = message.get("role")
        if role == "user":
            text = text_of(message.get("content"))
            trace.events.append(
                TraceEvent(seq=seq, kind=EventKind.USER, content_chars=len(text), excerpt=text)
            )
            seq += 1
        elif role == "assistant":
            stop_reason = str(message.get("stopReason") or "")
            usage = message.get("usage") or {}
            text = ""
            for item in message.get("content") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "toolCall":
                    try:
                        arguments = json.dumps(item.get("arguments"), default=str)
                    except (TypeError, ValueError):
                        arguments = ""
                    trace.events.append(
                        TraceEvent(
                            seq=seq,
                            kind=EventKind.TOOL_CALL,
                            tool_name=str(item.get("name") or ""),
                            excerpt=arguments,
                            content_chars=len(arguments),
                            model=model,
                        )
                    )
                    seq += 1
                elif item.get("type") in ("text", "thinking"):
                    text += str(item.get("text") or item.get("thinking") or "")
            trace.events.append(
                TraceEvent(
                    seq=seq,
                    kind=EventKind.ASSISTANT,
                    content_chars=len(text),
                    excerpt=text,
                    model=str(message.get("model") or model),
                    tokens_in=int(usage.get("input") or 0),
                    tokens_out=int(usage.get("output") or 0),
                    is_error=stop_reason == "error",
                )
            )
            seq += 1
            if stop_reason == "error":
                error_message = str(message.get("errorMessage") or "provider error")
                trace.events.append(
                    TraceEvent(
                        seq=seq,
                        kind=EventKind.TOOL_RESULT,
                        tool_name="provider",
                        excerpt=error_message,
                        content_chars=len(error_message),
                        is_error=True,
                    )
                )
                seq += 1
        elif role == "toolResult":
            text = text_of(message.get("content"))
            # The format stamps isError=true on user-interrupt cancellations
            # ("Skipped due to queued user message.") — that is a cancellation,
            # not a tool failure or evidence of successful recovery.
            cancelled = "Skipped due to queued user message" in text[:200]
            error_flag = message.get("isError")
            is_error = None if cancelled or not isinstance(error_flag, bool) else error_flag
            trace.events.append(
                TraceEvent(
                    seq=seq,
                    kind=EventKind.TOOL_RESULT,
                    tool_name=str(message.get("toolName") or ""),
                    content_chars=len(text),
                    excerpt=text,
                    is_error=is_error,
                )
            )
            seq += 1

    if trace.events:
        yield trace


ADAPTER = TraceAdapter(name="pi-session", sniff=sniff, iter_traces=iter_traces)
