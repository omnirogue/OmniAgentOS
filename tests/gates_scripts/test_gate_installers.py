"""Security probes for the gate launchd installers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALL_AGENT_WATCHDOG = ROOT / "scripts" / "gates" / "install-agent-watchdog.sh"


def test_agent_watchdog_label_cannot_escape_render_directory(tmp_path: Path) -> None:
    """A label supplied by the environment must not become a path traversal."""
    var_root = tmp_path / "var"
    render_dir = var_root / "launchd" / "rendered"
    escaped_target = var_root / "launchd" / "escaped.plist"
    fake_python = tmp_path / "bin" / "python3"
    fake_python.parent.mkdir()
    fake_python.write_text(
        f"#!{sys.executable}\n"
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[3]).write_text('rendered')\n"
    )
    fake_python.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "OMNIAGENTOS_VAR_DIR": str(var_root),
            "OMNIAGENTOS_LAUNCHD_TARGET_DIR": str(render_dir),
            "OMNIAGENTOS_AGENT_WATCHDOG_LABEL": "../escaped",
            "PATH": f"{fake_python.parent}{os.pathsep}{env['PATH']}",
        }
    )

    result = subprocess.run(
        ["sh", str(INSTALL_AGENT_WATCHDOG)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert not escaped_target.exists()
