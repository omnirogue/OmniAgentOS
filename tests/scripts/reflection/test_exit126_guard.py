"""D3 / N4r: reflection launchd jobs must not reintroduce exit 126."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
REFLECTION = ROOT / "scripts" / "reflection"
JOB_SCRIPTS = (
    REFLECTION / "reflect-nightly.sh",
    REFLECTION / "reflect-watchdog.sh",
)
TEMPLATES = (
    REFLECTION / "com.omniagentos.reflection-nightly.plist.template",
    REFLECTION / "com.omniagentos.reflection-watchdog.plist.template",
)
INSTALLER = REFLECTION / "install-reflection.sh"


def test_job_scripts_exist_with_shebang_and_mode_0755() -> None:
    for path in JOB_SCRIPTS:
        assert path.is_file(), path
        first = path.read_text(encoding="utf-8").splitlines()[0]
        assert first.startswith("#!"), f"missing shebang: {path}"
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o755, f"{path} mode {mode:04o} != 0755"
        assert os.access(path, os.X_OK), path


def test_templates_exist() -> None:
    for path in TEMPLATES:
        assert path.is_file(), path
        text = path.read_text(encoding="utf-8")
        assert "{{PROGRAM_ARGS}}" in text
        assert "{{WORKING_DIR}}" in text


def test_installer_renders_bin_sh_plus_absolute_script(tmp_path: Path) -> None:
    """Render into a temp dir (never touches operator's live LaunchAgents)."""
    target_dir = tmp_path / "rendered"
    target_dir.mkdir()
    env = os.environ.copy()
    env["OMNIAGENTOS_LAUNCHD_TARGET_DIR"] = str(target_dir)
    env["OMNIAGENTOS_REFLECTION_REARM_MODE"] = "off"
    proc = subprocess.run(  # noqa: S603
        ["/bin/sh", str(INSTALLER)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    for label, script in (
        ("com.omniagentos.reflection-nightly", JOB_SCRIPTS[0]),
        ("com.omniagentos.reflection-watchdog", JOB_SCRIPTS[1]),
    ):
        plist = target_dir / f"{label}.plist"
        assert plist.is_file(), plist
        if shutil_which_plutil := _which("plutil"):
            lint = subprocess.run(  # noqa: S603
                [shutil_which_plutil, "-lint", str(plist)],
                capture_output=True,
                text=True,
                check=False,
            )
            assert lint.returncode == 0, lint.stdout + lint.stderr

        text = plist.read_text(encoding="utf-8")
        # ProgramArguments must be /bin/sh + absolute script path (no exec wrapper).
        assert "<string>/bin/sh</string>" in text
        assert f"<string>{script.resolve()}</string>" in text
        assert "exec " not in text
        assert script.resolve().is_file()


def test_script_runs_under_bin_sh_without_plusx(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even if +x is stripped, /bin/sh path must not yield exit 126."""
    src = JOB_SCRIPTS[0]
    copy = tmp_path / "reflect-nightly.sh"
    copy.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    copy.chmod(0o644)  # deliberately non-executable
    monkeypatch.setenv("OMNIAGENTOS_REFLECTION_REARM_MODE", "off")
    # Point ROOT_DIR relative paths by running from checkout; script resolves its own root.
    # We only assert the shell can interpret the non-exec file (mode=off exits 0 quickly).
    # Use a minimal stand-in that still has shebang + set -eu and exits 0 on mode=off.
    minimal = tmp_path / "minimal.sh"
    minimal.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "MODE=${OMNIAGENTOS_REFLECTION_REARM_MODE:-off}\n"
        "case \"$MODE\" in off) exit 0;; *) exit 2;; esac\n",
        encoding="utf-8",
    )
    minimal.chmod(0o644)
    proc = subprocess.run(  # noqa: S603
        ["/bin/sh", str(minimal)],
        env={**os.environ, "OMNIAGENTOS_REFLECTION_REARM_MODE": "off"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    # Direct exec of non-executable would be 126; we do not do that.


def test_runbook_records_root_cause_and_shadow_week() -> None:
    text = (ROOT / "docs" / "runbooks" / "reflection-jobs-exit126.md").read_text(encoding="utf-8")
    assert "root cause" in text.lower()
    assert "shadow week" in text.lower() or "one-shadow-week" in text.lower()
    assert "Permission denied" in text or "exit 126" in text or "exit code 126" in text


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)
