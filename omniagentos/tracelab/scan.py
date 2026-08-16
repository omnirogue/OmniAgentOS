"""Corpus discovery and streaming scan.

Walks corpus roots, matches each data file to the first adapter whose sniff
accepts it, and streams normalized traces with a hard per-source cap so a
single 2 GB parquet cannot monopolize a run. Also knows how to scan
OmniAgentOS's own telemetry (run ledger + recent Claude Code transcripts).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from itertools import islice
from pathlib import Path

from omniagentos.tracelab.adapters import ADAPTERS, TraceAdapter
from omniagentos.tracelab.events import Trace

_LOG = logging.getLogger(__name__)

_DATA_SUFFIXES = {".jsonl", ".parquet"}
_SKIP_DIR_NAMES = {".cache", ".git", "node_modules", "__pycache__"}

#: Own Claude Code transcripts larger than this are skipped (streaming a
#: multi-hundred-MB transcript is fine for the parser but pointless for the
#: per-source cap; recency matters more than bulk).
_MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024


class TraceScanError(RuntimeError):
    """A source failure that makes a corpus scan incomplete."""


def _walk_data_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in _DATA_SUFFIXES:
            continue
        # Skip-list applies below the root only — a corpus root that itself
        # lives under a skip-listed name must still be scannable.
        if any(part in _SKIP_DIR_NAMES for part in path.relative_to(root).parts):
            continue
        yield path


def _capped(stream: Iterator[Trace], max_per_source: int | None) -> Iterator[Trace]:
    return islice(stream, max_per_source) if max_per_source is not None else stream


def discover_sources(root: Path) -> Iterator[tuple[TraceAdapter, Path]]:
    """Pair every recognizable data file under ``root`` with its adapter."""
    for path in _walk_data_files(root):
        for adapter in ADAPTERS:
            try:
                matched = adapter.sniff(path)
            except Exception as exc:
                raise TraceScanError(
                    f"adapter {adapter.name} sniff failed on {path}"
                ) from exc
            if matched:
                yield adapter, path
                break


def scan_corpus(
    roots: list[Path],
    max_per_source: int | None = None,
    adapter_names: set[str] | None = None,
) -> Iterator[Trace]:
    """Stream traces from every recognized source under the given roots."""
    # An explicitly empty allowlist is an empty set intersection, not a request
    # to disable filtering. Avoid even sniffing sources that cannot be selected.
    if adapter_names is not None and not adapter_names:
        return

    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            _LOG.warning("corpus root missing: %s", root)
            continue
        for adapter, path in discover_sources(root):
            if adapter_names and adapter.name not in adapter_names:
                continue
            resolved = path.resolve()
            if resolved in seen:  # overlapping/repeated roots must not double-count
                continue
            seen.add(resolved)
            _LOG.info("scanning %s via %s", path, adapter.name)
            try:
                yield from _capped(adapter.iter_traces(path), max_per_source)
            except Exception as exc:
                raise TraceScanError(
                    f"adapter {adapter.name} trace iteration failed on {path}"
                ) from exc


def _own_transcript_files(home: Path, limit: int) -> list[Path]:
    # Stat each candidate exactly once — session files rotate, and a file
    # deleted between separate is_file/stat calls would abort the scan.
    stated: list[tuple[float, Path]] = []
    for config_dir in sorted(home.glob(".claude*")):
        projects = config_dir / "projects"
        if not projects.is_dir():
            continue
        for path in projects.glob("*/*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size <= _MAX_TRANSCRIPT_BYTES:
                stated.append((stat.st_mtime, path))
    stated.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in stated[:limit]]


def scan_own(
    repo_root: Path | None = None,
    home: Path | None = None,
    transcript_limit: int = 25,
    max_per_source: int | None = None,
) -> Iterator[Trace]:
    """Stream traces from OmniAgentOS's own telemetry.

    Sources: the run ledger (``ledger/runs-*.jsonl``, resolved through
    ``contracts.default_ledger_dir()`` so it never depends on cwd) and the
    most recent Claude Code transcripts under ``~/.claude*/projects``.
    """
    from omniagentos.contracts import default_ledger_dir
    from omniagentos.tracelab.adapters.claude_code import iter_traces as iter_claude
    from omniagentos.tracelab.adapters.own_ledger import iter_traces as iter_runs

    ledger_dir = repo_root / "ledger" if repo_root else Path(default_ledger_dir())
    for runs_file in sorted(ledger_dir.glob("runs-*.jsonl")):
        try:
            yield from _capped(iter_runs(runs_file), max_per_source)
        except Exception as exc:
            # Same doctrine as scan_corpus: a source that failed is not a clean
            # empty source. Swallowing here made partial/broken own telemetry
            # look identical to "no runs / no transcripts".
            raise TraceScanError(f"own ledger source failed on {runs_file}") from exc

    home = home or Path.home()
    for transcript in _own_transcript_files(home, transcript_limit):
        try:
            for trace in iter_claude(transcript):
                trace.dataset = "own-claude-code"
                yield trace
        except Exception as exc:
            raise TraceScanError(f"own transcript failed on {transcript}") from exc
