"""Pins the counterfeit harness's per-worker scratch REUSE contract.

Two properties, and each test is written so that it goes RED against the
behaviour it is supposed to pin — a test that a no-op would also satisfy pins
nothing:

1. RESET EQUIVALENCE — a reused scratch is byte-equivalent to a genuinely fresh
   :func:`materialise_scratch` copy, so entry N's patch/additions/deletions/
   runtime residue cannot reach entry N+1. Every test that asserts this FIRST
   asserts the second tree is the *same directory* as the first. Without that,
   the harness could silently fall back to a fresh copy (the ``rsync`` failure
   path does exactly that) and the content assertions would pass while
   measuring nothing — the favourable-absence shape.

2. ABNORMAL-EXIT POISONING — an entry that times out (or dies any other way)
   must not hand its lease on. The discriminating assertion is deliberately
   about IDENTITY, not content: a leaked lease would be reset before the next
   entry ran, so ``no residue in the next tree`` is true either way. Only
   ``the next tree is a different directory`` separates a poisoned lease from a
   reused one.

Deliberately NOT marked ``counterfeit_gate`` (unlike every other file in this
directory): those run the real corpus and are excluded from the default suite,
while these are hermetic tmp_path unit tests of the harness itself and finish in
~2s. Marking them would mean a regression in the reset or the lease lifecycle
went unnoticed until somebody ran the gate.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from tests.counterfeits import harness

# Guard the three-carrier import trap: a stale `omniagentos`/harness resolved
# out of the serving checkout would test code this file did not change.
assert Path(harness.__file__).resolve().parents[2] == Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_scratch_pool(monkeypatch: pytest.MonkeyPatch):
    """Module-global pristine/lease maps are process state — clear around each test."""
    monkeypatch.delenv(harness.SCRATCH_REUSE_ENV, raising=False)
    harness.reset_scratch_pool()
    yield
    harness.reset_scratch_pool()


def _fake_root(tmp_path: Path) -> Path:
    """A minimal tree that satisfies ``_TREE_SENTINELS``.

    Deliberately not the real checkout: the reset contract is about file
    equivalence, and a 5k-file copy per assertion would make this suite too slow
    to run on every gate. Symlink and exec-mode entries are present so the
    manifest comparison exercises more than regular-file content.
    """
    root = tmp_path / "repo"
    for sentinel in harness._TREE_SENTINELS:
        target = root / sentinel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# sentinel {sentinel}\n")
    (root / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "pkg" / "keep.py").write_text("KEEP = 1\n")
    (root / "pkg" / "doomed.txt").write_text("the entry deletes me\n")
    (root / "nested" / "deep").mkdir(parents=True, exist_ok=True)
    (root / "nested" / "deep" / "leaf.json").write_text('{"a": 1}\n')
    os.symlink("keep.py", root / "pkg" / "link.py")
    script = root / "bin" / "run.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    return root


def _mutate_like_an_entry(tree: Path) -> None:
    """Everything a counterfeit entry + its pytest run does to a scratch tree."""
    (tree / "pkg" / "keep.py").write_text("KEEP = 999  # counterfeit patch\n")
    (tree / "pkg" / "doomed.txt").unlink()
    (tree / "added_by_entry.py").write_text("LEAKED = True\n")
    (tree / "nested" / "deep" / "leaf.json").write_text('{"a": 2}\n')
    (tree / "var" / "counterfeit-entry").mkdir(parents=True, exist_ok=True)
    (tree / "var" / "counterfeit-entry" / "state.sqlite3").write_text("runtime residue\n")
    (tree / "pkg" / "__pycache__").mkdir(parents=True, exist_ok=True)
    (tree / "pkg" / "__pycache__" / "keep.cpython-312.pyc").write_text("bytecode\n")
    (tree / "bin" / "run.sh").chmod(0o600)


def _manifest(tree: Path) -> dict[str, tuple[str, int, str]]:
    """kind + mode + content-digest (or symlink target) for every path in ``tree``."""
    out: dict[str, tuple[str, int, str]] = {}
    for path in sorted(tree.rglob("*")):
        rel = str(path.relative_to(tree))
        mode = path.lstat().st_mode & 0o7777
        if path.is_symlink():
            out[rel] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            out[rel] = ("dir", mode, "")
        else:
            out[rel] = ("file", mode, hashlib.sha256(path.read_bytes()).hexdigest())
    return out


def _entry(tmp_path: Path) -> harness.CounterfeitEntry:
    patch = tmp_path / "cf-fake.patch"
    patch.write_text("# apply_patch is stubbed in these tests\n")
    return harness.CounterfeitEntry(
        id="cf-scratch-reuse-fake",
        patch=patch.name,
        rationale="lease lifecycle fixture",
        must_fail=("tests/does_not_matter.py::test_x",),
        failure_re="boom",
        source=tmp_path / "corpus.toml",
    )


# --- property 1: the reset is a real reset ---------------------------------


def test_reuse_returns_the_same_tree_reset_to_pristine(tmp_path: Path) -> None:
    """Modified restored, deleted re-created, added and residue removed."""
    root = _fake_root(tmp_path)

    first = harness.acquire_scratch(root)
    _mutate_like_an_entry(first)
    harness.release_scratch(first, repo_root=root)
    assert first.is_dir(), "a leased tree must survive release — reuse depends on it"

    second = harness.acquire_scratch(root)
    # Identity first: proves we are measuring the RESET and not a fresh copy.
    assert second == first

    assert (second / "pkg" / "keep.py").read_text() == "KEEP = 1\n"
    assert (second / "nested" / "deep" / "leaf.json").read_text() == '{"a": 1}\n'
    assert (second / "pkg" / "doomed.txt").is_file()
    assert not (second / "added_by_entry.py").exists()
    assert not (second / "var").exists()
    assert not (second / "pkg" / "__pycache__").exists()
    assert (second / "bin" / "run.sh").stat().st_mode & 0o777 == 0o755


def test_reset_tree_is_byte_equivalent_to_a_fresh_copy(tmp_path: Path) -> None:
    """Equivalence, not assertion: full manifest vs a genuine materialise."""
    root = _fake_root(tmp_path)

    first = harness.acquire_scratch(root)
    _mutate_like_an_entry(first)
    harness.release_scratch(first, repo_root=root)
    reset = harness.acquire_scratch(root)
    assert reset == first

    fresh = harness.materialise_scratch(root)
    try:
        reset_manifest = _manifest(reset)
        fresh_manifest = _manifest(fresh)
        only_reset = sorted(set(reset_manifest) - set(fresh_manifest))
        only_fresh = sorted(set(fresh_manifest) - set(reset_manifest))
        changed = sorted(
            rel
            for rel in set(reset_manifest) & set(fresh_manifest)
            if reset_manifest[rel] != fresh_manifest[rel]
        )
        assert (only_reset, only_fresh, changed) == ([], [], [])
    finally:
        harness.destroy_scratch(fresh)


def test_same_size_edit_inside_the_mtime_window_is_still_restored(tmp_path: Path) -> None:
    """The openrsync quick-check hole, pinned.

    macOS ``rsync`` is openrsync, which compares mtimes at whole-second
    granularity: a same-size edit whose mtime lands in the same second as the
    pristine's is NOT transferred by ``rsync -a --delete``, so the mutation
    survives the reset. Verified directly on this host before the fix.

    This fixture is built entirely inside that window on purpose — root written,
    pristine staged and file edited within one second — which is why it fails
    against a reset that always takes the quick check.
    """
    root = _fake_root(tmp_path)
    original = (root / "nested" / "deep" / "leaf.json").read_text()

    first = harness.acquire_scratch(root)
    same_size = '{"a": 2}\n'
    assert len(same_size) == len(original), "fixture must exercise the SIZE-equal path"
    (first / "nested" / "deep" / "leaf.json").write_text(same_size)
    harness.release_scratch(first, repo_root=root)

    second = harness.acquire_scratch(root)
    assert second == first
    assert (second / "nested" / "deep" / "leaf.json").read_text() == original


def test_reuse_disabled_gives_a_new_tree_every_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The escape hatch has to actually escape, or an operator's re-run lies."""
    root = _fake_root(tmp_path)
    monkeypatch.setenv(harness.SCRATCH_REUSE_ENV, "0")

    first = harness.acquire_scratch(root)
    harness.release_scratch(first, repo_root=root)
    assert not first.exists(), "unleased trees must be destroyed on release"
    second = harness.acquire_scratch(root)
    try:
        assert second != first
    finally:
        harness.release_scratch(second, repo_root=root)


# --- property 2: an abnormal exit poisons the lease ------------------------


def _stub_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for ``git apply``: mutate the tree the way a real patch would.

    The lease lifecycle, not diff application, is under test here, and a real
    ``git apply`` against a non-repo temp dir would make the fixture about
    patch(1) portability instead.
    """

    def _apply(scratch: Path, patch_path: Path) -> None:
        (Path(scratch) / "pkg" / "keep.py").write_text("KEEP = 999  # counterfeit patch\n")

    monkeypatch.setattr(harness, "apply_patch", _apply)


def test_timed_out_entry_poisons_its_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout must drop the lease, not hand an unknown tree to the next entry."""
    root = _fake_root(tmp_path)
    _stub_apply(monkeypatch)
    seen: dict[str, Path] = {}

    def _hang(*, tree: Path, nodes, repo_root, timeout, isolate_runtime=False):
        seen["tree"] = Path(tree)
        # Residue of the entry that hung, i.e. exactly what must not survive.
        (Path(tree) / "left_by_the_hung_entry.txt").write_text("half-written\n")
        raise subprocess.TimeoutExpired(cmd=["pytest"], timeout=timeout, output="", stderr="")

    monkeypatch.setattr(harness, "run_pytest_nodes", _hang)

    result = harness.run_entry(_entry(tmp_path), repo_root=root, timeout=1.0)
    assert result.status == "survived"

    timed_out_tree = seen["tree"]
    assert not timed_out_tree.exists(), "the poisoned tree must be destroyed, not kept"

    nxt = harness.acquire_scratch(root)
    # THE discriminating assertion. Content checks below pass with or without
    # the fix (a retained lease is reset before reuse); only a different
    # directory proves the lease was poisoned and the tree re-materialised.
    assert nxt != timed_out_tree
    assert not (nxt / "left_by_the_hung_entry.txt").exists()
    assert (nxt / "pkg" / "keep.py").read_text() == "KEEP = 1\n"


def test_entry_after_a_timeout_runs_on_a_clean_new_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: two entries on ONE worker, the first hangs, the second is clean."""
    root = _fake_root(tmp_path)
    _stub_apply(monkeypatch)
    trees: list[Path] = []
    observed: list[dict[str, object]] = []

    def _first_hangs_then_pass(*, tree: Path, nodes, repo_root, timeout, isolate_runtime=False):
        tree = Path(tree)
        trees.append(tree)
        if len(trees) == 1:
            (tree / "left_by_the_hung_entry.txt").write_text("half-written\n")
            raise subprocess.TimeoutExpired(cmd=["pytest"], timeout=timeout, output="", stderr="")
        observed.append(
            {
                "leaked": (tree / "left_by_the_hung_entry.txt").exists(),
                "patched": (tree / "pkg" / "keep.py").read_text(),
            }
        )
        return subprocess.CompletedProcess(
            args=["pytest"], returncode=1, stdout="1 failed\nboom\n", stderr=""
        )

    monkeypatch.setattr(harness, "run_pytest_nodes", _first_hangs_then_pass)

    entry = _entry(tmp_path)
    first = harness.run_entry(entry, repo_root=root, timeout=1.0)
    second = harness.run_entry(entry, repo_root=root, timeout=1.0)

    assert first.status == "survived"
    assert second.status == "caught"
    assert trees[1] != trees[0], "second entry reused the timed-out worker's tree"
    # The second entry saw its OWN patch and none of the hung entry's residue.
    assert observed == [{"leaked": False, "patched": "KEEP = 999  # counterfeit patch\n"}]


def test_unexpected_exception_also_poisons_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sibling of the timeout path — a worker_error leaves the same unknown tree."""
    root = _fake_root(tmp_path)
    _stub_apply(monkeypatch)
    seen: dict[str, Path] = {}

    def _explode(*, tree: Path, nodes, repo_root, timeout, isolate_runtime=False):
        seen["tree"] = Path(tree)
        raise OSError("could not spawn pytest")

    monkeypatch.setattr(harness, "run_pytest_nodes", _explode)

    with pytest.raises(OSError):
        harness.run_entry(_entry(tmp_path), repo_root=root, timeout=1.0)

    dead_tree = seen["tree"]
    assert not dead_tree.exists()
    assert harness.acquire_scratch(root) != dead_tree


def test_apply_error_keeps_the_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Content-only damage is NOT poisoned — the reset provably restores it.

    Pins the boundary of the poison rule: widening it to every non-``caught``
    status would silently reintroduce a full copy per bit-rotted entry.
    """
    root = _fake_root(tmp_path)
    seen: dict[str, Path] = {}

    def _half_apply(scratch: Path, patch_path: Path) -> None:
        seen["tree"] = Path(scratch)
        (Path(scratch) / "pkg" / "keep.py").write_text("KEEP = 999  # half-applied\n")
        raise harness.CounterfeitApplyError("hunk 2 of 3 failed")

    monkeypatch.setattr(harness, "apply_patch", _half_apply)

    result = harness.run_entry(_entry(tmp_path), repo_root=root, timeout=1.0)
    assert result.status == "apply_error"

    reused = harness.acquire_scratch(root)
    assert reused == seen["tree"]
    assert (reused / "pkg" / "keep.py").read_text() == "KEEP = 1\n"
