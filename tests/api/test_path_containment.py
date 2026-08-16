"""Differential and adversarial tests for the shared containment primitive."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omniagentos.api import files_common
from omniagentos.api.routes import board_files, projects
from omniagentos.api.services import ApiError
from omniagentos.path_containment import (
    inode_path_is_within_anchored,
    inode_paths_equal,
    inode_relative_parts,
    inode_relative_parts_anchored,
)
from omniagentos.scope.paths import resolve_into_realm

_REFUSAL_KINDS = (
    "production-checkout",
    "production-vault",
    "legacy-home-checkout",
    "bare-home",
    "home-work",
    "home-desktop",
    "home-ssh",
    "home-library-messages",
    "etc",
    "filesystem-root",
    "private-tmp",
    "prefix-collision",
    "resolved-dotdot-traversal",
    "symlink-into-production",
    "apfs-case-variant",
    "macos-firmlink",
)


def _legacy_launcher_inside(candidate: Path, root: Path) -> bool:
    """Exact pre-migration launcher oracle used only for differential tests."""
    try:
        root_stat = os.stat(root)
    except OSError:
        return False
    node = os.fspath(candidate)
    while not os.path.exists(node):
        parent = os.path.dirname(node)
        if parent == node:
            return False
        node = parent
    node = os.path.realpath(node)
    while True:
        try:
            if os.path.samestat(os.stat(node), root_stat):
                return True
        except OSError:
            pass
        parent = os.path.dirname(node)
        if parent == node:
            return False
        node = parent


def test_shared_primitive_matches_strongest_legacy_variant(tmp_path: Path) -> None:
    """The new oracle matches the launcher's old inode proof case by case."""
    root = tmp_path / "root"
    inside = root / "inside"
    outside = tmp_path / "outside"
    prefix_collision = tmp_path / "root-evil"
    inside.mkdir(parents=True)
    outside.mkdir()
    prefix_collision.mkdir()
    inward_link = tmp_path / "inward-link"
    inward_link.symlink_to(inside, target_is_directory=True)
    outward_link = root / "outward-link"
    outward_link.symlink_to(outside, target_is_directory=True)

    candidates = (
        root,
        inside,
        inside / "not-created" / "leaf",
        inward_link,
        outside,
        prefix_collision,
        outward_link,
        root / ".." / "outside",
    )
    for candidate in candidates:
        assert (inode_relative_parts(candidate, root) is not None) is _legacy_launcher_inside(
            candidate, root
        ), candidate


def test_files_common_reexports_the_exact_shared_primitive() -> None:
    assert files_common.inode_relative_parts is inode_relative_parts


def test_shared_primitive_recanonicalizes_unresolved_symlink_and_dotdot(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "approved"
    production = tmp_path / "production"
    approved.mkdir()
    production.mkdir()
    link = approved / "link-to-production"
    link.symlink_to(production, target_is_directory=True)

    assert inode_relative_parts(link, approved) is None
    assert inode_relative_parts(approved / ".." / "production", approved) is None
    assert inode_relative_parts(Path("approved"), approved) is None


def test_inode_identity_supports_absent_leaf_through_inode_anchored_alias(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    # Control (`is True`): the inode-anchored alias is positively equal.
    assert inode_paths_equal(alias / "new" / "token", root / "new" / "token") is True
    # Control (`is False`): a different absent suffix is positively different.
    assert inode_paths_equal(alias / "new" / "other", root / "new" / "token") is False


def test_absent_remaining_components_are_exact_not_case_folded(tmp_path: Path) -> None:
    """Nearest-existing-ancestor identity plus EXACT remaining components.

    Case-folding absent suffixes would admit ``AbsentToken`` as ``absenttoken``
    and ``futureroot`` under ``FutureRoot``.  Existing path identity still
    collapses through ``samestat``; only the unstatable suffix is literal.
    """
    root = tmp_path / "root"
    root.mkdir()

    assert inode_paths_equal(root / "AbsentToken", root / "absenttoken") is False
    assert inode_paths_equal(root / "AbsentToken", root / "AbsentToken") is True

    future = root / "FutureRoot"
    assert inode_relative_parts_anchored(root / "futureroot" / "secret", future) is None
    assert inode_path_is_within_anchored(root / "futureroot" / "secret", future) is False
    assert inode_relative_parts_anchored(root / "FutureRoot" / "secret", future) == ("secret",)
    assert inode_path_is_within_anchored(root / "FutureRoot" / "secret", future) is True


def test_inode_containment_supports_absent_root_from_existing_anchor(tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    future_root = tmp_path / "future-root"

    assert inode_relative_parts_anchored(
        alias / "future-root" / "nested" / "file",
        future_root,
    ) == ("nested", "file")
    assert inode_relative_parts_anchored(tmp_path / "outside", future_root) is None
    assert (
        inode_path_is_within_anchored(
            alias / "future-root" / "nested" / "file",
            future_root,
        )
        is True
    )
    assert inode_path_is_within_anchored(tmp_path / "outside", future_root) is False


def test_files_common_migration_refuses_broken_symlink_escape(tmp_path: Path) -> None:
    """The former ``exists`` walk climbed past a broken escaping symlink."""
    approved = tmp_path / "approved"
    approved.mkdir()
    missing_outside_target = tmp_path / "outside" / "not-created"
    broken_link = approved / "broken-escape"
    broken_link.symlink_to(missing_outside_target, target_is_directory=True)

    assert not files_common.is_within(broken_link, approved)
    assert files_common.inode_relative_parts(broken_link, approved) is None


def test_string_prefix_counterfeit_fails_loudly(tmp_path: Path) -> None:
    """Tripwire: ``str(candidate).startswith(str(root))`` admits this sibling."""
    root = tmp_path / "campaign"
    prefix_collision = tmp_path / "campaign-evil"
    root.mkdir()
    prefix_collision.mkdir()

    assert inode_relative_parts(prefix_collision, root) is None, (
        "string-prefix containment counterfeit admitted '<root>-evil'"
    )


def test_module_cli_matches_python_primitive(tmp_path: Path) -> None:
    """The shell adapter used by the launcher exposes this exact oracle."""
    root = tmp_path / "root"
    inside = root / "inside"
    outside = tmp_path / "outside"
    inside.mkdir(parents=True)
    outside.mkdir()

    for candidate in (inside, outside):
        result = subprocess.run(
            [sys.executable, "-m", "omniagentos.path_containment", str(candidate), str(root)],
            capture_output=True,
            text=True,
            check=False,
        )
        expected = 0 if inode_relative_parts(candidate, root) is not None else 1
        assert result.returncode == expected
        assert result.stdout == ""
        assert result.stderr == ""


def _outside_fallback(parent: Path, name: str) -> Path:
    fallback = parent / name
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _refusal_candidate(kind: str, tmp_path: Path, campaign: Path) -> Path:
    home = Path(os.path.realpath(os.path.expanduser("~")))
    production = home / "OmniAgentOS"
    if not production.exists():
        production = _outside_fallback(tmp_path / "outside", "production-checkout")

    actual: dict[str, Path] = {
        "production-checkout": production,
        "production-vault": production / "vault",
        "legacy-home-checkout": home / "OmniAgentOS",
        "bare-home": home,
        "home-work": home / "Work",
        "home-desktop": home / "Desktop",
        "home-ssh": home / ".ssh",
        "home-library-messages": home / "Library" / "Messages",
        "etc": Path("/etc"),
        "filesystem-root": Path("/"),
        "private-tmp": Path("/private/tmp"),
        "apfs-case-variant": home / "library" / "messages",
        "macos-firmlink": Path(f"/System/Volumes/Data{home}") / "Library" / "Messages",
    }
    if kind in actual:
        candidate = actual[kind]
        if not candidate.exists():
            return _outside_fallback(tmp_path / "outside", kind)
        return candidate
    if kind == "prefix-collision":
        return _outside_fallback(tmp_path, f"{campaign.name}-evil")
    if kind == "resolved-dotdot-traversal":
        outside = _outside_fallback(tmp_path, "dotdot-outside")
        return (campaign / ".." / outside.name).resolve()
    if kind == "symlink-into-production":
        link = campaign / "production-link"
        link.symlink_to(production, target_is_directory=True)
        return link
    raise AssertionError(f"unknown refusal kind: {kind}")


@pytest.mark.parametrize("candidate_kind", _REFUSAL_KINDS)
def test_full_refusal_battery_hits_every_migrated_consumer(
    candidate_kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    candidate = _refusal_candidate(candidate_kind, tmp_path, campaign)

    assert not files_common.is_within(candidate, campaign), candidate_kind
    assert files_common.inode_relative_parts(candidate, campaign) is None, candidate_kind
    assert not projects._is_within(candidate, campaign), candidate_kind
    assert resolve_into_realm(str(candidate), str(campaign)) is None, candidate_kind

    launcher_adapter = subprocess.run(
        [sys.executable, "-m", "omniagentos.path_containment", str(candidate), str(campaign)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert launcher_adapter.returncode == 1, candidate_kind

    monkeypatch.setattr(
        board_files,
        "_approved_workspace_roots",
        lambda: [str(campaign.resolve())],
    )
    with pytest.raises(ApiError) as excinfo:
        board_files._enforce_workspace_floor(candidate)
    assert excinfo.value.status_code == 403, candidate_kind


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS firmlink spelling")
def test_home_firmlink_has_one_inode_verdict() -> None:
    home = Path(os.path.realpath(os.path.expanduser("~")))
    firmlink_home = Path(f"/System/Volumes/Data{home}")
    if not firmlink_home.exists() or not os.path.samestat(os.stat(home), os.stat(firmlink_home)):
        pytest.skip("macOS /System/Volumes/Data home firmlink is unavailable")

    normal = home / "Library" / "Messages"
    firmlink = firmlink_home / "Library" / "Messages"
    unrelated_root = Path("/private/tmp")
    assert inode_relative_parts(normal, home) == ("Library", "Messages")
    assert inode_relative_parts(firmlink, home) == ("Library", "Messages")
    assert inode_relative_parts(normal, unrelated_root) is None
    assert inode_relative_parts(firmlink, unrelated_root) is None
