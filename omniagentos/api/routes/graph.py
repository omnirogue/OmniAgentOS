"""HTTP API for Swarm Graph V2 (typed diamond runtime) — LIVE."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from omniagentos.api.services import fail
from omniagentos.contracts import default_db_path
from omniagentos.graph_runtime.service import GraphRuntimeService

router = APIRouter(prefix="/api/graph", tags=["graph"])

_SERVICE: GraphRuntimeService | None = None


def get_graph_service() -> GraphRuntimeService:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = GraphRuntimeService(db_path=str(default_db_path()))
    return _SERVICE


class StartDiamondBody(BaseModel):
    title: str = "Diamond graph run"
    goal: str = ""
    company_id: str | None = None
    project_id: str | None = None
    completeness_policy: str = "fail_closed"


class StartTemplateBody(BaseModel):
    slug: str = "diamond-v1"
    title: str = "Graph run"
    company_id: str | None = None
    project_id: str | None = None
    completeness_policy: str | None = None
    swarm_run_id: str | None = None


class CompleteNodeBody(BaseModel):
    outputs: dict[str, Any] = Field(default_factory=dict)
    verified: bool = False
    error: str | None = None


class DiamondDemoBody(BaseModel):
    title: str = "Diamond demo"
    findings_a: dict[str, Any] | None = None
    findings_b: dict[str, Any] | None = None


@router.get("/health")
def health() -> dict[str, Any]:
    return get_graph_service().health()


@router.get("/templates")
def list_templates() -> dict[str, Any]:
    return {"templates": get_graph_service().list_templates()}


@router.get("/runs")
def list_runs(limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    return {"runs": get_graph_service().list_runs(limit=limit)}


@router.post("/runs/diamond")
def start_diamond(body: StartDiamondBody) -> dict[str, Any]:
    try:
        run = get_graph_service().start_diamond(
            title=body.title,
            goal=body.goal,
            company_id=body.company_id,
            project_id=body.project_id,
            completeness_policy=body.completeness_policy,
        )
    except Exception as exc:  # noqa: BLE001
        fail(400, "graph_start_failed", str(exc))
    return {"ok": True, "run": run}


@router.post("/runs")
def start_from_template(body: StartTemplateBody) -> dict[str, Any]:
    try:
        run = get_graph_service().start_from_template(
            body.slug,
            title=body.title,
            company_id=body.company_id,
            project_id=body.project_id,
            completeness_policy=body.completeness_policy,
            swarm_run_id=body.swarm_run_id,
        )
    except KeyError as exc:
        fail(404, "unknown_template", str(exc))
    except Exception as exc:  # noqa: BLE001
        fail(400, "graph_start_failed", str(exc))
    return {"ok": True, "run": run}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    run = get_graph_service().get_run(run_id)
    if run is None:
        fail(404, "not_found", f"graph run {run_id}")
    return {"run": run}


@router.get("/runs/{run_id}/view")
def live_view(run_id: str) -> dict[str, Any]:
    try:
        return get_graph_service().live_view(run_id)
    except KeyError:
        fail(404, "not_found", f"graph run {run_id}")


@router.get("/runs/{run_id}/ready")
def ready_nodes(run_id: str) -> dict[str, Any]:
    try:
        nodes = get_graph_service().ready_nodes(run_id)
    except KeyError:
        fail(404, "not_found", f"graph run {run_id}")
    return {"ready": nodes}


@router.post("/runs/{run_id}/nodes/{node_key}/complete")
def complete_node(run_id: str, node_key: str, body: CompleteNodeBody) -> dict[str, Any]:
    try:
        run = get_graph_service().complete_node(
            run_id,
            node_key,
            outputs=body.outputs,
            verified=body.verified,
            error=body.error,
        )
    except KeyError as exc:
        fail(404, "not_found", str(exc))
    except ValueError as exc:
        fail(400, "invalid_outputs", str(exc))
    return {"ok": True, "run": run}


@router.post("/demo/diamond")
def demo_diamond(body: DiamondDemoBody) -> dict[str, Any]:
    """Deterministic full diamond (no LLM) — cert / smoke path."""
    run = get_graph_service().run_diamond_deterministic(
        title=body.title,
        findings_a=body.findings_a,
        findings_b=body.findings_b,
    )
    return {"ok": True, "status": run.get("status"), "run": run}
