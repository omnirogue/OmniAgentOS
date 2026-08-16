"""Agentless (arXiv 2407.01489) as the CHEAP-lane executor runner.

WHY: the default CHEAP tier spawns a monitored cheap-model session that wanders the
repo turn by turn. Agentless instead spends its compute where a verifier converts it
into accuracy: ONE localization pass, N candidate patches sampled against a single
byte-stable prompt, each applied+tested in a THROWAWAY scratch worktree, and the
project's own test suite (never an LLM's opinion) picks the winner. The selected diff
is applied to the real working tree only AFTER it has objectively passed the tests.

This runner is a drop-in for :class:`omniagentos.orchestrator.executor.CheapAdapterRunner`
(same ``run(request, gateway) -> ExecutorResult`` duck type). It ONLY takes over when
all three preconditions hold: the flag is on (``OMNIAGENTOS_AGENTLESS=1``), the
``working_dir`` is inside a git repo, and a verifier command is configured
(``OMNIAGENTOS_AGENTLESS_TEST_CMD``). Otherwise it DELEGATES to a wrapped
``CheapAdapterRunner`` -- there is nothing to verify against, so the localize/verify
loop has no signal to act on.

WRITE TRUST: this runner ``git apply``\\s the selected diff into the real working dir,
exactly the same write class as ``CheapAdapterRunner`` (whose spawned CLI writes the
working dir directly). The verifier command is operator-authored -- the same trust
class as a project's runner validate steps -- and only ever runs against disposable
scratch checkouts, never the caller's tree.

CASCADE INTERPLAY (OMNIAGENTOS_CASCADE=1): when Agentless produces NO verified fix
(nothing selected, or the winner fails to apply to the real tree) it returns
``ExecutorResult(status="error", ...)`` carrying the per-candidate test evidence. In
the Orchestrator's ``_execute_task`` this objectively-verified failure is exactly the
signal that escalates the task one tier up the ladder (CHEAP -> FUSION), so a hard bug
the cheap sampler could not crack is handed to the Fable-led fusion path with the
failure evidence attached, rather than being silently reported as done.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import TYPE_CHECKING, Protocol

from omniagentos.orchestrator.contracts import (
    ApprovalGatewayProto,
    ExecutorRequest,
    ExecutorResult,
)

if TYPE_CHECKING:
    from omniagentos.agentless.contracts import AgentlessResult

LOG = logging.getLogger(__name__)

_DEFAULT_N = 4
_DEFAULT_ADAPTER = "cli-claude"


class _CheapRunnerProto(Protocol):
    """The slice of ``CheapAdapterRunner`` this wrapper delegates to."""

    def run(self, request: ExecutorRequest, gateway: ApprovalGatewayProto) -> ExecutorResult: ...


class _PipelineFn(Protocol):
    """The ``run_agentless`` shape the wrapper calls (injectable for tests)."""

    def __call__(
        self,
        task: str,
        repo_dir: str,
        test_cmd: str,
        *,
        n: int = ...,
        adapter: str = ...,
        model: str | None = ...,
    ) -> AgentlessResult: ...


def agentless_enabled() -> bool:
    """Whether Agentless takes over the CHEAP lane. OFF by default: with the flag
    unset the orchestrator's ``_runner_for`` returns the unchanged cheap runner."""
    return os.environ.get("OMNIAGENTOS_AGENTLESS") == "1"


def _configured_test_cmd() -> str | None:
    cmd = os.environ.get("OMNIAGENTOS_AGENTLESS_TEST_CMD")
    return cmd if cmd and cmd.strip() else None


def _configured_n() -> int:
    try:
        value = int(os.environ.get("OMNIAGENTOS_AGENTLESS_N", str(_DEFAULT_N)))
    except (TypeError, ValueError):
        return _DEFAULT_N
    return value if value > 0 else _DEFAULT_N


def _configured_adapter() -> str:
    adapter = os.environ.get("OMNIAGENTOS_AGENTLESS_ADAPTER")
    return adapter.strip() if adapter and adapter.strip() else _DEFAULT_ADAPTER


def _inside_git_repo(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _working_tree_dirty(working_dir: str) -> bool:
    """Whether ``git status --porcelain`` reports uncommitted changes.

    Agentless verifies candidates against a scratch checkout of the COMMITTED
    ``HEAD`` (:func:`omniagentos.agentless.patch._prepare_checkout`) but applies the
    winner back to this real tree, so a dirty tree means the verify baseline and the
    apply target diverge. Treated conservatively: a git error / unreadable status is
    reported as dirty so we fall back to the safe cheap-session path."""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=working_dir,
            capture_output=True,
            text=True,
        )
    except OSError:
        return True
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


def _run_git_apply(working_dir: str, diff: str, extra_args: list[str]) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", *extra_args, "-"],
            cwd=working_dir,
            input=diff,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return False, f"git apply could not run: {exc}"
    if result.returncode == 0:
        return True, ""
    return False, (result.stderr or result.stdout or "git apply failed").strip()


def _apply_to_working_tree(working_dir: str, diff: str) -> tuple[bool, str]:
    """Apply the verified diff into the real working dir, mirroring the scratch-verify
    ladder (:func:`omniagentos.agentless.patch.apply_candidate`): plain ``git apply``
    first, then ``git apply --3way``. Returns ``(applied, detail)``. Using the SAME
    ladder as verification means a diff that verified via a 3-way merge is not rejected
    here just because plain apply would have failed on incidental context drift."""
    applied, _detail = _run_git_apply(working_dir, diff, [])
    if applied:
        return True, ""
    applied_3way, detail_3way = _run_git_apply(working_dir, diff, ["--3way"])
    if applied_3way:
        return True, ""
    return False, detail_3way


def _candidate_evidence(result: AgentlessResult) -> str:
    lines: list[str] = []
    for candidate in result.candidates:
        tail = (candidate.test_output_tail or "").strip()
        lines.append(
            f"#{candidate.patch.index}: applied={candidate.applied} "
            f"tests_passed={candidate.tests_passed} returncode={candidate.returncode}"
            + (f"\n{tail}" if tail else "")
        )
    return "\n\n".join(lines)


class AgentlessOrCheapRunner:
    """CHEAP-lane runner that prefers Agentless, falling back to a cheap session."""

    def __init__(
        self,
        *,
        wrapped: _CheapRunnerProto,
        pipeline_fn: _PipelineFn | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._pipeline_fn = pipeline_fn

    def _resolve_pipeline(self) -> _PipelineFn:
        if self._pipeline_fn is not None:
            return self._pipeline_fn
        from omniagentos.agentless.pipeline import run_agentless

        return run_agentless

    def run(self, request: ExecutorRequest, gateway: ApprovalGatewayProto) -> ExecutorResult:
        test_cmd = _configured_test_cmd()
        if not agentless_enabled() or test_cmd is None or not _inside_git_repo(request.working_dir):
            # No verifier / not a git repo: nothing to localize-and-verify against.
            return self._wrapped.run(request, gateway)

        if _working_tree_dirty(request.working_dir):
            # Agentless verifies against HEAD but applies to the real tree; a dirty
            # tree means the verify baseline and the apply target diverge (uncommitted
            # edits are invisible to the verifier), so defer to the cheap session which
            # operates on the working dir directly.
            LOG.debug(
                "agentless: working tree %s is dirty; delegating to cheap runner "
                "(verify baseline is HEAD, apply target is the dirty tree)",
                request.working_dir,
            )
            return self._wrapped.run(request, gateway)

        pipeline = self._resolve_pipeline()
        try:
            result = pipeline(
                task=request.prompt,
                repo_dir=request.working_dir,
                test_cmd=test_cmd,
                n=_configured_n(),
                adapter=_configured_adapter(),
                model=request.model,
            )
        except Exception as exc:  # noqa: BLE001 -- a pipeline fault is an objective failure.
            return ExecutorResult(
                status="error",
                error=f"agentless pipeline raised: {type(exc).__name__}: {exc}",
            )

        selected = result.selected
        if selected is None or not selected.patch.diff:
            evidence = _candidate_evidence(result)
            detail = result.selection_reason or "no candidate was selected"
            return ExecutorResult(
                status="error",
                error="agentless produced no verified fix: "
                + detail
                + (f"\n\n{evidence}" if evidence else ""),
            )

        applied, apply_detail = _apply_to_working_tree(request.working_dir, selected.patch.diff)
        if not applied:
            evidence = _candidate_evidence(result)
            return ExecutorResult(
                status="error",
                error=(
                    f"agentless selected candidate #{selected.patch.index} but its "
                    f"verified diff failed to apply to the working tree: {apply_detail}"
                    + (f"\n\n{evidence}" if evidence else "")
                ),
            )

        tail = (selected.test_output_tail or "").strip()
        summary = (
            f"Agentless applied candidate #{selected.patch.index} "
            f"({result.selection_reason}).\n"
            f"Verifier: {os.environ.get('OMNIAGENTOS_AGENTLESS_TEST_CMD', '')}\n"
            f"Test output tail:\n{tail}\n\n"
            f"Applied diff:\n{selected.patch.diff}"
        )
        return ExecutorResult(status="ok", output_text=summary)


__all__ = ["AgentlessOrCheapRunner", "agentless_enabled"]
