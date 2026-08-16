"""The full worker-as-planner loop, token-free (every external seam mocked).

One orchestration run drives: plan -> spec-written -> spawn(with injected context) ->
auto-approve-a-safe-action + escalate-a-money-action + escalate-a-delete ->
reviewer-confirm ends it / reviewer-deny spawns one corrective -> calls the learners.
The tier selection and the priority/pins knobs are asserted here too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.memory.contracts import ConversationTurn, ScopeRef
from omniagentos.orchestrator import Orchestrator, run_orchestration
from omniagentos.orchestrator.contracts import (
    ApprovalRequest,
    ExecutorRequest,
    ExecutorResult,
    ExecutorTier,
    HardStop,
    LearnEvent,
    ResumeState,
    ResumeStep,
    ReviewVerdict,
)

# --- goals whose cheap complexity estimate is stable -----------------------
_SIMPLE_GOAL = "Add a docstring to the greet function"
_COMPLEX_GOAL = (
    "Build an end-to-end platform: integrate multiple services, a pipeline, "
    "a dashboard and a migration workflow, then wire the whole system together"
)


# --- mock seams ------------------------------------------------------------


def _plan_llm(*, tasks: list[dict[str, Any]] | None = None, complexity: str = "complex") -> Any:
    """A planner-LLM stub: returns a fixed plan JSON, records the effort it ran at."""
    calls: list[str] = []

    def _llm(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
        calls.append(effort)
        return {
            "project_name": "Greeter revamp",
            "description": "Improve the greeter",
            "complexity": complexity,
            "tasks": tasks
            or [
                {
                    "title": "Task one",
                    "description": "do the thing",
                    "acceptance_criteria": ["works"],
                }
            ],
        }

    _llm.calls = calls  # type: ignore[attr-defined]
    return _llm


@dataclass
class _MockRunner:
    """A stand-in executor: drives the approval gateway, then returns output."""

    approval_requests: list[ApprovalRequest] = field(default_factory=list)
    output: str = "executor did the work"
    calls: list[ExecutorRequest] = field(default_factory=list)
    session_store: Any = None

    def run(self, request: ExecutorRequest, gateway: Any) -> ExecutorResult:
        self.calls.append(request)
        for req in self.approval_requests:
            gateway.resolve(req)
        session_id = f"ses{len(self.calls)}"
        if request.on_spawn is not None:
            request.on_spawn(session_id)
        return ExecutorResult(status="ok", output_text=self.output, session_id=session_id)

    def attach(self, session_id: str) -> ExecutorResult:
        """Re-attach to a running session (for resume scenarios)."""
        if not hasattr(self, "session_store") or self.session_store is None:
            return ExecutorResult(
                status="error",
                session_id=session_id,
                error="no session store available for attach",
            )
        session = self.session_store.get_session(session_id)
        if session["state"] == "completed":
            return ExecutorResult(
                status="ok",
                output_text=session.get("output_text", self.output),
                session_id=session_id,
            )
        elif session["state"] == "killed":
            return ExecutorResult(
                status="error",
                session_id=session_id,
                error="session was killed",
                output_text=session.get("output_text", ""),
            )
        return ExecutorResult(
            status="error",
            session_id=session_id,
            error="session in unknown state",
        )


@dataclass
class _MockReviewer:
    verdicts: list[str]
    feedback: str = "address criterion 'works'"
    calls: int = 0

    def review(self, *, task: Any, spec_markdown: str, result: ExecutorResult) -> ReviewVerdict:
        verdict = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        return ReviewVerdict(verdict=verdict, feedback=self.feedback, reviewer="mock")


@dataclass
class _ExplodingReviewer:
    called: bool = False

    def review(self, *, task: Any, spec_markdown: str, result: ExecutorResult) -> ReviewVerdict:
        self.called = True
        raise AssertionError("reviewer must not run when the quality gate is skipped")


@dataclass
class _RecordingNotifier:
    calls: list[str] = field(default_factory=list)

    def escalate(self, request: ApprovalRequest, category: HardStop) -> str | None:
        self.calls.append(category)
        return f"notif-{len(self.calls)}"


@dataclass
class _RecordingLearn:
    events: list[LearnEvent] = field(default_factory=list)

    def __call__(self, event: LearnEvent) -> None:
        self.events.append(event)


@dataclass
class _RecordingCheckpoint:
    events: list[tuple[Any, ...]] = field(default_factory=list)

    def record_plan(self, run_id: str, plan_json: str, step_titles: list[str]) -> None:
        self.events.append(("plan", run_id, plan_json, step_titles))

    def step_started(self, run_id: str, seq: int, attempts: int) -> None:
        self.events.append(("started", run_id, seq, attempts))

    def step_session(self, run_id: str, seq: int, session_id: str) -> None:
        self.events.append(("session", run_id, seq, session_id))

    def step_finished(
        self,
        run_id: str,
        seq: int,
        status: str,
        attempts: int,
        output_tail: str,
    ) -> None:
        self.events.append(("finished", run_id, seq, status, attempts, output_tail))


class _AttachStore:
    def __init__(self, state: str, *, output_text: str = "") -> None:
        self.state = state
        self.output_text = output_text
        self.lookups: list[str] = []

    def mark_orchestrator_session(self, session_id: str, run_id: str | None = None) -> None:
        del session_id, run_id

    def get_session(self, session_id: str) -> dict[str, Any]:
        self.lookups.append(session_id)
        return {
            "id": session_id,
            "state": self.state,
            "output_text": self.output_text,
        }


class _NeverSpawn:
    def spawn(self, **kwargs: Any) -> str:
        raise AssertionError(f"attach must not spawn: {kwargs}")


class _StubReader:
    """A ConversationReader stub so a memory block is genuinely injected."""

    def recent_turns(self, scope_type: str, scope_id: str, limit: int) -> list[ConversationTurn]:
        return [ConversationTurn(seq=1, role="user", content="prior decision: use tabs")]

    def resolve_ancestors(self, scope_type: str, scope_id: str) -> list[ScopeRef]:
        return []

    def rolling_summary(self, scope_type: str, scope_id: str) -> str | None:
        return "This project standardises greetings."


def _skill_search(q: str, *, limit: int = 20) -> list[dict[str, Any]]:
    return [
        {
            "id": "sk1",
            "title": "Docstring style",
            "summary": "Google-style docstrings",
            "score": 0.9,
        }
    ]


def _skill_get(skill_id: str) -> dict[str, Any]:
    return {"id": skill_id, "summary": "Google-style docstrings", "preferred_method": "..."}


def _file_lister(working_dir: str) -> list[str]:
    return ["greeter.py", "tests/test_greeter.py"]


def _resume_plan_json(*titles: str) -> str:
    from omniagentos.intake.planner import ProjectPlan

    return ProjectPlan.model_validate(
        {
            "project_name": "Resumable work",
            "complexity": "simple",
            "tasks": [{"title": title, "acceptance_criteria": ["works"]} for title in titles],
        }
    ).model_dump_json()


def _build(
    *,
    runner: _MockRunner,
    reviewer: Any,
    notifier: _RecordingNotifier,
    learn: _RecordingLearn,
    vault_dir: Path,
    plan_llm: Any | None = None,
) -> Orchestrator:
    return Orchestrator(
        planner_llm=plan_llm or _plan_llm(),
        reviewer=reviewer,
        approval_notifier=notifier,
        context_reader=_StubReader(),
        skill_search=_skill_search,
        skill_get=_skill_get,
        file_lister=_file_lister,
        learn_hook=learn,
        executor_runner=runner,
        vault_dir=str(vault_dir),
    )


# --- the full loop ---------------------------------------------------------


def test_full_run_plan_spec_spawn_approvals_review_learn(tmp_path: Path) -> None:
    runner = _MockRunner(
        approval_requests=[
            ApprovalRequest(
                "edit greeter.py", "consequential", "Edit", {"file_path": "/w/greeter.py"}
            ),
            ApprovalRequest("transfer $200 to the CI vendor", "consequential"),
            ApprovalRequest("clean build", "consequential", "Bash", {"command": "rm -rf build"}),
        ]
    )
    reviewer = _MockReviewer(verdicts=["confirm"])
    notifier = _RecordingNotifier()
    learn = _RecordingLearn()
    orch = _build(
        runner=runner, reviewer=reviewer, notifier=notifier, learn=learn, vault_dir=tmp_path
    )

    result = orch.run(
        _COMPLEX_GOAL, priority="balanced", working_dir=str(tmp_path), project_id="proj1"
    )

    # Stage 1: plan -> spec WRITTEN to the vault, and the plan is the planner's.
    assert result.plan.project_name == "Greeter revamp"
    assert result.spec_note_path is not None
    assert Path(result.spec_note_path).is_file()
    assert "Greeter revamp" in Path(result.spec_note_path).read_text()

    # Stage 2: the executor was spawned ONCE with the INJECTED context.
    assert len(runner.calls) == 1
    prompt = runner.calls[0].prompt
    assert "## Spec" in prompt and "Greeter revamp" in prompt  # spec
    assert "This project standardises greetings." in prompt  # memory
    assert "greeter.py" in prompt  # scoped files
    assert "Docstring style" in prompt  # matched skills
    assert "Task one" in prompt  # the task itself

    # Stage 3: safe auto-approves; money + delete escalate.
    assert len(result.approvals) == 3
    approved = [d for d in result.approvals if d.approved]
    escalated = [d for d in result.approvals if d.escalated]
    assert len(approved) == 1 and approved[0].category is None
    assert {d.category for d in escalated} == {"money", "delete"}
    assert notifier.calls == ["money", "delete"]

    # Stage 4: reviewer CONFIRM ends the task.
    assert reviewer.calls == 1
    assert result.tasks[0].status == "done"
    assert result.status == "done"

    # Stage 5: the learners fired for the completed session.
    assert len(learn.events) == 1
    assert learn.events[0].session_id == "ses1"
    assert learn.events[0].vault_dir == str(tmp_path)


def test_checkpoint_records_plan_and_step_transitions_in_order(
    tmp_path: Path,
) -> None:
    from omniagentos.intake.planner import ProjectPlan

    runner = _MockRunner(output="x" * 2100)
    checkpoint = _RecordingCheckpoint()
    orch = _build(
        runner=runner,
        reviewer=_MockReviewer(verdicts=["confirm"]),
        notifier=_RecordingNotifier(),
        learn=_RecordingLearn(),
        vault_dir=tmp_path,
    )

    result = orch.run(
        _COMPLEX_GOAL,
        priority="balanced",
        working_dir=str(tmp_path),
        run_id="orch-checkpoint",
        checkpoint=checkpoint,
    )

    assert [event[0] for event in checkpoint.events] == [
        "plan",
        "started",
        "session",
        "finished",
    ]
    assert checkpoint.events[0][1] == "orch-checkpoint"
    assert checkpoint.events[0][3] == ["Task one"]
    assert ProjectPlan.model_validate_json(checkpoint.events[0][2]) == result.plan
    assert checkpoint.events[1] == ("started", "orch-checkpoint", 0, 1)
    assert checkpoint.events[2] == ("session", "orch-checkpoint", 0, "ses1")
    assert checkpoint.events[3][:5] == (
        "finished",
        "orch-checkpoint",
        0,
        "done",
        1,
    )
    assert checkpoint.events[3][5] == "x" * 2000


def test_resume_skips_finished_step_without_planning(tmp_path: Path) -> None:
    def exploding_planner(prompt: str, schema: dict[str, Any], effort: str) -> dict[str, Any]:
        del prompt, schema, effort
        raise AssertionError("planner must not run during resume")

    runner = _MockRunner()
    orch = _build(
        runner=runner,
        reviewer=_MockReviewer(verdicts=["confirm"]),
        notifier=_RecordingNotifier(),
        learn=_RecordingLearn(),
        vault_dir=tmp_path,
        plan_llm=exploding_planner,
    )
    resume = ResumeState(
        plan_json=_resume_plan_json("Already done", "Still pending"),
        steps=[
            ResumeStep(0, "Already done", "done", "ses-old", 1, "saved output"),
            ResumeStep(1, "Still pending", "pending", None, 0),
        ],
    )

    result = orch.run(
        _SIMPLE_GOAL,
        priority="balanced",
        working_dir=str(tmp_path),
        run_id="orch-resumed",
        resume_state=resume,
    )

    assert len(runner.calls) == 1
    assert runner.calls[0].title == "Still pending"
    assert [outcome.task_title for outcome in result.tasks] == [
        "Already done",
        "Still pending",
    ]
    assert result.tasks[0].output_text == "saved output"
    assert result.tasks[0].session_id == "ses-old"
    assert result.status == "done"
    assert result.spec_note_path is None
    assert not (tmp_path / "ORCHESTRATION_SPEC.md").exists()


def test_resume_attaches_completed_session_then_runs_remaining_step(
    tmp_path: Path,
) -> None:
    store = _AttachStore("completed", output_text="attached output")
    runner = _MockRunner()
    runner.session_store = store
    reviewer = _MockReviewer(verdicts=["confirm", "confirm"])
    orch = Orchestrator(
        planner_llm=lambda *_args: None,
        reviewer=reviewer,
        executor_runner=runner,
        spawner=_NeverSpawn(),
        session_store=store,
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
    )
    resume = ResumeState(
        plan_json=_resume_plan_json("Was running", "Next task"),
        steps=[
            ResumeStep(0, "Was running", "running", "ses-live", 1),
            ResumeStep(1, "Next task", "pending", None, 0),
        ],
    )

    result = orch.run(
        _SIMPLE_GOAL,
        priority="balanced",
        working_dir=str(tmp_path),
        run_id="orch-resumed",
        resume_state=resume,
    )

    assert store.lookups == ["ses-live"]
    assert [request.title for request in runner.calls] == ["Next task"]
    assert reviewer.calls == 2
    assert result.tasks[0].output_text == "attached output"
    assert result.tasks[0].session_id == "ses-live"
    assert result.status == "done"


def test_killed_attachment_retries_with_persisted_attempt_count(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    store = _AttachStore("killed", output_text="partial output")
    runner = _MockRunner(output="retry succeeded")
    runner.session_store = store
    orch = Orchestrator(
        reviewer=_MockReviewer(verdicts=["confirm"]),
        executor_runner=runner,
        spawner=_NeverSpawn(),
        session_store=store,
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
        cascade_trace_path=str(tmp_path / "traces.jsonl"),
    )
    resume = ResumeState(
        plan_json=_resume_plan_json("Was killed"),
        steps=[ResumeStep(0, "Was killed", "running", "ses-killed", 1)],
    )

    result = orch.run(
        _SIMPLE_GOAL,
        priority="balanced",
        working_dir=str(tmp_path),
        run_id="orch-resumed",
        resume_state=resume,
    )

    assert store.lookups == ["ses-killed"]
    assert len(runner.calls) == 1
    assert runner.calls[0].attempt == 2
    assert result.tasks[0].attempts == 2
    assert result.tasks[0].status == "done"


def test_run_threads_granted_roots_into_executor_request_and_prompt(tmp_path: Path) -> None:
    """FIX 6: granted_roots passed to run() reach BOTH the executor request (frozen onto
    the spawned session) AND the injected prompt's granted-scope section, so an
    orchestrate-mode session is scoped exactly like an intake session."""
    runner = _MockRunner()
    reviewer = _MockReviewer(verdicts=["confirm"])
    orch = _build(
        runner=runner,
        reviewer=reviewer,
        notifier=_RecordingNotifier(),
        learn=_RecordingLearn(),
        vault_dir=tmp_path,
    )
    roots = ["/srv/repo2", "/srv/shared"]

    orch.run(
        _COMPLEX_GOAL,
        priority="balanced",
        working_dir=str(tmp_path),
        project_id="proj1",
        granted_roots=roots,
    )

    assert runner.calls[0].granted_roots == roots
    prompt = runner.calls[0].prompt
    assert "## Granted scope" in prompt
    assert "/srv/repo2" in prompt and "/srv/shared" in prompt


def test_run_without_granted_roots_leaves_request_scope_null(tmp_path: Path) -> None:
    """Invariant: omitting granted_roots keeps the request's scope None and injects no
    granted-scope section -- pre-P3 working-dir-only behavior, unchanged."""
    runner = _MockRunner()
    reviewer = _MockReviewer(verdicts=["confirm"])
    orch = _build(
        runner=runner,
        reviewer=reviewer,
        notifier=_RecordingNotifier(),
        learn=_RecordingLearn(),
        vault_dir=tmp_path,
    )

    orch.run(_COMPLEX_GOAL, priority="balanced", working_dir=str(tmp_path))

    assert runner.calls[0].granted_roots is None
    assert "## Granted scope" not in runner.calls[0].prompt


def test_reviewer_deny_spawns_one_corrective_with_feedback(tmp_path: Path) -> None:
    runner = _MockRunner()
    reviewer = _MockReviewer(verdicts=["deny", "confirm"])
    notifier = _RecordingNotifier()
    learn = _RecordingLearn()
    orch = _build(
        runner=runner, reviewer=reviewer, notifier=notifier, learn=learn, vault_dir=tmp_path
    )

    result = orch.run(_COMPLEX_GOAL, priority="balanced", working_dir=str(tmp_path))

    # One corrective iteration: a second executor spawn, carrying the review feedback.
    assert len(runner.calls) == 2
    assert "Reviewer feedback" in runner.calls[1].prompt
    assert "address criterion 'works'" in runner.calls[1].prompt
    assert result.tasks[0].status == "done"
    assert result.tasks[0].attempts == 2
    assert len(learn.events) == 2  # learners fire per executor completion


def test_reviewer_deny_capped_marks_denied(tmp_path: Path) -> None:
    runner = _MockRunner()
    reviewer = _MockReviewer(verdicts=["deny", "deny", "deny"])
    orch = _build(
        runner=runner,
        reviewer=reviewer,
        notifier=_RecordingNotifier(),
        learn=_RecordingLearn(),
        vault_dir=tmp_path,
    )

    result = orch.run(_COMPLEX_GOAL, priority="balanced", working_dir=str(tmp_path))

    # balanced caps at ONE corrective retry: attempt1 + one retry, then denied.
    assert len(runner.calls) == 2
    assert result.tasks[0].status == "denied"
    assert result.status == "failed"


def test_fast_skips_quality_gate_and_uses_cheap_tier(tmp_path: Path) -> None:
    runner = _MockRunner()
    reviewer = _ExplodingReviewer()
    orch = _build(
        runner=runner,
        reviewer=reviewer,
        notifier=_RecordingNotifier(),
        learn=_RecordingLearn(),
        vault_dir=tmp_path,
    )

    result = orch.run(_SIMPLE_GOAL, priority="fast", working_dir=str(tmp_path))

    assert result.resolved.executor_tier is ExecutorTier.CHEAP
    assert reviewer.called is False  # gate skipped: reviewer never ran
    assert len(runner.calls) == 1  # no iteration
    assert result.tasks[0].status == "unreviewed"
    assert result.status == "done"


def test_quality_uses_fusion_ultra_and_iterates_to_cap(tmp_path: Path) -> None:
    runner = _MockRunner()
    reviewer = _MockReviewer(verdicts=["deny", "deny", "confirm"])
    orch = _build(
        runner=runner,
        reviewer=reviewer,
        notifier=_RecordingNotifier(),
        learn=_RecordingLearn(),
        vault_dir=tmp_path,
    )

    result = orch.run(_SIMPLE_GOAL, priority="quality", working_dir=str(tmp_path))

    assert result.resolved.executor_tier is ExecutorTier.FUSION_ULTRA
    # quality caps at 2 correctives: attempt1 + 2 retries = 3 spawns, then confirm.
    assert len(runner.calls) == 3
    assert result.tasks[0].status == "done"
    assert result.tasks[0].attempts == 3


def test_tier_selection_simple_cheap_complex_fusion(tmp_path: Path) -> None:
    def _run(goal: str) -> ExecutorTier:
        runner = _MockRunner()
        orch = _build(
            runner=runner,
            reviewer=_MockReviewer(verdicts=["confirm"]),
            notifier=_RecordingNotifier(),
            learn=_RecordingLearn(),
            vault_dir=tmp_path,
            plan_llm=_plan_llm(complexity="simple"),
        )
        return orch.run(goal, priority="balanced", working_dir=str(tmp_path)).resolved.executor_tier

    assert _run(_SIMPLE_GOAL) is ExecutorTier.CHEAP
    assert _run(_COMPLEX_GOAL) is ExecutorTier.FUSION


def test_priority_drives_planner_effort(tmp_path: Path) -> None:
    for priority, expected in (("fast", "medium"), ("balanced", "high"), ("quality", "max")):
        spy = _plan_llm()
        orch = _build(
            runner=_MockRunner(),
            reviewer=_MockReviewer(verdicts=["confirm"]),
            notifier=_RecordingNotifier(),
            learn=_RecordingLearn(),
            vault_dir=tmp_path,
            plan_llm=spy,
        )
        orch.run(_SIMPLE_GOAL, priority=priority, working_dir=str(tmp_path))  # type: ignore[arg-type]
        assert spy.calls == [expected], (priority, spy.calls)


def test_planner_model_and_level_pins_surface_on_result(tmp_path: Path) -> None:
    spy = _plan_llm()
    orch = _build(
        runner=_MockRunner(),
        reviewer=_MockReviewer(verdicts=["confirm"]),
        notifier=_RecordingNotifier(),
        learn=_RecordingLearn(),
        vault_dir=tmp_path,
        plan_llm=spy,
    )
    # A simple goal under fast would default to medium; the explicit level pin wins,
    # and the pin drives the effort the planner actually runs at.
    result = orch.run(
        _SIMPLE_GOAL,
        priority="fast",
        pins={"planner_model": "opus", "planner_effort": "max"},
        working_dir=str(tmp_path),
    )
    assert result.resolved.planner_model == "opus"
    assert result.resolved.planner_effort == "max"
    assert spy.calls == ["max"]


def test_run_orchestration_entrypoint_is_concurrency_safe(tmp_path: Path) -> None:
    # The function form holds no shared per-run state -> two runs don't interfere.
    r1 = run_orchestration(
        _SIMPLE_GOAL,
        priority="fast",
        planner_llm=_plan_llm(complexity="simple"),
        executor_runner=_MockRunner(),
        reviewer=_MockReviewer(verdicts=["confirm"]),
        approval_notifier=_RecordingNotifier(),
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
        working_dir=str(tmp_path),
    )
    r2 = run_orchestration(
        _COMPLEX_GOAL,
        priority="balanced",
        planner_llm=_plan_llm(),
        executor_runner=_MockRunner(),
        reviewer=_MockReviewer(verdicts=["confirm"]),
        approval_notifier=_RecordingNotifier(),
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
        working_dir=str(tmp_path),
    )
    assert r1.run_id != r2.run_id
    assert r1.resolved.executor_tier is ExecutorTier.CHEAP
    assert r2.resolved.executor_tier is ExecutorTier.FUSION


# --- F-007: missing resume tests -----------------------------------------


def test_resume_early_exhausted_boundary(tmp_path: Path, monkeypatch: Any) -> None:
    """F-007: resume step status='failed' with attempts exactly at the cap → immediate
    failed outcome, no spawn; attempts one below cap → exactly one more attempt runs."""
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    runner = _MockRunner(output="retry succeeded")
    orch = Orchestrator(
        planner_llm=lambda *_: None,
        reviewer=_MockReviewer(verdicts=["confirm"]),
        executor_runner=runner,
        spawner=_NeverSpawn(),
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
    )
    # Exactly at cap (cascade effective_max_retries = max(0, 1) = 1)
    resume_at_cap = ResumeState(
        plan_json=_resume_plan_json("Was failed"),
        steps=[ResumeStep(0, "Was failed", "failed", None, 2)],
    )

    result_cap = orch.run(
        _SIMPLE_GOAL,
        priority="fast",
        working_dir=str(tmp_path),
        run_id="orch-exhausted",
        resume_state=resume_at_cap,
    )

    assert len(runner.calls) == 0
    assert result_cap.tasks[0].status == "failed"
    assert result_cap.tasks[0].attempts == 2

    # One below cap: should spawn once more
    runner_retry = _MockRunner(output="retry succeeded")
    orch_retry = Orchestrator(
        planner_llm=lambda *_: None,
        reviewer=_MockReviewer(verdicts=["confirm"]),
        executor_runner=runner_retry,
        spawner=_NeverSpawn(),
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
    )
    resume_below_cap = ResumeState(
        plan_json=_resume_plan_json("Was failed"),
        steps=[ResumeStep(0, "Was failed", "failed", None, 1)],
    )

    result_below = orch_retry.run(
        _SIMPLE_GOAL,
        priority="balanced",
        working_dir=str(tmp_path),
        run_id="orch-retryable",
        resume_state=resume_below_cap,
    )

    assert len(runner_retry.calls) == 1
    assert runner_retry.calls[0].attempt == 2
    assert result_below.tasks[0].status == "done"


def test_resume_multi_running_guard(tmp_path: Path) -> None:
    """F-007: resume_state with TWO running+session_id steps → only the first
    re-attaches; second executes fresh."""
    store = _AttachStore("completed", output_text="step0 output")
    runner = _MockRunner(output="step1 output")
    runner.session_store = store
    orch = Orchestrator(
        planner_llm=lambda *_: None,
        reviewer=_MockReviewer(verdicts=["confirm", "confirm"]),
        executor_runner=runner,
        spawner=_NeverSpawn(),
        session_store=store,
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
    )
    resume = ResumeState(
        plan_json=_resume_plan_json("First task", "Second task"),
        steps=[
            ResumeStep(0, "First task", "running", "ses-0", 1),
            ResumeStep(1, "Second task", "running", "ses-1", 1),
        ],
    )

    result = orch.run(
        _SIMPLE_GOAL,
        priority="balanced",
        working_dir=str(tmp_path),
        run_id="multi-running",
        resume_state=resume,
    )

    # Only ses-0 should be looked up (attached); ses-1 should execute fresh
    assert store.lookups == ["ses-0"]
    assert len(runner.calls) == 1
    assert runner.calls[0].title == "Second task"
    assert result.tasks[0].output_text == "step0 output"
    assert result.tasks[1].output_text == "step1 output"


def test_resume_plan_drift(tmp_path: Path) -> None:
    """F-007: resume_state with fewer steps than the plan (missing seq → step
    executes fresh) and with an extra seq beyond the plan (ignored, no crash)."""
    runner = _MockRunner()
    orch = Orchestrator(
        planner_llm=lambda *_: None,
        reviewer=_MockReviewer(verdicts=["confirm", "confirm"]),
        executor_runner=runner,
        spawner=_NeverSpawn(),
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
    )
    # Resume has only step 0 and a phantom step 2; plan has both 0 and 1
    resume = ResumeState(
        plan_json=_resume_plan_json("Task 0", "Task 1"),
        steps=[
            ResumeStep(0, "Task 0", "done", "ses-0", 1, "output0"),
            ResumeStep(2, "Phantom", "done", "ses-2", 1, "output2"),
        ],
    )

    result = orch.run(
        _SIMPLE_GOAL,
        priority="balanced",
        working_dir=str(tmp_path),
        run_id="drift-test",
        resume_state=resume,
    )

    # Step 1 should execute fresh (missing from resume)
    assert len(runner.calls) == 1
    assert runner.calls[0].title == "Task 1"
    # Both tasks should be in the result (reconstructed + new)
    assert len(result.tasks) == 2
    assert result.tasks[0].session_id == "ses-0"
    assert result.tasks[1].output_text == "executor did the work"


def test_resume_tier_seeding_and_attach_error_fallback(tmp_path: Path, monkeypatch: Any) -> None:
    """F-007: tier seeding (F-003) and attach-error fallback (F-005). When cascade
    is on, a resumed step with prior attempts should run at the escalated tier;
    a stub runner without attach should error gracefully and retry, not touch real store."""
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")

    # Stub runner: has no attach() method, so _attach should error and retry
    @dataclass
    class _StubRunnerNoAttach:
        output: str = "stub succeeded"
        calls: list[ExecutorRequest] = field(default_factory=list)

        def run(self, request: ExecutorRequest, gateway: Any) -> ExecutorResult:
            self.calls.append(request)
            session_id = f"ses{len(self.calls)}"
            if request.on_spawn is not None:
                request.on_spawn(session_id)
            return ExecutorResult(status="ok", output_text=self.output, session_id=session_id)

    stub_runner = _StubRunnerNoAttach()
    orch = Orchestrator(
        planner_llm=lambda *_: None,
        reviewer=_MockReviewer(verdicts=["confirm"]),
        executor_runner=stub_runner,
        spawner=_NeverSpawn(),
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
        cascade_trace_path=str(tmp_path / "traces.jsonl"),
    )
    # Resume with running step: should attempt to attach, get error, and retry
    resume = ResumeState(
        plan_json=_resume_plan_json("Task with attach error"),
        steps=[
            ResumeStep(0, "Task with attach error", "running", "ses-attached", 1),
        ],
    )

    result = orch.run(
        _SIMPLE_GOAL,
        priority="balanced",
        working_dir=str(tmp_path),
        run_id="attach-error",
        resume_state=resume,
    )

    # Should spawn once (after attach error), and the spawned attempt should have
    # the correct tier escalation (1 escalation for attempt 2)
    assert len(stub_runner.calls) == 1
    assert stub_runner.calls[0].attempt == 2
    # Tier escalation verification: with cascade on and attempt 2 from a running session,
    # current_tier should be escalated 1 rung (initial_attempts - 1 = 2 - 1 = 1)
    assert result.tasks[0].status == "done"
    assert result.tasks[0].attempts == 2
    assert result.tasks[0].tier in (ExecutorTier.FUSION, ExecutorTier.FUSION_ULTRA)


def test_cascade_replay_exact_tier_and_escalation(tmp_path: Path, monkeypatch: Any) -> None:
    """F-003: replay at initial_attempts=2, cascade ON.
    Assert: (1) replay tier is EXACTLY one rung above resolved.executor_tier,
    (2) subsequent failure escalates exactly one further rung,
    (3) escalated_from is set to the replayed tier value on that escalation."""
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")
    runner = _MockRunner(output="first attempt")
    runner.session_store = _AttachStore("completed", output_text="first attempt")
    reviewer_verdicts = ["deny", "confirm"]  # First verdict triggers escalation
    trace_path = tmp_path / "traces.jsonl"
    orch = Orchestrator(
        planner_llm=lambda *_: None,
        reviewer=_MockReviewer(verdicts=reviewer_verdicts),
        executor_runner=runner,
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
        cascade_trace_path=str(trace_path),
    )
    # Resume at attempt 2, cascade=1: should replay 1 rung above CHEAP
    resume = ResumeState(
        plan_json=_resume_plan_json("Resumable task"),
        steps=[ResumeStep(0, "Resumable task", "running", "ses-0", 2)],
    )
    result = orch.run(
        _SIMPLE_GOAL,
        priority="quality",
        pins={"executor_effort": "superfast"},  # cheap tier with two retries
        working_dir=str(tmp_path),
        run_id="cascade-replay",
        resume_state=resume,
    )

    # Replay is CHEAP → FUSION; the denial then escalates FUSION → FUSION_ULTRA.
    rows = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
    assert rows[0]["tier_name"] == ExecutorTier.FUSION.value
    assert rows[0]["escalated_from"] is None
    assert rows[1]["tier_name"] == ExecutorTier.FUSION_ULTRA.value
    assert rows[1]["escalated_from"] == ExecutorTier.FUSION.value
    assert result.tasks[0].attempts == 3
    assert result.tasks[0].tier == ExecutorTier.FUSION_ULTRA


def test_cascade_injected_runner_no_attach_returns_error(tmp_path: Path, monkeypatch: Any) -> None:
    """F-005: injected runner without attach method, resume with running+session_id.
    Assert: (1) _monitored_runner_for is NOT called (monkeypatch raises),
    (2) attach() returns error with 'no attacher available',
    (3) step result reflects the error."""
    monkeypatch.setenv("OMNIAGENTOS_CASCADE", "1")

    def raise_on_monitored_call(*_: Any, **__: Any) -> None:
        raise AssertionError(
            "_monitored_runner_for should not be called for injected runner without attach"
        )

    # Inject stub runner WITHOUT attach method
    class StubRunner:
        def run(self, request: ExecutorRequest, gateway: Any) -> ExecutorResult:
            raise AssertionError(
                f"runner should not execute after exhausted attach error: {request}, {gateway}"
            )

    orch = Orchestrator(
        planner_llm=lambda *_: None,
        reviewer=_MockReviewer(verdicts=["confirm"]),
        executor_runner=StubRunner(),
        learn_hook=_RecordingLearn(),
        vault_dir=str(tmp_path),
    )

    # Patch _monitored_runner_for to catch any call
    monkeypatch.setattr(orch, "_monitored_runner_for", raise_on_monitored_call)

    # Resume with running+session_id: should NOT spawn or call _monitored_runner_for
    # Should use _attach which checks for injected attach, finds none, returns error
    resume = ResumeState(
        plan_json=_resume_plan_json("Unreachable task"),
        steps=[ResumeStep(0, "Unreachable task", "running", "ses-gone", 2)],
    )

    result = orch.run(
        _SIMPLE_GOAL,
        priority="fast",
        working_dir=str(tmp_path),
        run_id="injected-no-attach",
        resume_state=resume,
    )

    # Executor error maps to the task-level failed status and preserves its message.
    assert result.tasks[0].status == "failed"
    assert "no attacher available" in result.tasks[0].output_text
