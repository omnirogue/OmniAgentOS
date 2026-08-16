"""Clone-path import check — workfs must load from THIS clone, not another tree."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import omniagentos.workfs as workfs

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="workfs requires Darwin kernel atomic operations (renameatx_np)"
)


def test_workfs_imports_from_this_clone() -> None:
    clone_root = Path(__file__).resolve().parents[2]
    module_path = Path(workfs.__file__).resolve()
    assert clone_root in module_path.parents or module_path.is_relative_to(clone_root)
    assert "workfs-1" in str(module_path) or str(clone_root) in str(module_path)
    # Public surface present.
    assert hasattr(workfs, "ensure")
    assert hasattr(workfs, "read_tree")
    assert hasattr(workfs, "map_scope")
