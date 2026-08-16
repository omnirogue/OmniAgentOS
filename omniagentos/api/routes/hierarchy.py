"""HTTP surface for W3 project hierarchy and scoped conversations.

Three concerns live here so they can share the ``/api/projects`` and ``/api/tasks``
prefixes without re-opening the flat W2 project routes:

* ``GET /api/projects/tree`` -- the whole project forest, top-level first, each
  node carrying its own tasks and a task-derived status rollup.
* ``GET|POST /api/projects/{id}/conversation`` & ``/message`` -- a project's log.
* ``GET|POST /api/tasks/{id}/conversation`` & ``/message`` -- a task's log.

This router MUST be registered before the flat projects router so that the
literal ``/api/projects/tree`` wins over the ``/api/projects/{project_id}``
detail route (otherwise "tree" is read as a project id and 404s).
"""

from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from omniagentos.api.deps import StoreDep
from omniagentos.api.services import fail
from omniagentos.conversations import ConversationError, ConversationStore
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectStore

router = APIRouter(prefix="/api", tags=["hierarchy"])

_MAX_CONTENT = 100_000
_MAX_MODEL = 200


class MessageRequest(BaseModel):
    # Forbid extras so a typo'd field is a 422, not a silently dropped turn.
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "agent", "system"]
    content: str = Field(min_length=1, max_length=_MAX_CONTENT)
    model: str | None = Field(default=None, max_length=_MAX_MODEL)


def _project_store(store: StoreDep) -> ProjectStore:
    return ProjectStore(cast(SqliteStore, store))


def _conversation_store(store: StoreDep) -> ConversationStore:
    return ConversationStore(cast(SqliteStore, store))


def _require_project(store: StoreDep, project_id: str) -> None:
    if _project_store(store).get_project(project_id) is None:
        fail(404, "not_found", "project not found", {"id": project_id})


def _require_task(store: StoreDep, task_id: str) -> None:
    if store.get_task(task_id) is None:
        fail(404, "not_found", "task not found", {"id": task_id})


@router.get("/projects/tree")
def project_tree(store: StoreDep) -> list[dict[str, Any]]:
    return _project_store(store).project_tree()


@router.get("/projects/{project_id}/conversation")
def get_project_conversation(project_id: str, store: StoreDep) -> list[dict[str, Any]]:
    _require_project(store, project_id)
    return _conversation_store(store).read("project", project_id)


@router.post("/projects/{project_id}/message", status_code=201)
def post_project_message(project_id: str, body: MessageRequest, store: StoreDep) -> dict[str, Any]:
    _require_project(store, project_id)
    return _append(store, "project", project_id, body)


@router.get("/tasks/{task_id}/conversation")
def get_task_conversation(task_id: str, store: StoreDep) -> list[dict[str, Any]]:
    _require_task(store, task_id)
    return _conversation_store(store).read("task", task_id)


@router.post("/tasks/{task_id}/message", status_code=201)
def post_task_message(task_id: str, body: MessageRequest, store: StoreDep) -> dict[str, Any]:
    _require_task(store, task_id)
    return _append(store, "task", task_id, body)


def _append(
    store: StoreDep, scope_type: str, scope_id: str, body: MessageRequest
) -> dict[str, Any]:
    try:
        return _conversation_store(store).append(
            scope_type,
            scope_id,
            body.role,
            body.content,
            model=body.model,
        )
    except ConversationError as exc:
        fail(422, "validation", str(exc), {"scope_type": scope_type, "id": scope_id})


__all__ = ["router"]
