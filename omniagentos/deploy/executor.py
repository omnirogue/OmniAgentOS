"""Run a :class:`DeployPlan` through an INJECTED runner.

SAFETY BOUNDARY — read before using this module.

Every command in every plan this package emits targets a REMOTE HOST and is
consequential (it installs packages, creates users, writes systemd units,
opens firewall ports and restarts services). This module therefore executes
NOTHING itself: it never imports ``subprocess``, ``paramiko``, ``asyncssh`` or
any socket library, and it has no default runner. The caller supplies a
``runner`` callable, and the ONLY runner permitted to reach a real machine is
one that goes through the SSH policy lane behind a human-approved consequential
grant. Tests, dry runs and previews pass a fake runner and get a full report
without a single byte leaving the process.

A dry run is therefore just ``execute_plan(plan, dry_run_runner())`` — or, for
human review, ``plan.to_script()``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from omniagentos.deploy.contracts import DeployPlan, DeployStep, RunResult

Runner = Callable[[str], RunResult]


@dataclass(frozen=True, slots=True)
class StepReport:
    """Outcome of one step."""

    step_id: str
    description: str
    command: str
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    skipped: bool = False
    """True for steps never attempted because an earlier step failed."""

    @property
    def ok(self) -> bool:
        return not self.skipped and self.exit_code == 0


@dataclass(frozen=True, slots=True)
class ExecReport:
    """Per-step outcome of a plan run, in plan order."""

    plan_name: str
    target_host: str
    steps: tuple[StepReport, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return all(step.ok for step in self.steps)

    @property
    def failed_step(self) -> StepReport | None:
        """The first step that failed, or None if the whole plan succeeded."""
        for step in self.steps:
            if not step.skipped and not step.ok:
                return step
        return None

    @property
    def failed_step_id(self) -> str | None:
        failed = self.failed_step
        return None if failed is None else failed.step_id

    @property
    def executed_step_ids(self) -> tuple[str, ...]:
        return tuple(step.step_id for step in self.steps if not step.skipped)

    @property
    def skipped_step_ids(self) -> tuple[str, ...]:
        return tuple(step.step_id for step in self.steps if step.skipped)

    def summary(self) -> str:
        if self.ok:
            return f"{self.plan_name}: {len(self.steps)} step(s) ok on {self.target_host}"
        failed = self.failed_step
        assert failed is not None  # not ok => there is a failed step
        return (
            f"{self.plan_name}: FAILED at step {failed.step_id!r} "
            f"(exit {failed.exit_code}) on {self.target_host}; "
            f"{len(self.skipped_step_ids)} step(s) not attempted"
        )


def _skipped(step: DeployStep) -> StepReport:
    return StepReport(
        step_id=step.step_id,
        description=step.description,
        command=step.command,
        exit_code=-1,
        skipped=True,
    )


def execute_plan(plan: DeployPlan, runner: Runner) -> ExecReport:
    """Run every step of ``plan`` through ``runner``, stopping at the first failure.

    ``runner`` is the ONLY way a command leaves this function — there is no
    fallback path, so a caller that forgets to wire the policy-lane runner gets
    a ``TypeError``, never an accidental local execution.

    A runner that RAISES is treated as a failed step (exit code 1, exception
    text on stderr) so a transport error reports like any other failure instead
    of unwinding a half-applied plan.
    """
    if not callable(runner):
        raise TypeError("execute_plan requires a callable runner; none was injected")

    reports: list[StepReport] = []
    failed = False
    for step in plan.steps:
        if failed:
            reports.append(_skipped(step))
            continue
        try:
            result = runner(step.command)
        except Exception as exc:  # noqa: BLE001 - transport errors are step failures
            result = RunResult(exit_code=1, stderr=f"{type(exc).__name__}: {exc}")
        if not isinstance(result, RunResult):
            raise TypeError(
                f"runner returned {type(result).__name__}, expected RunResult "
                f"(step {step.step_id!r})"
            )
        reports.append(
            StepReport(
                step_id=step.step_id,
                description=step.description,
                command=step.command,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        )
        if not result.ok:
            failed = True

    return ExecReport(
        plan_name=plan.name,
        target_host=plan.target_host,
        steps=tuple(reports),
    )


def dry_run_runner(recorder: list[str] | None = None) -> Runner:
    """A runner that records commands and always succeeds. Touches nothing."""
    sink = recorder if recorder is not None else []

    def _run(command: str) -> RunResult:
        sink.append(command)
        return RunResult(exit_code=0, stdout="[dry-run] not executed")

    return _run


__all__ = ["ExecReport", "RunResult", "Runner", "StepReport", "dry_run_runner", "execute_plan"]
