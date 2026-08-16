"""Adapter for conversation-list parquet corpora (AgentTrove terminus-2 rows
and hermes ShareGPT rows).

Two shapes behind one ``conversations`` column:

- **AgentTrove / terminus-2**: items ``{role: user|assistant, content}``.
  Turn 0 (user) embeds the task; later user turns are terminal output
  (observations). Assistant turns are JSON — optionally prefixed by a
  ``<think>…</think>`` block that must be stripped before parsing — whose
  ``commands`` list holds ``{keystrokes, duration}`` actions.
- **hermes**: items ``{from: system|human|gpt|tool, value}`` with a ``tools``
  schema column; ``gpt`` turns may embed ``<tool_call>`` JSON markup.

Neither corpus has a reliable ground-truth outcome (AgentTrove's ``result``
column is null except harness-failure classes, which we map to failure).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from omniagentos.tracelab.adapters.base import (
    TraceAdapter,
    iter_parquet_rows,
    parquet_columns,
    text_of,
)
from omniagentos.tracelab.events import EventKind, Outcome, Trace, TraceEvent

_COLUMNS = ["conversations", "agent", "model", "result", "original_source", "id", "tools"]
_THINK = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_TOOL_CALL_MARKUP = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def sniff(path: Path) -> bool:
    if path.suffix != ".parquet":
        return False
    return "conversations" in parquet_columns(path)


def _parse_terminus_assistant(text: str) -> tuple[str, list[str]]:
    """Return (visible text, command keystrokes) from a terminus-2 turn."""
    stripped = _THINK.sub("", text)
    brace = stripped.find("{")
    if brace < 0:
        return text, []
    try:
        # strict=False: terminus-2 turns often carry raw control characters
        # (literal newlines/tabs) inside JSON string values.
        payload: dict[str, Any] = json.loads(stripped[brace:], strict=False)
    except json.JSONDecodeError:
        return text, []
    commands = [
        str(c.get("keystrokes") or "") for c in payload.get("commands") or [] if isinstance(c, dict)
    ]
    return str(payload.get("analysis") or "") + str(payload.get("plan") or ""), commands


def _result_outcome(result_label: str) -> Outcome:
    """Map a polymorphic ``result`` column to an outcome.

    Numeric results are scores (glm52-style: 1.0 = verified success, 0.0 =
    fail); non-numeric non-empty strings are harness-failure classes
    (AgentTrove-style, e.g. ``AgentTimeoutError``); empty/null is unlabeled.
    """
    if not result_label:
        return Outcome.UNKNOWN
    try:
        return Outcome.SUCCESS if float(result_label) >= 0.5 else Outcome.FAILURE
    except ValueError:
        return Outcome.FAILURE


#: hermes wraps tool results in <tool_response> JSON where failure shows up as
#: a success flag or an error field, never an ERROR/Traceback prefix.
_HERMES_ERROR_MARKERS = ('"success": false', '"success":false', '"error": "', '"error":"')


def _tool_text_is_error(text: str) -> bool:
    head = text.lstrip()[:2000]
    if head.startswith(("ERROR", "Traceback")):
        return True
    return any(marker in head for marker in _HERMES_ERROR_MARKERS)


def _iter_turns(row: dict[str, Any]) -> Iterator[tuple[str, str]]:
    """Yield (normalized role, text) pairs across both conversation shapes."""
    for item in row.get("conversations") or []:
        if not isinstance(item, dict):
            continue
        role = item.get("role") or item.get("from") or ""
        text = text_of(item.get("content") if "content" in item else item.get("value"))
        yield str(role), text


def iter_traces(path: Path) -> Iterator[Trace]:
    for index, row in enumerate(iter_parquet_rows(path, _COLUMNS)):
        is_terminus = bool(row.get("agent")) or any(
            role in ("user", "assistant") for role, _ in _iter_turns(row)
        )
        # NOT `or ""`: numeric-zero / False results are real failure labels.
        result_raw = row.get("result")
        result_label = "" if result_raw is None else str(result_raw)
        trace = Trace(
            trace_id=str(row.get("id") or f"{path.stem}:{index}"),
            dataset="agenttrove" if row.get("original_source") else "conversations",
            source_path=str(path),
            outcome=_result_outcome(result_label),
            outcome_field="result" if result_label else "",
            meta={
                "model": str(row.get("model") or ""),
                "original_source": str(row.get("original_source") or ""),
                "result": result_label[:120],
            },
        )
        seq = 0
        first_user_seen = False
        for role, text in _iter_turns(row):
            if role in ("system",):
                kind = EventKind.SYSTEM
            elif role in ("human",):
                kind = EventKind.USER
            elif role in ("user",):
                # terminus-2: first user turn is the task, later ones are
                # terminal observations.
                kind = EventKind.USER if not first_user_seen else EventKind.TOOL_RESULT
                first_user_seen = True
            elif role in ("gpt", "assistant"):
                kind = EventKind.ASSISTANT
            elif role in ("tool", "observation"):
                kind = EventKind.TOOL_RESULT
            else:
                kind = EventKind.META

            if kind is EventKind.ASSISTANT and is_terminus and role == "assistant":
                visible, commands = _parse_terminus_assistant(text)
                trace.events.append(
                    TraceEvent(
                        seq=seq,
                        kind=EventKind.ASSISTANT,
                        content_chars=len(text),
                        excerpt=visible,
                        model=str(row.get("model") or ""),
                    )
                )
                seq += 1
                for command in commands:
                    trace.events.append(
                        TraceEvent(
                            seq=seq,
                            kind=EventKind.TOOL_CALL,
                            tool_name="terminal",
                            excerpt=command,
                            content_chars=len(command),
                        )
                    )
                    seq += 1
                continue

            if kind is EventKind.ASSISTANT:
                for match in _TOOL_CALL_MARKUP.finditer(text):
                    call_json = match.group(1)
                    tool_name = ""
                    try:
                        tool_name = str(json.loads(call_json).get("name") or "")
                    except json.JSONDecodeError:
                        pass
                    trace.events.append(
                        TraceEvent(
                            seq=seq,
                            kind=EventKind.TOOL_CALL,
                            tool_name=tool_name,
                            excerpt=call_json,
                            content_chars=len(call_json),
                        )
                    )
                    seq += 1

            trace.events.append(
                TraceEvent(
                    seq=seq,
                    kind=kind,
                    content_chars=len(text),
                    excerpt=text,
                    is_error=kind is EventKind.TOOL_RESULT and _tool_text_is_error(text),
                )
            )
            seq += 1
        if trace.events:
            yield trace


ADAPTER = TraceAdapter(name="conversations", sniff=sniff, iter_traces=iter_traces)
