"""Security probes for the planner-canary launchd installer."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / "scripts" / "gates" / "install-planner-canary.sh"


def test_planner_canary_label_cannot_escape_render_directory(tmp_path: Path) -> None:
    """An environment label must not become a launchd output path traversal."""
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
            "OMNIAGENTOS_PLANNER_CANARY_LABEL": "../escaped",
            "PATH": f"{fake_python.parent}{os.pathsep}{env['PATH']}",
        }
    )

    result = subprocess.run(
        ["sh", str(INSTALLER)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    # Assert the refusal is ABOUT THE LABEL, not its exact wording. This test
    # originally pinned "unexpected planner-canary launchd label", the message
    # of a per-installer literal comparison. The guard is now the shared
    # shape validator in scripts/lib/launchd-label.sh, which refuses this input
    # for the more general reason (it contains "/") and says so differently.
    # The property under test -- an environment label cannot become a path
    # traversal -- is unchanged and is what the two assertions around this one
    # actually check.
    assert "launchd label" in result.stderr.lower(), result.stderr
    assert not escaped_target.exists()
