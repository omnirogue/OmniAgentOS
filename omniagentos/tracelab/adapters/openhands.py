"""Adapter for nebius/SWE-rebench-openhands-trajectories (trajectories.parquet).

Rows are OpenAI chat-completions transcripts: system/user/assistant/tool
messages; assistant messages may carry ``tool_calls`` (arguments are
JSON-encoded strings, occasionally malformed); tool messages link back via
``tool_call_id``. Ground truth is the ``resolved`` int column.

The 2 GB single file is streamed in small batches with the per-row ``tools``
schema copy and ``model_patch`` columns excluded.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from omniagentos.tracelab.adapters.base import (
    TraceAdapter,
    iter_parquet_rows,
    parquet_columns,
    text_of,
)
from omniagentos.tracelab.events import EventKind, Outcome, Trace, TraceEvent

_COLUMNS = ["trajectory_id", "instance_id", "trajectory", "exit_status", "resolved"]

_ERROR_HEADS = ("ERROR:", "ERROR\n", "Traceback (most recent call last)")


def sniff(path: Path) -> bool:
    if path.suffix != ".parquet":
        return False
    columns = parquet_columns(path)
    return {"trajectory_id", "resolved", "trajectory"} <= columns


def _result_is_error(text: str) -> bool:
    head = text.lstrip()[:400]
    return head.startswith(_ERROR_HEADS) or "Invalid `new_str` parameter" in head


def _resolved_outcome(value: object) -> tuple[Outcome, str]:
    """Map the ``resolved`` column to (outcome, outcome_field).

    Null/missing is UNKNOWN — never forced into SUCCESS or FAILURE. Explicit
    numeric zero is failure; any other explicit numeric/bool value is success.
    """
    if value is None:
        return Outcome.UNKNOWN, ""
    if isinstance(value, bool):
        return (Outcome.SUCCESS if value else Outcome.FAILURE), "resolved"
    if isinstance(value, (int, float)):
        return (Outcome.SUCCESS if value != 0 else Outcome.FAILURE), "resolved"
    return Outcome.UNKNOWN, ""


def iter_traces(path: Path) -> Iterator[Trace]:
    for index, row in enumerate(iter_parquet_rows(path, _COLUMNS, batch_size=8)):
        outcome, outcome_field = _resolved_outcome(row.get("resolved"))
        trace = Trace(
            trace_id=str(row.get("trajectory_id") or f"{path.stem}:{index}"),
            dataset="openhands",
            source_path=str(path),
            outcome=outcome,
            outcome_field=outcome_field,
            meta={
                "instance_id": str(row.get("instance_id") or ""),
                "exit_status": str(row.get("exit_status") or "")[:200],
            },
        )
        seq = 0
        for message in row.get("trajectory") or []:
            role = message.get("role")
            content = text_of(message.get("content"))
            if role == "system":
                trace.events.append(
                    TraceEvent(
                        seq=seq, kind=EventKind.SYSTEM, content_chars=len(content), excerpt=content
                    )
                )
                seq += 1
            elif role == "user":
                trace.events.append(
                    TraceEvent(
                        seq=seq, kind=EventKind.USER, content_chars=len(content), excerpt=content
                    )
                )
                seq += 1
            elif role == "assistant":
                if content:
                    trace.events.append(
                        TraceEvent(
                            seq=seq,
                            kind=EventKind.ASSISTANT,
                            content_chars=len(content),
                            excerpt=content,
                        )
                    )
                    seq += 1
                for call in message.get("tool_calls") or []:
                    function = (call or {}).get("function") or {}
                    arguments = function.get("arguments")
                    if not isinstance(arguments, str):
                        try:
                            arguments = json.dumps(arguments, default=str)
                        except (TypeError, ValueError):
                            arguments = ""
                    trace.events.append(
                        TraceEvent(
                            seq=seq,
                            kind=EventKind.TOOL_CALL,
                            tool_name=str(function.get("name") or ""),
                            excerpt=arguments,
                            content_chars=len(arguments),
                        )
                    )
                    seq += 1
            elif role == "tool":
                trace.events.append(
                    TraceEvent(
                        seq=seq,
                        kind=EventKind.TOOL_RESULT,
                        tool_name=str(message.get("name") or ""),
                        content_chars=len(content),
                        excerpt=content,
                        is_error=_result_is_error(content),
                    )
                )
                seq += 1
        if trace.events:
            yield trace


ADAPTER = TraceAdapter(name="openhands", sniff=sniff, iter_traces=iter_traces)
