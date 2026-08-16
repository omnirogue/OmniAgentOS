"""Budgets are task-sized and advisory: they inform, they do not stop work.

the operator's directive (2026-07-24): *"raise the budget limit or remove it, it should be
task dependent but we should not have hard blockers."*

Four places used to turn an overshoot into a stop, and each is covered here:
the provider CLI cap (`--max-budget-usd`, the source of Claude Code's "Budget
limit reached ($X of $Y)"), the runner's run-level fail, the swarm scheduler's
block-every-remaining-task, and the session reaper's kill. ``block`` mode must
still restore every one of them, or the escape hatch is a lie.
"""

from __future__ import annotations

import pytest

from omniagentos.budget.policy import (
    ADVISORY,
    BLOCK,
    DEFAULT_PER_TASK_USD,
    ENFORCEMENT_ENV,
    MIN_RUN_BUDGET_USD,
    blocks,
    enforcement_mode,
    session_budget_cap,
    swarm_run_budget,
)


class TestEnforcementMode:
    def test_advisory_is_the_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENFORCEMENT_ENV, raising=False)
        assert enforcement_mode() == ADVISORY
        assert blocks() is False

    def test_block_is_opt_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENFORCEMENT_ENV, "block")
        assert enforcement_mode() == BLOCK
        assert blocks() is True

    @pytest.mark.parametrize("value", ["", "  ", "advisory", "warn", "true", "1", "BLOCKING"])
    def test_only_the_exact_word_blocks(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """Fail OPEN: an unrecognised value must not silently re-arm the blocker."""
        monkeypatch.setenv(ENFORCEMENT_ENV, value)
        assert blocks() is False

    def test_block_is_case_and_space_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENFORCEMENT_ENV, "  BLOCK  ")
        assert blocks() is True


class TestTaskDependentSizing:
    def test_scales_with_task_count(self) -> None:
        assert swarm_run_budget(40) == pytest.approx(40 * DEFAULT_PER_TASK_USD)
        assert swarm_run_budget(100) == pytest.approx(100 * DEFAULT_PER_TASK_USD)

    def test_small_plans_get_the_floor(self) -> None:
        """A 1-task run must not die near the line for being small."""
        assert swarm_run_budget(1) == MIN_RUN_BUDGET_USD
        assert swarm_run_budget(0) == MIN_RUN_BUDGET_USD

    def test_explicit_request_always_wins(self) -> None:
        """Naming a budget is a deliberate act; sizing must not override it."""
        assert swarm_run_budget(100, 12.5) == pytest.approx(12.5)
        assert swarm_run_budget(1, 500.0) == pytest.approx(500.0)

    def test_non_positive_request_falls_back_to_sizing(self) -> None:
        assert swarm_run_budget(10, 0) == pytest.approx(10 * DEFAULT_PER_TASK_USD)
        assert swarm_run_budget(10, None) == pytest.approx(10 * DEFAULT_PER_TASK_USD)


class TestProviderCliCap:
    """`--max-budget-usd` is what produced 'Budget limit reached' inside a worker."""

    def test_advisory_leaves_the_cli_uncapped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENFORCEMENT_ENV, raising=False)
        assert session_budget_cap(3.0) is None

    def test_block_restores_the_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENFORCEMENT_ENV, "block")
        assert session_budget_cap(3.0) == 3.0

    def test_no_budget_is_still_no_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENFORCEMENT_ENV, "block")
        assert session_budget_cap(None) is None


class TestSupervisorArgv:
    """The end-to-end shape: what actually reaches the claude CLI argv."""

    @staticmethod
    def _argvs() -> tuple[list[str], list[str]]:
        """Both the launch AND the resume argv — a cap on either one still stops work."""
        from omniagentos.sessions import supervisor as sup

        stub = sup.SessionSupervisor.__new__(sup.SessionSupervisor)
        stub.claude_binary = "claude"
        launch = sup.SessionSupervisor._bridge_launch_argv(
            stub, "ses_ref", "haiku", "do the thing", 3.0
        )
        resume = sup.SessionSupervisor._bridge_resume_argv(stub, "ses_ref", "keep going", None, 3.0)
        return launch, resume

    def test_advisory_omits_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENFORCEMENT_ENV, raising=False)
        launch, resume = self._argvs()
        assert "--max-budget-usd" not in launch
        assert "--max-budget-usd" not in resume

    def test_block_passes_the_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENFORCEMENT_ENV, "block")
        launch, resume = self._argvs()
        assert "--max-budget-usd" in launch
        assert "--max-budget-usd" in resume
