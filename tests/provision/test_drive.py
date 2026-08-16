"""grant_drive_dir: the safety-checked Drive-subfolder grant.

Monkeypatches $HOME to a throwaway tmp_path (see tests/test_drive.py) so this
never touches the operator's real iCloud Drive / Google Drive mounts.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectStore
from omniagentos.provision import ProvisionError, ProvisionStore, grant_drive_dir


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def icloud_root(fake_home: Path) -> Path:
    root = fake_home / "Library" / "Mobile Documents" / "com~apple~CloudDocs"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def store() -> SqliteStore:
    return SqliteStore(":memory:")


def _project(store: SqliteStore, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"name": "drive-access-test"}
    payload.update(overrides)
    return ProjectStore(store).create_project(payload)


def test_grants_a_real_subfolder(store: SqliteStore, icloud_root: Path) -> None:
    vault = icloud_root / "Media Buying" / "CopywritingBrainVault"
    vault.mkdir(parents=True)
    project = _project(store)

    roots = grant_drive_dir(ProvisionStore(store), str(project["id"]), str(vault))

    resolved = str(vault.resolve())
    assert resolved in roots
    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert resolved in row["root_dirs"]
    assert resolved in row["allowed_dirs"]


def test_grant_is_idempotent(store: SqliteStore, icloud_root: Path) -> None:
    vault = icloud_root / "Vault"
    vault.mkdir()
    project = _project(store)
    prov = ProvisionStore(store)

    grant_drive_dir(prov, str(project["id"]), str(vault))
    grant_drive_dir(prov, str(project["id"]), str(vault))

    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert row["root_dirs"].count(str(vault.resolve())) == 1


def test_rejects_the_whole_drive_root(store: SqliteStore, icloud_root: Path) -> None:
    project = _project(store)

    with pytest.raises(ProvisionError, match="entire Drive root"):
        grant_drive_dir(ProvisionStore(store), str(project["id"]), str(icloud_root))

    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert row["root_dirs"] == []


def test_rejects_a_path_outside_every_drive_root(store: SqliteStore, fake_home: Path) -> None:
    (fake_home / "Library" / "Mobile Documents" / "com~apple~CloudDocs").mkdir(parents=True)
    outside = fake_home / "Desktop" / "not-a-drive-folder"
    outside.mkdir(parents=True)
    project = _project(store)

    with pytest.raises(ProvisionError, match="known Drive root"):
        grant_drive_dir(ProvisionStore(store), str(project["id"]), str(outside))

    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert row["root_dirs"] == []


def test_rejects_dotdot_escape(store: SqliteStore, icloud_root: Path) -> None:
    sub = icloud_root / "sub"
    sub.mkdir()
    project = _project(store)

    with pytest.raises(ProvisionError):
        grant_drive_dir(
            ProvisionStore(store), str(project["id"]), str(sub / ".." / ".." / "Desktop")
        )


def test_rejects_a_path_that_does_not_exist(store: SqliteStore, icloud_root: Path) -> None:
    project = _project(store)
    missing = icloud_root / "never-created"

    with pytest.raises(ProvisionError, match="existing folder"):
        grant_drive_dir(ProvisionStore(store), str(project["id"]), str(missing))

    row = ProjectStore(store).get_project(str(project["id"]))
    assert row is not None
    assert row["root_dirs"] == []


def test_rejects_unknown_project(store: SqliteStore, icloud_root: Path) -> None:
    vault = icloud_root / "Vault"
    vault.mkdir()

    with pytest.raises(ProvisionError):
        grant_drive_dir(ProvisionStore(store), "proj_missing", str(vault))
