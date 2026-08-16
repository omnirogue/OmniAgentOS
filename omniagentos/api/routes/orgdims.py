"""HTTP API for multidimensional org + Grok metacog agents."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from omniagentos.api.services import fail
from omniagentos.contracts import default_db_path
from omniagentos.orgdims.service import OrgDimsService

router = APIRouter(prefix="/api/orgdims", tags=["orgdims"])

_SERVICE: OrgDimsService | None = None


def get_orgdims_service() -> OrgDimsService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = OrgDimsService(db_path=default_db_path())
        _SERVICE.ensure_seeded()
    return _SERVICE


class ClassifyBoardBody(BaseModel):
    task_id: str
    title: str = ""
    description: str = ""
    discipline: str | None = None
    priority: str | None = None
    company_slug: str | None = None
    product_slug: str | None = None
    apply: bool = True


class BulkClassifyBody(BaseModel):
    only_missing: bool = True
    limit: int = Field(default=500, ge=1, le=5000)
    company_slug: str | None = None
    product_slug: str | None = None
    cursor: str | None = None
    batch_size: int = Field(default=50, ge=1, le=200)


class PatchBoardDimsBody(BaseModel):
    classification: dict[str, Any] | None = None
    organization_context: dict[str, Any] | None = None
    execution_links: dict[str, Any] | None = None
    locked_fields: list[str] | None = None


class ClassifyObjectBody(BaseModel):
    object_type: str  # skill|agent|loop
    object_id: str
    title: str = ""
    description: str = ""
    apply: bool = True


class PatchObjectDimsBody(BaseModel):
    dimensions: dict[str, Any]


class RecommendLoopBody(BaseModel):
    workstream: str | None = None
    risk_class: str | None = None


@router.get("/health")
def health() -> dict[str, Any]:
    return get_orgdims_service().health()


@router.post("/seed")
def seed() -> dict[str, Any]:
    counts = get_orgdims_service().ensure_seeded()
    return {"ok": True, "seeded": counts, **get_orgdims_service().health()}


@router.get("/companies")
def companies() -> dict[str, Any]:
    return {"companies": get_orgdims_service().list_companies()}


@router.get("/workstreams")
def workstreams() -> dict[str, Any]:
    return {"workstreams": get_orgdims_service().list_workstreams()}


@router.get("/agents/grok")
def grok_agents(primary_only: bool = False) -> dict[str, Any]:
    agents = get_orgdims_service().list_grok_agents(primary_only=primary_only)
    return {
        "agents": [a.model_dump() for a in agents],
        "primary_orchestrator": "grok-orchestrator",
    }


@router.get("/loops")
def loops() -> dict[str, Any]:
    return {"loops": get_orgdims_service().list_loop_templates()}


@router.post("/loops/recommend")
def recommend_loop(body: RecommendLoopBody) -> dict[str, Any]:
    return get_orgdims_service().recommend_loop(
        workstream=body.workstream, risk_class=body.risk_class
    )


@router.post("/classify/board_task")
def classify_board_task(body: ClassifyBoardBody) -> dict[str, Any]:
    if not body.task_id.strip():
        fail(400, "validation", "task_id is required")
    result = get_orgdims_service().classify_board_task(
        task_id=body.task_id,
        title=body.title,
        description=body.description,
        discipline=body.discipline,
        priority=body.priority,
        company_slug=body.company_slug,
        product_slug=body.product_slug,
        apply=body.apply,
    )
    return result.model_dump()


@router.post("/classify/bulk")
def classify_bulk(body: BulkClassifyBody | None = None) -> dict[str, Any]:
    body = body or BulkClassifyBody()
    return get_orgdims_service().bulk_reclassify(
        only_missing=body.only_missing,
        limit=body.limit,
        company_slug=body.company_slug,
        product_slug=body.product_slug,
        cursor=body.cursor,
        batch_size=body.batch_size,
    )


@router.post("/classify/object")
def classify_object(body: ClassifyObjectBody) -> dict[str, Any]:
    try:
        result = get_orgdims_service().classify_object(
            object_type=body.object_type,
            object_id=body.object_id,
            title=body.title,
            description=body.description,
            apply=body.apply,
        )
    except ValueError as exc:
        fail(400, "validation", str(exc))
    return result.model_dump()


@router.get("/objects/{object_type}")
def list_objects(object_type: str) -> dict[str, Any]:
    if object_type not in {"skill", "agent", "loop"}:
        fail(400, "validation", "object_type must be skill|agent|loop")
    return {"items": get_orgdims_service().list_object_dims(object_type)}


@router.put("/objects/{object_type}/{object_id}")
def put_object_dims(object_type: str, object_id: str, body: PatchObjectDimsBody) -> dict[str, Any]:
    if object_type not in {"skill", "agent", "loop", "board_task"}:
        fail(400, "validation", "invalid object_type")
    try:
        bundle = get_orgdims_service().set_object_dims(object_type, object_id, body.dimensions)
    except Exception as exc:  # noqa: BLE001
        fail(400, "validation", str(exc))
    return bundle.model_dump()


@router.get("/board/{task_id}")
def get_board_dimensions(task_id: str) -> dict[str, Any]:
    bundle = get_orgdims_service().get_board_dimensions(task_id)
    if bundle is None:
        fail(404, "not_found", "board task not found or no dimensions", {"id": task_id})
    return bundle.model_dump()


@router.patch("/board/{task_id}")
def patch_board_dimensions(task_id: str, body: PatchBoardDimsBody) -> dict[str, Any]:
    try:
        bundle = get_orgdims_service().set_board_dimensions(
            task_id,
            classification=body.classification,
            organization_context=body.organization_context,
            execution_links=body.execution_links,
            locked_fields=body.locked_fields,
        )
    except Exception as exc:  # noqa: BLE001
        fail(400, "validation", str(exc))
    return bundle.model_dump()


@router.get("/views/matrix")
def matrix_view(limit: int = Query(default=500, ge=1, le=5000)) -> dict[str, Any]:
    return get_orgdims_service().matrix_view(limit=limit)


@router.get("/views/portfolio")
def portfolio_view(limit: int = Query(default=500, ge=1, le=5000)) -> dict[str, Any]:
    return get_orgdims_service().portfolio_view(limit=limit)
