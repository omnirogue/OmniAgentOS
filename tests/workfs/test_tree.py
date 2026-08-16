"""WORKFS-E4 — tree reader reflects reality; never creates; no hardcoded names."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from omniagentos.workfs import read_tree
from omniagentos.workfs.root import STAGE_PREFIX

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="workfs requires Darwin kernel atomic operations (renameatx_np)"
)


def test_tree_reflects_out_of_band_folder(work_tmp: Path) -> None:
    """Create a folder out-of-band → next read_tree shows it (D3b)."""
    before = read_tree(root=work_tmp)
    assert before["entries"] == []

    surprise = work_tmp / "SurpriseCo" / "Marketing" / "Campaign"
    surprise.mkdir(parents=True)

    tree = read_tree(root=work_tmp)
    names_l1 = {e["name"] for e in tree["entries"]}
    assert "SurpriseCo" in names_l1
    co = next(e for e in tree["entries"] if e["name"] == "SurpriseCo")
    depts = {c["name"] for c in co["children"]}
    assert "Marketing" in depts
    mkt = next(c for c in co["children"] if c["name"] == "Marketing")
    subs = {c["name"] for c in mkt["children"]}
    assert "Campaign" in subs


def test_tree_never_creates(work_tmp: Path) -> None:
    empty = work_tmp
    assert list(empty.iterdir()) == []
    read_tree(root=empty)
    assert list(empty.iterdir()) == []


def test_tree_hides_reserved_stage_namespace(work_tmp: Path) -> None:
    (work_tmp / f"{STAGE_PREFIX}residue").mkdir()
    (work_tmp / f"{STAGE_PREFIX.upper()}CASEFOLD").mkdir()
    (work_tmp / "VisibleCo").mkdir()
    names = {entry["name"] for entry in read_tree(root=work_tmp)["entries"]}
    assert names == {"VisibleCo"}


def test_tree_depth_bounded(work_tmp: Path) -> None:
    deep = work_tmp / "A" / "B" / "C" / "D" / "E"
    deep.mkdir(parents=True)
    tree = read_tree(root=work_tmp, max_depth=3)
    a = next(e for e in tree["entries"] if e["name"] == "A")
    b = next(c for c in a["children"] if c["name"] == "B")
    c = next(c for c in b["children"] if c["name"] == "C")
    # Depth 3 stops at C's children listing as empty (D not descended into).
    assert c["children"] == []


def test_tree_ignores_files(work_tmp: Path) -> None:
    (work_tmp / "OnlyFileCo").mkdir()
    (work_tmp / "OnlyFileCo" / "notes.txt").write_text("x", encoding="utf-8")
    tree = read_tree(root=work_tmp)
    co = next(e for e in tree["entries"] if e["name"] == "OnlyFileCo")
    assert co["children"] == []


def test_no_hardcoded_owner_folder_names_in_workfs() -> None:
    """WORKFS-E4: all six owner-folder names absent from omniagentos/workfs/."""
    root = Path(__file__).resolve().parents[2] / "omniagentos" / "workfs"
    pattern = re.compile(r"Hooli|Initech|Globex|AcmeUni|OmniAgentOS|Personal")
    hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path}:{i}:{line.strip()}")
    assert hits == [], "hardcoded owner-folder names in workfs:\n" + "\n".join(hits)


def test_tree_filters_non_contained_symlink_escape(work_tmp: Path) -> None:
    """Defect 5: non-contained symlinks are unpickable — omit from tree."""
    outside = work_tmp.parent / "outside-tree"
    outside.mkdir()
    (outside / "secret-child").mkdir()
    link = work_tmp / "escape"
    link.symlink_to(outside, target_is_directory=True)
    # Real pickable company must still appear.
    (work_tmp / "RealCo").mkdir()
    tree = read_tree(root=work_tmp)
    names = {e["name"] for e in tree["entries"]}
    assert "escape" not in names, "non-contained symlink must not be pickable"
    assert "RealCo" in names


def test_tree_filters_contained_directory_symlink(work_tmp: Path) -> None:
    """The no-follow Darwin mutator cannot accept even an in-root symlink."""
    real = work_tmp / "RealTarget"
    real.mkdir()
    (real / "Inner").mkdir()
    link = work_tmp / "AliasCo"
    link.symlink_to(real, target_is_directory=True)
    tree = read_tree(root=work_tmp)
    names = {e["name"] for e in tree["entries"]}
    assert "AliasCo" not in names
    assert "RealTarget" in names
