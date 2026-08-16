"""The Orchestrator core — the "worker-as-planner" loop (one task = one run).

:meth:`Orchestrator.run` (and the :func:`run_orchestration` convenience entry point,
annotated **Fable medium**) executes the five stages, each REUSING an existing module:

1. **Plan → spec** — ``intake.planner.plan_goal`` (Fable high/max) writes a
   :class:`ProjectPlan`, rendered to a spec doc via ``vault.write_note``.
2. **Spawn executor with injected context** — ``memory.assemble_context`` +
   ``skills.search``/``get_skill`` + the scoped file list feed the prompt; the tier
   (cheap-model session vs Fable-led fusion session via ``SessionSupervisor.spawn``)
   is DRIVEN by the ``priority`` knob + complexity; every tier is monitored through
   a genuine terminal state before orchestration continues.
3. **Approve-safe / escalate-hard** — an :class:`ApprovalGateway` auto-approves every
   request except money-moves + file deletions, which escalate via the notifications
   system.
4. **Quality gate** — a SEPARATE cross-lineage reviewer CONFIRMs/DENIEs against the
   spec; a DENY spawns one corrective executor with the feedback appended (capped).
5. **Learn** — on each session completion the learners fan out
   (``curate_sessions`` + a vault write), fire-and-forget.

Every external seam is injected, so a full run is token-free under test, and the class
holds no per-run state — it is safe to run N orchestrations concurrently.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from omniagentos.contracts import default_vault_dir, new_id
from omniagentos.orchestrator.approvals import ApprovalGateway, NotificationEscalator
from omniagentos.orchestrator.contracts import (
    ApprovalNotifier,
    ExecutorRequest,
    ExecutorResult,
    ExecutorRunner,
    ExecutorTier,
    FileLister,
    LearnEvent,
    LearnHook,
    OrchestrationCheckpoint,
    OrchestrationResult,
    OverridePins,
    Priority,
    ResolvedExecution,
    ResumeState,
    Reviewer,
    ReviewVerdict,
    SkillGet,
    SkillSearch,
    TaskOutcome,
)
from omniagentos.orchestrator.executor import (
    SessionSpawner,
    SessionStatusStore,
    build_executor_prompt,
    runner_for_tier,
)
from omniagentos.orchestrator.learn import default_learn_hook
from omniagentos.orchestrator.review import CrossLineageReviewer
from omniagentos.orchestrator.spec import render_spec_markdown, write_spec_doc
from omniagentos.orchestrator.tiers import resolve_execution

if TYPE_CHECKING:
    from omniagentos.intake.planner import Complexity, PlannedTask, PlannerLLM, ProjectPlan
    from omniagentos.memory.contracts import ConversationReader
    from omniagentos.selfimprove.reflexion import Reflection

LOG = logging.getLogger(__name__)


def _model_planner_llm(model: str) -> PlannerLLM:
    """A planner LLM seam pinned to a specific model, resolved BY LINEAGE.

    A claude-lineage pin runs through ``run_fable_json`` exactly as before. A
    gemini-lineage pin (D10 Fast Mode: ``planner_model="gemini-3.6-flash"``)
    resolves the CLI_GEMINI adapter with the pinned model id passed EXPLICITLY
    (no modelintel registry lookup — a stale var/modelintel/registry.json can
    never break Fast Mode planning) and falls through the chain to Fable on ANY
    gemini failure or format break: planning never silently degrades to
    heuristics while a Fable rung is still available.
    """
    lineage = model.strip().lower()
    if lineage.startswith("gemini"):

        def _gemini_llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any] | None:
            from omniagentos.contracts import HarnessType
            from omniagentos.intake.fallback import run_with_fallback

            return run_with_fallback(
                prompt,
                schema,
                effort=effort,
                max_turns=3,
                wall_ms=300_000,
                chain=(("gemini", HarnessType.CLI_GEMINI, model), "fable"),
            )

        return _gemini_llm

    def _llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any] | None:
        from omniagentos.intake.fable import run_fable_json

        return run_fable_json(
            prompt, schema, model=model, effort=effort, max_turns=3, wall_ms=300_000
        )

    return _llm


def _flatten_tasks(plan: ProjectPlan) -> list[PlannedTask]:
    tasks = list(plan.tasks)
    for sp in plan.sub_projects:
        tasks.extend(sp.tasks)
    return tasks


def _cascade_enabled() -> bool:
    from omniagentos.routing.cascade import cascade_enabled

    return cascade_enabled()


def _checkpoint_safe(callback: Callable[[], None]) -> None:
    """Persist one transition best-effort; durability faults never break execution."""
    try:
        callback()
    except Exception:  # noqa: BLE001 -- checkpoints cannot break the live run.
        LOG.debug("orchestrator checkpoint failed", exc_info=True)


def _reflexion_enabled() -> bool:
    """Whether gate-failure feedback is enriched with a Reflexion reflection.

    OFF by default (``OMNIAGENTOS_REFLEXION`` unset): the corrective retry carries the
    reviewer's raw feedback exactly as before."""
    return os.environ.get("OMNIAGENTOS_REFLEXION") == "1"


def _route_learn_min_samples() -> int:
    """Resolve the adaptive-route evidence floor (default remains five)."""
    raw = os.environ.get("OMNIAGENTOS_ROUTE_LEARN_MIN_SAMPLES", "5")
    try:
        return max(int(raw), 1)
    except ValueError:
        LOG.warning("invalid OMNIAGENTOS_ROUTE_LEARN_MIN_SAMPLES=%r; using 5", raw)
        return 5


def _default_reflexion_store_dir() -> str:
    # omniagentos/orchestrator/core.py -> omniagentos/orchestrator -> omniagentos -> root
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "var", "reflexion")


def _escalate_tier(tier: ExecutorTier) -> ExecutorTier:
    """Move one rung up the executor tier ladder; FUSION_ULTRA is the cap."""
    if tier is ExecutorTier.CHEAP:
        return ExecutorTier.FUSION
    if tier is ExecutorTier.FUSION:
        return ExecutorTier.FUSION_ULTRA
    return ExecutorTier.FUSION_ULTRA


# The orchestrator's own tier ladder, cheapest first. The pseudo cost_ranks feed
# ``recommend_start_tier``'s expected-chained-cost fallback; the NAMES match the
# ``tier.value`` strings ``_trace_attempt`` writes into the JSONL trace, so the learner
# mines the same rows the orchestrator records (no tier0-/tier1- namespace mismatch).
_TIER_LADDER: tuple[ExecutorTier, ...] = (
    ExecutorTier.CHEAP,
    ExecutorTier.FUSION,
    ExecutorTier.FUSION_ULTRA,
)
_TIER_COST_RANK: dict[ExecutorTier, float] = {
    ExecutorTier.CHEAP: 1.0,
    ExecutorTier.FUSION: 5.0,
    ExecutorTier.FUSION_ULTRA: 15.0,
}


class Orchestrator:
    """Reusable, stateless orchestrator. Construct once, run many goals concurrently."""

    def __init__(
        self,
        *,
        planner_llm: PlannerLLM | None = None,
        reviewer: Reviewer | None = None,
        approval_notifier: ApprovalNotifier | None = None,
        context_reader: ConversationReader | None = None,
        skill_search: SkillSearch | None = None,
        skill_get: SkillGet | None = None,
        file_lister: FileLister | None = None,
        learn_hook: LearnHook | None = None,
        executor_runner: ExecutorRunner | None = None,
        session_attacher: Callable[[str], ExecutorResult] | None = None,
        spawner: SessionSpawner | None = None,
        cheap_adapter: Any | None = None,
        session_store: SessionStatusStore | None = None,
        session_monotonic: Callable[[], float] | None = None,
        session_sleep: Callable[[float], None] | None = None,
        session_timeout_seconds: float = 30 * 60.0,
        session_poll_interval_seconds: float = 0.5,
        vault_dir: str | None = None,
        budget_usd_max: float | None = None,
        cascade_trace_path: str | None = None,
        reflector: Callable[[str, str], Reflection] | None = None,
        reflexion_store_dir: str | None = None,
        syntax_gate: Callable[[str], tuple[bool, str] | None] | None = None,
    ) -> None:
        self._planner_llm = planner_llm
        self._reviewer = reviewer or CrossLineageReviewer()
        self._approval_notifier = approval_notifier or NotificationEscalator()
        self._context_reader = context_reader
        self._skill_search = skill_search or _default_skill_search()
        self._skill_get = skill_get or _default_skill_get()
        self._file_lister = file_lister
        self._learn_hook = learn_hook or default_learn_hook
        self._executor_runner = executor_runner
        self._session_attacher = session_attacher
        self._spawner = spawner
        self._cheap_adapter = cheap_adapter
        self._session_store = session_store
        self._session_monotonic = session_monotonic
        self._session_sleep = session_sleep
        self._session_timeout_seconds = session_timeout_seconds
        self._session_poll_interval_seconds = session_poll_interval_seconds
        self._vault_dir = vault_dir or default_vault_dir()
        self._budget_usd_max = budget_usd_max
        self._cascade_trace_path = cascade_trace_path
        self._reflector = reflector
        self._reflexion_store_dir = reflexion_store_dir or _default_reflexion_store_dir()
        self._syntax_gate = syntax_gate

    # -- stage helpers ----------------------------------------------------

    def _monitored_runner_for(self, tier: ExecutorTier) -> Any:
        kwargs: dict[str, Any] = {
            "spawner": self._spawner,
            "cheap_adapter": self._cheap_adapter,
            "session_store": self._session_store,
            "timeout_seconds": self._session_timeout_seconds,
            "poll_interval_seconds": self._session_poll_interval_seconds,
        }
        if self._session_monotonic is not None:
            kwargs["monotonic"] = self._session_monotonic
        if self._session_sleep is not None:
            kwargs["sleep"] = self._session_sleep
        return runner_for_tier(tier, **kwargs)

    def _runner_for(self, tier: ExecutorTier) -> ExecutorRunner:
        if self._executor_runner is not None:
            return self._executor_runner
        runner = self._monitored_runner_for(tier)
        # Agentless as the CHEAP-lane executor (OMNIAGENTOS_AGENTLESS=1): wrap the
        # normal cheap runner so a git working dir + configured verifier goes through
        # the localize-sample-verify-select loop, delegating to the wrapped cheap
        # session runner otherwise. Flag off returns the cheap runner exactly as-is.
        if tier is ExecutorTier.CHEAP:
            from omniagentos.orchestrator.agentless_runner import (
                AgentlessOrCheapRunner,
                agentless_enabled,
            )

            if agentless_enabled():
                return AgentlessOrCheapRunner(wrapped=runner)
        return runner

    def _attach(self, tier: ExecutorTier, session_id: str) -> ExecutorResult:
        if self._session_attacher is not None:
            return self._session_attacher(session_id)
        if self._executor_runner is not None:
            injected_attach = getattr(self._executor_runner, "attach", None)
            if callable(injected_attach):
                return injected_attach(session_id)
            # F-005: injected runner without attach and no session_attacher must not
            # fall through to real monitored runner; return error instead
            return ExecutorResult(
                status="error",
                session_id=session_id,
                error=f"no attacher available for resumed session {session_id}",
            )
        runner = self._monitored_runner_for(tier)
        try:
            return runner.attach(session_id)
        finally:
            close = getattr(runner, "close", None)
            if callable(close):
                try:
                    close(terminate_children=False)
                except Exception:  # noqa: BLE001 -- disposal must never replace the result.
                    LOG.exception(
                        "could not close monitored runner after attaching session %s; "
                        "preserving executor result",
                        session_id,
                    )

    def _trace_attempt(
        self,
        *,
        task_class: str,
        tier: ExecutorTier,
        resolved: ResolvedExecution,
        verified: bool,
        seconds: float,
        error: bool,
        escalated_from: str | None,
    ) -> None:
        """Record one win/loss cascade trace row (best-effort: a trace failure must
        never break orchestration)."""
        try:
            from omniagentos.routing.cascade import default_trace_path, record_trace

            trace_path = self._cascade_trace_path or default_trace_path()
            record_trace(
                trace_path,
                task_class=task_class,
                tier_name=tier.value,
                adapter=tier.value,
                model=resolved.executor_model,
                verified=verified,
                seconds=seconds,
                error=error,
                escalated_from=escalated_from,
            )
        except Exception:  # noqa: BLE001 -- traces are best-effort; never break the run.
            LOG.debug("orchestrator cascade trace failed", exc_info=True)

    def _recommended_start_tier(self, task_class: str, resolved: ResolvedExecution) -> ExecutorTier:
        """RouteLLM-style start-tier recommendation from the recorded trace history.

        Mines ``task_class``'s win/loss traces (``omniagentos.routing.learn``) over a
        pseudo-ladder of the three executor tiers and returns the HIGHER of the
        operator-resolved tier and the recommended tier -- so the learner can only ever
        skip a cheap tier the evidence says reliably fails, never DOWNGRADE below the
        operator's knob. Best-effort: any failure resolving the recommendation keeps the
        resolved tier. Only consulted under the cascade flag."""
        resolved_index = _TIER_LADDER.index(resolved.executor_tier)
        try:
            from omniagentos.routing.cascade import CascadeTier, default_trace_path
            from omniagentos.routing.learn import (
                read_trace_hierarchy,
                recommend_start_tier,
            )

            trace_path = self._cascade_trace_path or default_trace_path()
            pseudo_ladder = [
                CascadeTier(name=tier.value, adapter=tier.value, cost_rank=_TIER_COST_RANK[tier])
                for tier in _TIER_LADDER
            ]
            trace_levels = read_trace_hierarchy(trace_path, task_class)
            leaf = trace_levels[0] if trace_levels else []
            rec_index = recommend_start_tier(
                leaf,
                pseudo_ladder,
                min_samples=_route_learn_min_samples(),
                parent_traces=trace_levels[1:],
            )
        except Exception:  # noqa: BLE001 -- learning a start tier is best-effort.
            LOG.debug("orchestrator start-tier recommendation failed", exc_info=True)
            return resolved.executor_tier
        return _TIER_LADDER[max(resolved_index, rec_index)]

    def _plan(self, goal: str, resolved: ResolvedExecution) -> ProjectPlan:
        from omniagentos.intake.planner import plan_goal

        llm = self._planner_llm
        if llm is None and resolved.planner_model:
            llm = _model_planner_llm(resolved.planner_model)
        return plan_goal(goal, llm=llm, effort=resolved.planner_effort)

    def _execute_task(
        self,
        task: PlannedTask,
        *,
        run_id: str,
        seq: int,
        spec_markdown: str,
        resolved: ResolvedExecution,
        working_dir: str,
        project_id: str | None,
        gateway: ApprovalGateway,
        granted_roots: list[str] | None = None,
        checkpoint: OrchestrationCheckpoint | None = None,
        initial_result: ExecutorResult | None = None,
        initial_attempts: int = 0,
        resume_status: str | None = None,
        resume_session_id: str | None = None,
        resume_output_tail: str = "",
    ) -> TaskOutcome:
        # Verification-gated tier escalation (OMNIAGENTOS_CASCADE=1). Flag OFF: the
        # loop runs exactly as before -- ``current_tier`` never moves off
        # ``resolved.executor_tier``, a ``status == "error"`` result fails immediately
        # with no retry, and no trace is written. Flag ON: every attempt records a
        # win/loss trace, a reviewer DENY or an objectively-errored attempt escalates
        # one rung of the tier ladder (CHEAP -> FUSION -> FUSION_ULTRA, capped) and
        # retries, and BEFORE the loop the RouteLLM-style learner mines this class's
        # recorded traces to pick the START tier (never below the operator's knob), so
        # a class the cheap tier reliably fails on skips straight to a pricier tier.
        cascade = _cascade_enabled()
        task_class = f"orch:{resolved.executor_lane or 'default'}:{resolved.complexity}"
        # Effective retry cap: even a priority=fast task (max_retries 0) earns ONE
        # escalation under cascade, so a verified cheap-tier failure is never shipped.
        effective_max_retries = max(resolved.max_retries, 1) if cascade else resolved.max_retries
        current_tier = resolved.executor_tier
        if cascade and initial_attempts == 0:
            current_tier = self._recommended_start_tier(task_class, resolved)
        escalated_from: str | None = None

        # F-003: When cascade is enabled and resuming (initial_attempts > 0),
        # replay the tier ladder to match where we left off. Prior attempt N ran
        # at start-tier escalated (N-1) rungs; the attempt we're about to
        # consume/execute must run at exactly that tier.
        if cascade and initial_attempts > 0:
            if initial_result is not None or resume_status == "running":
                prior_rungs = initial_attempts - 1
            else:
                prior_rungs = initial_attempts
            for _ in range(prior_rungs):
                current_tier = _escalate_tier(current_tier)

        if (
            initial_result is None
            and resume_status in {"denied", "failed"}
            and initial_attempts >= effective_max_retries + 1
        ):
            exhausted_status: Literal["denied", "failed"] = (
                "denied" if resume_status == "denied" else "failed"
            )
            return TaskOutcome(
                task_title=task.title,
                tier=current_tier,
                status=exhausted_status,
                attempts=initial_attempts,
                session_id=resume_session_id,
                output_text=resume_output_tail,
            )

        if initial_result is not None or resume_status == "running":
            attempt = max(initial_attempts, 1)
        else:
            attempt = max(initial_attempts + 1, 1)
        feedback: str | None = None
        last_result = ExecutorResult(status="error", error="not run")
        last_review: ReviewVerdict | None = None
        while True:
            started = time.monotonic()
            if initial_result is not None:
                last_result = initial_result
                initial_result = None
            else:
                prompt = build_executor_prompt(
                    task,
                    spec_markdown,
                    working_dir,
                    context_reader=self._context_reader,
                    context_scope_type="project",
                    context_scope_id=project_id,
                    skill_search=self._skill_search,
                    skill_get=self._skill_get,
                    file_lister=self._file_lister,
                    review_feedback=feedback,
                    granted_roots=granted_roots,
                )
                # An escalated (or learner-bumped) tier drops the operator's lane/model
                # pins -- those were chosen for the resolved tier; the escalated tier's
                # own defaults take over. The resolved tier keeps its pins as before.
                escalated_tier = current_tier != resolved.executor_tier

                def on_spawn(session_id: str) -> None:
                    if checkpoint is not None:
                        _checkpoint_safe(
                            partial(
                                checkpoint.step_session,
                                run_id,
                                seq,
                                session_id,
                            )
                        )

                request = ExecutorRequest(
                    task=task,
                    prompt=prompt,
                    working_dir=working_dir,
                    tier=current_tier,
                    model=None if escalated_tier else resolved.executor_model,
                    lane=None if escalated_tier else resolved.executor_lane,
                    title=task.title,
                    budget_usd_max=self._budget_usd_max,
                    attempt=attempt,
                    granted_roots=granted_roots,
                    run_id=run_id,
                    on_spawn=on_spawn if checkpoint is not None else None,
                )
                if checkpoint is not None:
                    _checkpoint_safe(partial(checkpoint.step_started, run_id, seq, attempt))
                last_result = self._runner_for(current_tier).run(request, gateway)
            if last_result.working_dir is None:
                last_result.working_dir = working_dir
            seconds = time.monotonic() - started

            # Stage 5: learners fan out on each session completion (fire-and-forget).
            self._fire_learners(task, last_result)

            if last_result.status == "error":
                if cascade:
                    self._trace_attempt(
                        task_class=task_class,
                        tier=current_tier,
                        resolved=resolved,
                        verified=False,
                        seconds=seconds,
                        error=True,
                        escalated_from=escalated_from,
                    )
                    if attempt <= effective_max_retries:
                        feedback = self._corrective_feedback(
                            task,
                            review_feedback=None,
                            error_text=last_result.error or "",
                            output_text=last_result.output_text,
                            task_class=task_class,
                        )
                        escalated_from = current_tier.value
                        current_tier = _escalate_tier(current_tier)
                        attempt += 1
                        continue
                return TaskOutcome(
                    task_title=task.title,
                    tier=current_tier,
                    status="failed",
                    attempts=attempt,
                    session_id=last_result.session_id,
                    output_text=last_result.error or "",
                )

            if not resolved.run_quality_gate:
                # run_quality_gate=False bypasses the mechanical gate too; this is deliberate.
                return TaskOutcome(
                    task_title=task.title,
                    tier=current_tier,
                    status="unreviewed",
                    attempts=attempt,
                    session_id=last_result.session_id,
                    output_text=last_result.output_text,
                )

            from omniagentos.verify import verify_mode

            mode = verify_mode()
            mechanical_failed = False
            mechanical_detail = ""

            if mode != "off":
                gate_fn = self._syntax_gate
                if gate_fn is None:
                    from omniagentos.verify import verify_working_dir

                    gate_fn = verify_working_dir

                wd = last_result.working_dir
                if not wd:
                    wd = working_dir

                try:
                    outcome = gate_fn(wd)
                    if outcome is not None:
                        ok, detail = outcome
                        if not ok:
                            if mode == "shadow":
                                LOG.warning("Syntax gate (shadow) failed: %s", detail)
                            else:
                                mechanical_failed = True
                                mechanical_detail = detail
                except Exception:  # noqa: BLE001 -- the syntax gate must never break a live run.
                    LOG.debug("Syntax gate failed unexpectedly", exc_info=True)

            if mechanical_failed:
                if cascade:
                    self._trace_attempt(
                        task_class=task_class,
                        tier=current_tier,
                        resolved=resolved,
                        verified=False,
                        seconds=seconds,
                        error=False,
                        escalated_from=escalated_from,
                    )
                if attempt <= effective_max_retries:
                    feedback = self._corrective_feedback(
                        task,
                        review_feedback=None,
                        error_text=mechanical_detail,
                        output_text=last_result.output_text,
                        task_class=task_class,
                    )
                    if cascade:
                        escalated_from = current_tier.value
                        current_tier = _escalate_tier(current_tier)
                    attempt += 1
                    continue
                return TaskOutcome(
                    task_title=task.title,
                    tier=current_tier,
                    status="denied",
                    attempts=attempt,
                    session_id=last_result.session_id,
                    review=ReviewVerdict(
                        verdict="deny", feedback=mechanical_detail, reviewer="mechanical:syntax"
                    ),
                    output_text=last_result.output_text,
                )

            last_review = self._reviewer.review(
                task=task, spec_markdown=spec_markdown, result=last_result
            )
            # H2 — three-valued review, the shape swarm/scheduler.py already uses.
            # ``error`` is a reviewer INFRASTRUCTURE failure (adapter down,
            # unparseable payload, unrecognised verdict). It is not a DENY: the
            # executor's work was never judged, so it must not be fed back as
            # corrective feedback and must not burn the task's retry budget.
            # Retry the REVIEWER once — a single failure is often a transient blip —
            # then block. Never auto-CONFIRM.
            if last_review.verdict == "error":
                LOG.warning(
                    "reviewer infrastructure failed for %r (%s); retrying the reviewer once",
                    task.title,
                    last_review.feedback,
                )
                last_review = self._reviewer.review(
                    task=task, spec_markdown=spec_markdown, result=last_result
                )
            if last_review.verdict == "error":
                # No cascade trace is written here on purpose: an infra failure is
                # evidence about the REVIEWER, not about this tier's executor, and
                # recording it as a loss would poison the tier learner.
                LOG.error(
                    "blocked on review for %r after two reviewer attempts: %s",
                    task.title,
                    last_review.feedback,
                )
                return TaskOutcome(
                    task_title=task.title,
                    tier=current_tier,
                    status="blocked_on_review",
                    attempts=attempt,
                    session_id=last_result.session_id,
                    review=last_review,
                    output_text=last_result.output_text,
                )
            if last_review.verdict == "confirm":
                if cascade:
                    self._trace_attempt(
                        task_class=task_class,
                        tier=current_tier,
                        resolved=resolved,
                        verified=True,
                        seconds=seconds,
                        error=False,
                        escalated_from=escalated_from,
                    )
                return TaskOutcome(
                    task_title=task.title,
                    tier=current_tier,
                    status="done",
                    attempts=attempt,
                    session_id=last_result.session_id,
                    review=last_review,
                    output_text=last_result.output_text,
                )
            # DENY: spawn one corrective executor with feedback, capped at max_retries.
            if cascade:
                self._trace_attempt(
                    task_class=task_class,
                    tier=current_tier,
                    resolved=resolved,
                    verified=False,
                    seconds=seconds,
                    error=False,
                    escalated_from=escalated_from,
                )
            if attempt <= effective_max_retries:
                feedback = self._corrective_feedback(
                    task,
                    review_feedback=last_review.feedback,
                    error_text=None,
                    output_text=last_result.output_text,
                    task_class=task_class,
                )
                if cascade:
                    escalated_from = current_tier.value
                    current_tier = _escalate_tier(current_tier)
                attempt += 1
                continue
            return TaskOutcome(
                task_title=task.title,
                tier=current_tier,
                status="denied",
                attempts=attempt,
                session_id=last_result.session_id,
                review=last_review,
                output_text=last_result.output_text,
            )

    def _corrective_feedback(
        self,
        task: PlannedTask,
        *,
        review_feedback: str | None,
        error_text: str | None,
        output_text: str,
        task_class: str,
    ) -> str:
        """Build the ``review_feedback`` carried into the corrective retry.

        Reflexion OFF (default): a DENY carries the reviewer's raw feedback UNCHANGED;
        an error retry carries the objective error text. Reflexion ON
        (``OMNIAGENTOS_REFLEXION=1``): a one-paragraph Reflexion reflection
        (arXiv:2303.11366) is built over the failure evidence -- the reviewer feedback
        (or error text) plus the tail of the prior attempt's output -- persisted to the
        Reflexion store, and prepended ABOVE the raw reviewer feedback so the escalated
        tier gets both the "why it failed / what to do differently" narrative AND the
        verbatim reviewer verdict.
        """
        if not _reflexion_enabled():
            if review_feedback is not None:
                return review_feedback
            return error_text or ""

        primary = (
            review_feedback if (review_feedback and review_feedback.strip()) else (error_text or "")
        )
        tail = (output_text or "")[-2000:]
        if primary and tail.strip():
            evidence = f"{primary}\n\n--- prior attempt output (tail) ---\n{tail}"
        elif tail.strip():
            evidence = tail
        else:
            evidence = primary

        reflection = self._reflect(task.title, evidence)
        self._persist_reflection_safe(reflection, task_class)
        paragraph = reflection.paragraph
        if review_feedback and review_feedback.strip():
            return f"{paragraph}\n\n{review_feedback.strip()}"
        return paragraph

    def _reflect(self, task_summary: str, evidence: str) -> Reflection:
        """Build a Reflexion reflection (injected reflector for tests, else the
        template-mode ``build_reflection`` -- adapter=None, no LLM call)."""
        if self._reflector is not None:
            return self._reflector(task_summary, evidence)
        from omniagentos.selfimprove.reflexion import build_reflection

        return build_reflection(task_summary, evidence)

    def _persist_reflection_safe(self, reflection: Reflection, task_class: str) -> None:
        """Persist the reflection to its OWN JSONL store (separate from selfimprove's
        PASSED-only skills capture). Persist failure never raises."""
        try:
            from omniagentos.selfimprove.reflexion import persist_reflection

            persist_reflection(
                reflection, task_class=task_class, store_dir=self._reflexion_store_dir
            )
        except Exception:  # noqa: BLE001 -- persistence is best-effort; never break the run.
            LOG.debug("orchestrator reflexion persist failed", exc_info=True)

    def _fire_learners(self, task: PlannedTask, result: ExecutorResult) -> None:
        try:
            self._learn_hook(
                LearnEvent(
                    session_id=result.session_id,
                    task_title=task.title,
                    output_text=result.output_text,
                    vault_dir=self._vault_dir,
                )
            )
        except Exception:  # noqa: BLE001 -- learners are fire-and-forget.
            LOG.debug("orchestrator learn hook failed", exc_info=True)

    # -- entry point ------------------------------------------------------

    def run(
        self,
        goal: str,
        *,
        priority: Priority = "balanced",
        pins: OverridePins | Mapping[str, Any] | None = None,
        working_dir: str | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
        granted_roots: list[str] | None = None,
        checkpoint: OrchestrationCheckpoint | None = None,
        resume_state: ResumeState | None = None,
    ) -> OrchestrationResult:
        """Run one full orchestration for ``goal`` under the given knobs.

        ``granted_roots`` (P3 / FIX 6) are the project's validated write roots BEYOND
        ``working_dir``, resolved SERVER-SIDE by the caller (the intake dispatch path,
        which owns the project store) and threaded here into every executor spawn so an
        orchestrate-mode session honors the SAME full project scope an intake session
        gets. ``None`` keeps the pre-P3 working-dir-only confinement.
        """
        from omniagentos.intake.planner import estimate_complexity

        if resume_state is not None and run_id is None:
            raise ValueError("run_id is required when resuming an orchestration")
        rid = run_id or new_id("orch")
        complexity: Complexity = estimate_complexity(goal)
        resolved = resolve_execution(priority, complexity, OverridePins.coerce(pins))

        # Stage 1: plan -> spec doc.
        wdir = working_dir or tempfile.mkdtemp(prefix="orch-")
        if resume_state is None:
            plan = self._plan(goal, resolved)
            spec_markdown = render_spec_markdown(plan, goal, rid)
            spec_note_path = write_spec_doc(
                plan, goal, rid, vault_dir=self._vault_dir, working_dir=wdir
            )
        else:
            from omniagentos.intake.planner import ProjectPlan

            plan = ProjectPlan.model_validate_json(resume_state.plan_json)
            spec_markdown = render_spec_markdown(plan, goal, rid)
            spec_note_path = None

        tasks = _flatten_tasks(plan)
        if checkpoint is not None and resume_state is None:
            _checkpoint_safe(
                lambda: checkpoint.record_plan(
                    rid,
                    plan.model_dump_json(),
                    [task.title for task in tasks],
                )
            )

        # Stage 3 gateway (shared across the run's tasks).
        gateway = ApprovalGateway(notifier=self._approval_notifier)

        # Stages 2/4/5, per task (one task = one executor unit).
        outcomes: list[TaskOutcome] = []
        resume_steps = (
            {step.seq: step for step in resume_state.steps} if resume_state is not None else {}
        )
        attached_running = False
        for seq, task in enumerate(tasks):
            resume_step = resume_steps.get(seq)
            if resume_step is not None and resume_step.status in {"done", "unreviewed"}:
                finished_status: Literal["done", "unreviewed"] = (
                    "done" if resume_step.status == "done" else "unreviewed"
                )
                outcomes.append(
                    TaskOutcome(
                        task_title=resume_step.title,
                        tier=resolved.executor_tier,
                        status=finished_status,
                        attempts=resume_step.attempts,
                        session_id=resume_step.session_id,
                        output_text=resume_step.output_tail,
                    )
                )
                continue

            initial_result: ExecutorResult | None = None
            if (
                resume_step is not None
                and resume_step.status == "running"
                and resume_step.session_id is not None
                and not attached_running
            ):
                initial_result = self._attach(resolved.executor_tier, resume_step.session_id)
                attached_running = True

            outcome = self._execute_task(
                task,
                run_id=rid,
                seq=seq,
                spec_markdown=spec_markdown,
                resolved=resolved,
                working_dir=wdir,
                project_id=project_id,
                gateway=gateway,
                granted_roots=granted_roots,
                checkpoint=checkpoint,
                initial_result=initial_result,
                initial_attempts=resume_step.attempts if resume_step is not None else 0,
                resume_status=resume_step.status if resume_step is not None else None,
                resume_session_id=(resume_step.session_id if resume_step is not None else None),
                resume_output_tail=(resume_step.output_tail if resume_step is not None else ""),
            )
            outcomes.append(outcome)
            if checkpoint is not None:
                _checkpoint_safe(
                    partial(
                        checkpoint.step_finished,
                        rid,
                        seq,
                        outcome.status,
                        outcome.attempts,
                        outcome.output_text[-2000:],
                    )
                )

        return OrchestrationResult(
            run_id=rid,
            goal=goal,
            plan=plan,
            resolved=resolved,
            spec_note_path=spec_note_path,
            tasks=outcomes,
            approvals=gateway.decisions,
            status=_aggregate_status(outcomes),
        )


def _aggregate_status(outcomes: list[TaskOutcome]) -> Literal["done", "partial", "failed"]:
    if not outcomes:
        return "failed"
    good = {"done", "unreviewed"}
    done = sum(1 for o in outcomes if o.status in good)
    if done == len(outcomes):
        return "done"
    if done == 0:
        return "failed"
    return "partial"


def run_orchestration(
    goal: str,
    *,
    priority: Priority = "balanced",
    pins: OverridePins | Mapping[str, Any] | None = None,
    working_dir: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    granted_roots: list[str] | None = None,
    checkpoint: OrchestrationCheckpoint | None = None,
    resume_state: ResumeState | None = None,
    planner_llm: PlannerLLM | None = None,
    reviewer: Reviewer | None = None,
    approval_notifier: ApprovalNotifier | None = None,
    context_reader: ConversationReader | None = None,
    skill_search: SkillSearch | None = None,
    skill_get: SkillGet | None = None,
    file_lister: FileLister | None = None,
    learn_hook: LearnHook | None = None,
    executor_runner: ExecutorRunner | None = None,
    session_attacher: Callable[[str], ExecutorResult] | None = None,
    spawner: SessionSpawner | None = None,
    cheap_adapter: Any | None = None,
    session_store: SessionStatusStore | None = None,
    session_monotonic: Callable[[], float] | None = None,
    session_sleep: Callable[[float], None] | None = None,
    session_timeout_seconds: float = 30 * 60.0,
    session_poll_interval_seconds: float = 0.5,
    vault_dir: str | None = None,
    budget_usd_max: float | None = None,
    cascade_trace_path: str | None = None,
    reflector: Callable[[str, str], Reflection] | None = None,
    reflexion_store_dir: str | None = None,
) -> OrchestrationResult:
    """Convenience entry point (Fable **medium**): one orchestration, one call.

    ``priority`` (``fast`` | ``balanced`` | ``quality``, default ``balanced``) and
    ``pins`` DRIVE the planner effort, executor tier, and quality gate; all other
    arguments are injectable seams (default to the real modules).
    """
    orchestrator = Orchestrator(
        planner_llm=planner_llm,
        reviewer=reviewer,
        approval_notifier=approval_notifier,
        context_reader=context_reader,
        skill_search=skill_search,
        skill_get=skill_get,
        file_lister=file_lister,
        learn_hook=learn_hook,
        executor_runner=executor_runner,
        session_attacher=session_attacher,
        spawner=spawner,
        cheap_adapter=cheap_adapter,
        session_store=session_store,
        session_monotonic=session_monotonic,
        session_sleep=session_sleep,
        session_timeout_seconds=session_timeout_seconds,
        session_poll_interval_seconds=session_poll_interval_seconds,
        vault_dir=vault_dir,
        budget_usd_max=budget_usd_max,
        cascade_trace_path=cascade_trace_path,
        reflector=reflector,
        reflexion_store_dir=reflexion_store_dir,
    )
    return orchestrator.run(
        goal,
        priority=priority,
        pins=pins,
        working_dir=working_dir,
        project_id=project_id,
        run_id=run_id,
        granted_roots=granted_roots,
        checkpoint=checkpoint,
        resume_state=resume_state,
    )


def _default_skill_search() -> SkillSearch | None:
    try:
        from omniagentos.skills import search

        return search
    except Exception:  # noqa: BLE001 -- skills backend optional; degrade to no skills.
        return None


def _default_skill_get() -> SkillGet | None:
    try:
        from omniagentos.skills import get_skill

        return get_skill
    except Exception:  # noqa: BLE001
        return None


__all__ = ["Orchestrator", "run_orchestration"]
