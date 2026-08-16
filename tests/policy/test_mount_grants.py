"""Grant-layer widening (P1): mounts.grantable_mount_roots feeds allowed_grant_roots.

A grant may now live under any grantable machine root (iCloud, Dropbox, ~/Work,
...) from ``configs/mounts.yaml`` -- but the secret hard-reject is untouched, so
the whole home directory stays UNgrantable by design.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from omniagentos import mounts
from omniagentos.policy import dir_grants
from omniagentos.simgate import SimContext


def _tmp_mounts_yaml(tmp_path: Path, drive_dir: Path) -> str:
    target = tmp_path / "mounts.yaml"
    target.write_text(
        textwrap.dedent(
            f"""
            version: 1
            mounts:
              - id: dropbox
                label: Dropbox
                path: "{drive_dir}"
                kind: dropbox
                cloud: true
                grantable: true
            """
        ),
        encoding="utf-8",
    )
    return str(target)


def test_grantable_mount_root_appears_in_allowed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive_dir = tmp_path / "Dropbox"
    drive_dir.mkdir()
    cfg = _tmp_mounts_yaml(tmp_path, drive_dir)

    monkeypatch.setattr(
        dir_grants, "grantable_mount_roots", lambda: mounts.grantable_mount_roots(cfg)
    )

    roots = dir_grants.allowed_grant_roots()
    assert str(drive_dir.resolve()) in roots


def test_sim_allowed_roots_are_only_the_canonical_campaign_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign_root = tmp_path / "campaign"
    campaign_root.mkdir()
    calls = 0

    def unexpected_mount_lookup() -> list[str]:
        nonlocal calls
        calls += 1
        return [str(tmp_path / "production-mount")]

    monkeypatch.setattr(dir_grants, "grantable_mount_roots", unexpected_mount_lookup)
    context = SimContext(sim_mode=True, campaign="campaign", campaign_root=campaign_root)

    assert dir_grants.allowed_grant_roots(sim_ctx=context) == [str(campaign_root.resolve())]
    assert calls == 0


def test_grant_inside_a_mounted_drive_validates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive_dir = tmp_path / "Dropbox"
    project = drive_dir / "clientA" / "reports"
    project.mkdir(parents=True)
    cfg = _tmp_mounts_yaml(tmp_path, drive_dir)

    monkeypatch.setattr(
        dir_grants, "grantable_mount_roots", lambda: mounts.grantable_mount_roots(cfg)
    )

    # A subfolder under the mounted drive is now a valid grant.
    resolved = dir_grants.validate_grant_dir(str(project))
    assert resolved == str(project.resolve())


def test_read_only_mount_absent_from_allowed_grant_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fix2: a read_only grantable mount is browsable but must never widen the
    WRITE-grant surface -- its root is absent from grantable_mount_roots() and
    therefore from policy.dir_grants.allowed_grant_roots()."""
    readonly_dir = tmp_path / "Downloads"
    readonly_dir.mkdir()
    target = tmp_path / "mounts.yaml"
    target.write_text(
        textwrap.dedent(
            f"""
            version: 1
            mounts:
              - id: downloads
                label: Downloads
                path: "{readonly_dir}"
                grantable: true
                read_only: true
            """
        ),
        encoding="utf-8",
    )
    cfg = str(target)

    assert str(readonly_dir.resolve()) not in mounts.grantable_mount_roots(cfg)

    monkeypatch.setattr(
        dir_grants, "grantable_mount_roots", lambda: mounts.grantable_mount_roots(cfg)
    )
    assert str(readonly_dir.resolve()) not in dir_grants.allowed_grant_roots()
    # ... and a subfolder under it cannot be validated as a write grant.
    project = readonly_dir / "statements"
    project.mkdir()
    with pytest.raises(dir_grants.DirGrantError):
        dir_grants.validate_grant_dir(str(project))


def test_home_is_still_hard_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Even with the mount widening, ~ engulfs ~/.ssh -> secret hard-reject.
    with pytest.raises(dir_grants.DirGrantError):
        dir_grants.validate_grant_dir("~")


def test_path_outside_every_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive_dir = tmp_path / "Dropbox"
    drive_dir.mkdir()
    cfg = _tmp_mounts_yaml(tmp_path, drive_dir)
    monkeypatch.setattr(
        dir_grants, "grantable_mount_roots", lambda: mounts.grantable_mount_roots(cfg)
    )

    outside = tmp_path / "elsewhere" / "nope"
    outside.mkdir(parents=True)
    with pytest.raises(dir_grants.DirGrantError):
        dir_grants.validate_grant_dir(str(outside))
