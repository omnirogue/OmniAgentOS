"""The deployed planner-canary script must delegate to its tested implementation."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRYPOINT = ROOT / "scripts" / "gates" / "planner_canary.py"


def test_planner_canary_entrypoint_propagates_implementation_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launchd-facing wrapper must invoke and propagate the real canary."""
    calls: list[str] = []

    implementation = ModuleType("omniagentos.gates.planner_canary")

    def fake_main() -> int:
        calls.append("called")
        return 17

    implementation.__dict__["main"] = fake_main
    monkeypatch.setitem(sys.modules, implementation.__name__, implementation)

    with pytest.raises(SystemExit) as raised:
        runpy.run_path(str(ENTRYPOINT), run_name="__main__")

    assert calls == ["called"]
    assert raised.value.code == 17
