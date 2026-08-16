"""HTTP surface for the workfs WORK-tree convention (D3).

Exactly two routes:

* ``GET  /api/workfs/tree``   — read the actual directory tree (depth ≤ 3)
* ``POST /api/workfs/ensure`` — create-only mkdir-p for a scope path

The production application mounts this router.  Its endpoints inherit the
application-level session-token policy because the WORK tree is a local
filesystem capability.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from omniagentos.api.services import fail
from omniagentos.workfs import (
    WorkfsError,
    WorkfsPathError,
    ensure,
    read_tree,
)

router = APIRouter(prefix="/api/workfs", tags=["workfs"])


class EnsureBody(BaseModel):
    """Scope tuple for create-only ensure."""

    model_config = ConfigDict(extra="forbid")

    company: str | None = Field(default=None, max_length=200)
    department: str | None = Field(default=None, max_length=200)
    subfolder: str | None = Field(default=None, max_length=200)


def _raise_workfs(exc: WorkfsError) -> None:
    status = 400 if isinstance(exc, WorkfsPathError) else 400
    fail(status, exc.code, exc.message, exc.detail)


@router.get("/tree")
def _tree() -> dict[str, Any]:
    """Return the real WORK directory tree (bounded depth 3). Never creates."""
    try:
        return read_tree()
    except WorkfsError as exc:
        _raise_workfs(exc)
        raise  # pragma: no cover — fail() is NoReturn


@router.post("/ensure")
def _ensure(body: EnsureBody) -> dict[str, Any]:
    """Create-only ensure of the mapped scope path under the WORK root."""
    try:
        result = ensure(body.company, body.department, body.subfolder)
    except WorkfsError as exc:
        _raise_workfs(exc)
        raise  # pragma: no cover
    return {
        "path": result.path,
        "created": result.created,
        "company": result.company,
        "department": result.department,
        "subfolder": result.subfolder,
    }
