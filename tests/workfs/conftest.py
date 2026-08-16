"""Fixtures for workfs tests — always a tmp WORK root, never the real tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.workfs.root import WORK_ROOT_ENV


@pytest.fixture
def work_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated WORK root; parent is the far-side witness surface."""
    root = tmp_path / "Work"
    root.mkdir()
    monkeypatch.setenv(WORK_ROOT_ENV, str(root))
    return root


@pytest.fixture
def work_parent(work_tmp: Path) -> Path:
    """Parent of the WORK root — used for far-side before/after walks."""
    return work_tmp.parent
