"""HTTP surface for project provisioning: view scope, request more scope.

* ``GET  /api/provision/{project_id}`` — the project's provisioned scope: the
  dirs it may work in, the connector APIs it may reach (enriched from the
  registry with each capability's action class + whether it is a hard stop), its
  policy grants, and the auto-grant/escalation decision log.
* ``POST /api/provision/{project_id}/request`` — an agent/run asks for one more
  dir OR connector. A normal dir or a read/auto-tier connector is AUTO-GRANTED; a
  hard-stop scope (money/transfer, delete-capable, or a dir outside the user's
  home) is ESCALATED (recorded pending, never granted here).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, model_validator

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.control import fail
from omniagentos.provision import (
    ProvisionError,
    ProvisionServiceError,
    build_scope_view,
    request_scope,
)
from omniagentos.simgate import SimGateError

router = APIRouter(prefix="/api/provision", tags=["provision"])


class ScopeRequestBody(BaseModel):
    # Reject unknown fields so a typo can't silently pick the wrong scope.
    model_config = ConfigDict(extra="forbid")

    connector: str | None = None
    dir: str | None = None
    run_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> ScopeRequestBody:
        if bool(self.connector) == bool(self.dir):
            raise ValueError("request exactly one of 'connector' or 'dir'")
        return self


@router.get("/{project_id}")
def get_scope(project_id: str, store: StoreDep) -> dict[str, Any]:
    view = build_scope_view(store, project_id)
    if view is None:
        fail(404, "not_found", "project not found", {"id": project_id})
    return view


@router.post("/{project_id}/request", status_code=201)
def post_scope_request(project_id: str, body: ScopeRequestBody, store: StoreDep) -> dict[str, Any]:
    try:
        result = request_scope(
            store,
            project_id,
            connector=body.connector,
            dir=body.dir,
            run_id=body.run_id,
        )
    except ProvisionError as exc:
        fail(404, "not_found", str(exc), {"id": project_id})
    except SimGateError as exc:
        fail(403, "forbidden", str(exc), {"project_id": project_id})
    except ProvisionServiceError as exc:
        fail(422, "validation", str(exc), {"project_id": project_id})
    return result.model_dump()


__all__ = ["router"]
