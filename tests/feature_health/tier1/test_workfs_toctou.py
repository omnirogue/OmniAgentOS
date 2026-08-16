"""WorkFS darwin TOCTOU — a root (or scope dir) swapped for a symlink is refused.

Root authority is lstat + O_NOFOLLOW|O_DIRECTORY (workfs/ensure.py): the write
path must fail closed with a WorkfsError refusal and perform ZERO writes
through the symlink into the outside directory.

Two swaps are covered:
* the WORK root itself replaced by a symlink -> refused before any open
  (``root_not_directory`` — lstat sees the link, never follows it);
* an already-ensured scope directory replaced by a symlink to an outside dir
  -> ``WorkfsPathError`` with code ``path_outside_root`` from inode
  containment (require_contained), the exact no-write-through guarantee.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from omniagentos.workfs.ensure import ensure
from omniagentos.workfs.errors import WorkfsError, WorkfsPathError

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="WorkFS atomic ensure is Darwin-only"
)


def _snapshot(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_scope_dir_swapped_for_symlink_raises_path_outside_root(
    tmp_path: Path,
) -> None:
    work_root = tmp_path / "Work"
    work_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    first = ensure("AcmeCo", root=work_root)
    assert first.created is True
    company_dir = work_root / "AcmeCo"
    assert company_dir.is_dir() and not company_dir.is_symlink()

    # TOCTOU swap: the ensured scope directory becomes a symlink escaping root.
    before_outside = _snapshot(outside)
    os.rename(company_dir, tmp_path / "AcmeCo-moved")
    os.symlink(outside, company_dir)

    with pytest.raises(WorkfsPathError) as excinfo:
        ensure("AcmeCo", "Planted", root=work_root)

    assert excinfo.value.code == "path_outside_root"
    # No write-through: the outside directory is byte-identical.
    assert _snapshot(outside) == before_outside
    assert not (outside / "Planted").exists()


def test_work_root_swapped_for_symlink_is_refused_with_no_write_through(
    tmp_path: Path,
) -> None:
    work_root = tmp_path / "Work"
    work_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    assert ensure("AcmeCo", root=work_root).created is True

    # Replace the WORK root itself with a symlink to an outside directory.
    before_outside = _snapshot(outside)
    os.rename(work_root, tmp_path / "Work-moved")
    os.symlink(outside, work_root)

    with pytest.raises(WorkfsError) as excinfo:
        ensure("AcmeCo", "Dept", root=work_root)

    # lstat authority refuses the link before any open/write; either spelling
    # is a fail-closed refusal, never a write into the link target.
    assert excinfo.value.code in {"root_not_directory", "path_outside_root"}
    assert _snapshot(outside) == before_outside
    assert not (outside / "AcmeCo").exists()
    # No hidden stage was planted through the link either.
    assert not any(name.startswith(".omniagentos-workfs-stage-") for name in os.listdir(outside))
