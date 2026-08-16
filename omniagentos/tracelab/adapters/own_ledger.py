"""Adapter for OmniAgentOS's own run ledger (``ledger/runs-YYYYMM.jsonl``).

Each run row becomes one trace whose events are the escalation ladder from
``extra.attempts`` — attempt N is a tool_call (provider/model/tier/effort)
followed by a tool_result whose error flag reflects ``end_reason``
(review_denied / timeout / crashed are failures of the attempt). Ground truth
is the run ``state`` (completed / failed / cancelled).

Mock-harness rows are test noise and are skipped.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from omniagentos.tracelab.adapters.base import TraceAdapter, iter_jsonl, peek_jsonl_record
from omniagentos.tracelab.events import EventKind, Outcome, Trace, TraceEvent

_ATTEMPT_SUCCESS_REASONS = {"completed"}


def sniff(path: Path) -> bool:
    if path.suffix != ".jsonl" or not path.name.startswith("runs-"):
        return False
    record = peek_jsonl_record(path)
    return bool(record and "run_id" in record and "harness" in record)


def _attempt_events(attempts: list[dict[str, Any]], start_seq: int) -> list[TraceEvent]:
    events: list[TraceEvent] = []
    seq = start_seq
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        descriptor = (
            f"tier={attempt.get('tier', '')} effort={attempt.get('effort', '')} "
            f"seq={attempt.get('seq', '')}"
        )
        provider_model = f"{attempt.get('provider', '')}/{attempt.get('model', '')}"
        events.append(
            TraceEvent(
                seq=seq,
                kind=EventKind.TOOL_CALL,
                tool_name=f"attempt:{provider_model}",
                excerpt=descriptor,
                content_chars=len(descriptor),
                model=str(attempt.get("model") or ""),
            )
        )
        seq += 1
        end_reason = str(attempt.get("end_reason") or "unknown")
        events.append(
            TraceEvent(
                seq=seq,
                kind=EventKind.TOOL_RESULT,
                tool_name=f"attempt:{provider_model}",
                excerpt=end_reason,
                content_chars=len(end_reason),
                # A missing or unfamiliar terminal reason is not evidence of
                # success. The normalized model has no unknown error state, so
                # use the unfavorable fallback unless completion is explicit.
                is_error=end_reason not in _ATTEMPT_SUCCESS_REASONS,
            )
        )
        seq += 1
    return events


def iter_traces(path: Path) -> Iterator[Trace]:
    for record in iter_jsonl(path):
        harness = (record.get("harness") or {}).get("harness", "")
        if harness == "mock":
            continue
        state = str(record.get("state") or "")
        if state == "completed":
            outcome = Outcome.SUCCESS
        elif state in ("failed", "cancelled"):
            outcome = Outcome.FAILURE
        else:
            outcome = Outcome.UNKNOWN
        extra = record.get("extra") or {}
        usage = record.get("usage") or {}
        trace = Trace(
            trace_id=str(record.get("run_id") or ""),
            dataset="own-runs",
            source_path=str(path),
            outcome=outcome,
            outcome_field="state",
            meta={
                "harness": str(harness),
                "task_id": str(record.get("task_id") or ""),
                "model": str(record.get("model") or ""),
                "discipline": str(record.get("discipline") or ""),
                "score": str(extra.get("score") or ""),
                "wall_ms": str(usage.get("wall_ms") or ""),
            },
        )
        goal = str(extra.get("goal") or "")
        if goal:
            trace.events.append(
                TraceEvent(seq=0, kind=EventKind.USER, content_chars=len(goal), excerpt=goal)
            )
        attempts = extra.get("attempts")
        if isinstance(attempts, list):
            trace.events.extend(_attempt_events(attempts, len(trace.events)))
        if not trace.events:
            # Runs without an attempt ladder (e.g. cli-claude) still carry an
            # outcome label — keep them countable with a single meta event.
            trace.events.append(
                TraceEvent(
                    seq=0,
                    kind=EventKind.META,
                    excerpt=f"run harness={harness} state={state}",
                    model=str(record.get("model") or ""),
                )
            )
        yield trace


ADAPTER = TraceAdapter(name="own-runs", sniff=sniff, iter_traces=iter_traces)
