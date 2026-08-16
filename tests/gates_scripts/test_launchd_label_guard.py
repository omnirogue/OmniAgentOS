"""Every launchd installer must refuse a label that escapes its render directory.

This test DISCOVERS its subjects by globbing rather than listing them. That is
the point: the traversal shape was fixed one file at a time in #17 and #20 and
survived both rounds, because a hand-written list stops covering the next
installer somebody adds. A test that enumerates files has the same failure mode
as the denylist it is testing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"

# A label reaching a path is the whole risk, so the payloads are the ones that
# leave the directory -- plus the shapes an enumerated denylist misses.
ESCAPING_LABELS = [
    "../../../../tmp/escaped",
    "..",
    "...",  # the variant that defeated the swarm ownership check (#31)
    "/absolute",
    ".hidden",
    "with/slash",
    "spaced label",
]


_LABEL_NAME = r"(?:[A-Z][A-Z0-9]*_)?LABEL(?:_[A-Z][A-Z0-9_]*)?"
_LABEL_PLIST_PATH = re.compile(r"(?:\$\{[A-Za-z_][A-Za-z0-9_]*\}|\$[A-Za-z_][A-Za-z0-9_]*)\.plist")
_LABEL_ASSIGNMENT = re.compile(
    r"""
    ^\s*(?:export\s+|readonly\s+)?(?:[A-Z][A-Z0-9]*_)?LABEL(?:_[A-Z][A-Z0-9_]*)?\s*=\s*
    (?P<quote>[\"'])?\$\{\s*(?P<env>[A-Za-z_][A-Za-z0-9_]*)\s*:-.*?\}
    (?(quote)(?P=quote))
    \s*(?:\#.*)?$
    """,
    re.VERBOSE,
)
_LABEL_ASSIGNMENT_START = re.compile(
    rf"^\s*(?:export\s+|readonly\s+)?{_LABEL_NAME}\s*="
)
_HARDCODED_LABEL_ASSIGNMENT = re.compile(
    rf"^\s*(?:export\s+|readonly\s+)?{_LABEL_NAME}\s*=\s*"
    r"(?:'[^']*'|\"[^\"]*\"|[^$'\"\s#;]+)\s*(?:\#.*)?$"
)


def _executable_lines(script: Path) -> list[str]:
    """Return non-comment lines, sufficient for this intentionally small shell scan."""
    return [
        line
        for line in script.read_text(encoding="utf-8", errors="replace").splitlines()
        if not line.lstrip().startswith("#")
    ]


def _installers() -> list[Path]:
    found = sorted(
        p
        for p in SCRIPTS.rglob("*.sh")
        if _builds_launchd_plist(_executable_lines(p))
    )
    assert found, "no launchd plist writers discovered -- the content matcher is wrong, not the repo"
    return found


def _builds_launchd_plist(lines: list[str]) -> bool:
    """Recognize the actual target-writing shapes used by launchd scripts."""
    return any(
        _LABEL_PLIST_PATH.search(line)
        or ("TARGET_DIR" in line and ".plist" in line)
        or (".plist" in line and ".write_text(" in line)
        for line in lines
    )


def _label_env_var(script: Path) -> str | None:
    """The env var this installer reads its label from, if it takes one."""
    unparsable_assignments: list[str] = []
    has_hardcoded_label = False
    for line in _executable_lines(script):
        match = _LABEL_ASSIGNMENT.match(line)
        if match:
            return match.group("env")
        if _LABEL_ASSIGNMENT_START.match(line):
            if _HARDCODED_LABEL_ASSIGNMENT.match(line):
                has_hardcoded_label = True
            else:
                unparsable_assignments.append(line.strip())

    if unparsable_assignments:
        try:
            display_name = script.relative_to(REPO)
        except ValueError:
            display_name = script
        raise AssertionError(
            f"{display_name} assigns a launchd label but its configurable label environment "
            f"variable could not be parsed: {unparsable_assignments!r}"
        )
    if has_hardcoded_label:
        return None
    return None


@pytest.mark.parametrize("script", _installers(), ids=lambda p: str(p.relative_to(REPO)))
@pytest.mark.parametrize("label", ESCAPING_LABELS)
def test_installer_refuses_an_escaping_label(script: Path, label: str, tmp_path: Path) -> None:
    var = _label_env_var(script)
    if var is None:
        pytest.skip(f"{script.name} does not take a label from the environment")

    # Point every write at a scratch directory so a REGRESSION cannot damage the
    # real LaunchAgents dir while proving that it would have.
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(tmp_path),
        "OMNIAGENTOS_VAR_DIR": str(tmp_path / "var"),
        "OMNIAGENTOS_LAUNCHD_TARGET_DIR": str(tmp_path / "rendered"),
        var: label,
    }
    result = subprocess.run(
        ["sh", str(script)], cwd=REPO, env=env, capture_output=True, text=True, check=False
    )

    assert result.returncode != 0, (
        f"{script.relative_to(REPO)} accepted label {label!r}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    # Refusing for the RIGHT reason: a script that dies on an unrelated error
    # would pass a bare non-zero assertion and prove nothing.
    assert "launchd label" in (result.stdout + result.stderr).lower(), (
        f"{script.relative_to(REPO)} exited non-zero for {label!r} but not because of the "
        f"label\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_a_normal_label_is_still_accepted_by_the_validator() -> None:
    """The control: the guard must not simply refuse everything."""
    lib = SCRIPTS / "lib" / "launchd-label.sh"
    probe = f'. "{lib}"; require_safe_launchd_label "com.omniagentos.agent-watchdog"'
    result = subprocess.run(["sh", "-c", probe], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


def test_installer_discovery_includes_curator_runner() -> None:
    assert SCRIPTS / "curator" / "run.sh" in _installers()


def test_validator_refuses_a_trailing_newline() -> None:
    lib = SCRIPTS / "lib" / "launchd-label.sh"
    probe = f'. "{lib}"; require_safe_launchd_label "$1"'
    result = subprocess.run(
        ["sh", "-c", probe, "launchd-label-probe", "com.omniagentos.agent-watchdog\n"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "launchd label" in result.stderr.lower()


@pytest.mark.parametrize(
    ("assignment", "expected"),
    [
        ("LABEL=${ONE:-com.example.one}", "ONE"),
        ('LABEL = "${TWO:-com.example.two}"', "TWO"),
        (" readonly LABEL = '${THREE:-com.example.three}' # configurable", "THREE"),
        ("LABEL_NIGHTLY=${FOUR:-com.example.four}", "FOUR"),
    ],
)
def test_label_env_var_parses_quoting_and_spacing(
    tmp_path: Path, assignment: str, expected: str
) -> None:
    script = tmp_path / "installer.sh"
    script.write_text(f"# LABEL=${{IGNORED:-comment}}\n{assignment}\n", encoding="utf-8")
    assert _label_env_var(script) == expected


def test_label_env_var_skips_only_a_hardcoded_label(tmp_path: Path) -> None:
    script = tmp_path / "installer.sh"
    script.write_text("LABEL='com.example.hardcoded'\n", encoding="utf-8")
    assert _label_env_var(script) is None


def test_label_env_var_fails_loudly_for_an_unparseable_assignment(tmp_path: Path) -> None:
    script = tmp_path / "installer.sh"
    script.write_text("LABEL=${UNTERMINATED:-com.example\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"installer\.sh assigns a launchd label"):
        _label_env_var(script)
