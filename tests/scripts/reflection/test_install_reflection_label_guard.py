"""The reflection installer must refuse a launchd label that escapes the render dir.

`install-reflection.sh` takes TWO labels from the environment
(`OMNIAGENTOS_REFLECTION_NIGHTLY_LABEL`, `OMNIAGENTOS_REFLECTION_WATCHDOG_LABEL`)
and interpolates each into `${TARGET_DIR}/${LABEL}.plist`. Without a shape guard a
`../` label writes the plist outside the render directory — and the installer then
prints a `launchctl bootstrap` line pointing at the escaped path. See issue #95;
this is the eighth instance of the class swept in #21, which missed this file.

Every refusal case below is asserted BEFORE the installer reaches its interpreter
check, so these tests cannot pass or fail because of a host property.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALLER = ROOT / "scripts" / "reflection" / "install-reflection.sh"

NIGHTLY_VAR = "OMNIAGENTOS_REFLECTION_NIGHTLY_LABEL"
WATCHDOG_VAR = "OMNIAGENTOS_REFLECTION_WATCHDOG_LABEL"

# Both labels are equally unguarded, so every case is run against each variable.
LABEL_VARS = (NIGHTLY_VAR, WATCHDOG_VAR)

HOSTILE_LABELS = (
    "../../escaped-proof",  # the traversal in issue #95's reproduction
    "../sibling",
    "sub/dir/nested",  # no traversal, but still leaves the render dir
    "/tmp/absolute-escape",
    ".hidden",  # leading dot: a dotfile, and the first half of `..`
    "..",
    "com.foo;rm -rf x",  # shell metacharacters
    "com.foo\nbar",  # a newline is a control character
)


def _run_installer(tmp_path: Path, var: str, label: str) -> subprocess.CompletedProcess[str]:
    """Render into a nested temp dir so any `../../` escape stays inside tmp_path."""
    target_dir = tmp_path / "rendered" / "a" / "b"
    target_dir.mkdir(parents=True)
    env = os.environ.copy()
    env["OMNIAGENTOS_LAUNCHD_TARGET_DIR"] = str(target_dir)
    env["OMNIAGENTOS_REFLECTION_REARM_MODE"] = "off"
    env[var] = label
    return subprocess.run(  # noqa: S603
        ["/bin/sh", str(INSTALLER)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _escaped_files(tmp_path: Path) -> list[str]:
    """Anything written under rendered/ but outside the declared target dir."""
    rendered = tmp_path / "rendered"
    target_dir = rendered / "a" / "b"
    return sorted(
        str(p.relative_to(rendered))
        for p in rendered.rglob("*")
        if p.is_file() and target_dir not in p.parents and p.parent != target_dir
    )


@pytest.mark.parametrize("var", LABEL_VARS)
@pytest.mark.parametrize("label", HOSTILE_LABELS)
def test_installer_refuses_a_label_that_leaves_the_render_dir(
    tmp_path: Path, var: str, label: str
) -> None:
    proc = _run_installer(tmp_path, var, label)

    assert proc.returncode != 0, (
        f"{var}={label!r} was ACCEPTED (rc=0)\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    # Refused for the shape of the label, not for some incidental failure.
    assert "launchd label" in proc.stderr, proc.stderr
    assert _escaped_files(tmp_path) == [], (
        f"{var}={label!r} wrote outside the target dir: {_escaped_files(tmp_path)}"
    )


@pytest.mark.parametrize("var", LABEL_VARS)
def test_an_empty_label_falls_back_to_the_default_rather_than_rendering_bare(
    tmp_path: Path, var: str
) -> None:
    """`${VAR:-default}` substitutes on empty as well as unset — so an empty label is
    indistinguishable from "not set" and correctly renders the default, NOT a bare
    `.plist`. The guard still rejects '' defensively for any future caller that
    passes a label directly, but that branch is unreachable through the environment.
    """
    proc = _run_installer(tmp_path, var, "")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    target_dir = tmp_path / "rendered" / "a" / "b"
    assert not (target_dir / ".plist").exists(), "rendered a bare `.plist`"
    default = {
        NIGHTLY_VAR: "com.omniagentos.reflection-nightly",
        WATCHDOG_VAR: "com.omniagentos.reflection-watchdog",
    }[var]
    assert (target_dir / f"{default}.plist").is_file(), proc.stdout
    assert _escaped_files(tmp_path) == []


@pytest.mark.parametrize("var", LABEL_VARS)
def test_a_legitimate_custom_label_is_still_accepted(tmp_path: Path, var: str) -> None:
    """The guard must refuse the shape, not remove the documented override."""
    label = "com.omniagentos.reflection-custom_1"
    proc = _run_installer(tmp_path, var, label)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (tmp_path / "rendered" / "a" / "b" / f"{label}.plist").is_file(), proc.stdout
    assert _escaped_files(tmp_path) == []
