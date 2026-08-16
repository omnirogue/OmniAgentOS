"""Drive Access for projects (W4): known Drive roots + safe path resolution.

Every test monkeypatches $HOME to a throwaway tmp_path and builds a synthetic
mirror of the real macOS layout under it (`os.path.expanduser` on POSIX reads
$HOME), so nothing here ever touches the operator's real iCloud Drive / Google
Drive mounts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omniagentos import drive


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _make_icloud(home: Path) -> Path:
    root = home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    root.mkdir(parents=True)
    return root


def _make_google_drive(home: Path, account: str = "owner@example.com") -> Path:
    root = home / "Library" / "CloudStorage" / f"GoogleDrive-{account}" / "My Drive"
    root.mkdir(parents=True)
    return root


# --- drive_roots -------------------------------------------------------------


def test_drive_roots_empty_when_nothing_mounted(fake_home: Path) -> None:
    assert drive.drive_roots() == []


def test_drive_roots_finds_icloud_and_google_drive(fake_home: Path) -> None:
    icloud = _make_icloud(fake_home)
    gdrive = _make_google_drive(fake_home)

    roots = drive.drive_roots()
    assert str(icloud.resolve()) in roots
    assert str(gdrive.resolve()) in roots
    # iCloud is listed before Google Drive (stable, documented order).
    assert roots.index(str(icloud.resolve())) < roots.index(str(gdrive.resolve()))


def test_drive_roots_finds_multiple_google_accounts(fake_home: Path) -> None:
    first = _make_google_drive(fake_home, "alpha@example.com")
    second = _make_google_drive(fake_home, "beta@example.com")

    roots = drive.drive_roots()
    assert str(first.resolve()) in roots
    assert str(second.resolve()) in roots


def test_drive_roots_finds_legacy_alias(fake_home: Path) -> None:
    legacy = fake_home / "Google Drive"
    legacy.mkdir()

    assert str(legacy.resolve()) in drive.drive_roots()


def test_drive_roots_dedupes_when_legacy_alias_is_a_symlink(fake_home: Path) -> None:
    gdrive = _make_google_drive(fake_home)
    legacy = fake_home / "Google Drive"
    legacy.symlink_to(gdrive)

    roots = drive.drive_roots()
    assert roots.count(str(gdrive.resolve())) == 1


# --- resolve_drive_path / is_within_drive ------------------------------------


def test_resolve_drive_path_accepts_a_real_subfolder(fake_home: Path) -> None:
    icloud = _make_icloud(fake_home)
    sub = icloud / "Media Buying" / "CopywritingBrainVault"
    sub.mkdir(parents=True)

    resolved = drive.resolve_drive_path(str(sub))
    assert resolved == str(sub.resolve())
    assert drive.is_within_drive(str(sub))


def test_resolve_drive_path_accepts_the_root_itself(fake_home: Path) -> None:
    icloud = _make_icloud(fake_home)
    assert drive.resolve_drive_path(str(icloud)) == str(icloud.resolve())


def test_resolve_drive_path_rejects_unrelated_path(fake_home: Path) -> None:
    _make_icloud(fake_home)
    outside = fake_home / "Desktop" / "secrets"
    outside.mkdir(parents=True)

    assert drive.resolve_drive_path(str(outside)) is None
    assert drive.is_within_drive(str(outside)) is False


def test_resolve_drive_path_rejects_dotdot_escape(fake_home: Path) -> None:
    icloud = _make_icloud(fake_home)
    sub = icloud / "sub"
    sub.mkdir()
    escaping = str(sub / ".." / ".." / "Desktop")

    assert drive.resolve_drive_path(escaping) is None


def test_resolve_drive_path_rejects_symlink_escape(fake_home: Path) -> None:
    icloud = _make_icloud(fake_home)
    outside = fake_home / "outside"
    outside.mkdir()
    trap = icloud / "looks-safe"
    trap.symlink_to(outside)

    # The symlink's realpath lands outside every known root -> rejected, even
    # though the un-resolved path string starts under the iCloud root.
    assert drive.resolve_drive_path(str(trap)) is None


def test_resolve_drive_path_handles_a_nonexistent_path_under_a_root(fake_home: Path) -> None:
    icloud = _make_icloud(fake_home)
    not_yet_created = icloud / "brand-new-folder"

    # realpath works lexically for a path that doesn't exist yet -- still
    # correctly recognized as under the root (grant_drive_dir separately
    # requires the folder to actually exist before granting it).
    assert drive.resolve_drive_path(str(not_yet_created)) == str(not_yet_created)


# --- google_drive_workspace ---------------------------------------------------


def test_google_drive_workspace_none_when_no_mount(fake_home: Path) -> None:
    assert drive.google_drive_workspace() is None


def test_google_drive_workspace_creates_omniagent_subfolder(fake_home: Path) -> None:
    gdrive = _make_google_drive(fake_home)

    workspace = drive.google_drive_workspace()
    assert workspace == str(gdrive.resolve() / "OmniAgent")
    assert os.path.isdir(workspace)


def test_google_drive_workspace_is_idempotent(fake_home: Path) -> None:
    _make_google_drive(fake_home)

    first = drive.google_drive_workspace()
    second = drive.google_drive_workspace()
    assert first == second
    assert os.path.isdir(str(first))
