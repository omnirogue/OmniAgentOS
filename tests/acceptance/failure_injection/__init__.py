"""A reusable, parameterized failure-injection suite for the swarm executor.

Five faults are modelled, each as a :class:`FailureInjection` describing WHAT to
break and WHAT recovery the policy owes:

==============  ==========================================================
``timeout``     the worker never lands; the tier deadline must reap it
``tool_failure``the worker's tooling dies mid-attempt
``failed_test`` the work is delivered but fails verification
``merge_conflict`` the unit branch cannot be merged into the workspace
``rate_limit``  the provider refuses with a rate limit
==============  ==========================================================

Design notes
------------
*Reusable*: nothing here imports the acceptance tests. Any suite can
``from tests.acceptance.failure_injection import INJECTIONS, run_injection``.

*Parameterized*: :data:`INJECTIONS` is an ordered mapping keyed by name, so a
caller writes ``@pytest.mark.parametrize("name", sorted(INJECTIONS))`` and gets
every fault, including ones added later.

*Runnable on its own*: ``uv run pytest -q tests/acceptance/failure_injection/``
executes ``test_injectors.py``, which proves each injector actually injects its
fault (an injector that silently no-ops would make every downstream recovery
assertion vacuous).

*Hermetic*: the whole suite runs on the injected scheduler fakes — a temp SQLite
control plane, an in-memory spawner, an injected clock. No network, no provider
CLI, no writes outside ``tmp_path``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.swarm.scheduler_fakes import (
    FakeClock,
    FakeWorktrees,
    Harness,
    make_harness,
    make_scheduler,
)

__all__ = [
    "INJECTIONS",
    "FailureInjection",
    "InjectionResult",
    "RealMergeConflict",
    "RecoveryExpectation",
    "Scenario",
    "inject_real_merge_conflict",
    "run_injection",
]

#: Wall-clock ceiling for one injected scenario. Generous enough for a loaded
#: machine, small enough that a genuine hang fails the test instead of the suite.
RUN_TIMEOUT_SECONDS = 90.0


@dataclass
class Scenario:
    """The mutable world an injector arms before the run starts."""

    harness: Harness
    worktrees: FakeWorktrees | None = None

    @property
    def world(self) -> Any:
        return self.harness.world


@dataclass(frozen=True)
class RecoveryExpectation:
    """What the recovery policy owes once the fault fires.

    ``attempt_end_reasons`` is the fault's *signature*: at least one recorded
    attempt must carry one of these ``swarm_attempts.end_reason`` values, which
    is how a test proves the fault was really injected rather than skipped.
    """

    attempt_end_reasons: frozenset[str]
    terminal_status: str
    emitted_actions: frozenset[str] = frozenset()
    block_reason: str | None = None
    #: A task key that must end up blocked as collateral (dependency parking).
    collateral_blocked: str | None = None
    collateral_block_reason: str | None = None


@dataclass(frozen=True)
class FailureInjection:
    """One injectable fault plus the recovery it must produce."""

    name: str
    description: str
    target: str
    specs: list[dict[str, Any]]
    arm: Callable[[Scenario], None]
    expect: RecoveryExpectation
    scheduler_kwargs: Mapping[str, Any] = field(default_factory=dict)
    integration: bool = False
    max_concurrency: int = 1
    fake_clock: bool = False
    use_worktrees: bool = False
    #: Seconds of virtual time to advance per poll while the run is live. Only
    #: meaningful with ``fake_clock``; this is how a deadline is crossed without
    #: sleeping.
    advance_seconds: float = 0.0


@dataclass
class InjectionResult:
    """Everything an assertion needs, gathered after the run settles."""

    injection: FailureInjection
    harness: Harness
    worktrees: FakeWorktrees | None
    run_finished: bool

    # -- convenience accessors ------------------------------------------------

    @property
    def target_status(self) -> str:
        return self.harness.status_of(self.injection.target)

    @property
    def target_attempts(self) -> list[dict[str, Any]]:
        return self.harness.attempts_of(self.injection.target)

    @property
    def end_reasons(self) -> list[str]:
        return [str(a["end_reason"]) for a in self.target_attempts]

    @property
    def swarm_json(self) -> dict[str, Any]:
        return self.harness.swarm_json_of(self.injection.target)

    def block_reasons(self, task_key: str | None = None) -> list[str]:
        events = self.harness.emitter.of("task_blocked")
        if task_key is None:
            return [str(e.get("reason")) for e in events]
        task_id = self.harness.task_id(task_key)
        return [str(e.get("reason")) for e in events if e.get("task_id") == task_id]

    @property
    def actions(self) -> list[str]:
        return self.harness.emitter.actions()

    def close(self) -> None:
        self.harness.close()


# --------------------------------------------------------------------------
# The injectors
# --------------------------------------------------------------------------


def _arm_timeout(scenario: Scenario) -> None:
    """The worker starts and simply never lands."""
    scenario.world.set_behavior("target", {"kind": "hang"})


def _arm_tool_failure(scenario: Scenario) -> None:
    """The worker's tooling dies; the session terminates non-cleanly."""
    scenario.world.set_behavior(
        "target",
        {"kind": "fail", "polls": 1, "error": "tool invocation failed: rg exited 127"},
    )


def _arm_failed_test(scenario: Scenario) -> None:
    """Work is delivered, but the verify command fails. Armed on the scheduler's
    verifier seam rather than the world, which is where a test result enters."""
    # Handled via scheduler_kwargs; nothing to arm on the world.
    return None


def _arm_rate_limit(scenario: Scenario) -> None:
    scenario.world.set_behavior(
        "target", {"kind": "rate_limited", "polls": 1, "error": "429 rate limit exceeded"}
    )


def _arm_merge_conflict(scenario: Scenario) -> None:
    assert scenario.worktrees is not None, "merge_conflict requires the worktree seam"
    scenario.worktrees.set_merge("target", "conflict")


def _always_fails_verification(task: Any, swarm_json: Any, working_dir: Any) -> tuple[bool, str]:
    del task, working_dir
    if str(swarm_json.get("task_key")) == "target":
        return False, "3 tests failed: test_widget.py::test_roundtrip"
    return True, ""


INJECTIONS: dict[str, FailureInjection] = {
    "timeout": FailureInjection(
        name="timeout",
        description="worker hangs past its tier deadline",
        target="target",
        specs=[{"id": "target", "complexity": "simple"}],
        arm=_arm_timeout,
        fake_clock=True,
        # Comfortably past the 15-minute "simple" tier deadline each poll.
        advance_seconds=20 * 60,
        expect=RecoveryExpectation(
            attempt_end_reasons=frozenset({"timeout"}),
            terminal_status="blocked",
            emitted_actions=frozenset({"task_blocked"}),
            block_reason="split_failed",
        ),
    ),
    "tool_failure": FailureInjection(
        name="tool_failure",
        description="worker tooling dies mid-attempt",
        target="target",
        specs=[{"id": "target", "complexity": "simple"}],
        arm=_arm_tool_failure,
        expect=RecoveryExpectation(
            attempt_end_reasons=frozenset({"crashed"}),
            terminal_status="blocked",
            emitted_actions=frozenset({"task_blocked"}),
            block_reason="retry_cap",
        ),
    ),
    "failed_test": FailureInjection(
        name="failed_test",
        description="delivered work fails its verify command",
        target="target",
        specs=[{"id": "target", "complexity": "simple"}],
        arm=_arm_failed_test,
        scheduler_kwargs={"verifier": _always_fails_verification},
        expect=RecoveryExpectation(
            attempt_end_reasons=frozenset({"review_denied"}),
            terminal_status="blocked",
            emitted_actions=frozenset({"task_blocked"}),
            block_reason="retry_cap",
        ),
    ),
    "rate_limit": FailureInjection(
        name="rate_limit",
        description="provider refuses the attempt with a rate limit",
        target="target",
        specs=[{"id": "target", "complexity": "simple"}],
        arm=_arm_rate_limit,
        scheduler_kwargs={"rate_limit_requeue_cap": 2},
        expect=RecoveryExpectation(
            attempt_end_reasons=frozenset({"rate_limited"}),
            terminal_status="blocked",
            emitted_actions=frozenset({"rate_limit", "task_blocked"}),
            block_reason="rate_limit_flapping",
        ),
    ),
    "merge_conflict": FailureInjection(
        name="merge_conflict",
        description="the unit branch cannot be merged into the main workspace",
        target="target",
        specs=[{"id": "target", "est": 30}, {"id": "dependent", "depends_on": ["target"]}],
        arm=_arm_merge_conflict,
        integration=True,
        max_concurrency=2,
        use_worktrees=True,
        expect=RecoveryExpectation(
            # The unit's own WORK is complete and committed to its branch; it is
            # the MERGE that failed, so the attempt itself closes cleanly.
            attempt_end_reasons=frozenset({"completed"}),
            terminal_status="done",
            emitted_actions=frozenset({"merge_conflict", "task_blocked"}),
            collateral_blocked="dependent",
            collateral_block_reason="dep_merge_conflict",
        ),
    ),
}


# --------------------------------------------------------------------------
# The driver
# --------------------------------------------------------------------------


def run_injection(
    name: str,
    tmp_path: Path,
    *,
    timeout_seconds: float = RUN_TIMEOUT_SECONDS,
    scheduler_overrides: Mapping[str, Any] | None = None,
) -> InjectionResult:
    """Arm ``name``, run the swarm to settlement, and return what happened.

    Never raises on a failed run — a caller asserts on :class:`InjectionResult`.
    ``run_finished`` is False only when the run did not reach a terminal state
    inside ``timeout_seconds``, which is itself the "recovery hung" finding.
    """
    if name not in INJECTIONS:
        raise KeyError(f"unknown injection {name!r}; known: {sorted(INJECTIONS)}")
    injection = INJECTIONS[name]

    harness = make_harness(
        tmp_path,
        injection.specs,
        integration=injection.integration,
        max_concurrency=injection.max_concurrency,
        fake_clock=injection.fake_clock,
    )
    worktrees: FakeWorktrees | None = None
    kwargs: dict[str, Any] = dict(injection.scheduler_kwargs)
    if injection.use_worktrees:
        worktrees = FakeWorktrees(tmp_path / "wt-root")
        kwargs.update(worktrees=worktrees, worktrees_enabled=True)
    if scheduler_overrides:
        kwargs.update(scheduler_overrides)

    scenario = Scenario(harness=harness, worktrees=worktrees)
    injection.arm(scenario)

    scheduler = make_scheduler(harness, **kwargs)
    handle = scheduler.start_run(harness.run_id)
    if handle is None:  # pragma: no cover - provisioning failure, not a fault path
        return InjectionResult(injection, harness, worktrees, run_finished=False)

    finished = _join_advancing_clock(harness, handle, injection, timeout_seconds)
    if not finished:  # pragma: no cover - only on a genuine hang
        scheduler.shutdown()
    return InjectionResult(injection, harness, worktrees, run_finished=finished)


def _join_advancing_clock(
    harness: Harness, handle: Any, injection: FailureInjection, timeout_seconds: float
) -> bool:
    """Wait for the run, advancing virtual time when the fault needs a deadline.

    A deadline-driven fault is crossed by moving the injected clock forward, not
    by sleeping, so a 15-minute tier timeout costs milliseconds of wall time.
    """
    if not injection.advance_seconds:
        return bool(handle.join(timeout=timeout_seconds))

    deadline = time.monotonic() + timeout_seconds
    clock = harness.clock
    assert isinstance(clock, FakeClock)  # advance_seconds implies fake_clock

    # Do not move virtual time until the scheduler has captured the attempt's
    # deadline and entered its clock wait. Advancing first can shift the clock
    # before that deadline is based, turning the injected timeout into a
    # scheduling race under a loaded full-suite run.
    while True:
        if handle.join(timeout=0):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if clock.wait_for_first_sleep(timeout=min(0.05, remaining)):
            break

    while time.monotonic() < deadline:
        if handle.join(timeout=0.05):
            return True
        clock.advance(injection.advance_seconds)
    return False


def injection_names() -> Sequence[str]:
    """Stable, sorted parameterization source."""
    return sorted(INJECTIONS)


# --------------------------------------------------------------------------
# Real-git merge conflict
# --------------------------------------------------------------------------
#
# The scheduler-level ``merge_conflict`` injection above runs on ``FakeWorktrees``,
# which scripts the merge STATUS but does not model git's own semantics — notably
# ``delete_run_branches`` there deletes unconditionally, whereas production uses
# ``git branch -d`` (merged-only). Any claim about work SURVIVING a conflict must
# therefore be proven against real git, which is what this injector is for.


@dataclass(frozen=True)
class RealMergeConflict:
    """A genuine, unmergeable divergence between two unit branches."""

    repo: Path
    worktrees: Any
    owner_id: str
    merged_branch: str
    conflicted_branch: str
    outcome: Any
    conflicted_sha: str


def inject_real_merge_conflict(
    tmp_path: Path, *, owner_id: str = "run_inject", namespace: str = "inject"
) -> RealMergeConflict:
    """Create a real repo, two worktrees editing one file, and conflict them.

    Returns after the SECOND merge has been attempted and refused, so a caller
    asserts on the aftermath: conflict reported, workspace pristine, work intact.
    """
    import subprocess

    from omniagentos.worktrees.git import SubprocessWorktrees

    def git(cwd: Path | str, *args: str) -> str:
        proc = subprocess.run(
            ("git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *args),
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()

    repo = tmp_path / "conflict-repo"
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(("git", "init", "-q", "-b", "main", str(repo)), check=True, capture_output=True)
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial")

    worktrees = SubprocessWorktrees(namespace=namespace, var_root=tmp_path / "var")
    first = worktrees.create(str(repo), owner_id, "unit-first", "HEAD")
    second = worktrees.create(str(repo), owner_id, "unit-second", "HEAD")

    for info, text in ((first, "first wins\n"), (second, "second wins\n")):
        Path(info.path, "shared.txt").write_text(text, encoding="utf-8")
        git(info.path, "add", "-A")
        git(info.path, "commit", "-q", "-m", f"edit from {info.branch}")

    conflicted_sha = git(second.path, "rev-parse", "HEAD")
    assert worktrees.merge_branch(str(repo), first.branch, "merge first").status == "merged"
    outcome = worktrees.merge_branch(str(repo), second.branch, "merge second")

    return RealMergeConflict(
        repo=repo,
        worktrees=worktrees,
        owner_id=owner_id,
        merged_branch=first.branch,
        conflicted_branch=second.branch,
        outcome=outcome,
        conflicted_sha=conflicted_sha,
    )
