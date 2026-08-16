"""Execution runtime for prompt and orchestration-genome lab surfaces.

Candidate inputs enter this module only through ``LabStore.candidate_cases``.  Each
surface/case pair is persisted as an H1 runner task and run, so the normal ledger
manifest and vault-note finalization remains authoritative.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from omniagentos.adapters.registry import resolve_adapter
from omniagentos.contracts import (
    ActionClass,
    AgentAdapter,
    AgentInput,
    AgentResult,
    BudgetSpec,
    HarnessType,
    ResultStatus,
    RunState,
    TaskState,
    new_id,
    utc_now_iso,
)
from omniagentos.lab.contracts import (
    Budgets,
    CandidateEvalCase,
    GenomeRole,
    GenomeSpec,
    GenomeStage,
    SurfaceKind,
)
from omniagentos.lab.eval.paths import EVAL_KEY_ENV, PROTECTED_DB_ENV, assert_env_scrubbed
from omniagentos.lab.surfaces import OPERATOR_TOKEN_ENV
from omniagentos.mock_adapter import MockAdapter
from omniagentos.runner.core import Runner, RunnerDependencies

_SCRUBBED_CANDIDATE_ENV_VARS: tuple[str, ...] = (
    PROTECTED_DB_ENV,
    EVAL_KEY_ENV,
    OPERATOR_TOKEN_ENV,
    "TMPDIR",
    "TMP",
    "TEMP",
)
_ENV_LOCK = threading.RLock()
_ROLE_ALIASES = {
    "claude": HarnessType.CLI_CLAUDE,
    "codex": HarnessType.CLI_CODEX,
    "grok": HarnessType.CLI_GROK,
}


@contextmanager
def _scrubbed_environment(enabled: bool) -> Iterator[None]:
    """Temporarily remove protected-store discovery hints before child spawn.

    Environment mutation is process-global, so adapter launches are serialized for
    the short interval in which their subprocess inherits the environment.

    Temp-root scrubbing is defense in depth only: a candidate can discover a
    platform temp root another way, so encryption at rest is the primary defense.
    """

    if not enabled:
        yield
        return
    with _ENV_LOCK:
        sentinel = object()
        previous = {key: os.environ.pop(key, sentinel) for key in _SCRUBBED_CANDIDATE_ENV_VARS}
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is not sentinel:
                    os.environ[key] = cast(str, value)


def _harness(agent: str) -> HarnessType:
    normalized = agent.strip().lower()
    if normalized in _ROLE_ALIASES:
        return _ROLE_ALIASES[normalized]
    try:
        return HarnessType(normalized)
    except ValueError as exc:
        raise ValueError(f"genome role uses unknown adapter {agent!r}") from exc


def _budget_spec(budgets: Budgets, genome_budget: dict[str, Any] | None = None) -> BudgetSpec:
    configured = genome_budget or {}

    def minimum(left: int | float | None, right: int | float | None) -> int | float | None:
        values = [value for value in (left, right) if value is not None]
        return min(values) if values else None

    genome_wall = configured.get("wall_min")
    genome_tokens = configured.get("tokens")
    genome_cost = configured.get("cost_usd")
    wall_minutes = minimum(
        budgets.wall_minutes, float(genome_wall) if genome_wall is not None else None
    )
    tokens = minimum(budgets.tokens, int(genome_tokens) if genome_tokens is not None else None)
    cost = minimum(budgets.cost_usd, float(genome_cost) if genome_cost is not None else None)
    return BudgetSpec(
        wall_ms_max=None if wall_minutes is None else int(float(wall_minutes) * 60_000),
        tokens_max=None if tokens is None else int(tokens),
        cost_usd_max=None if cost is None else float(cost),
    )


def _validate_genome(genome: GenomeSpec) -> tuple[dict[str, GenomeRole], set[str]]:
    roles: dict[str, GenomeRole] = {}
    for role in genome.roles:
        if role.name in roles:
            raise ValueError(f"duplicate genome role {role.name!r}")
        _harness(role.agent)
        roles[role.name] = role
    if not roles:
        raise ValueError("genome must contain at least one role")

    stage_names: set[str] = set()
    for stage in genome.flow:
        if stage.stage in stage_names:
            raise ValueError(f"duplicate genome stage {stage.stage!r}")
        if stage.role not in roles:
            raise ValueError(f"genome stage {stage.stage!r} references unknown role {stage.role!r}")
        if stage.kind not in {"generate", "review", "judge", "iterate", "synthesize"}:
            raise ValueError(f"genome stage {stage.stage!r} has unsupported kind {stage.kind!r}")
        if stage.iterations < 1:
            raise ValueError(f"genome stage {stage.stage!r} must run at least once")
        unknown = set(stage.inputs_from) - stage_names
        if unknown:
            raise ValueError(
                f"genome stage {stage.stage!r} references unavailable prior stages: "
                f"{', '.join(sorted(unknown))}"
            )
        stage_names.add(stage.stage)
    if not genome.flow:
        raise ValueError("genome flow must contain at least one stage")
    return roles, stage_names


def _render_sources(
    stage: GenomeStage,
    context: dict[str, Any],
    *,
    blind: bool,
) -> str:
    rendered: list[str] = []
    for source in stage.inputs_from:
        value = context.get(source, {})
        if isinstance(value, dict):
            text = str(value.get("output_text") or value.get("text") or "")
            structured = value.get("output_json", value.get("json"))
        else:
            text = str(value)
            structured = None
        label = secrets.token_urlsafe(12) if blind else source
        payload = text
        if structured is not None:
            payload += f"\nStructured output: {json.dumps(structured, sort_keys=True)}"
        rendered.append(f"[{label}]\n{payload}")
    return "\n\n".join(rendered) or "(no prior-stage output)"


def _stage_prompt(
    stage: GenomeStage,
    case_input: dict[str, Any],
    context: dict[str, Any],
    genome: GenomeSpec,
) -> str:
    blind = stage.kind == "judge" and bool(genome.judge.get("blind", True))
    sources = _render_sources(stage, context, blind=blind)
    prior_iteration = ""
    if stage.stage in context and stage.stage not in stage.inputs_from:
        prior_iteration = "\n\nPrior iteration of this stage:\n" + _render_sources(
            stage.model_copy(update={"inputs_from": [stage.stage]}),
            context,
            blind=blind,
        )
    instructions = {
        "generate": "Produce the strongest candidate response to the case.",
        "review": "Review the candidate material and identify concrete corrections.",
        "judge": "Judge the anonymized candidate material against the case; do not infer authorship.",
        "iterate": "Revise the candidate response using the supplied material.",
        "synthesize": "Synthesize the supplied material into the final answer only.",
    }[stage.kind]
    policy = (
        genome.judge if stage.kind == "judge" else genome.review if stage.kind == "review" else {}
    )
    return (
        f"Orchestration stage: {stage.stage} ({stage.kind})\n"
        f"Instruction: {instructions}\n"
        f"Stage policy: {json.dumps(policy, sort_keys=True)}\n"
        f"Case input: {json.dumps(case_input, sort_keys=True)}\n\n"
        f"Prior-stage material:\n{sources}{prior_iteration}"
    )


def _prompt_surface_prompt(system_prompt: str, case_input: dict[str, Any], rubric: str) -> str:
    return (
        "System instructions:\n"
        f"{system_prompt}\n\n"
        f"Case input:\n{json.dumps(case_input, sort_keys=True)}\n\n"
        f"Candidate-visible rubric:\n{rubric or '(none)'}"
    )


class _ExecutorAdapter:
    """Runner adapter that adds prompt/genome semantics before delegating."""

    version = "1.0"

    def __init__(self, delegate: AgentAdapter, *, env_scrub: bool) -> None:
        self.delegate = delegate
        self.env_scrub = env_scrub
        self.name = str(getattr(delegate, "name", "executor"))
        self.version = str(getattr(delegate, "version", self.version))

    def run(self, input: AgentInput) -> AgentResult:
        executor = input.metadata.get("executor", {})
        if not isinstance(executor, dict):
            executor = {}
        mode = executor.get("mode")
        if mode == "prompt":
            prompt = _prompt_surface_prompt(
                str(executor.get("system_prompt", "")),
                dict(executor.get("case_input", {})),
                str(executor.get("rubric", "")),
            )
        elif mode == "genome":
            stage = GenomeStage.model_validate(executor["stage"])
            genome = GenomeSpec.model_validate(executor["genome"])
            context = input.metadata.get("context", {})
            prompt = _stage_prompt(
                stage,
                dict(executor.get("case_input", {})),
                dict(context) if isinstance(context, dict) else {},
                genome,
            )
        else:
            prompt = input.prompt
        delegated = input.model_copy(update={"prompt": prompt})
        with _scrubbed_environment(self.env_scrub):
            if self.env_scrub:
                assert_env_scrubbed(os.environ)
            return self.delegate.run(delegated)

    def cancel(self, session_ref: str) -> bool:
        return self.delegate.cancel(session_ref)

    def health(self) -> Any:
        return self.delegate.health()


def _resolve_for_run(*, dry_run: bool, env_scrub: bool) -> Callable[[HarnessType], AgentAdapter]:
    cache: dict[HarnessType, AgentAdapter] = {}

    def resolve(harness: HarnessType) -> AgentAdapter:
        if harness not in cache:
            delegate: AgentAdapter = MockAdapter() if dry_run else resolve_adapter(harness)
            cache[harness] = cast(AgentAdapter, _ExecutorAdapter(delegate, env_scrub=env_scrub))
        return cache[harness]

    return resolve


def execute_genome(
    genome: GenomeSpec,
    case_input: dict[str, Any],
    budgets: Budgets,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute a validated genome directly and return its final synthesized output."""

    genome = GenomeSpec.model_validate(genome)
    roles, _ = _validate_genome(genome)
    budget = _budget_spec(budgets, genome.budget)
    from omniagentos.budget.simulation import reserve_live_simulation

    reservation = reserve_live_simulation(
        budgets,
        budget,
        new_id("sim"),
        dry_run=dry_run,
    )
    context: dict[str, Any] = {}
    total_wall = 0
    total_tokens = 0
    total_cost = 0.0

    for stage in genome.flow:
        role = roles[stage.role]
        adapter: AgentAdapter = MockAdapter() if dry_run else resolve_adapter(_harness(role.agent))
        result: AgentResult | None = None
        for iteration in range(stage.iterations):
            iteration_context = dict(context)
            if result is not None:
                iteration_context[stage.stage] = result.model_dump(mode="json")
            prompt = _stage_prompt(stage, case_input, iteration_context, genome)
            metadata: dict[str, Any] = {
                "genome_id": genome.id,
                "stage": stage.stage,
                "iteration": iteration,
                "effort": role.effort,
            }
            if dry_run:
                metadata["mock"] = {"reply": f"dry-run {stage.kind}:{stage.stage}"}
            with _scrubbed_environment(True):
                result = adapter.run(
                    AgentInput(
                        run_id=new_id("run"),
                        task_id=new_id("tsk"),
                        prompt=prompt,
                        model=role.model,
                        tools_allowed=[],
                        budget=budget,
                        metadata=metadata,
                    )
                )
            if result.status is not ResultStatus.OK:
                raise RuntimeError(result.error or result.status.value)
            total_wall += result.usage.wall_ms
            total_tokens += int(result.usage.input_tokens or 0) + int(
                result.usage.output_tokens or 0
            )
            total_cost += float(result.usage.cost_usd or 0.0)
            if budget.wall_ms_max is not None and total_wall > budget.wall_ms_max:
                raise RuntimeError("budget_exceeded:wall")
            if budget.tokens_max is not None and total_tokens > budget.tokens_max:
                raise RuntimeError("budget_exceeded:tokens")
            if budget.cost_usd_max is not None and total_cost > budget.cost_usd_max:
                raise RuntimeError("budget_exceeded:cost")
            if reservation is not None:
                reservation.record_spend(total_cost)
        assert result is not None
        context[stage.stage] = result.model_dump(mode="json")

    final = context[genome.flow[-1].stage]
    return {
        "text": str(final.get("output_text") or ""),
        "json": final.get("output_json"),
    }


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _surface_content(surface: dict[str, Any]) -> str | dict[str, Any]:
    if "content" in surface:
        content = surface["content"]
        if isinstance(content, str | dict):
            return content
        raise ValueError("surface content must be text or an object")
    raw_path = surface.get("path")
    if not raw_path:
        raise ValueError("surface has neither content nor path")
    path = Path(str(raw_path)).expanduser()
    if not path.is_absolute():
        path = _repo_root() / path
    text = path.read_text(encoding="utf-8")
    if str(surface.get("kind")) == SurfaceKind.ORCHESTRATION_GENOME.value:
        decoded = json.loads(text)
        if not isinstance(decoded, dict):
            raise ValueError("genome surface file must contain a JSON object")
        return decoded
    return text


def _runner_store(store: Any) -> Any:
    candidate = getattr(store, "_store", store)
    required = ("create_task", "enqueue_run", "get_run", "get_steps")
    if not all(callable(getattr(candidate, method, None)) for method in required):
        raise TypeError("store must expose the H1 Store API (directly or via _store)")
    return candidate


def _case_plan(
    surface: dict[str, Any],
    content: str | dict[str, Any],
    case: CandidateEvalCase,
    working_dir: str,
    *,
    dry_run: bool,
) -> tuple[list[dict[str, Any]], HarnessType, BudgetSpec]:
    kind = SurfaceKind(str(surface["kind"]))
    if kind is SurfaceKind.PROMPT:
        if not isinstance(content, str):
            raise ValueError("prompt surface content must be text")
        harness = (
            HarnessType.MOCK
            if dry_run
            else _harness(str(surface.get("agent") or surface.get("adapter") or "cli-claude"))
        )
        params: dict[str, Any] = {
            "adapter": harness.value,
            "working_dir": working_dir,
            "metadata": {
                "executor": {
                    "mode": "prompt",
                    "system_prompt": content,
                    "case_input": case.input,
                    "rubric": case.rubric,
                }
            },
        }
        if dry_run:
            params["mock"] = {"reply": f"dry-run prompt:{case.id}"}
        return [_agent_step("prompt", params)], harness, BudgetSpec()

    if kind is not SurfaceKind.ORCHESTRATION_GENOME:
        raise ValueError(f"unsupported executable surface kind {kind.value!r}")
    genome = GenomeSpec.model_validate(content)
    roles, _ = _validate_genome(genome)
    plan: list[dict[str, Any]] = []
    first_harness = HarnessType.MOCK if dry_run else _harness(roles[genome.flow[0].role].agent)
    for stage in genome.flow:
        role = roles[stage.role]
        for iteration in range(stage.iterations):
            harness = HarnessType.MOCK if dry_run else _harness(role.agent)
            params = {
                "adapter": harness.value,
                "working_dir": working_dir,
                "model": role.model,
                "metadata": {
                    "executor": {
                        "mode": "genome",
                        "genome": genome.model_dump(mode="json"),
                        "stage": stage.model_dump(mode="json"),
                        "case_input": case.input,
                        "iteration": iteration,
                        "effort": role.effort,
                    }
                },
            }
            if dry_run:
                params["mock"] = {"reply": f"dry-run {stage.kind}:{stage.stage}"}
            plan.append(_agent_step(stage.stage, params))
    return plan, first_harness, _budget_spec(Budgets(), genome.budget)


def _agent_step(name: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "agent",
        "action_class": ActionClass.SANDBOXED_CREATION.value,
        "params": params,
    }


def run_surface_over_cases(
    store: Any,
    surface: dict[str, Any],
    suite_id: str,
    split: str,
    budgets: Budgets,
    *,
    env_scrub: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run a prompt or genome surface over candidate-safe suite cases."""

    budgets = Budgets.model_validate(budgets)
    cases = [
        CandidateEvalCase.model_validate(case) for case in store.candidate_cases(suite_id, split)
    ]
    content = _surface_content(surface)
    h1_store = _runner_store(store)
    outputs: dict[str, dict[str, Any]] = {}
    manifests: list[str] = []

    base_dependencies = RunnerDependencies.load()
    dependencies = replace(
        base_dependencies,
        resolve_adapter=_resolve_for_run(dry_run=dry_run, env_scrub=env_scrub),
    )
    runner = Runner(h1_store, new_id("lab-worker"), dependencies=dependencies)

    for case in cases:
        run_id = new_id("run")
        workspace = runner._run_workspace(run_id)
        plan, harness, genome_budget = _case_plan(
            surface,
            content,
            case,
            str(workspace),
            dry_run=dry_run,
        )
        requested_budget = _budget_spec(budgets)
        effective_budget = BudgetSpec(
            wall_ms_max=_min_optional(requested_budget.wall_ms_max, genome_budget.wall_ms_max),
            tokens_max=_min_optional(requested_budget.tokens_max, genome_budget.tokens_max),
            cost_usd_max=_min_optional(requested_budget.cost_usd_max, genome_budget.cost_usd_max),
        )
        from omniagentos.budget.simulation import reserve_live_simulation

        reservation = reserve_live_simulation(
            budgets,
            effective_budget,
            run_id,
            dry_run=dry_run,
        )
        now = utc_now_iso()
        task_id = new_id("tsk")
        h1_store.create_task(
            {
                "id": task_id,
                "discipline_id": None,
                "title": f"lab surface {surface.get('id', '')} case {case.id}",
                "input_json": json.dumps(
                    {"case_id": case.id, "input": case.input, "rubric": case.rubric},
                    sort_keys=True,
                ),
                "acceptance_json": "{}",
                # This synchronous executor owns the fresh task/run immediately;
                # execute_run is also the H1 restart/resume entry point.
                "state": TaskState.RUNNING.value,
                "risk": "low",
                "created_at": now,
                "updated_at": now,
            }
        )
        h1_store.enqueue_run(
            {
                "id": run_id,
                "task_id": task_id,
                "discipline_id": None,
                "harness": harness.value,
                "harness_params": json.dumps(
                    {
                        "surface_id": surface.get("id"),
                        "suite_id": suite_id,
                        "split": split,
                        "case_id": case.id,
                        "env_scrub": env_scrub,
                        "dry_run": dry_run,
                    },
                    sort_keys=True,
                ),
                "state": RunState.RUNNING.value,
                "worker_id": runner.worker_id,
                "plan_json": json.dumps(plan, sort_keys=True),
                "budget_json": effective_budget.model_dump_json(),
                "trace_id": new_id("trace"),
                "queued_at": now,
                "started_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
        runner.execute_run(run_id)
        run = h1_store.get_run(run_id)
        if reservation is not None and run is not None:
            reservation.record_spend(float(run.get("cost_usd") or 0.0))
        if (
            run is None
            or run.get("state") != RunState.COMPLETED.value
            or not run.get("manifest_path")
        ):
            if run is None:
                error = "run was not claimed"
            elif run.get("state") != RunState.COMPLETED.value:
                error = str(run.get("error") or run.get("state"))
            else:
                error = "manifest finalization failed"
            raise RuntimeError(f"surface execution failed for case {case.id}: {error}")
        output_json = run.get("output_json")
        outputs[case.id] = {
            "text": str(run.get("output_text") or ""),
            "json": json.loads(str(output_json)) if output_json else None,
        }
        manifests.append(run_id)

    return {"outputs": outputs, "manifests": manifests}


def _min_optional(left: int | float | None, right: int | float | None) -> Any:
    values = [value for value in (left, right) if value is not None]
    return min(values) if values else None


__all__ = ["execute_genome", "run_surface_over_cases"]
