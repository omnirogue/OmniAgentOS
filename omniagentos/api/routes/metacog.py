"""HTTP surface for artifacts / memory / metacognition (Phases 1–8)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from omniagentos.api.services import fail
from omniagentos.contracts import default_db_path
from omniagentos.metacog.service import MetacogService

router = APIRouter(prefix="/api/metacog", tags=["metacog"])

_SERVICE: MetacogService | None = None


def get_metacog_service() -> MetacogService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = MetacogService(db_path=default_db_path())
    return _SERVICE


# --- request bodies --------------------------------------------------------


class RegisterArtifactBody(BaseModel):
    artifact_type: str
    content: str
    format: str = "json"
    company_id: str = "default"
    project_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    producer_agent_id: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    quality: dict[str, Any] = Field(default_factory=dict)
    retention_class: str = "project"
    access_scope: str = "task"
    supersedes: list[str] = Field(default_factory=list)


class CheckpointBody(BaseModel):
    state: dict[str, Any]
    run_id: str | None = None
    task_id: str | None = None
    attempt_id: str | None = None
    company_id: str = "default"
    project_id: str | None = None
    completed_criteria: list[str] = Field(default_factory=list)
    pending_criteria: list[str] = Field(default_factory=list)
    side_effects: list[dict[str, Any]] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    context_digest: str | None = None
    safe_to_resume: bool | None = None
    replay_risk: str = "none"


class CompileContextBody(BaseModel):
    task_id: str | None = None
    run_id: str | None = None
    objective: str = ""
    acceptance_criteria: list[str] = Field(default_factory=list)
    company_id: str = "default"
    project_id: str | None = None
    domain: str = "coding"


class MemorySearchBody(BaseModel):
    query: str
    company_id: str = "default"
    project_id: str | None = None
    limit: int = 20


class MemoryCandidateBody(BaseModel):
    statement: str
    evidence: list[str]
    memory_type: str = "lesson"
    company_id: str = "default"
    project_id: str | None = None
    domain: str | None = None
    confidence: float = 0.5
    applicability: dict[str, Any] = Field(default_factory=dict)


class PromoteBody(BaseModel):
    force: bool = False


class EvaluateBody(BaseModel):
    task_id: str | None = None
    run_id: str | None = None
    criteria_total: int = 0
    criteria_passed: int = 0
    previous_progress: float | None = None
    previous_actions: list[str] = Field(default_factory=list)
    strategy_id: str | None = None
    strategy_age_iterations: int = 0
    cost_used: float = 0.0
    time_used: float = 0.0
    fanout_active: int = 0
    verifier_capacity_reserved: int = 0
    context_pressure: float = 0.0
    recent_outputs: list[str] = Field(default_factory=list)


class StrategySelectBody(BaseModel):
    domain: str = "coding"
    novel: bool = False
    failed_strategy: str | None = None
    prefer_verifier: bool = False


class StrategySwitchBody(BaseModel):
    from_strategy: str | None = None
    to_strategy: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    force: bool = False


class ReflectBody(BaseModel):
    outcome: str
    run_id: str | None = None
    task_id: str | None = None
    evidence_artifact_ids: list[str] = Field(default_factory=list)
    failure_summary: str = ""
    taxonomy: str | None = None


class SynthesizeSkillBody(BaseModel):
    skill_slug: str
    memory_ids: list[str]
    version: str = "1.0.0"
    body_md: str | None = None
    force: bool = False


# --- routes ----------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, Any]:
    return get_metacog_service().health()


@router.post("/artifacts/register")
def register_artifact(body: RegisterArtifactBody) -> dict[str, Any]:
    try:
        art = get_metacog_service().register_artifact(
            artifact_type=body.artifact_type,
            content=body.content,
            format=body.format,
            company_id=body.company_id,
            project_id=body.project_id,
            run_id=body.run_id,
            task_id=body.task_id,
            attempt_id=body.attempt_id,
            producer_agent_id=body.producer_agent_id,
            provenance=body.provenance,
            quality=body.quality,
            retention_class=body.retention_class,
            access_scope=body.access_scope,
            supersedes=body.supersedes,
        )
    except ValueError as exc:
        fail(400, "validation", str(exc))
    return art.model_dump()


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str) -> dict[str, Any]:
    art = get_metacog_service().get_artifact(artifact_id)
    if art is None:
        fail(404, "not_found", "artifact not found", {"id": artifact_id})
    return art.model_dump()


@router.get("/artifacts")
def list_artifacts(
    task_id: str | None = None,
    run_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    arts = get_metacog_service().store.list_artifacts(
        task_id=task_id,
        run_id=run_id,
        limit=limit,
    )
    return {"artifacts": [art.model_dump() for art in arts]}


@router.get("/memory")
@router.get("/memories")
def list_memories(
    promotion_status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    mems = get_metacog_service().list_memories(
        promotion_status=promotion_status,
        limit=limit,
    )
    return {"memories": [mem.model_dump() for mem in mems]}


@router.post("/checkpoints")
def create_checkpoint(body: CheckpointBody) -> dict[str, Any]:
    ckp = get_metacog_service().create_checkpoint(
        state=body.state,
        run_id=body.run_id,
        task_id=body.task_id,
        attempt_id=body.attempt_id,
        company_id=body.company_id,
        project_id=body.project_id,
        completed_criteria=body.completed_criteria,
        pending_criteria=body.pending_criteria,
        side_effects=body.side_effects,
        artifact_ids=body.artifact_ids,
        context_digest=body.context_digest,
        safe_to_resume=body.safe_to_resume,
        replay_risk=body.replay_risk,
    )
    return ckp.model_dump()


@router.get("/checkpoints/{checkpoint_id}")
def get_checkpoint(checkpoint_id: str) -> dict[str, Any]:
    ckp = get_metacog_service().get_checkpoint(checkpoint_id)
    if ckp is None:
        fail(404, "not_found", "checkpoint not found", {"id": checkpoint_id})
    return ckp.model_dump()


@router.post("/checkpoints/{checkpoint_id}/resume")
def resume_checkpoint(checkpoint_id: str) -> dict[str, Any]:
    try:
        return get_metacog_service().resume_from_checkpoint(checkpoint_id)
    except KeyError:
        fail(404, "not_found", "checkpoint not found", {"id": checkpoint_id})
    except ValueError as exc:
        fail(409, "unsafe_resume", str(exc), {"id": checkpoint_id})


@router.post("/context/compile")
def compile_context(body: CompileContextBody) -> dict[str, Any]:
    packet = get_metacog_service().compile_context(
        task_id=body.task_id,
        run_id=body.run_id,
        objective=body.objective,
        acceptance_criteria=body.acceptance_criteria,
        company_id=body.company_id,
        project_id=body.project_id,
        domain=body.domain,
        include_shadow=True,
    )
    return packet.model_dump()


@router.post("/memory/search")
def memory_search(body: MemorySearchBody) -> dict[str, Any]:
    rows = get_metacog_service().search_memory(
        body.query,
        company_id=body.company_id,
        project_id=body.project_id,
        limit=body.limit,
    )
    return {"memories": [m.model_dump() for m in rows]}


@router.post("/memory/candidates")
def memory_candidates(body: MemoryCandidateBody) -> dict[str, Any]:
    try:
        mem = get_metacog_service().create_memory_candidate(
            statement=body.statement,
            evidence=body.evidence,
            memory_type=body.memory_type,
            company_id=body.company_id,
            project_id=body.project_id,
            domain=body.domain,
            confidence=body.confidence,
            applicability=body.applicability,
        )
    except ValueError as exc:
        fail(400, "validation", str(exc))
    return mem.model_dump()


@router.post("/memory/{memory_id}/promote")
def promote_memory(memory_id: str, body: PromoteBody | None = None) -> dict[str, Any]:
    body = body or PromoteBody()
    try:
        mem = get_metacog_service().promote_memory(memory_id, force=body.force)
    except KeyError:
        fail(404, "not_found", "memory not found", {"id": memory_id})
    except (PermissionError, ValueError) as exc:
        fail(409, "promotion_refused", str(exc), {"id": memory_id})
    return mem.model_dump()


@router.post("/metacognition/evaluate")
def evaluate(body: EvaluateBody) -> dict[str, Any]:
    state = get_metacog_service().evaluate(
        task_id=body.task_id,
        run_id=body.run_id,
        criteria_total=body.criteria_total,
        criteria_passed=body.criteria_passed,
        previous_progress=body.previous_progress,
        previous_actions=body.previous_actions or None,
        strategy_id=body.strategy_id,
        strategy_age_iterations=body.strategy_age_iterations,
        cost_used=body.cost_used,
        time_used=body.time_used,
        fanout_active=body.fanout_active,
        verifier_capacity_reserved=body.verifier_capacity_reserved,
        context_pressure=body.context_pressure,
        recent_outputs=body.recent_outputs,
    )
    return state.model_dump()


@router.post("/strategies/select")
def select_strategy(body: StrategySelectBody) -> dict[str, Any]:
    s = get_metacog_service().select_strategy(
        domain=body.domain,
        novel=body.novel,
        failed_strategy=body.failed_strategy,
        prefer_verifier=body.prefer_verifier,
    )
    return s.model_dump()


@router.post("/strategies/switch")
def switch_strategy(body: StrategySwitchBody) -> dict[str, Any]:
    try:
        return get_metacog_service().switch_strategy(
            from_strategy=body.from_strategy,
            to_strategy=body.to_strategy,
            task_id=body.task_id,
            run_id=body.run_id,
            reason_codes=body.reason_codes,
            evidence=body.evidence,
            force=body.force,
        )
    except PermissionError as exc:
        fail(403, "forbidden", str(exc))


@router.post("/reflection/run")
def reflection_run(body: ReflectBody) -> dict[str, Any]:
    report = get_metacog_service().reflect(
        outcome=body.outcome,
        run_id=body.run_id,
        task_id=body.task_id,
        evidence_artifact_ids=body.evidence_artifact_ids,
        failure_summary=body.failure_summary,
        taxonomy=body.taxonomy,
    )
    return report.model_dump()


@router.post("/skills/synthesize")
def synthesize_skill(body: SynthesizeSkillBody) -> dict[str, Any]:
    try:
        skill = get_metacog_service().synthesize_skill(
            skill_slug=body.skill_slug,
            memory_ids=body.memory_ids,
            version=body.version,
            body_md=body.body_md,
            force=body.force,
        )
    except (PermissionError, ValueError, KeyError) as exc:
        fail(409, "synthesize_refused", str(exc))
    return skill.model_dump()
