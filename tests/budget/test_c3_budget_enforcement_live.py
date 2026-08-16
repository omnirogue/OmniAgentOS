"""C3-budget-env: verify budget enforcement is active on live daemons.

This tests the fix for C3-budget-env: when a process sources launch-env.sh,
OMNIAGENTOS_BUDGET_ENFORCEMENT is set to 'block', activating terminal budget limits.

The mechanism in omniagentos/budget/policy.py is simple:
  * ENFORCEMENT_ENV = "OMNIAGENTOS_BUDGET_ENFORCEMENT"
  * BLOCK = "block" (the literal string)
  * enforcement_mode() reads the env var and returns BLOCK or ADVISORY
  * blocks() calls enforcement_mode() and returns True only when set to BLOCK

This test verifies:
  1. The literal value set in launch-env.sh is exactly "block"
  2. When sourced, launch-env.sh exports OMNIAGENTOS_BUDGET_ENFORCEMENT=block
  3. The policy module interprets this correctly
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omniagentos.budget.policy import ENFORCEMENT_ENV, blocks, enforcement_mode


class TestLaunchEnvBudgetEnforcement:
    """Verify budget enforcement is configured in launch-env.sh."""

    def test_launch_env_has_budget_enforcement_setting(self) -> None:
        """Verify launch-env.sh defines OMNIAGENTOS_BUDGET_ENFORCEMENT=block."""
        # Find launch-env.sh relative to the repo root
        launch_env_path = (
            Path(__file__).resolve().parent.parent.parent / "scripts" / "launch-env.sh"
        )
        assert launch_env_path.exists(), f"launch-env.sh not found at {launch_env_path}"

        content = launch_env_path.read_text(encoding="utf-8")

        # Verify the setting line exists
        assert (
            'OMNIAGENTOS_BUDGET_ENFORCEMENT:=block' in content
        ), "launch-env.sh must set OMNIAGENTOS_BUDGET_ENFORCEMENT:=block"

        # Verify it's exported
        assert (
            "export OMNIAGENTOS_BUDGET_ENFORCEMENT" in content
        ), "launch-env.sh must export OMNIAGENTOS_BUDGET_ENFORCEMENT"

    def test_sourcing_launch_env_enables_enforcement(self, tmp_path: Path) -> None:
        """Verify sourcing launch-env.sh sets the env var to 'block'."""
        # Create a test script that sources launch-env.sh and echoes the value
        repo_root = Path(__file__).resolve().parent.parent.parent
        test_script = tmp_path / "test_launch_env.sh"
        test_script.write_text(
            f"""#!/bin/bash
set -e
export OMNIAGENTOS_SIM_MODE=  # Ensure not in simulation mode
cd "{repo_root}"
. ./scripts/launch-env.sh
echo "$OMNIAGENTOS_BUDGET_ENFORCEMENT"
""",
            encoding="utf-8",
        )
        test_script.chmod(0o755)

        # tests/conftest.py's session-scoped `_budget_enforcement_advisory_by_default`
        # fixture pins OMNIAGENTOS_BUDGET_ENFORCEMENT=advisory in THIS process's
        # os.environ for the whole pytest session (so production paths a test may
        # enter answer deterministically regardless of the operator shell's launch
        # posture). subprocess.run() inherits os.environ by default, so the child
        # bash script would see that advisory pin already set and launch-env.sh's
        # `: "${OMNIAGENTOS_BUDGET_ENFORCEMENT:=block}"` default-assignment is a
        # no-op against an already-set var — the child never reaches "block". This
        # test is specifically about a *live* daemon's environment, which never has
        # that pytest-only advisory var, so scrub it from the child's env instead of
        # touching the fixture (test-only var; the fixture itself is out of scope).
        child_env = dict(os.environ)
        child_env.pop(ENFORCEMENT_ENV, None)

        result = subprocess.run(
            ["bash", str(test_script)],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=10,
            env=child_env,
        )

        assert result.returncode == 0, f"Script failed: {result.stderr}"
        output = result.stdout.strip()
        assert output == "block", (
            f"OMNIAGENTOS_BUDGET_ENFORCEMENT should be 'block', got '{output}'"
        )

    def test_enforcement_mode_with_block_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the policy module interprets OMNIAGENTOS_BUDGET_ENFORCEMENT=block."""
        monkeypatch.setenv(ENFORCEMENT_ENV, "block")
        assert (
            enforcement_mode() == "block"
        ), "enforcement_mode() should return 'block' when env is set to 'block'"
        assert blocks() is True, "blocks() should return True when enforcement is 'block'"

    def test_enforcement_blocks_work(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify blocks() reflects the live launch-env setting."""
        # Simulate the live daemon environment
        monkeypatch.setenv(ENFORCEMENT_ENV, "block")
        assert blocks() is True

    def test_enforcement_advisory_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify the fallback: unset defaults to advisory."""
        monkeypatch.delenv(ENFORCEMENT_ENV, raising=False)
        assert (
            enforcement_mode() == "advisory"
        ), "enforcement_mode() should return 'advisory' when env is unset"
        assert blocks() is False, "blocks() should return False when unset"
