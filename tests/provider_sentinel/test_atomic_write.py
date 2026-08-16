"""sentinel.atomic_write_json -- tmp-file-then-os.replace, no partial writes
ever observable and no leftover tmp files."""

from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType

import pytest


def test_writes_expected_json_and_leaves_no_tmp_files(sentinel: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "health.json"
    sentinel.atomic_write_json(target, {"ts": "2026-07-24T22:30:00Z", "results": {"a": 1}})

    assert json.loads(target.read_text()) == {"ts": "2026-07-24T22:30:00Z", "results": {"a": 1}}
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == []


def test_overwrite_replaces_content_atomically(sentinel: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "health.json"
    sentinel.atomic_write_json(target, {"n": 1})
    sentinel.atomic_write_json(target, {"n": 2})
    assert json.loads(target.read_text()) == {"n": 2}
    assert list(tmp_path.iterdir()) == [target]


def test_creates_parent_directories(sentinel: ModuleType, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deeper" / "health.json"
    sentinel.atomic_write_json(target, {"ok": True})
    assert json.loads(target.read_text()) == {"ok": True}


def test_failed_replace_leaves_original_file_untouched_and_cleans_tmp(
    sentinel: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "health.json"
    sentinel.atomic_write_json(target, {"n": "original"})

    def _boom(_src: str, _dst: str) -> None:
        raise OSError("simulated crash mid-replace")

    monkeypatch.setattr(sentinel.os, "replace", _boom)

    with pytest.raises(OSError):
        sentinel.atomic_write_json(target, {"n": "new-and-should-never-land"})

    # The reader never sees a partial/corrupt file: the original content
    # survives a failed replace.
    assert json.loads(target.read_text()) == {"n": "original"}
    # And the tmp file used for the failed attempt is cleaned up, not left
    # behind to accumulate.
    leftovers = [p for p in tmp_path.iterdir() if p != target]
    assert leftovers == []
