"""Tests for shared TraceLab adapter I/O helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omniagentos.tracelab.adapters.base import iter_parquet_rows, parquet_columns


class _Batch:
    def to_pylist(self) -> list[dict[str, str]]:
        return [{"wanted": "value"}]


class _TrackingParquetFile:
    def __init__(self, source: Any, *, raise_while_reading: bool = False) -> None:
        self.source = source
        self.raise_while_reading = raise_while_reading
        self.schema_arrow = SimpleNamespace(names=["wanted"])
        self.closed = False

    def __enter__(self) -> _TrackingParquetFile:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def iter_batches(self, **kwargs: object) -> list[_Batch]:
        if self.raise_while_reading:
            raise RuntimeError("batch read failed")
        return [_Batch()]


def _install_tracking_parquet_file(
    monkeypatch: pytest.MonkeyPatch,
    *,
    raise_while_reading: bool = False,
    schema_arrow: Any | None = None,
) -> list[_TrackingParquetFile]:
    pq = pytest.importorskip("pyarrow.parquet")
    opened: list[_TrackingParquetFile] = []

    def open_parquet(source: Any) -> _TrackingParquetFile:
        parquet_file = _TrackingParquetFile(source, raise_while_reading=raise_while_reading)
        if schema_arrow is not None:
            parquet_file.schema_arrow = schema_arrow
        opened.append(parquet_file)
        return parquet_file

    monkeypatch.setattr(pq, "ParquetFile", open_parquet)
    return opened


def _assert_all_resources_closed(parquet_file: _TrackingParquetFile) -> None:
    assert parquet_file.closed
    assert parquet_file.source.closed


def test_iter_parquet_rows_closes_resources_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rows.parquet"
    path.touch()
    opened = _install_tracking_parquet_file(monkeypatch)

    assert list(iter_parquet_rows(path, ["wanted"])) == [{"wanted": "value"}]

    _assert_all_resources_closed(opened[0])


def test_iter_parquet_rows_closes_resources_after_read_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rows.parquet"
    path.touch()
    opened = _install_tracking_parquet_file(monkeypatch, raise_while_reading=True)

    with pytest.raises(RuntimeError, match="batch read failed"):
        list(iter_parquet_rows(path, ["wanted"]))

    _assert_all_resources_closed(opened[0])


def test_parquet_columns_closes_resources_after_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rows.parquet"
    path.touch()
    opened = _install_tracking_parquet_file(monkeypatch)

    assert parquet_columns(path) == {"wanted"}

    _assert_all_resources_closed(opened[0])


def test_parquet_columns_closes_resources_after_schema_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rows.parquet"
    path.touch()

    class _RaisingSchema:
        @property
        def names(self) -> list[str]:
            raise RuntimeError("schema read failed")

    opened = _install_tracking_parquet_file(monkeypatch, schema_arrow=_RaisingSchema())

    with pytest.raises(RuntimeError, match="schema read failed"):
        parquet_columns(path)

    _assert_all_resources_closed(opened[0])


def test_unreadable_parquet_does_not_look_like_empty_unrecognized_source(
    tmp_path: Path,
) -> None:
    """A corrupt .parquet under a corpus root must fail the scan observably.

    Counterfeit: parquet_columns swallows the error, every sniff returns False,
    and scan_corpus yields [] — identical to a genuinely empty corpus.
    """
    from omniagentos.tracelab.scan import TraceScanError, scan_corpus

    path = tmp_path / "trajectories.parquet"
    path.write_bytes(b"this is not parquet")

    with pytest.raises(TraceScanError, match=r"sniff failed on .*trajectories\.parquet"):
        list(scan_corpus([tmp_path]))
