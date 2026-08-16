from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WATCHDOG = ROOT / "scripts/gates/agent_watchdog.sh"


@pytest.mark.parametrize("etime", ["12-34", "1-2:03", "1-02:03"])
def test_etime_rejects_partial_day_shapes(etime: str) -> None:
    """A day prefix is valid only with the complete HH:MM:SS suffix."""
    result = subprocess.run(
        [str(WATCHDOG), "--etime-seconds", etime],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "unparseable etime" in result.stderr


@pytest.mark.parametrize("etime", ["1:60", "1:2:60", "1-24:00:00", "1-02:60:00"])
def test_etime_rejects_out_of_range_components(etime: str) -> None:
    """Malformed ps output must not be reinterpreted as a real process age."""
    result = subprocess.run(
        [str(WATCHDOG), "--etime-seconds", etime],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "unparseable etime" in result.stderr
