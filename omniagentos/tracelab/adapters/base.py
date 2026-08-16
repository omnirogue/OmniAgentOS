"""Adapter contract + shared streaming helpers."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.tracelab.events import Trace


@dataclass(frozen=True, slots=True)
class TraceAdapter:
    """One trace-format adapter.

    ``sniff`` must be cheap (extension + a peek at the first bytes/schema) and
    conservative — a false positive sends a whole file to the wrong parser.
    ``iter_traces`` must stream: adapters are pointed at multi-GB files.
    """

    name: str
    sniff: Callable[[Path], bool]
    iter_traces: Callable[[Path], Iterator[Trace]]


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Stream JSON objects from a JSONL file, skipping unparseable lines."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj


def peek_jsonl_record(path: Path) -> dict[str, Any] | None:
    """Parse just the first JSON line of a file (for sniffing)."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                return obj if isinstance(obj, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
    return None


def parquet_columns(path: Path) -> set[str]:
    """Top-level column names of a parquet file.

    Failures (missing pyarrow, unreadable/corrupt bytes, schema read errors)
    propagate. Sniff must not treat "could not measure" as "not our format" —
    that made broken parquet sources look identical to an empty corpus.
    """
    import pyarrow.parquet as pq  # type: ignore[import-untyped, import-not-found]

    with path.open("rb") as handle, pq.ParquetFile(handle) as parquet_file:
        return set(parquet_file.schema_arrow.names)


def iter_parquet_rows(
    path: Path, columns: list[str], batch_size: int = 16
) -> Iterator[dict[str, Any]]:
    """Stream rows from a parquet file as dicts, never loading the whole table."""
    import pyarrow.parquet as pq  # type: ignore[import-untyped, import-not-found]

    with path.open("rb") as handle, pq.ParquetFile(handle) as parquet_file:
        available = set(parquet_file.schema_arrow.names)
        use_columns = [c for c in columns if c in available]
        for batch in parquet_file.iter_batches(batch_size=batch_size, columns=use_columns):
            yield from batch.to_pylist()


def text_of(content: Any) -> str:
    """Best-effort plain-text rendering of a polymorphic content payload."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(content, dict):
        text = content.get("text") or content.get("content")
        if isinstance(text, str):
            return text
    return "" if content is None else str(content)
