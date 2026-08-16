"""Superfast front door: task-speed classification + target-location parsing.

The classifier decides fast (spawn a session now, no planning) vs planned (Fable
plans first). The parser decides where a fast task may write -- and, crucially,
REFUSES to widen scope onto secret/system paths.
"""

from __future__ import annotations

import os

# import cycle before importing the intake leaf below, so this file runs standalone.
import pytest

import omniagentos.api.main  # noqa: F401  -- settle the pre-existing intake<->api
from omniagentos.intake import fastlane
from omniagentos.intake.fastlane import (
    classify_task_speed,
    parse_target_location,
    target_and_todo_prompt,
)

HOME = os.path.realpath(os.path.expanduser("~"))


@pytest.mark.parametrize(
    "goal",
    [
        "make a folder on my desktop which is called tiger",
        "create an html file on my desktop which is a bird app",
        "write a haiku about the ocean",
        "rename report.txt to final.txt",
        "add a .gitignore file",
    ],
)
def test_simple_goals_take_the_fast_lane(goal: str) -> None:
    assert classify_task_speed(goal) == "fast"


@pytest.mark.parametrize(
    "goal",
    [
        "build 10 creatives for initech and then write ad copy",
        "give it a plan to make 10 creatives for initech",
        "research the best pricing strategy and refactor the checkout",
        "migrate the entire billing system across multiple services",
        "set up a CI pipeline and deploy to production",
    ],
)
def test_complex_goals_take_the_planned_lane(goal: str) -> None:
    assert classify_task_speed(goal) == "planned"


def test_empty_goal_is_planned() -> None:
    assert classify_task_speed("") == "planned"
    assert classify_task_speed("   ") == "planned"


def test_named_locations_resolve_under_home() -> None:
    assert parse_target_location("make a folder on my desktop called tiger") == os.path.join(
        HOME, "Desktop"
    )
    assert parse_target_location("save it to my downloads") == os.path.join(HOME, "Downloads")


def test_no_named_location_returns_none() -> None:
    assert parse_target_location("write a haiku about the ocean") is None


def test_cloud_and_work_named_locations_parse(tmp_path, monkeypatch) -> None:
    """The carve-out is proven via inode_relative_parts, which fails closed when
    the root can't be statted (see _is_forbidden's docstring). A hosted CI
    runner has no real ~/Library/CloudStorage or iCloud Drive, so this points
    fastlane at a fixture HOME with the carve-out roots created, instead of
    relying on the developer machine's real ~/Library contents."""
    home = tmp_path / "home"
    home.mkdir()
    home_s = os.path.realpath(str(home))
    icloud_root_s = os.path.join(home_s, "Library", "Mobile Documents", "com~apple~CloudDocs")
    cloudstorage_s = os.path.join(home_s, "Library", "CloudStorage")
    os.makedirs(icloud_root_s, exist_ok=True)
    os.makedirs(cloudstorage_s, exist_ok=True)
    gdrive_mydrive_s = os.path.join(cloudstorage_s, "GoogleDrive-fixture@example.com", "My Drive")
    dropbox_root_s = os.path.join(cloudstorage_s, "Dropbox")

    named_locations = {
        "icloud desktop": os.path.join(icloud_root_s, "Desktop"),
        "icloud drive": icloud_root_s,
        "icloud": icloud_root_s,
        "google drive": gdrive_mydrive_s,
        "gdrive": gdrive_mydrive_s,
        "coding projects": os.path.join(home_s, "Coding Projects"),
        "dropbox": dropbox_root_s,
        "work": os.path.join(home_s, "Work"),
        "desktop": os.path.join(home_s, "Desktop"),
        "documents": os.path.join(home_s, "Documents"),
        "downloads": os.path.join(home_s, "Downloads"),
        "pictures": os.path.join(home_s, "Pictures"),
        "movies": os.path.join(home_s, "Movies"),
        "music": os.path.join(home_s, "Music"),
    }

    monkeypatch.setenv("HOME", home_s)
    monkeypatch.setattr(fastlane, "_HOME", home_s)
    monkeypatch.setattr(fastlane, "_NAMED_LOCATIONS", named_locations)
    monkeypatch.setattr(fastlane, "_ALLOWED_LIBRARY_SUBTREES", (icloud_root_s, cloudstorage_s))
    monkeypatch.setattr(
        fastlane,
        "_FORBIDDEN",
        tuple(
            os.path.join(home_s, part)
            for part in (
                ".ssh",
                ".aws",
                ".config",
                ".gnupg",
                ".claude",
                ".claude-account-2",
                "Library",
            )
        ),
    )

    def expected(name: str) -> str:
        return os.path.realpath(named_locations[name])

    assert parse_target_location("put it on my icloud") == expected("icloud")
    assert parse_target_location("save it to my icloud drive") == expected("icloud drive")
    # "icloud desktop" must win over the bare "desktop"/"icloud" aliases.
    assert parse_target_location("make a folder on my icloud desktop") == expected("icloud desktop")
    assert parse_target_location("save it to my google drive") == expected("google drive")
    assert parse_target_location("write it to gdrive") == expected("gdrive")
    assert parse_target_location("drop it in my dropbox") == expected("dropbox")
    assert parse_target_location("put it in my work folder") == os.path.join(home_s, "Work")
    assert parse_target_location("save it to coding projects") == os.path.join(
        home_s, "Coding Projects"
    )


def test_cloudstorage_carveout_is_allowed(tmp_path, monkeypatch) -> None:
    """Same fixture-HOME fix as test_cloud_and_work_named_locations_parse: the
    carve-out roots must be statable on disk for inode_relative_parts to prove
    containment, and a hosted CI runner has no real ~/Library/CloudStorage."""
    home = tmp_path / "home"
    home.mkdir()
    home_s = os.path.realpath(str(home))
    icloud_root_s = os.path.join(home_s, "Library", "Mobile Documents", "com~apple~CloudDocs")
    cloudstorage_s = os.path.join(home_s, "Library", "CloudStorage")
    os.makedirs(icloud_root_s, exist_ok=True)
    os.makedirs(cloudstorage_s, exist_ok=True)

    monkeypatch.setenv("HOME", home_s)
    monkeypatch.setattr(fastlane, "_HOME", home_s)
    monkeypatch.setattr(fastlane, "_ALLOWED_LIBRARY_SUBTREES", (icloud_root_s, cloudstorage_s))

    # An explicit path inside ~/Library/CloudStorage is permitted despite the
    # blanket ~/Library block (Google Drive / Dropbox live there).
    got = parse_target_location("save the report to ~/Library/CloudStorage/Dropbox/reports")
    assert got is not None
    assert got.startswith(os.path.join(home_s, "Library", "CloudStorage") + os.sep)
    assert got.endswith(os.path.join("Dropbox", "reports"))


def test_keychains_still_refused_after_carveout() -> None:
    # The ~/Library carve-out is only for the cloud-drive subtrees -- Keychains
    # (and the rest of ~/Library) stays hard-refused.
    assert parse_target_location("save it to ~/Library/Keychains/login.keychain") is None
    assert parse_target_location("write to ~/Library/Application Support/evil") is None


@pytest.mark.parametrize(
    "goal",
    [
        "write a key to ~/.ssh/authorized_keys",
        "create a file at /etc/passwd",
        "put something in ~/.aws/credentials",
        "write to /usr/local/bin/evil",
        "save it to ~/Library/Keychains/login.keychain",
    ],
)
def test_secret_and_system_targets_are_refused(goal: str) -> None:
    # A parse must never widen the sandbox onto a secret/system path.
    assert parse_target_location(goal) is None


def test_case_variant_secret_paths_are_forbidden() -> None:
    """macOS APFS is case-insensitive: ~/.SSH is the same inode as ~/.ssh.

    String startswith after realpath compares case-sensitively, so a case-folded
    secret path was reported as allowed (non-result of the string check treated
    as a favourable "safe to grant"). path_containment inode ancestry must refuse.
    """
    import sys

    if sys.platform != "darwin":
        pytest.skip("case-insensitive FS security check is a macOS defect")

    # Direct gate: case variants of credential stores must be forbidden.
    assert fastlane._is_forbidden(os.path.join(HOME, ".SSH")) is True
    assert fastlane._is_forbidden(os.path.join(HOME, ".Ssh")) is True
    assert fastlane._is_forbidden(os.path.join(HOME, ".AWS")) is True
    assert fastlane._is_forbidden(os.path.join(HOME, "LIBRARY")) is True
    assert fastlane._is_forbidden(os.path.join(HOME, "LIBRARY", "Keychains")) is True
    # Canonical spellings stay forbidden too.
    assert fastlane._is_forbidden(os.path.join(HOME, ".ssh")) is True
    assert fastlane._is_forbidden(os.path.join(HOME, ".aws")) is True

    # Front door: a goal that spells a secret with alternate case must not grant.
    assert parse_target_location("write a key to ~/.SSH/authorized_keys") is None
    assert parse_target_location("put something in ~/.AWS/credentials") is None
    assert parse_target_location("save it to ~/LIBRARY/Keychains/login.keychain") is None


def test_firmlink_home_spelling_is_not_outside_home() -> None:
    """Counterfeit catcher for 'just .lower() the startswith'.

    On macOS, /System/Volumes/Data$HOME is the same inode as $HOME but the
    realpath strings do not share a prefix (even case-folded). A string-prefix
    gate (with or without .lower()) treats the firmlink Desktop as outside
    $HOME and refuses a safe personal directory. inode_relative_parts is the
    only containment that admits it. Skip when the firmlink spelling is absent.
    """
    import sys

    if sys.platform != "darwin":
        pytest.skip("macOS firmlink spelling")

    firmlink_home = f"/System/Volumes/Data{HOME}"
    if not os.path.isdir(firmlink_home):
        pytest.skip("macOS /System/Volumes/Data home firmlink is unavailable")
    try:
        if not os.path.samestat(os.stat(HOME), os.stat(firmlink_home)):
            pytest.skip("firmlink home is not the same inode as $HOME")
    except OSError:
        pytest.skip("could not stat firmlink home")

    desktop = os.path.join(HOME, "Desktop")
    firmlink_desktop = os.path.join(firmlink_home, "Desktop")
    if not os.path.isdir(desktop):
        pytest.skip("~/Desktop missing")

    # Safe personal dir under firmlink spelling of home must be allowed.
    assert fastlane._is_forbidden(firmlink_desktop) is False
    # Secret under firmlink spelling must still be refused (not "outside home"
    # laundered into a different outcome — still forbidden, by secret rule).
    assert fastlane._is_forbidden(os.path.join(firmlink_home, ".ssh")) is True


def test_absent_forbidden_root_is_not_allowed(tmp_path, monkeypatch) -> None:
    """Fresh-machine fail-closed: missing ~/.ssh must not render as allowed.

    inode_relative_parts(candidate, bad_root) returns None when bad_root cannot
    be statted. Mapping that to `_is_under` False and then to `_is_forbidden`
    False is the counterfeit favourable result (grant write into a secret path
    that does not exist yet). Home-relative parts keep the secret refuseable.

    The ``.SSH`` case-variant assertion below only holds on macOS: `_parts_under`
    case-folds ONLY on darwin (ext4 is case-sensitive), same as the sibling
    test_case_variant_secret_paths_are_forbidden. Only THAT one assertion is
    platform-guarded -- the canonical missing-root forbidden checks and the
    safe-Desktop negative control below are platform-independent and must run
    on Linux too.
    """
    import sys

    home = tmp_path / "home"
    home.mkdir()
    desktop = home / "Desktop"
    desktop.mkdir()
    # Intentionally do NOT create .ssh / .aws / Library.

    home_s = str(home)
    monkeypatch.setattr(fastlane, "_HOME", home_s)
    monkeypatch.setattr(
        fastlane,
        "_FORBIDDEN",
        (
            os.path.join(home_s, ".ssh"),
            os.path.join(home_s, ".aws"),
            os.path.join(home_s, ".config"),
            os.path.join(home_s, ".gnupg"),
            os.path.join(home_s, "Library"),
        ),
    )
    # Carve-outs under a home that has no Library must not open anything else.
    monkeypatch.setattr(fastlane, "_ALLOWED_LIBRARY_SUBTREES", ())

    secret = os.path.join(home_s, ".ssh", "authorized_keys")
    assert os.path.exists(os.path.join(home_s, ".ssh")) is False
    # Direct gate: unmeasurable forbidden root must still forbid.
    assert fastlane._is_forbidden(secret) is True
    if sys.platform == "darwin":
        # Case-variant folding is a macOS-only defect surface (_parts_under
        # case-folds only on darwin; ext4 is case-sensitive) -- see the
        # sibling test_case_variant_secret_paths_are_forbidden.
        assert fastlane._is_forbidden(os.path.join(home_s, ".SSH", "id_rsa")) is True
    assert fastlane._is_forbidden(os.path.join(home_s, ".aws", "credentials")) is True
    assert fastlane._is_forbidden(os.path.join(home_s, "Library", "Keychains")) is True
    # Safe personal dir under the synthetic home remains allowed.
    assert fastlane._is_forbidden(str(desktop)) is False


def test_relative_candidate_is_forbidden_fail_closed() -> None:
    """Relative inputs must not abspath into cwd and render as allowed.

    `_is_under` documents fail-closed for relatives; converting via abspath
    made `Desktop` look like a non-forbidden path under cwd.
    """
    assert fastlane._is_under("Desktop", fastlane._HOME) is False
    assert fastlane._is_forbidden("Desktop") is True
    assert fastlane._is_forbidden(".ssh/authorized_keys") is True


def test_explicit_home_path_is_allowed_and_returns_its_dir() -> None:
    got = parse_target_location("create a file at ~/projects/demo/notes.txt")
    assert got == os.path.join(HOME, "projects", "demo")


def test_prompt_carries_target_and_todo_nudge() -> None:
    prompt = target_and_todo_prompt("make a folder", os.path.join(HOME, "Desktop"))
    assert os.path.join(HOME, "Desktop") in prompt
    assert "TodoWrite" in prompt
    assert "make a folder" in prompt


def test_prompt_without_target_still_nudges_todo() -> None:
    prompt = target_and_todo_prompt("write a poem", None)
    assert "TodoWrite" in prompt
    assert "write a poem" in prompt
