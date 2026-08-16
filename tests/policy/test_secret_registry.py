"""AC-policy fix4 -- the ONE shared secret registry, proved across ALL layers.

For every entry in ``omniagentos.policy.secrets`` this asserts the three guardrail
layers agree that reading it is a hard-stop:

  (i)   the native-tool classifier  -> ``classify_tool("Read", ...)`` IRREVERSIBLE
  (ii)  the shell classifier        -> ``classify_shell("cat ...")`` IRREVERSIBLE
  (iii) the OS sandbox              -> BOTH the session AND adapter SBPL profiles
        deny-read the secret dirs, and a live sandbox-exec read is blocked.

This is the regression guard for BLOCKER 1: the sandbox deny-list used to be a
SUBSET of the classifier's set (missing ~/.ssh, ~/.config/gcloud), so a secret
read that one layer missed the other also missed. Deriving all three from this
module makes that drift impossible; this test fails if any layer forgets an entry.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omniagentos.contracts import ActionClass
from omniagentos.policy import secrets as secretreg
from omniagentos.policy.shell import classify_shell
from omniagentos.runner import sandbox
from omniagentos.secret_registry import (
    _casefold_path,
    path_relocates_secret_dir,
    references_secret,
    write_target_references_secret,
)
from omniagentos.sessions.policy_map import classify_tool

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(sandbox.__file__)))

SBX = "/usr/bin/sandbox-exec"


def _classifier_entries(home: str) -> list[tuple[str, str]]:
    """(label, concrete secret path) for EVERY registry entry the classifier gates."""
    entries: list[tuple[str, str]] = []
    # Secret DIRECTORIES -> a file inside each is a secret read (dir containment).
    for rel in secretreg.SECRET_DIR_RELS:
        entries.append((f"dir:{rel}", os.path.join(home, rel, "probe_secret.txt")))
    # Distinctive secret BASENAMES -> flagged anywhere, even outside a secret dir.
    for base in secretreg.SECRET_BASENAMES:
        entries.append((f"base:{base}", os.path.join(home, "Downloads", base)))
    # Unconditional secret SUFFIXES -> flagged anywhere (currently empty).
    for suffix in secretreg.SECRET_SUFFIXES:
        entries.append((f"suffix:{suffix}", os.path.join(home, "Downloads", f"app{suffix}")))
    # Repo-local var/secrets (relative form -> string match; absolute -> dir match).
    entries.append(("var/secrets(rel)", "var/secrets/db.txt"))
    return entries


def test_every_registry_entry_hard_stops_in_both_classifiers(tmp_path: Path) -> None:
    home = os.path.expanduser("~")
    project = str(tmp_path)  # a real, unrelated project scope
    for label, path in _classifier_entries(home):
        assert classify_tool("Read", {"file_path": path}, project) == ActionClass.IRREVERSIBLE, (
            f"native Read missed {label}: {path}"
        )
        assert classify_shell(f"cat {path}", project) == ActionClass.IRREVERSIBLE, (
            f"shell cat missed {label}: {path}"
        )


def test_secret_dirs_denied_read_in_session_and_adapter_profiles() -> None:
    """(iii) BOTH the session and adapter SBPL profiles deny-read every secret dir."""
    session = sandbox.build_profile("/tmp/ws", sandbox.session_write_roots("/tmp/ws"))
    adapter = sandbox.build_profile("/tmp/ws", sandbox.adapter_write_roots())
    for secret_dir in sandbox.secret_read_deny_roots():
        needle = f'(deny file-read* (subpath "{secret_dir}"))'
        assert needle in session, f"session profile does not deny-read {secret_dir}"
        assert needle in adapter, f"adapter profile does not deny-read {secret_dir}"


@pytest.mark.live
def test_live_sandbox_blocks_reading_every_secret_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(iii live) With a hermetic HOME, a real sandbox-exec read of a file inside
    EACH registry secret dir is physically denied under BOTH profiles."""
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec unavailable/unproven; classifier is the guarantee")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    # Plant a readable secret file inside each home-based registry secret dir.
    secret_files: list[str] = []
    for rel in secretreg.SECRET_DIR_RELS:
        d = fake_home / rel
        d.mkdir(parents=True, exist_ok=True)
        f = d / "leak.txt"
        f.write_text("TOPSECRET_sk_live_do_not_leak")
        secret_files.append(str(f))

    project = tmp_path / "proj"
    project.mkdir()
    os.makedirs(sandbox.claude_tmp_root(), exist_ok=True)
    profiles = {
        "session": sandbox.build_profile(str(project), sandbox.session_write_roots(str(project))),
        "adapter": sandbox.build_profile(str(project), sandbox.adapter_write_roots()),
    }
    for name, profile in profiles.items():
        for secret in secret_files:
            proc = subprocess.run(
                [SBX, "-p", profile, "/bin/cat", secret],
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert "TOPSECRET" not in proc.stdout, f"{name} profile leaked {secret}"


def test_case_variant_secret_paths_hard_stop_in_both_classifiers(tmp_path: Path) -> None:
    """fix5 BLOCKER 2: macOS case variants cannot bypass containment."""
    home = os.path.expanduser("~")
    project = str(tmp_path)
    for label, path in [
        ("dir:.SSH", os.path.join(home, ".SSH", "id_rsa")),
        ("dir:.Config/gcloud", os.path.join(home, ".Config", "gcloud", "creds.db")),
        ("dir:.AWS", os.path.join(home, ".AWS", "credentials")),
        ("dir:.GnuPG", os.path.join(home, ".GnuPG", "secring.gpg")),
        ("dir:.Docker", os.path.join(home, ".Docker", "config.json")),
        ("dir:.Kube", os.path.join(home, ".Kube", "config")),
    ]:
        assert classify_tool("Read", {"file_path": path}, project) == ActionClass.IRREVERSIBLE, (
            label
        )
        assert classify_shell(f"cat {path}", project) == ActionClass.IRREVERSIBLE, label


def test_case_variant_secret_dirs_match_without_filesystem_aliasing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Linux gap: case-variant secret-dir references must hard-stop on EVERY
    filesystem, not only where the OS aliases case.

    ``test_case_variant_secret_paths_hard_stop_in_both_classifiers`` above passes on
    macOS for the wrong reason -- APFS is case-insensitive, so ``~/.SSH`` and
    ``~/.ssh`` share an inode and the inode-containment rule matches. On a
    case-sensitive Linux filesystem they never alias, so ``references_secret``
    returned False and the classifier auto-ran the read (Ubuntu CI failure of
    ``test_classifier_layer_hard_stops_every_hostile_case``).

    HOME here points at a tmp directory whose secret dirs are deliberately NOT
    created, so inode containment cannot fire on ANY platform and the portable
    case-folded rule is the only thing that can carry these assertions -- which is
    what makes this test filesystem-independent."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()  # deliberately empty: no .ssh, no .SSH, no .aws ...
    monkeypatch.setenv("HOME", str(fake_home))
    project = str(tmp_path / "proj")

    # (a) case variants of every registered home store are secret references.
    for token in (
        "~/.SSH/authorized_keys",
        "~/.Ssh/id_ed25519.pub",
        "~/.Aws/config",
        "~/.Config/gcloud/creds.db",
        "~/.CONFIG/OMNI/notes.txt",
        "~/.GnuPG/secring.gpg",
        "~/.Docker/config.json",
        "~/.Kube/config",
        "~/.Config/GH/hosts.yml",
        "~/.SSH",  # the store directory itself
        "$HOME/.SSH/authorized_keys",  # $VAR spelling
        os.path.join(str(fake_home), ".SSH", "authorized_keys"),  # absolute spelling
    ):
        assert references_secret(token, project) is True, token
        assert classify_tool("Read", {"file_path": token}, project) == ActionClass.IRREVERSIBLE, (
            token
        )

    # (b) benign look-alikes stay readable -- the widening is segment-anchored, not
    #     a string prefix and not a blanket home deny.
    for token in (
        "~/.sshfoo/bar",
        "~/.config/gcloudx/foo",
        "~/.configuration/omni/x",
        "~/Documents/notes.txt",
        "~/.bashrc",
        "notes.txt",
        "src/main.py",
        "/etc/hosts",
    ):
        assert references_secret(token, project) is False, token


def test_case_variant_write_target_denied_without_filesystem_aliasing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) The WRITE side had the same inode-only assumption: ``_within_secret_dir``
    proves a case-variant store spelling only where the filesystem aliases case, so
    a WRITE to ``~/.SSH/authorized_keys`` was denied on macOS and allowed on Linux.

    The portable check is a pure WIDENING of the deny set -- consulted only after
    the existing scoped-dir check declines, and it can only return True -- so the
    look-alike and in-project allows below (the behavior PR #414 established) are
    unchanged. Same hermetic, dir-less HOME as above, so nothing here depends on
    APFS aliasing."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()  # again: the case-variant dirs are never created
    monkeypatch.setenv("HOME", str(fake_home))
    project = tmp_path / "proj"
    project.mkdir()

    for token in (
        "~/.SSH/authorized_keys",
        "~/.Aws/config",
        "~/.Config/gcloud/creds.db",
        os.path.join(str(fake_home), ".Kube", "config"),
    ):
        assert write_target_references_secret(token, str(project)) is True, token
        for tool in ("Write", "Edit", "MultiEdit"):
            assert classify_tool(tool, {"file_path": token}, str(project)) == (
                ActionClass.IRREVERSIBLE
            ), f"{tool} {token}"
        # The P3 laundered-scope spelling (the store's own home passed AS project_dir)
        # must not downgrade it either.
        assert write_target_references_secret(token, str(fake_home)) is True, token

    # Look-alikes and ordinary home/project writes stay allowed.
    for token in ("~/.sshfoo/bar.txt", "~/.config/gcloudx/y.txt", "~/Documents/notes.txt"):
        assert write_target_references_secret(token, str(project)) is False, token
    in_project = str(project / "dashboard" / ".env.local")
    assert write_target_references_secret(in_project, str(project)) is False


def test_relative_case_variant_resolves_against_project_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini round 1, F1 (BLOCKER): a RELATIVE case-variant token bypassed the
    portable check entirely.

    ``expanduser``/``expandvars`` never make ``.SSH/authorized_keys`` home-rooted,
    so round 1 returned None for every relative token and fell through to allow --
    even though that token, with ``project_dir`` at HOME, names exactly the same
    file as ``~/.SSH/authorized_keys`` (the caller's cwd IS the project scope, which
    is how ``_resolve`` already treats relative paths). The home-relative check now
    mirrors that base resolution string-wise."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()  # no case-variant dirs: no inode aliasing on any platform
    monkeypatch.setenv("HOME", str(fake_home))

    for token in (".SSH/authorized_keys", "./.SSH/authorized_keys", ".Config/gcloud/creds.db"):
        assert references_secret(token, str(fake_home)) is True, token
        assert write_target_references_secret(token, str(fake_home)) is True, token
        assert classify_tool("Read", {"file_path": token}, str(fake_home)) == (
            ActionClass.IRREVERSIBLE
        ), token

    # Same relative spelling under an unrelated project scope is NOT the store, and
    # relative look-alikes stay allowed even when the scope IS home.
    unrelated = tmp_path / "proj"
    unrelated.mkdir()
    assert references_secret(".SSH/authorized_keys", str(unrelated)) is False
    for token in (".sshfoo/bar", "src/.config/gcloudx/y"):
        assert references_secret(token, str(fake_home)) is False, token
    # No scope at all: nothing to resolve against, and rule 5 must not invent one.
    assert references_secret(".SSH/authorized_keys", None) is False


def test_relative_project_dir_resolves_like_resolve_and_never_fails_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini round 2, F5 (BLOCKER): ``project_dir`` ITSELF may be relative.

    The F1 fix bailed out to ``()`` on ``not os.path.isabs(base)``, so a relative
    scope (``"."``, ``"src/"``) silently SKIPPED rule 5 entirely -- favourable
    absence, failing OPEN on exactly the token the F1 fix exists to catch. It was
    also a disagreement with the module's own ``_resolve``, which has always handled
    a relative scope correctly because ``_realpath`` resolves it against the process
    CWD. The base now comes from ``_project_root`` -- the same expression
    ``_resolve`` uses -- so the two resolution paths cannot diverge."""
    fake_home = tmp_path / "home"
    (fake_home / "sub").mkdir(parents=True)  # no case-variant dirs: no inode aliasing
    monkeypatch.setenv("HOME", str(fake_home))

    # Relative project_dir whose CWD-resolved base IS the home directory.
    monkeypatch.chdir(fake_home)
    for scope in (".", "./", os.curdir):
        for token in (".SSH/authorized_keys", ".Config/gcloud/creds.db"):
            assert references_secret(token, scope) is True, f"{scope} {token}"
            assert write_target_references_secret(token, scope) is True, f"{scope} {token}"
            assert classify_tool("Read", {"file_path": token}, scope) == (
                ActionClass.IRREVERSIBLE
            ), f"{scope} {token}"
    # A relative scope one level down, reached back out with "..".
    assert references_secret("../.SSH/authorized_keys", "sub") is True
    # Benign look-alikes under the same relative scope stay allowed.
    for token in (".sshfoo/bar", "src/.config/gcloudx/y", "notes/todo.txt"):
        assert references_secret(token, ".") is False, token

    # NEGATIVE: a relative scope that does NOT resolve under home is not the store.
    outside = tmp_path / "proj"
    outside.mkdir()
    monkeypatch.chdir(outside)
    for scope in (".", "./"):
        assert references_secret(".SSH/authorized_keys", scope) is False, scope
        assert write_target_references_secret(".SSH/authorized_keys", scope) is False, scope


def test_double_slash_spelling_cannot_bypass_home_containment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini round 1, F2 (MAJOR): POSIX leaves a leading ``//`` implementation-
    defined and ``os.path.normpath`` deliberately PRESERVES it (three or more
    slashes are collapsed, exactly two are not). ``//home/u/.SSH/x`` therefore
    failed the single-slash home prefix test and walked past the guard, while the
    kernel resolves it to the same file."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    project = str(tmp_path / "proj")

    for token in (
        "/" + str(fake_home / ".SSH" / "authorized_keys"),  # exactly two leading slashes
        "//" + str(fake_home / ".SSH" / "authorized_keys"),  # three -> normpath collapses
        "/" + str(fake_home / ".Aws" / "config"),
    ):
        assert token.startswith("//")
        assert references_secret(token, project) is True, token
        assert write_target_references_secret(token, project) is True, token
    assert references_secret("/" + str(fake_home / ".sshfoo" / "bar"), project) is False


def test_root_home_still_guarded_and_stays_segment_precise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini round 1, F3 (MAJOR): with ``HOME=/`` (root-home service accounts,
    some containers) round 1's ``rstrip("/")`` reduced the home candidate to the
    empty string and ``continue``d, silently disabling the check for those
    accounts. A root HOME is honest: every absolute path IS home-rooted then, and
    ``~/.ssh`` genuinely IS ``/.ssh``. Matching stays precise because it is anchored
    on SEGMENTS, so ordinary absolute paths do not become secrets."""
    monkeypatch.setenv("HOME", "/")
    for token in ("/.SSH/authorized_keys", "/.ssh/id_rsa", "/.Config/gcloud/creds.db", "~/.SSH"):
        assert references_secret(token, "/tmp") is True, token
        assert write_target_references_secret(token, "/tmp") is True, token
    for token in (
        "/etc/hosts",
        # NOT the registered store when HOME=/ (that store is ``/.ssh``); the
        # distinctive-basename rule still covers ``/home/u/.ssh/id_rsa`` separately.
        "/home/u/.ssh/config",
        "/usr/local/bin/tool",
        "/.sshfoo/bar",
    ):
        assert references_secret(token, "/tmp") is False, token


def test_backslash_is_a_legal_posix_filename_char_not_a_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini round 1, F4 (MINOR): round 1 rewrote ``\\`` to ``/`` unconditionally,
    but on POSIX a backslash is a LEGAL filename character -- ``~/.ssh\\harmless.txt``
    is one file sitting directly in ``$HOME``, not something inside ``~/.ssh``, and
    the rewrite turned it into a false hard-stop. The rewrite is now applied only
    where ``\\`` really is the separator (Windows). Genuine containment is
    unaffected, including a backslash appearing INSIDE a real secret dir path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    project = str(tmp_path / "proj")

    if os.sep == "/":  # POSIX: a backslash is part of the NAME
        assert references_secret("~/.ssh\\harmless.txt", project) is False
        assert references_secret("~/.SSH\\harmless.txt", project) is False
        assert write_target_references_secret("~/.ssh\\harmless.txt", project) is False
    # Real containment is untouched either way.
    assert references_secret("~/.ssh/harmless.txt", project) is True
    assert references_secret("~/.SSH/sub\\file.txt", project) is True


def test_oos_basenames_hard_stop_only_out_of_project(tmp_path: Path) -> None:
    """fix5 #1/#6: user credential names hard-stop without blocking fixtures."""
    home = os.path.expanduser("~")
    project = str(tmp_path)
    for name in (
        ".netrc",
        ".git-credentials",
        ".npmrc",
        ".pypirc",
        ".terraformrc",
        "credentials",
    ):
        oos = os.path.join(home, name)
        assert classify_shell(f"cat {oos}", project) == ActionClass.IRREVERSIBLE, name
        in_project = os.path.join(project, name)
        assert classify_shell(f"cat {in_project}", project) == ActionClass.READ_ONLY, name
    assert classify_shell("cat app.key", project) == ActionClass.READ_ONLY
    assert classify_shell("cat ~/app.key", project) == ActionClass.IRREVERSIBLE


@pytest.mark.live
def test_live_sandbox_blocks_out_of_registry_home_dotfiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fix5 BLOCKER 1: unknown home dotfiles fail closed at the OS layer."""
    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec unavailable/unproven")

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    names = (
        ".netrc",
        ".git-credentials",
        ".npmrc",
        ".docker/config.json",
        ".kube/config",
        ".config/gh/hosts.yml",
        ".pypirc",
        ".terraformrc",
    )
    files: dict[str, Path] = {}
    for name in names:
        path = fake_home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("TOPSECRET_do_not_leak")
        files[name] = path

    project = tmp_path / "proj"
    project.mkdir()
    os.makedirs(sandbox.claude_tmp_root(), exist_ok=True)
    profiles = {
        "session": sandbox.build_profile(str(project), sandbox.session_write_roots(str(project))),
        "adapter": sandbox.build_profile(str(project), sandbox.adapter_write_roots()),
    }
    for profile_name, profile in profiles.items():
        for name, path in files.items():
            proc = subprocess.run(
                [SBX, "-p", profile, "/bin/cat", str(path)],
                capture_output=True,
                text=True,
                timeout=20,
            )
            assert "TOPSECRET" not in proc.stdout, f"{profile_name} profile leaked {name}"


def test_in_scope_pem_is_not_a_secret_but_oos_pem_is(tmp_path: Path) -> None:
    """fix4 LOW: a .pem inside the project is routine material (auto); an
    out-of-scope .pem read hard-stops."""
    project = str(tmp_path)
    assert classify_shell("cat server.pem", project) == ActionClass.READ_ONLY
    assert (
        classify_tool("Read", {"file_path": "certs/fullchain.pem"}, project)
        == ActionClass.READ_ONLY
    )
    assert classify_shell("cat /etc/ssl/x.pem", project) == ActionClass.IRREVERSIBLE
    assert classify_tool("Read", {"file_path": "~/secret.pem"}, project) == ActionClass.IRREVERSIBLE


def test_unambiguous_secret_suffixes_hard_stop_and_public_certificates_read(
    tmp_path: Path,
) -> None:
    project = str(tmp_path)
    for suffix in (".enc", ".vault", ".secrets", ".creds", ".key-backup", ".db-backup"):
        path = tmp_path / f"fixture{suffix}"
        path.write_text("dummy")
        assert references_secret(str(path), project)
        assert classify_shell(f"cat {path}", project) == ActionClass.IRREVERSIBLE

    for name in ("server.pem", "server.key"):
        path = tmp_path / "certs" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("public fixture")
        assert not references_secret(str(path), project)
        assert classify_shell(f"cat {path}", project) == ActionClass.READ_ONLY


def test_secret_basename_backup_and_archive_compounds_hard_stop(tmp_path: Path) -> None:
    project = str(tmp_path)
    names = (
        "connections.env.bak",
        "connections.env.bak-2024-01",
        "connections.env.backup",
        "connections.env.bak-foo",
        "secrets.tar",
        "secrets.tar.gz",
        "secrets.zip",
        "vault.zip",
        ".env.old",
        ".env.backup",
        "id_rsa.bak",
    )
    for name in names:
        path = tmp_path / name
        path.write_text("dummy")
        assert references_secret(str(path), project), name
        assert classify_shell(f"cat {path}", project) == ActionClass.IRREVERSIBLE, name
        assert (
            classify_tool("Read", {"file_path": str(path)}, project) == ActionClass.IRREVERSIBLE
        ), name


def test_compound_secret_symlink_traversal_and_nonmatching_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    (fake_home / ".ssh" / "ordinary.txt").write_text("dummy")
    monkeypatch.setenv("HOME", str(fake_home))
    dir_link = project / "store-link"
    dir_link.symlink_to(fake_home / ".ssh", target_is_directory=True)
    # The registry's realpath containment catches traversal even when the leaf
    # filename itself is ordinary.
    assert references_secret(str(dir_link / "ordinary.txt"), str(project))

    renamed = project / "connections.env.bak"
    renamed.write_text("dummy")
    assert references_secret(str(renamed), str(project))

    ordinary = project / "fixtures" / "ordinary.txt"
    ordinary.parent.mkdir()
    ordinary.write_text("public fixture")
    assert not references_secret(str(ordinary), str(project))
    assert classify_shell(f"cat {ordinary}", str(project)) == ActionClass.READ_ONLY

    near_miss = project / "id_rsa.bakery"
    near_miss.write_text("public fixture")
    assert not references_secret(str(near_miss), str(project))


def test_write_into_registered_store_hard_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0 (write side): a write INTO a REGISTERED secret store HARD-STOPS even when
    the store's OWN parent is passed AS project_dir -- the exact per-root call the
    hook makes when it re-classifies a granted root AS project_dir (the P3 downgrade
    seam). The store is matched by inode containment, not by the caller's scope, so
    a credential-store write can never be laundered to auto.
    """
    fake_home = tmp_path / "home"
    (fake_home / ".ssh").mkdir(parents=True)
    (fake_home / ".config" / "omni").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    for store_parent, rel in (
        (str(fake_home / ".ssh"), "id_rsa"),
        (str(fake_home / ".config" / "omni"), "connections.env"),
    ):
        for tool in ("Write", "Edit", "MultiEdit"):
            target = os.path.join(store_parent, rel)
            assert classify_tool(tool, {"file_path": target}, store_parent) == (
                ActionClass.IRREVERSIBLE
            ), f"{tool} into the store {target} must hard-stop"
        assert write_target_references_secret(os.path.join(store_parent, rel), store_parent)
    # The repo-relative var/secrets store, caught by the case-insensitive fast-path
    # (covers a relative spelling with an unrelated project scope).
    project = tmp_path / "proj"
    project.mkdir()
    for spelling in ("var/secrets/token", "var/SECRETS/token"):
        assert classify_tool("Write", {"file_path": spelling}, str(project)) == (
            ActionClass.IRREVERSIBLE
        ), spelling


def test_secret_named_write_outside_a_store_is_allowed(tmp_path: Path) -> None:
    """P0 (write side): matching a secret BASENAME anywhere for WRITES over-refused
    ~201 legitimate in-project writes. A write to a secret-NAMED file that is NOT
    inside a registered store now classifies as an ordinary in-scope write
    (reversible-auto); READ protection for the distinctive basenames is unchanged.
    """
    project = str(tmp_path)
    allowed_writes = (
        "dashboard/.env.local",
        "certs/server.pem",
        "app.key",
        "aws/credentials",
        ".git-credentials",
        "deploy/id_rsa",
        "config/secrets",
    )
    for name in allowed_writes:
        target = os.path.join(project, name)
        for tool in ("Write", "Edit", "MultiEdit"):
            assert classify_tool(tool, {"file_path": target}, project) == (
                ActionClass.INTERNAL_REVERSIBLE
            ), f"legit in-project write {name} must not be over-refused"
        assert not write_target_references_secret(target, project), name
    # READ protection for the UNCONDITIONAL distinctive basenames stays a hard-stop.
    for name in ("dashboard/.env.local", "deploy/id_rsa", "config/secrets"):
        assert classify_tool("Read", {"file_path": os.path.join(project, name)}, project) == (
            ActionClass.IRREVERSIBLE
        ), f"read protection for {name} must stay"


def test_oos_scoped_write_still_hard_stops_out_of_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini review round 1, finding 2 (MAJOR): the refactor that produced
    ``test_secret_named_write_outside_a_store_is_allowed`` dropped OOS-scoped
    basename/suffix protection for writes ENTIRELY -- a write to
    ``~/.git-credentials`` or ``~/.npmrc`` under a granted OOS root was no longer
    blocked. Restored ON TOP of (union with, not replacing) the scoped dir check:
    an in-project fixture write (proven allowed above) stays allowed, while an
    OOS-scoped credential-named write -- including the P3 downgrade seam where a
    hook re-classifies a granted OOS root (``~``) AS ``project_dir`` -- still
    hard-stops.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    real_project = fake_home / "proj"
    real_project.mkdir()

    oos_names = (".netrc", ".git-credentials", ".npmrc", ".pypirc", ".terraformrc", "credentials")
    oos_suffixed = ("server.pem", "app.key")

    # Ordinary OOS write: a REAL, unrelated project scope, target sits directly in
    # the user's home -- never "in scope" of the real project either way.
    for name in (*oos_names, *oos_suffixed):
        target = str(fake_home / name)
        for tool in ("Write", "Edit", "MultiEdit"):
            assert classify_tool(tool, {"file_path": target}, str(real_project)) == (
                ActionClass.IRREVERSIBLE
            ), f"OOS write {name} (real project scope) must hard-stop"
        assert write_target_references_secret(target, str(real_project)), name

    # P3 downgrade seam: the hook re-classifies the granted OOS root (~) AS
    # project_dir itself for this per-root write-scope call. A plain
    # ``_within(resolved, project_dir)`` check would WRONGLY say "in scope" here
    # (the file genuinely sits inside the passed project_dir=~) -- this is exactly
    # what the dropped check must catch regardless.
    for name in (*oos_names, *oos_suffixed):
        target = str(fake_home / name)
        assert classify_tool("Write", {"file_path": target}, str(fake_home)) == (
            ActionClass.IRREVERSIBLE
        ), f"P3-laundered OOS write {name} (project_dir=~) must still hard-stop"
        assert write_target_references_secret(target, str(fake_home)), name


def test_oos_write_denied_at_any_nesting_depth_under_laundered_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini review round 2, finding 2 (MAJOR, remaining gap): round 1's version
    of the P3 downgrade-seam check (``_is_direct_child_of_real_home``, since
    replaced by ``_is_under_real_home_outside_repo``) only caught a target
    sitting DIRECTLY in ``~`` -- a NESTED OOS credential under the SAME laundered
    root (``~/.config/.npmrc``, one level deep) still fell through: its parent
    isn't ``~`` itself, and it genuinely IS "within" ``project_dir=~`` once that
    is the laundered claimed scope, so the old check's fallback
    (``not _within(resolved, project_dir)``) wrongly said "in scope". The
    generalized check denies an OOS-scoped write ANYWHERE under the real home
    directory, at any depth, provided it is not ALSO inside a genuine repo
    checkout -- independent of nesting depth and independent of what
    ``project_dir`` claims. Mirrors Gemini's own round-2 repro
    (``write_target_references_secret(~/.config/.npmrc, ~)`` must be True).
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    laundered_project_dir = str(fake_home)  # the P3 downgrade seam: ~ AS project_dir

    for rel in (
        ".npmrc",  # depth 0 (direct child) -- already covered pre-round-2
        os.path.join(".config", ".npmrc"),  # depth 1 -- gemini's round-2 repro shape
        os.path.join(".config", "sub", ".npmrc"),  # depth 2
        os.path.join(".aws", "credentials"),  # a different OOS basename, depth 1
        os.path.join("nested", "deeper", "still", ".netrc"),  # depth 3, another basename
    ):
        target = os.path.join(laundered_project_dir, rel)
        assert write_target_references_secret(target, laundered_project_dir) is True, rel
        assert classify_tool("Write", {"file_path": target}, laundered_project_dir) == (
            ActionClass.IRREVERSIBLE
        ), rel


def test_oos_write_inside_a_genuine_repo_checkout_under_home_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The carve-out in ``_is_under_real_home_outside_repo``: an OOS-basenamed
    write that is home-rooted but ALSO sits inside a genuine omniagentos repo
    checkout (this module's own ``_REPO_ROOT``) is a real, in-scope project
    fixture, not a bare home dotfile, and must stay allowed -- proves the
    round-2 generalization did not turn every home-rooted project into a
    blanket OOS deny. HOME is pointed at the repo's OWN parent directory so the
    real checkout root genuinely sits under "home" for this assertion, exactly
    like a real deployment (e.g. ``~/omniagentos``)."""
    from omniagentos import secret_registry as sr

    monkeypatch.setattr(sr, "_MAIN_WORKTREE_ROOT", None)
    monkeypatch.setenv("HOME", os.path.dirname(_REPO_ROOT))
    target = os.path.join(_REPO_ROOT, "aws", "credentials")
    assert sr.write_target_references_secret(target, _REPO_ROOT) is False


def test_case_variant_var_secrets_reference_hard_stops(tmp_path: Path) -> None:
    """P0 fix1: rule 2 is case-FOLDED, so a case-variant spelling of the repo secret
    store cannot bypass containment on a case-insensitive filesystem. The live P0 --
    with no project scope, ``var/SECRETS/token`` slipped past the case-sensitive
    literal and the reviewer read+wrote+hashed the real session token."""
    for spelling in ("var/SECRETS/token", "VAR/SECRETS/token", "var/Secrets/token"):
        assert references_secret(spelling, None), spelling
        assert classify_shell(f"cat {spelling}", str(tmp_path)) == ActionClass.IRREVERSIBLE, (
            spelling
        )


def test_casefold_path_uses_unicode_casefold_not_lower(tmp_path: Path) -> None:
    """Gemini review round 1, finding 4 (MINOR): ``_casefold_path`` used
    ``str.lower()``, which is not the same operation as filesystem-caseless
    matching on APFS -- ``str.casefold()`` additionally maps several Unicode
    multi-character case-fold expansions (e.g. German ``ẞ``/``ß`` both to
    ``ss``) that ``.lower()`` leaves distinct, a collision ``.lower()`` alone
    would miss."""
    import sys

    for path in (
        "var/SECRETS/token",
        "var/secrets/STRASSE_ẞ",  # LATIN CAPITAL LETTER SHARP S (ẞ)
        "var/ẞecrets/token",
    ):
        if sys.platform == "darwin":
            assert _casefold_path(path) == os.path.normcase(path).casefold(), path
        else:
            assert _casefold_path(path) == os.path.normcase(path), path
    if sys.platform == "darwin":
        # The exact collision plain .lower() misses: 'ẞ'.lower() == 'ß' (still a
        # single, DIFFERENT character), while 'ẞ'.casefold() == 'ss' -- so only
        # casefold() maps "STRASSE_ẞ" and "strasse_ss" to the identical string.
        assert "ẞ".lower() != "ss"
        assert "ẞ".casefold() == "ss"
        assert _casefold_path("var/secrets/STRASSE_ẞ") == _casefold_path("var/secrets/strasse_ss")


def test_parent_rename_of_secret_ancestor_is_denied(tmp_path: Path) -> None:
    """P0 fix7 (the load-bearing new piece): renaming an ANCESTOR of a registered
    secret dir relocates the store past every path-based deny, so ``mv var var2``
    (and moving the store itself) must HARD-STOP in the classifier, while an
    unrelated in-project rename stays reversible-auto."""
    repo = _REPO_ROOT
    assert path_relocates_secret_dir("var", repo) is True
    assert path_relocates_secret_dir("src", repo) is False
    assert classify_shell("mv var var2", repo) == ActionClass.IRREVERSIBLE
    assert classify_shell("mv ./var ./var2", repo) == ActionClass.IRREVERSIBLE
    assert classify_shell("mv var/secrets /tmp/x", repo) == ActionClass.IRREVERSIBLE
    assert classify_shell("mv -f var var2", repo) == ActionClass.IRREVERSIBLE
    # An unrelated rename inside the repo is not touched by the ancestor guard.
    assert classify_shell("mv src/a.py src/b.py", repo) == ActionClass.INTERNAL_REVERSIBLE


def test_non_mv_relocation_spellings_are_also_denied(tmp_path: Path) -> None:
    """Gemini review round 1, finding 3 (MAJOR): the ancestor-rename guard only
    recognized literal ``mv``, so ``git mv``, ``rename``, and
    ``rsync --remove-source-files`` fell through fail-open. Every relocation
    spelling that would move ``<repo>/var`` (an ancestor of the registered
    ``<repo>/var/secrets`` store) past the path-based deny must hard-stop, exactly
    like plain ``mv var var2``; an unrelated relocation inside the repo, and a
    plain (non-removing) rsync copy, stay reversible-auto / read-only."""
    repo = _REPO_ROOT
    for cmd in (
        "git mv var var2",
        "git -C . mv var var2",
        "git mv -f var var2",
        "rename var var2 var",
        "rsync --remove-source-files -a var/ var2/",
        "rsync -a --remove-source-files var/ var2/",
    ):
        assert classify_shell(cmd, repo) == ActionClass.IRREVERSIBLE, cmd
    # An unrelated relocation inside the repo is not touched by the ancestor guard.
    assert classify_shell("git mv src/a.py src/b.py", repo) == ActionClass.INTERNAL_REVERSIBLE
    # A plain rsync copy (no --remove-source-files) never deletes its source, so the
    # ancestor-rename guard specifically must not fire for it (a plain `cp`-style
    # copy is unaffected -- unlike `mv`/`git mv`/`rename`/`rsync --remove-source-
    # files`, nothing is moved past the path-based deny).
    from omniagentos.policy.shell import _move_relocates_secret

    assert _move_relocates_secret([["rsync", "-a", "var/", "var2/"]], repo) is False
    assert _move_relocates_secret([["cp", "-r", "var", "var2"]], repo) is False


@pytest.mark.live
def test_live_sandbox_blocks_store_write_and_parent_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0 fix4/fix7 (live): a real sandbox-exec run cannot write INTO the repo secret
    store, cannot overwrite the token, and cannot RENAME an ancestor (``mv var var2``)
    to relocate it -- while an ordinary in-workspace write and a sibling write under
    ``var/`` still succeed. The parent-rename bypass is physically closed."""
    import subprocess

    from omniagentos import secret_registry as sr

    if not sandbox.sandbox_available():
        pytest.skip("sandbox-exec unavailable/unproven; classifier is the guarantee")

    repo = tmp_path / "repo"
    (repo / "var" / "secrets").mkdir(parents=True)
    (repo / "var" / "secrets" / "token").write_text("SUPERSECRET_sk_live")
    (repo / "var" / "sessions").mkdir(parents=True)
    (repo / "src").mkdir()
    monkeypatch.setattr(sr, "_REPO_ROOT", repo)
    monkeypatch.setattr(sr, "_MAIN_WORKTREE_ROOT", None)
    profile = sandbox.build_profile(str(repo), [str(repo)])

    def run(script: str) -> None:
        subprocess.run(
            [SBX, "-p", profile, "/bin/sh", "-c", script], capture_output=True, timeout=20
        )

    keeper = repo / "src" / "ok.txt"
    run(f'echo x > "{keeper}"')
    assert keeper.exists(), "ordinary in-workspace write must still succeed"
    run(f'echo x > "{repo / "var" / "secrets" / "planted"}"')
    assert not (repo / "var" / "secrets" / "planted").exists(), "write into store must be denied"
    run(f'echo HACKED > "{repo / "var" / "secrets" / "token"}"')
    assert (repo / "var" / "secrets" / "token").read_text() == "SUPERSECRET_sk_live"
    run(f'mv "{repo / "var"}" "{repo / "var2"}"')
    assert (repo / "var").exists() and not (repo / "var2").exists(), (
        "ancestor rename must be denied"
    )
    run(f'echo x > "{repo / "var" / "sessions" / "s.txt"}"')
    assert (repo / "var" / "sessions" / "s.txt").exists(), (
        "sibling write under var/ must be allowed"
    )


def test_sandbox_write_denies_secret_dirs_and_ancestors() -> None:
    """P0 fix4/fix7: every registered secret dir is WRITE-denied (not only read-
    denied) in the session AND adapter profiles, and each in-workspace ancestor of a
    nested store gets a LITERAL write-deny so a parent rename cannot relocate it."""
    repo = _REPO_ROOT
    session = sandbox.build_profile(repo, sandbox.session_write_roots(repo))
    adapter = sandbox.build_profile(repo, sandbox.adapter_write_roots())
    for secret_dir in sandbox.secret_read_deny_roots():
        needle = f'(deny file-write* (subpath "{secret_dir}"))'
        assert needle in session, f"session profile does not write-deny {secret_dir}"
        assert needle in adapter, f"adapter profile does not write-deny {secret_dir}"
    _subpaths, ancestors = sandbox.secret_store_write_deny_targets(
        sandbox.session_write_roots(repo)
    )
    assert ancestors, "expected at least one in-workspace ancestor literal (<repo>/var)"
    for ancestor in ancestors:
        assert f'(deny file-write* (literal "{ancestor}"))' in session, ancestor


def test_sandbox_ancestor_deny_present_when_store_absent_at_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gemini review round 1, finding 1 (BLOCKER): a REGISTERED secret dir that
    does not exist yet at sandbox-BUILD time must still contribute its
    ancestor-rename deny -- omitting it would fail OPEN (``mv var var2`` could
    relocate a not-yet-created ``<repo>/var/secrets`` past every path-based deny
    the moment it later comes into existence). Neither ``<repo>/var`` NOR
    ``<repo>/var/secrets`` exists at call time here."""
    from omniagentos import secret_registry as sr

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    monkeypatch.setattr(sr, "_REPO_ROOT", repo)
    monkeypatch.setattr(sr, "_MAIN_WORKTREE_ROOT", None)
    assert not (repo / "var").exists()
    assert not (repo / "var" / "secrets").exists()

    subpaths, ancestors = sandbox.secret_store_write_deny_targets([str(repo)])
    assert os.path.realpath(str(repo / "var" / "secrets")) in subpaths
    assert os.path.realpath(str(repo / "var")) in ancestors, (
        "ancestor deny silently omitted for a secret dir absent at build time"
    )

    # The literal string-based fallback (:func:`omniagentos.runner.sandbox.
    # _literal_relative_parts`) is what makes this deterministic even when inode
    # resolution of the (nonexistent) candidate is inconclusive for any reason --
    # exercise it directly, independent of real filesystem state.
    from omniagentos.runner.sandbox import _literal_relative_parts

    assert _literal_relative_parts("/a/b/var/secrets", "/a/b") == ("var", "secrets")
    assert _literal_relative_parts("/a/b/var/secrets", "/x/y") is None
    assert _literal_relative_parts("/a/b", "/a/b") == ()


def test_ordinary_in_project_write_still_auto_approves(tmp_path: Path) -> None:
    """FIX 3 does not over-block: a NON-secret in-project write stays reversible-auto,
    and an ordinary out-of-scope write still hard-stops (unchanged)."""
    project = str(tmp_path)
    assert (
        classify_tool("Write", {"file_path": os.path.join(project, "notes.txt")}, project)
        == ActionClass.INTERNAL_REVERSIBLE
    )
    assert classify_tool("Write", {"file_path": "/etc/pwned"}, project) == ActionClass.IRREVERSIBLE
