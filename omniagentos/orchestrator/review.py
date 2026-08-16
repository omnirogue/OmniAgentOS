"""Stage 4 — the quality gate (a SEPARATE, cross-lineage reviewer).

When an executor finishes, a separate reviewer CONFIRMs or DENIEs its output against
the spec's acceptance criteria — the codex-critic / opus-critic pattern (a different
lineage than the executor, read-only). The default reviewer runs the review prompt
through a cheap NON-Anthropic adapter (``cli-codex``) so the check is genuinely
cross-lineage and costs nothing on the Anthropic account; it is fully injectable so a
test drives CONFIRM/DENY without a live process.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from omniagentos.contracts import AgentInput, HarnessType
from omniagentos.orchestrator.contracts import ExecutorResult, ReviewVerdict
from omniagentos.swarm.plan_safety import parse_verifier_command

if TYPE_CHECKING:
    from omniagentos.intake.planner import PlannedTask

LOG = logging.getLogger(__name__)

_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict"],
    "properties": {
        "verdict": {"type": "string", "enum": ["confirm", "deny"]},
        "feedback": {"type": "string"},
    },
}


class AdapterRunProto(Protocol):
    def run(self, input: AgentInput) -> Any: ...


def _review_prompt(task: PlannedTask, spec_markdown: str, result: ExecutorResult) -> str:
    lines = [
        "You are a SEPARATE, cross-lineage REVIEWER (read-only). CONFIRM or DENY whether",
        "the executor's work satisfies the spec and every acceptance criterion. Deny if",
        "any criterion is unmet, giving concrete, actionable feedback.",
        "",
        "## Task",
        task.title,
    ]
    if task.acceptance_criteria:
        lines += ["", "## Acceptance criteria"]
        lines += [f"- {c}" for c in task.acceptance_criteria]
    lines += [
        "",
        "## Spec",
        spec_markdown.strip(),
        "",
        "## Executor output",
        (result.output_text or "(no textual output; a monitored session ran)").strip(),
        "",
        'Return JSON: {"verdict": "confirm"|"deny", "feedback": "..."}.',
    ]
    return "\n".join(lines)


class CrossLineageReviewer:
    """Default reviewer — a cheap cross-lineage adapter run (``cli-codex``).

    Parses a ``{"verdict","feedback"}`` JSON envelope. Infrastructure failure is
    surfaced as ``verdict="error"``, NEVER as a confirm.

    H2 — WHY THIS IS NOT ALLOWED TO FAIL OPEN
    -----------------------------------------
    Three sites here used to return ``verdict="confirm"`` with "passed by default":
    an adapter exception, an unparseable payload, and an unrecognised verdict
    string. This class is the quality gate for EVERY orchestration
    (``orchestrator/core.py`` constructs it as the default ``reviewer``), so a
    logged-out or quota-dead reviewer CLI silently approved everything it was
    asked to judge — the failure mode measured live on 2026-07-31, when a
    signed-out grok CLI returned ``{"type":"error","message":"Not signed in..."}``
    for every review.

    Commit ``1e8a93`` closed the SWARM seat
    (``swarm.scheduler.CrossLineageSwarmReviewer``) and left this one open. The
    contract below is deliberately copied from that class rather than reinvented:

      * infrastructure failure  -> ``verdict="error"``  (not deny, not confirm)
      * the caller retries the reviewer ONCE, then blocks on review
      * a reviewer-infrastructure failure NEVER consumes the task's retry budget,
        because the executor's work was not the thing that failed.

    A DENY is still a DENY: a reviewer that answers "deny" has judged the work, and
    that path is untouched.
    """

    def __init__(
        self,
        *,
        harness: HarnessType = HarnessType.CLI_CODEX,
        adapter: AdapterRunProto | None = None,
        validation_runner: Callable[[str, str | None], Any] | None = None,
    ) -> None:
        self._harness = harness
        self._adapter = adapter
        self._validation_runner = validation_runner or _run_validation

    def _resolve(self) -> AdapterRunProto:
        if self._adapter is not None:
            return self._adapter
        from omniagentos.adapters.registry import resolve_adapter

        return resolve_adapter(self._harness)

    def review(
        self, *, task: PlannedTask, spec_markdown: str, result: ExecutorResult
    ) -> ReviewVerdict:
        mechanical = self._mechanical_precheck(task, result)
        if mechanical is not None:
            return mechanical
        try:
            adapter = self._resolve()
            agent_input = AgentInput(
                run_id=f"review-{task.title[:16]}",
                task_id=task.title,
                prompt=_review_prompt(task, spec_markdown, result),
                output_schema=_REVIEW_SCHEMA,
            )
            out = adapter.run(agent_input)
        except Exception as exc:  # noqa: BLE001 -- any adapter fault is INFRA, not a verdict.
            LOG.warning("orchestrator reviewer adapter failed", exc_info=True)
            return _infrastructure_error(f"reviewer adapter failed: {type(exc).__name__}: {exc}")
        return _parse_verdict(out)

    def _mechanical_precheck(
        self, task: PlannedTask, result: ExecutorResult
    ) -> ReviewVerdict | None:
        """Reject objective failures before resolving or spending on an LLM."""
        if not result.output_text.strip():
            return _mechanical_deny("executor output is empty")

        if task.commits_expected and not _has_commit_evidence(result):
            return _mechanical_deny("task requires commits, but the executor reported no commit")

        command = (task.validation_command or "").strip()
        if not command:
            return None
        try:
            validation = self._validation_runner(command, result.working_dir)
            passed, detail = _validation_outcome(validation)
        except Exception as exc:  # noqa: BLE001 -- a broken validation is a DENY.
            return _mechanical_deny(
                f"validation command could not run: {type(exc).__name__}: {exc}"
            )
        if not passed:
            suffix = f": {detail}" if detail else ""
            return _mechanical_deny(f"validation command failed{suffix}")
        return None


_COMMIT_HASH = re.compile(r"(?<![0-9a-f])[0-9a-f]{7,40}(?![0-9a-f])", re.IGNORECASE)


def _has_commit_evidence(result: ExecutorResult) -> bool:
    if any(str(commit).strip() for commit in result.commits):
        return True
    return _COMMIT_HASH.search(result.output_text) is not None


def _run_validation(command: str, working_dir: str | None) -> subprocess.CompletedProcess[str]:
    argv = parse_verifier_command(command)
    return subprocess.run(
        argv,
        cwd=working_dir,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def _validation_outcome(value: Any) -> tuple[bool, str]:
    if isinstance(value, bool):
        return value, ""
    if isinstance(value, tuple) and len(value) == 2:
        return bool(value[0]), str(value[1] or "").strip()
    returncode = getattr(value, "returncode", None)
    if returncode is None:
        return bool(value), ""
    output = str(getattr(value, "stderr", "") or getattr(value, "stdout", "") or "").strip()
    return int(returncode) == 0, output[-1000:]


def _mechanical_deny(feedback: str) -> ReviewVerdict:
    return ReviewVerdict(
        verdict="deny",
        feedback=feedback,
        reviewer="mechanical",
    )


def _infrastructure_error(feedback: str) -> ReviewVerdict:
    """The single constructor for "the reviewer could not produce a verdict".

    One factory rather than three inline literals: it is what makes the fail-open
    sites countable, and re-introducing a ``verdict="confirm"`` default anywhere in
    this module becomes a visible edit rather than a one-word change.
    """
    return ReviewVerdict(verdict="error", feedback=feedback, reviewer="cli-codex")


def _parse_verdict(out: Any) -> ReviewVerdict:
    payload = getattr(out, "output_json", None)
    if not isinstance(payload, dict):
        text = str(getattr(out, "output_text", "") or "").strip()
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = None
    if not isinstance(payload, dict):
        # Unparseable payload: the reviewer said something, but not a verdict. That
        # is an infrastructure/format failure, and guessing "confirm" from it is
        # indistinguishable from not reviewing at all.
        return _infrastructure_error("reviewer returned no parseable verdict")
    verdict = str(payload.get("verdict", "")).strip().lower()
    feedback = str(payload.get("feedback", "")).strip()
    if verdict not in ("confirm", "deny"):
        # An unrecognised verdict string is NOT evidence of approval. Coercing it
        # to "confirm" turned every drifted reviewer reply into a silent pass.
        return _infrastructure_error(f"reviewer returned unrecognised verdict {verdict!r}")
    return ReviewVerdict(
        verdict=verdict,  # type: ignore[arg-type]
        feedback=feedback,
        reviewer="cli-codex",
    )


__all__ = ["CrossLineageReviewer"]
