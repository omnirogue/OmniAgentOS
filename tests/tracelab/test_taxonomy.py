"""Taxonomy classifies only error tool-results, never prose mentions."""

from __future__ import annotations

from omniagentos.tracelab.events import EventKind, Trace, TraceEvent
from omniagentos.tracelab.taxonomy import ALL_TAXONS, classify_error_text, classify_trace_errors


def test_extended_taxons_present_alongside_reflection_taxons() -> None:
    assert "rate_limit" in ALL_TAXONS  # reflection lane
    assert "file_not_found" in ALL_TAXONS  # tracelab extension


def test_classify_error_text_reflection_and_extended() -> None:
    tags = classify_error_text("HTTP 429 too many requests; retry later")
    assert "rate_limit" in tags
    tags = classify_error_text("bash: cd: examples: No such file or directory")
    assert "file_not_found" in tags
    tags = classify_error_text("ModuleNotFoundError: No module named 'pyarrow'")
    assert "missing_dependency" in tags


def test_prose_mentions_never_counted() -> None:
    trace = Trace(trace_id="t", dataset="d", source_path="p")
    trace.events = [
        # assistant discussing errors — must not classify
        TraceEvent(seq=0, kind=EventKind.ASSISTANT, excerpt="let's fix the timeout error"),
        # successful tool result mentioning stderr — must not classify
        TraceEvent(seq=1, kind=EventKind.TOOL_RESULT, is_error=False, excerpt="timeout ok"),
        # real error result — classifies
        TraceEvent(seq=2, kind=EventKind.TOOL_RESULT, is_error=True, excerpt="Timed out after 30s"),
    ]
    counts = classify_trace_errors(trace)
    assert counts == {"timeout": 1}


def test_unclassified_bucket() -> None:
    trace = Trace(trace_id="t", dataset="d", source_path="p")
    trace.events = [
        TraceEvent(seq=0, kind=EventKind.TOOL_RESULT, is_error=True, excerpt="weird failure xyz")
    ]
    assert classify_trace_errors(trace) == {"unclassified": 1}
