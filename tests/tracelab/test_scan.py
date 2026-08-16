"""Completeness and filtering guarantees for TraceLab corpus scans."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from omniagentos.tracelab import scan
from omniagentos.tracelab.adapters import TraceAdapter
from omniagentos.tracelab.events import Trace
from omniagentos.tracelab.scan import TraceScanError, discover_sources, scan_corpus


def _trace(path: Path, trace_id: str = "trace-1") -> Trace:
    return Trace(trace_id=trace_id, dataset="synthetic", source_path=str(path))


def _data_file(tmp_path: Path) -> Path:
    path = tmp_path / "source.jsonl"
    path.write_text("{}\n")
    return path


def test_sniff_failure_is_observable_with_original_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _data_file(tmp_path)
    problem = RuntimeError("synthetic sniff outage")

    def broken_sniff(_path: Path) -> bool:
        raise problem

    monkeypatch.setattr(
        scan,
        "ADAPTERS",
        (TraceAdapter("broken-sniff", broken_sniff, lambda _path: iter(())),),
    )

    with pytest.raises(
        TraceScanError, match=rf"adapter broken-sniff sniff failed on {path}"
    ) as caught:
        list(discover_sources(tmp_path))

    assert caught.value.__cause__ is problem


def test_empty_adapter_allowlist_matches_nothing_without_touching_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _data_file(tmp_path)
    calls: list[Path] = []

    def sniff(candidate: Path) -> bool:
        calls.append(candidate)
        return True

    adapter = TraceAdapter("selected", sniff, lambda candidate: iter((_trace(candidate),)))
    monkeypatch.setattr(scan, "ADAPTERS", (adapter,))

    assert list(scan_corpus([tmp_path], adapter_names=set())) == []
    assert calls == []
    # Healthy control: a non-empty allowlist still runs the production path.
    assert list(scan_corpus([tmp_path], adapter_names={"selected"})) == [_trace(path)]
    assert calls == [path]


def test_trace_iteration_failure_marks_partially_yielded_scan_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _data_file(tmp_path)
    first = _trace(path)
    problem = RuntimeError("synthetic parser outage")

    def broken_stream(_path: Path) -> Iterator[Trace]:
        yield first
        raise problem

    adapter = TraceAdapter("broken-parser", lambda _path: True, broken_stream)
    monkeypatch.setattr(scan, "ADAPTERS", (adapter,))

    stream = scan_corpus([tmp_path])
    assert next(stream) is first
    with pytest.raises(
        TraceScanError,
        match=rf"adapter broken-parser trace iteration failed on {path}",
    ) as caught:
        next(stream)

    assert caught.value.__cause__ is problem


def test_complete_trace_iteration_still_finishes_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _data_file(tmp_path)
    expected = _trace(path, trace_id="healthy")
    adapter = TraceAdapter("healthy", lambda _path: True, lambda _path: iter((expected,)))
    monkeypatch.setattr(scan, "ADAPTERS", (adapter,))

    assert list(scan_corpus([tmp_path])) == [expected]


def test_scan_own_ledger_failure_is_not_a_clean_empty_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable own-ledger file must not look like a successfully empty source.

    Counterfeit this test would accept: swallow the OSError, yield nothing from
    that file, and finish the scan as if the ledger simply had no runs.
    """
    from omniagentos.tracelab.scan import scan_own

    ledger = tmp_path / "ledger"
    ledger.mkdir()
    runs = ledger / "runs-202607.jsonl"
    runs.write_text(
        '{"run_id":"r1","state":"completed","harness":{"harness":"swarm"},"extra":{}}\n'
    )

    problem = PermissionError("ledger unreadable")

    def boom(_path: Path):
        raise problem
        yield  # pragma: no cover — make this a generator type

    monkeypatch.setattr("omniagentos.tracelab.adapters.own_ledger.iter_traces", boom)

    with pytest.raises(
        TraceScanError, match=rf"own ledger source failed on {runs}"
    ) as caught:
        list(scan_own(repo_root=tmp_path, home=tmp_path / "no-home"))

    assert caught.value.__cause__ is problem


def test_scan_own_transcript_failure_is_not_a_clean_empty_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken own Claude transcript must fail the scan, not vanish."""
    from omniagentos.tracelab.scan import scan_own

    home = tmp_path / "home"
    projects = home / ".claude" / "projects" / "p"
    projects.mkdir(parents=True)
    transcript = projects / "sess.jsonl"
    transcript.write_text("{}\n")

    problem = RuntimeError("transcript parser outage")

    def boom(_path: Path):
        raise problem
        yield  # pragma: no cover

    monkeypatch.setattr("omniagentos.tracelab.adapters.claude_code.iter_traces", boom)
    monkeypatch.setattr(
        scan,
        "_own_transcript_files",
        lambda _home, _limit: [transcript],
    )

    with pytest.raises(
        TraceScanError, match=rf"own transcript failed on {transcript}"
    ) as caught:
        list(scan_own(repo_root=tmp_path / "empty-repo", home=home))

    assert caught.value.__cause__ is problem
