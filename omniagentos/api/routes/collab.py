"""HTTP handlers for the agent collaboration control plane (board + messaging)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, NoReturn

import yaml
from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from omniagentos.api.routes.control import ApiError
from omniagentos.collab.contracts import (
    Agent,
    BoardTask,
    Channel,
    ChannelKind,
    CollabEvents,
    Message,
    MessageKind,
)
from omniagentos.collab.store import CollabStore
from omniagentos.company_goals.store import CompanyGoalsStore
from omniagentos.contracts import utc_now_iso

router = APIRouter(prefix="/api/collab")


class CollabModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CreateAgentRequest(CollabModel):
    name: str
    lineage: str = ""
    model: str | None = None
    expertise: list[str] = Field(default_factory=list)
    trust_level: str = "T1"
    # Capability ids (configs/connectors.yaml) this agent may use. Validated at
    # write time; an unknown id fails the whole create rather than yielding an
    # agent that silently holds less access than the operator selected.
    granted: list[str] = Field(default_factory=list)


class CreateBoardTaskRequest(CollabModel):
    title: str = Field(max_length=1024)
    description: str = ""
    required_expertise: list[str] = Field(default_factory=list)
    discipline: str | None = None
    priority: str = "normal"
    # M1 project axis. Optional; omitted/blank creates an UNSCOPED card (NULL),
    # which is this route's pre-M1 behaviour. A named project must exist.
    project_id: str | None = None
    # Team Work OS (migration 123).  These remain optional so the pre-existing
    # agent-board create shape stays byte-for-byte usable.
    parent_task_id: str | None = None
    goal_id: str | None = None
    owner_employee_id: str | None = None
    ref: str | None = None
    size: str = "M"
    acceptance_criteria: str = ""
    due_date: str | None = None
    source: str = ""


class ClaimTaskRequest(CollabModel):
    agent_id: str
    employee_id: str | None = None
    owner_employee_id: str | None = None


class ReleaseClaimRequest(CollabModel):
    return_to_pool: bool = False
    expect_version: int | None = None


class UpdateBoardTaskRequest(CollabModel):
    status: str | None = None
    result_ref: str | None = None
    title: str | None = Field(default=None, max_length=1024)
    description: str | None = None
    priority: str | None = None
    discipline: str | None = None
    claimed_by: str | None = None
    required_expertise: list[str] | None = None
    parent_task_id: str | None = None
    goal_id: str | None = None
    owner_employee_id: str | None = None
    ref: str | None = None
    size: str | None = None
    acceptance_criteria: str | None = None
    blocked_reason: str | None = None
    due_date: str | None = None
    source: str | None = None
    # Automation maturity (migration 132): the one 131 column that IS patchable —
    # "how much did the system do, and what could it do itself next time" is the
    # closer's judgement, and nothing downstream scores it.
    automation_maturity: str | None = None
    automation_note: str | None = None
    # Deliberately parsed so a caller gets the store's 400 rather than silently
    # dropping a forbidden verification stamp.  They are never allowlisted by
    # CollabStore.update_board_task.
    verified_at: str | None = None
    verified_by: str | None = None


class CreateChannelRequest(CollabModel):
    name: str
    kind: str
    topic: str = ""
    members: list[str] = Field(default_factory=list)


class PostMessageRequest(CollabModel):
    from_agent: str
    body: str
    kind: str = MessageKind.MESSAGE.value
    ref: str | None = None
    to_agent: str | None = None


def fail(status: int, code: str, message: str, detail: Any = None) -> NoReturn:
    raise ApiError(status, code, message, detail)


@lru_cache(maxsize=1)
def get_collab_store() -> CollabStore:
    """Process-wide collab store (same default DB path as H1 Store)."""
    from omniagentos import contracts

    return CollabStore(db_path=contracts.default_db_path())


CollabStoreDep = Annotated[CollabStore, Depends(get_collab_store)]

_TEAM_GITHUB_MAP_PATH = Path(__file__).resolve().parents[3] / "configs/team_github_map.yaml"


def _employee_for_email(principal: str, path: Path | None = None) -> str | None:
    """Resolve an email through the optional Team Work OS identity map."""
    target = path or _TEAM_GITHUB_MAP_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    mapped = raw.get(principal)
    return str(mapped).strip() if mapped is not None else None


def _employee_for_principal(store: CollabStore, principal: str) -> str | None:
    """Resolve a transport principal to a real roster employee, if mapped."""
    # Imported late for the same cycle-avoidance reason as the claim route.
    from omniagentos.api.main import _SYSTEM_PRINCIPAL

    if principal == _SYSTEM_PRINCIPAL:
        return None
    goals = CompanyGoalsStore(store._store)
    if principal.startswith("emp_") and goals.get_employee(principal) is not None:
        return principal
    mapped = _employee_for_email(principal)
    if mapped and mapped.startswith("emp_") and goals.get_employee(mapped) is not None:
        return mapped
    # Unmapped is a first-class answer, not an error: ordinary (non-pool) claims
    # proceed on the legacy ownerless path. The claim route refuses the cases
    # that genuinely need an employee (pool cards, body-asserted employee ids).
    return None


def _resolve_project_id(store: Any, project_id: str | None) -> str | None:
    """The project a new card is scoped to, or ``None`` for unscoped (M1).

    The default is NULL and it is declared, not derived. Filing an unscoped
    card under some "default project" would make the project axis lie in the
    one direction that matters: per-project budget rollups and C11 grant
    binding would then treat work nobody scoped as work that project owns.

    A NAMED project must exist. Migration 087's foreign key would refuse an
    unknown id anyway, but as a 500 out of sqlite; resolving it here makes it
    the 404 it actually is. Stores without a live connection (in-memory
    doubles used by tests and the lab) have no projects table to consult and
    take the value as given — same capability-sniffing shape as ``_emit``.
    """
    if project_id is None:
        return None
    normalized = project_id.strip()
    if not normalized:
        return None
    connection = getattr(store, "_connection", None)
    if connection is None:
        return normalized
    row = connection.execute("SELECT id FROM projects WHERE id = ?", (normalized,)).fetchone()
    if row is None:
        fail(404, "not_found", "unknown project", {"project_id": normalized})
    return normalized


def _resolve_goal_id(store: CollabStore, goal_id: str | None) -> str | None:
    """Resolve an optional Team Work OS goal without leaking an FK 500."""
    if goal_id is None:
        return None
    normalized = goal_id.strip()
    if not normalized or CompanyGoalsStore(store._store).get_goal(normalized) is None:
        fail(400, "validation", "unknown goal", {"goal_id": normalized})
    return normalized


def _emit(store: CollabStore, event_type: str, action: str, **kwargs: Any) -> None:
    """Best-effort event emission through the composed H1 store."""
    h1 = getattr(store, "_store", None)
    if h1 is None or not hasattr(h1, "insert_event"):
        return
    h1.insert_event(
        event_type,
        "api",
        action,
        target_type=kwargs.get("target_type", ""),
        target_id=kwargs.get("target_id", ""),
        payload=kwargs.get("payload") or {},
        trace_id=kwargs.get("trace_id", ""),
    )


# --- agents ---


@router.get("/agents")
def list_agents(store: CollabStoreDep) -> list[dict[str, Any]]:
    return store.list_agents()


@router.post("/agents", status_code=201)
def create_agent(body: CreateAgentRequest, store: CollabStoreDep) -> dict[str, Any]:
    if not body.name or not body.name.strip():
        fail(400, "validation", "name is required")
    agent = Agent(
        name=body.name.strip(),
        lineage=body.lineage,
        model=body.model,
        expertise=list(body.expertise),
        trust_level=body.trust_level,
    )
    store.register_agent(agent)
    row = store.get_agent(agent.id)
    if row is None:
        fail(500, "internal", "agent not persisted")

    if body.granted:
        # Imported here rather than at module scope: access.py imports this module
        # for its store dependency, so a top-level import would cycle.
        from omniagentos.api.routes.access import get_capability_store
        from omniagentos.connectors import ConnectorError

        try:
            get_capability_store().set_grant(
                agent.id, list(body.granted), note="granted at agent creation"
            )
        except ConnectorError as exc:
            fail(422, "unknown_capability", str(exc))
    _emit(
        store,
        CollabEvents.AGENT_UPDATED,
        "agent.registered",
        target_type="agent",
        target_id=agent.id,
        payload={"agent_id": agent.id, "name": agent.name},
    )
    return row


# --- board ---


# GET /api/collab/board is GONE. It was a SECOND, unauthenticated listing of the
# same table as ``GET /api/board`` -- 2.16 MB on the live board -- with no caller
# in the dashboard. Two list endpoints over one table means two projections, two
# filter sets and two rounds of performance work, and only one of them
# reconciles each card against its linked run. That one survived:
#
#   card list   -> GET /api/board          (bounded: status/updated_after/limit)
#   single card -> GET /api/board/{task_id}
#
# ``CollabStore.list_board_tasks`` (the projection this route used) is unchanged
# and still the store-level read; it is simply no longer published twice.


@router.post("/board", status_code=201)
def create_board_task(body: CreateBoardTaskRequest, store: CollabStoreDep) -> Any:
    if not body.title or not body.title.strip():
        fail(400, "validation", "title is required")
    task = BoardTask(
        title=body.title.strip(),
        description=body.description,
        required_expertise=list(body.required_expertise),
        discipline=body.discipline,
        priority=body.priority,
        project_id=_resolve_project_id(store, body.project_id),
        parent_task_id=body.parent_task_id,
        goal_id=_resolve_goal_id(store, body.goal_id),
        owner_employee_id=body.owner_employee_id,
        ref=body.ref,
        size=body.size,
        acceptance_criteria=body.acceptance_criteria,
        due_date=body.due_date,
        source=body.source,
    )
    try:
        store.create_board_task(task)
    except ValueError as exc:
        # This is intentionally the small legacy-compatible error shape called
        # out by the Team Work OS contract: refs are a natural conflict, not a
        # generic malformed payload.
        if str(exc) == "ref_conflict":
            return JSONResponse(status_code=409, content={"detail": "ref_conflict"})
        fail(400, "validation", str(exc))
    # Multidimensional org: Grok orchestrator classification (best-effort).
    try:
        from omniagentos.orgdims.service import OrgDimsService

        OrgDimsService().classify_board_task(
            task_id=task.id,
            title=task.title,
            description=task.description,
            discipline=task.discipline,
            priority=task.priority,
            apply=True,
        )
    except Exception:  # noqa: BLE001 — classification must never block create
        pass
    row = store.get_board_task(task.id)
    if row is None:
        fail(500, "internal", "board task not persisted")
    _emit(
        store,
        CollabEvents.BOARD_UPDATED,
        "board.created",
        target_type="board_task",
        target_id=task.id,
        payload={"task_id": task.id, "status": str(task.status)},
    )
    return row


@router.post("/board/{task_id}/claim")
def claim_board_task(
    task_id: str,
    body: ClaimTaskRequest,
    store: CollabStoreDep,
    x_omni_authenticated_principal: Annotated[
        str | None, Header(alias="X-Omni-Authenticated-Principal")
    ] = None,
) -> dict[str, Any]:
    if not body.agent_id:
        fail(400, "validation", "agent_id is required")
    task = store.get_board_task(task_id)
    if task is None:
        fail(404, "not_found", "board task not found", {"id": task_id})
    employee_id = None if body.employee_id is None else body.employee_id.strip()
    explicit_owner = None if body.owner_employee_id is None else body.owner_employee_id.strip()
    if body.employee_id is not None and not employee_id:
        fail(400, "validation", "employee_id must not be blank")
    if body.owner_employee_id is not None and not explicit_owner:
        fail(400, "validation", "owner_employee_id must not be blank")
    if employee_id and explicit_owner and employee_id != explicit_owner:
        fail(
            400,
            "validation",
            "employee_id and owner_employee_id must match",
        )
    # Import late: api.main includes this router before defining the shared
    # principal normalizer, so a module-scope import would create a cycle.
    from omniagentos.api.main import _request_principal

    principal = _request_principal(x_omni_authenticated_principal)
    claimed_employee = explicit_owner if explicit_owner is not None else employee_id
    if (
        claimed_employee is not None
        and principal.startswith("emp_")
        and claimed_employee != principal
    ):
        fail(400, "validation", "employee_id does not match authenticated principal")
    # The composed base store may be absent on lab/test doubles — mirror the
    # _emit helper's guard so an alternate store shape degrades to the legacy
    # ownerless claim instead of a 500 (a REAL store always carries _store).
    base_store = getattr(store, "_store", None)
    if claimed_employee is not None and base_store is not None:
        if CompanyGoalsStore(base_store).get_employee(claimed_employee) is None:
            fail(404, "not_found", "employee not found", {"id": claimed_employee})
    owner_employee_id = (
        _employee_for_principal(store, principal) if base_store is not None else None
    )
    if claimed_employee is not None and owner_employee_id is None:
        fail(400, "validation", f"principal {principal} is not mapped to an employee")
    if claimed_employee is not None and claimed_employee != owner_employee_id:
        fail(400, "validation", "employee_id does not match authenticated principal")
    if owner_employee_id is None and base_store is not None and task.get("owner_employee_id") is None:
        from omniagentos.team.store import TeamStore

        if task_id in {card.id for card in TeamStore(base_store).pool_cards()}:
            fail(
                400,
                "validation",
                "pool card requires a resolvable employee owner to claim; "
                f"principal {principal} is not mapped to an employee",
            )
    expect_version = int(task.get("claim_version") or 0)
    if owner_employee_id is None:
        won = store.claim_task(task_id, body.agent_id, expect_version)
    else:
        won = store.claim_task(
            task_id,
            body.agent_id,
            expect_version,
            actor=owner_employee_id,
            owner_employee_id=owner_employee_id,
        )
    if not won:
        fail(
            409,
            "claim_conflict",
            "task already claimed or version mismatch",
            {"id": task_id, "agent_id": body.agent_id},
        )
    _emit(
        store,
        CollabEvents.TASK_CLAIMED,
        "board.claimed",
        target_type="board_task",
        target_id=task_id,
        payload={"task_id": task_id, "agent_id": body.agent_id},
    )
    return {
        "success": True,
        "owner_employee_id": owner_employee_id,
        "claimed_by": body.agent_id,
    }


@router.post("/board/{task_id}/release")
def release_board_task(
    task_id: str,
    body: ReleaseClaimRequest,
    store: CollabStoreDep,
    x_omni_authenticated_principal: Annotated[
        str | None, Header(alias="X-Omni-Authenticated-Principal")
    ] = None,
) -> dict[str, Any]:
    task = store.get_board_task(task_id)
    if task is None:
        fail(404, "not_found", "board task not found", {"id": task_id})
    from omniagentos.api.main import _request_principal

    actor = _request_principal(x_omni_authenticated_principal)
    released = store.release_claim(
        task_id,
        body.expect_version,
        actor=actor,
        return_to_pool=body.return_to_pool,
    )
    if not released:
        fail(
            409,
            "release_conflict",
            "task is not releasable or version mismatch",
            {"id": task_id},
        )
    row = store.get_board_task(task_id)
    if row is None:  # pragma: no cover - persistence postcondition
        fail(500, "internal", "released board task not persisted")
    _emit(
        store,
        CollabEvents.BOARD_UPDATED,
        "board.released",
        target_type="board_task",
        target_id=task_id,
        payload={"task_id": task_id, "return_to_pool": body.return_to_pool},
    )
    return row


@router.patch("/board/{task_id}")
def update_board_task(
    task_id: str,
    body: UpdateBoardTaskRequest,
    store: CollabStoreDep,
    x_omni_authenticated_principal: Annotated[
        str | None, Header(alias="X-Omni-Authenticated-Principal")
    ] = None,
) -> Any:
    existing = store.get_board_task(task_id)
    if existing is None:
        fail(404, "not_found", "board task not found", {"id": task_id})
    fields = body.model_dump(exclude_unset=True)
    if not fields:
        return existing
    if "goal_id" in fields:
        fields["goal_id"] = _resolve_goal_id(store, fields["goal_id"])
    # The task_events row this write appends must name WHO made it — an
    # assignment recorded as 'system' both loses the assigner and makes a
    # self-assignment indistinguishable from an assignment-by-other (the
    # Slack DM trigger keys on exactly that difference).
    from omniagentos.api.main import _SYSTEM_PRINCIPAL, _request_principal

    principal = _request_principal(x_omni_authenticated_principal)
    actor = (
        _employee_for_principal(store, principal) or principal
        if principal != _SYSTEM_PRINCIPAL
        else "system"
    )
    try:
        store.update_board_task(task_id, fields, actor=actor)
    except ValueError as exc:
        if str(exc) == "ref_conflict":
            return JSONResponse(status_code=409, content={"detail": "ref_conflict"})
        fail(400, "validation", str(exc))
    row = store.get_board_task(task_id)
    if row is None:
        fail(404, "not_found", "board task not found", {"id": task_id})
    _emit(
        store,
        CollabEvents.BOARD_UPDATED,
        "board.updated",
        target_type="board_task",
        target_id=task_id,
        payload={"task_id": task_id, "fields": list(fields.keys())},
    )
    return row


# POST /api/collab/board/{task_id}/archive is GONE. There were TWO archive
# routes for one card, and they did different things: the intake one
# (``POST /api/board/{task_id}/archive``) PAUSES the card's linked run/session
# first, this one only stamped ``archived_at`` — so archiving through it left
# the work running against a card nobody was watching any more. One archive
# path now, the one that stops the work:
#
#   archive -> POST /api/board/{task_id}/archive   (pauses linked run/session)
#   bulk    -> POST /api/board/archive
#
# Restore stays here: it is the inverse of a stamp, has no work to resume, and
# has no counterpart on the intake router.


@router.post("/board/{task_id}/restore")
def restore_board_task(task_id: str, store: CollabStoreDep) -> dict[str, Any]:
    """Restore an archived board card. Authorization matches archive: same store dep.

    Idempotent: restoring a live (or missing-archive) card returns the current
    row without error. 404 only when the task does not exist.
    """
    task = store.get_board_task(task_id)
    if task is None:
        fail(404, "not_found", "board task not found", {"id": task_id})

    restored = store.restore_archived_board_task(task_id)
    row = store.get_board_task(task_id)
    if row is None:
        fail(404, "not_found", "board task not found", {"id": task_id})
    if restored:
        _emit(
            store,
            CollabEvents.BOARD_UPDATED,
            "board.restored",
            target_type="board_task",
            target_id=task_id,
            payload={"task_id": task_id, "archived": False},
        )
    return row


# --- channels ---


@router.get("/channels")
def list_channels(
    store: CollabStoreDep,
    agent: Annotated[str | None, Query()] = None,
) -> list[dict[str, Any]]:
    return store.list_channels(agent_id=agent)


@router.post("/channels", status_code=201)
def create_channel(body: CreateChannelRequest, store: CollabStoreDep) -> dict[str, Any]:
    if not body.name or not body.name.strip():
        fail(400, "validation", "name is required")
    try:
        kind = ChannelKind(body.kind)
    except ValueError:
        fail(400, "validation", f"invalid channel kind: {body.kind}")
    channel = Channel(
        name=body.name.strip(),
        kind=kind,
        topic=body.topic,
        members=list(body.members),
    )
    store.create_channel(channel)
    row = store.get_channel(channel.id)
    if row is None:
        fail(500, "internal", "channel not persisted")
    _emit(
        store,
        CollabEvents.CHANNEL_UPDATED,
        "channel.created",
        target_type="channel",
        target_id=channel.id,
        payload={"channel_id": channel.id, "kind": str(channel.kind)},
    )
    return row


@router.get("/channels/{channel_id}/messages")
def list_messages(
    channel_id: str,
    store: CollabStoreDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[dict[str, Any]]:
    channel = store.get_channel(channel_id)
    if channel is None:
        fail(404, "not_found", "channel not found", {"id": channel_id})
    return store.list_messages(channel_id, limit=limit)


@router.post("/channels/{channel_id}/messages", status_code=201)
def post_message(
    channel_id: str, body: PostMessageRequest, store: CollabStoreDep
) -> dict[str, Any]:
    channel = store.get_channel(channel_id)
    if channel is None:
        fail(404, "not_found", "channel not found", {"id": channel_id})
    if not body.from_agent:
        fail(400, "validation", "from_agent is required")
    if body.body is None:
        fail(400, "validation", "body is required")
    try:
        kind = MessageKind(body.kind)
    except ValueError:
        fail(400, "validation", f"invalid message kind: {body.kind}")
    message = Message(
        channel_id=channel_id,
        from_agent=body.from_agent,
        to_agent=body.to_agent,
        kind=kind,
        body=body.body,
        ref=body.ref,
        ts=utc_now_iso(),
    )
    store.post_message(message)
    messages = store.list_messages(channel_id, limit=1000)
    row = next((m for m in messages if m["id"] == message.id), None)
    if row is None:
        # Fall back to the in-memory model if the row is not yet visible.
        row = message.model_dump()
    _emit(
        store,
        CollabEvents.MESSAGE_POSTED,
        "message.posted",
        target_type="channel",
        target_id=channel_id,
        payload={"message_id": message.id, "channel_id": channel_id},
    )
    return row


@router.get("/messages/search")
def search_messages(
    store: CollabStoreDep,
    q: Annotated[str, Query(min_length=1)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    return store.search_messages(q, limit=limit)
