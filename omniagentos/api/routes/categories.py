"""HTTP handlers for longhaul task categories."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from omniagentos.api.deps import StoreDep
from omniagentos.api.routes.sessions import _authorized
from omniagentos.api.services import fail
from omniagentos.contracts import default_db_path
from omniagentos.longhaul import LonghaulStore
from omniagentos.longhaul.store import slugify

router = APIRouter(prefix="/api/categories")


class CategoryModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CreateCategoryRequest(CategoryModel):
    name: str = Field(max_length=1024)
    color: str = ""
    wip_limit: int = Field(default=1, ge=1)


class UpdateCategoryRequest(CategoryModel):
    name: str | None = Field(default=None, max_length=1024)
    color: str | None = None
    wip_limit: int | None = Field(default=None, ge=1)


_LONGHAUL_STORE: LonghaulStore | None = None


def get_longhaul_store() -> LonghaulStore:
    """Process-lifetime LonghaulStore instance."""
    global _LONGHAUL_STORE
    if _LONGHAUL_STORE is None:
        _LONGHAUL_STORE = LonghaulStore(default_db_path())
    return _LONGHAUL_STORE


LonghaulStoreDep = Annotated[LonghaulStore, Depends(get_longhaul_store)]
AuthorizedDep = Annotated[None, Depends(_authorized)]


def _emit_category_event(
    audit_store: Any,
    event_type: str,
    action: str,
    target_id: str,
    **kwargs: Any,
) -> None:
    """Best-effort category event emission through the H1 store."""
    try:
        audit_store.insert_event(
            event_type,
            "api",
            action,
            target_type="category",
            target_id=target_id,
            payload=kwargs.get("payload") or {"target_id": target_id},
        )
    except Exception:
        pass  # Best-effort telemetry


@router.get("")
def list_categories(
    _: AuthorizedDep,
    store: LonghaulStoreDep,
) -> list[dict[str, Any]]:
    """List all task categories."""
    del _
    categories = store.list_categories()
    return [dict(cat) for cat in categories]


@router.post("", status_code=201)
def create_category(
    body: CreateCategoryRequest,
    _: AuthorizedDep,
    store: LonghaulStoreDep,
    audit_store: StoreDep,
    response: Response,
) -> dict[str, Any]:
    """Create a new task category."""
    del _
    if not body.name or not body.name.strip():
        fail(400, "validation", "name is required")
    name = body.name.strip()
    category_slug = slugify(name)
    if not category_slug:
        fail(422, "validation", "category name must contain at least one alphanumeric character")
    existing = store.get_category(category_slug)
    if existing is not None:
        result = dict(existing)
        if result["name"] != name:
            fail(
                409,
                "conflict",
                "category name conflicts with an existing slug",
                {"slug": category_slug},
            )
        response.status_code = 200
        return result

    cat = store.create_category(
        name,
        color=body.color or "",
        wip_limit=body.wip_limit,
    )
    result = dict(cat)
    # A concurrent writer may claim this slug between our lookup and insert.
    # Preserve the same public semantics instead of treating its row as ours.
    if result["name"] != name:
        fail(
            409,
            "conflict",
            "category name conflicts with an existing slug",
            {"slug": category_slug},
        )
    _emit_category_event(
        audit_store,
        "category.created",
        "category.created",
        str(result["id"]),
        payload={"name": result["name"], "slug": result["slug"]},
    )
    return result


@router.patch("/{category_id}")
def update_category(
    category_id: str,
    body: UpdateCategoryRequest,
    _: AuthorizedDep,
    store: LonghaulStoreDep,
    audit_store: StoreDep,
) -> dict[str, Any]:
    """Update an existing task category."""
    del _
    existing = store.get_category(category_id)
    if existing is None:
        fail(404, "not_found", "category not found", {"id": category_id})

    fields: dict[str, Any] = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.color is not None:
        fields["color"] = body.color
    if body.wip_limit is not None:
        fields["wip_limit"] = body.wip_limit

    if not fields:
        return dict(existing)

    result = store.update_category(category_id, **fields)
    if result is None:
        fail(404, "not_found", "category not found", {"id": category_id})

    _emit_category_event(
        audit_store,
        "category.updated",
        "category.updated",
        category_id,
        payload={"fields": list(fields.keys())},
    )
    return dict(result)


__all__ = ["LonghaulStoreDep", "get_longhaul_store", "router", "slugify"]
