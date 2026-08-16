"""Metrics are mechanical counts — every signal has a hand-built fixture."""

from __future__ import annotations

import json

from omniagentos.tracelab.events import EventKind, Outcome, Trace, TraceEvent
from omniagentos.tracelab.metrics import GIANT_RESULT_CHARS, compute_metrics


def _call(seq: int, tool: str = "bash", excerpt: str = "ls") -> TraceEvent:
    return TraceEvent(seq=seq, kind=EventKind.TOOL_CALL, tool_name=tool, excerpt=excerpt)


def _result(seq: int, *, error: bool = False, chars: int = 10) -> TraceEvent:
    return TraceEvent(seq=seq, kind=EventKind.TOOL_RESULT, is_error=error, content_chars=chars)


def test_error_streak_and_recovery() -> None:
    trace = Trace(trace_id="t", dataset="d", source_path="p")
    trace.events = [
        _call(0),
        _result(1, error=True),
        _call(2),
        _result(3, error=True),
        _call(4),
        _result(5),  # recovery
        _call(6),
        _result(7, error=True),
    ]
    metrics = compute_metrics(trace)
    assert metrics.n_tool_calls == 4
    assert metrics.n_tool_errors == 3
    assert metrics.error_streak_max == 2
    assert metrics.errors_recovered == 1
    assert metrics.tool_error_rate == 0.75
    # two error episodes (the 2-error streak, then the final unrecovered error)
    assert metrics.error_episodes == 2
    assert metrics.recovery_rate == 0.5


def test_empty_denominator_rates_are_unknown_through_row_serialization() -> None:
    metrics = compute_metrics(Trace(trace_id="empty", dataset="d", source_path="p"))

    assert metrics.tool_error_rate is None
    assert metrics.recovery_rate is None

    row = metrics.to_row()
    assert row["tool_error_rate"] is None
    assert row["recovery_rate"] is None
    assert json.loads(json.dumps(row))["tool_error_rate"] is None
    assert json.loads(json.dumps(row))["recovery_rate"] is None
def test_unknown_tool_result_does_not_fake_successful_recovery() -> None:
    unknown = TraceEvent(seq=3, kind=EventKind.TOOL_RESULT, tool_name="bash")
    assert unknown.is_error is None
    trace = Trace(
        trace_id="t",
        dataset="d",
        source_path="p",
        events=[
            _call(0),
            TraceEvent(
                seq=1,
                kind=EventKind.TOOL_RESULT,
                tool_name="bash",
                is_error=True,
            ),
            _call(2),
            unknown,
        ],
    )

    metrics = compute_metrics(trace)

    assert metrics.n_tool_errors == 1
    assert metrics.error_episodes == 1
    assert metrics.errors_recovered == 0


def test_repeat_call_detection_requires_identical_signature() -> None:
    trace = Trace(trace_id="t", dataset="d", source_path="p")
    trace.events = [
        _call(0, excerpt="pytest -q"),
        _call(1, excerpt="pytest -q"),
        _call(2, excerpt="pytest -q"),
        _call(3, excerpt="ls"),
        _call(4, excerpt="pytest -q"),
    ]
    metrics = compute_metrics(trace)
    assert metrics.repeat_call_max == 3


def test_giant_results_and_verification() -> None:
    trace = Trace(trace_id="t", dataset="d", source_path="p", outcome=Outcome.SUCCESS)
    trace.events = [
        _call(0, excerpt="pytest tests/"),
        _result(1, chars=GIANT_RESULT_CHARS + 5),
        _call(2, tool="Read", excerpt="file.py"),
        _result(3, chars=100),
    ]
    metrics = compute_metrics(trace)
    assert metrics.giant_results == 1
    assert metrics.largest_result_chars == GIANT_RESULT_CHARS + 5
    assert metrics.verification_calls == 1
    assert metrics.outcome == "success"


def test_excerpt_capped() -> None:
    event = TraceEvent(seq=0, kind=EventKind.ASSISTANT, excerpt="x" * 10_000)
    assert len(event.excerpt) == 400


def test_parallel_calls_do_not_dilute_streak_or_fake_recovery() -> None:
    """Interleaved results (CALL A, CALL B, RESULT A err, RESULT B ok) x3:
    tool A's streak must reach 3 and tool B's successes are not A's recovery."""
    trace = Trace(trace_id="t", dataset="d", source_path="p")
    seq = 0
    for _ in range(3):
        trace.events.append(_call(seq, tool="screenshot", excerpt="shot"))
        seq += 1
        trace.events.append(_call(seq, tool="bash", excerpt="echo ok"))
        seq += 1
        trace.events.append(
            TraceEvent(seq=seq, kind=EventKind.TOOL_RESULT, tool_name="screenshot", is_error=True)
        )
        seq += 1
        trace.events.append(
            TraceEvent(seq=seq, kind=EventKind.TOOL_RESULT, tool_name="bash", is_error=False)
        )
        seq += 1
    metrics = compute_metrics(trace)
    assert metrics.error_streak_max == 3
    assert metrics.errors_recovered == 0
    assert metrics.error_episodes == 1


def test_repeat_signature_uses_full_excerpt() -> None:
    shared_prefix = "x" * 250
    trace = Trace(trace_id="t", dataset="d", source_path="p")
    trace.events = [
        _call(0, excerpt=shared_prefix + "AAA"),
        _call(1, excerpt=shared_prefix + "BBB"),
        _call(2, excerpt=shared_prefix + "CCC"),
    ]
    assert compute_metrics(trace).repeat_call_max == 1
