"""Machine-roots registry (P1 mounts core): loader, realpath, grant roots.

Every test writes its OWN throwaway ``mounts.yaml`` under ``tmp_path`` and loads
it via ``load_mounts(str(path))`` (the path is the lru_cache key), so nothing here
depends on -- or perturbs -- the real ``configs/mounts.yaml`` cache.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from omniagentos import mounts
from omniagentos.mounts import (
    Mount,
    MountsError,
    grantable_mount_roots,
    load_mounts,
    mount_by_id,
)


def _write(tmp_path: Path, body: str) -> str:
    target = tmp_path / "mounts.yaml"
    target.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(target)


# --- loader ------------------------------------------------------------------


def test_default_mounts_path_is_cwd_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = mounts._default_mounts_path()
    other_cwd = tmp_path / "other-cwd"
    other_cwd.mkdir()

    monkeypatch.chdir(other_cwd)
    second = mounts._default_mounts_path()

    assert first.is_absolute()
    assert second.is_absolute()
    assert second == first


def test_loads_and_realpaths_paths_once(tmp_path: Path) -> None:
    real = tmp_path / "Work"
    real.mkdir()
    link = tmp_path / "WorkLink"
    link.symlink_to(real)

    cfg = _write(
        tmp_path,
        f"""
        version: 1
        mounts:
          - id: work
            label: Work
            path: "{link}"
            kind: local
            grantable: true
        """,
    )
    mounts = load_mounts(cfg)
    assert isinstance(mounts, tuple)
    (mount,) = mounts
    assert isinstance(mount, Mount)
    # Symlink followed + collapsed to the real directory at load time.
    assert mount.resolved == str(real.resolve())
    assert mount.exists is True
    # The raw configured path is retained verbatim for display.
    assert mount.path == str(link)


def test_defaults_are_applied(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        f"""
        version: 1
        mounts:
          - id: gd
            path: "{tmp_path}"
            kind: gdrive
        """,
    )
    (mount,) = load_mounts(cfg)
    assert mount.label == "gd"  # falls back to id
    assert mount.grantable is True  # default grantable
    assert mount.read_only is False  # default writable
    assert mount.cloud is True  # non-local kind defaults cloud=True
    assert mount.notes == ""


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        f"""
        version: 1
        mounts:
          - id: work
            path: "{tmp_path}"
          - id: work
            path: "{tmp_path}"
        """,
    )
    with pytest.raises(MountsError, match="Duplicate mount id"):
        load_mounts(cfg)


def test_missing_id_or_path_is_rejected(tmp_path: Path) -> None:
    no_path = _write(
        tmp_path,
        """
        version: 1
        mounts:
          - id: work
        """,
    )
    with pytest.raises(MountsError, match="missing its 'path'"):
        load_mounts(no_path)


def test_nonexistent_path_retained_with_exists_false(tmp_path: Path) -> None:
    missing = tmp_path / "not-created-yet"
    cfg = _write(
        tmp_path,
        f"""
        version: 1
        mounts:
          - id: ghost
            path: "{missing}"
            grantable: true
        """,
    )
    (mount,) = load_mounts(cfg)
    assert mount.exists is False
    # Retained for display, but never offered as a grant root.
    assert grantable_mount_roots(cfg) == []


def test_mount_by_id(tmp_path: Path) -> None:
    cfg = _write(
        tmp_path,
        f"""
        version: 1
        mounts:
          - id: work
            path: "{tmp_path}"
        """,
    )
    assert mount_by_id("work", cfg) is not None
    assert mount_by_id("nope", cfg) is None


# --- grantable_mount_roots ---------------------------------------------------


def test_grantable_roots_skip_non_grantable_and_missing(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    vault = tmp_path / "vault"
    vault.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    missing = tmp_path / "gone"

    cfg = _write(
        tmp_path,
        f"""
        version: 1
        mounts:
          - id: home
            path: "{home}"
            grantable: false
          - id: work
            path: "{work}"
            grantable: true
          - id: gdrive
            path: "{missing}"
            grantable: true
          - id: vault
            path: "{vault}"
            grantable: true
        """,
    )
    roots = grantable_mount_roots(cfg)
    assert str(work.resolve()) in roots
    assert str(vault.resolve()) in roots
    # home is grantable:false -> browse-only, never a grant root.
    assert str(home.resolve()) not in roots
    # missing path -> excluded even though grantable.
    assert str(missing) not in roots


def test_grantable_roots_exclude_read_only_mounts(tmp_path: Path) -> None:
    """A ``read_only: true`` mount is browsable but never a WRITE-grant root, even
    when ``grantable: true`` (fix2) -- e.g. Downloads holds financial/tax files."""
    writable = tmp_path / "writable"
    writable.mkdir()
    readonly = tmp_path / "downloads"
    readonly.mkdir()

    cfg = _write(
        tmp_path,
        f"""
        version: 1
        mounts:
          - id: writable
            path: "{writable}"
            grantable: true
            read_only: false
          - id: downloads
            path: "{readonly}"
            grantable: true
            read_only: true
        """,
    )
    roots = grantable_mount_roots(cfg)
    assert str(writable.resolve()) in roots
    # grantable AND exists, but read_only -> excluded as a write-grant root.
    assert str(readonly.resolve()) not in roots


# --- the shipped configs/mounts.yaml ----------------------------------------


def test_default_registry_loads_expected_ids() -> None:
    ids = {m.id for m in load_mounts()}
    expected = {
        "home",
        "work",
        "desktop",
        "documents",
        "downloads",
        "coding-projects",
        "icloud",
        "gdrive",
        "dropbox",
        "vault",
    }
    assert expected <= ids
    home = mount_by_id("home")
    assert home is not None and home.grantable is False
