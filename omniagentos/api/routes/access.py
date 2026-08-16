"""HTTP handlers for the capability/access control plane.

Serves the connector catalogue and agent grants to the dashboard.

SECRETS NEVER CROSS THIS BOUNDARY. Responses carry environment variable *names*
(so an operator can see what a grant reaches) but never values. ``broker.py`` is
the only module that resolves a credential, and it does so on the outbound path
into a request header -- nothing it reads is ever returned to a caller.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omniagentos.api.routes.collab import CollabStoreDep, get_collab_store
from omniagentos.api.routes.control import ApiError
from omniagentos.collab.store import CollabStore
from omniagentos.connectors import ConnectorError, broker, load_registry
from omniagentos.connectors.broker import HARD_HUMAN_CLASSES, BrokerDenied, describe_grant
from omniagentos.connectors.store import CapabilityStore
from omniagentos.sessions.ssh_keys import read_server_inventory
from omniagentos.toolplane.catalog import default_catalog
from omniagentos.toolplane.exposure import ExposureContext, compute_exposure
from omniagentos.toolplane.search import search_tools

router = APIRouter(prefix="/api/access")

#: The canonical U-E7 identity of the API broker door itself.
#:
#: ``AuditContext.normalized`` refuses non-canonical holder spellings, and the
#: collab agent ids this route authenticates (``agt_*``) are a DIFFERENT
#: vocabulary from the canonical ``lane:``/``loop:``/``job:``/``human:``
#: identities the grant system speaks -- a gap the integration left unresolved
#: and this route cannot paper over by pretending an ``agt_*`` is a lane. So the
#: HOLDER names the lane (this door) and the concrete actor rides in the row's
#: own ``agent_id`` column, where it belongs. Two columns, two questions: which
#: grant authorised this, and who was holding the token.
API_BROKER_HOLDER = "lane:api"


class AccessModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SetGrantRequest(AccessModel):
    granted: list[str] = Field(default_factory=list)
    mode: str = "read"
    expires_at: str | None = None
    issued_by: str | None = None
    request_id: str | None = None
    note: str = ""

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        if value not in {"read", "write"}:
            raise ValueError("mode must be 'read' or 'write'")
        return value

    @field_validator("expires_at")
    @classmethod
    def validate_expires_at(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("expires_at must be an ISO 8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise ValueError("expires_at must include a UTC offset")
        return value


@lru_cache(maxsize=1)
def get_capability_store() -> CapabilityStore:
    """Process-wide capability store.

    Built on the collab store's already-open SqliteStore rather than a second
    connection, so grant writes serialize against the same lock as agent writes.
    """
    collab: CollabStore = get_collab_store()
    return CapabilityStore(collab._store)


CapStoreDep = Annotated[CapabilityStore, Depends(get_capability_store)]


@router.get("/capabilities")
def get_catalogue() -> dict[str, Any]:
    """The full connector catalogue: what access it is *possible* to grant.

    ``callable_now: false`` means the capability is declared but has no reviewed
    outbound call path yet, so the broker will refuse it. That is a fail-closed
    default, not a bug -- it is surfaced so the UI can say so plainly.
    """
    registry = load_registry()
    connectors = []
    for connector in registry.connectors.values():
        connectors.append(
            {
                "id": connector.id,
                "label": connector.label,
                "group": connector.group,
                "env_names": list(connector.env),
                "capabilities": [
                    {
                        "id": cap.id,
                        "label": cap.label,
                        "action_class": cap.action_class.value,
                        "callable_now": cap.callable_now,
                        "always_human": cap.action_class in HARD_HUMAN_CLASSES,
                    }
                    for cap in connector.capabilities.values()
                ],
            }
        )
    return {
        "groups": {
            gid: {"label": g.label, "danger": g.danger} for gid, g in registry.groups.items()
        },
        "connectors": connectors,
    }


@router.get("/servers")
def get_servers() -> dict[str, Any]:
    """The server fleet documented in ``vault/servers/inventory.md``.

    Display-only: this is the same document ``ssh_keys.read_server_inventory_hosts``
    reads for SSH grant aliases, but parsed here for the dashboard's Servers section
    (ACTIVE / EPHEMERAL / LEGACY-STALE), never as an authorization decision. Key
    *paths* are shown (e.g. ``~/.ssh/example_a.pem``) — never key material.
    """
    return {"servers": read_server_inventory()}


class ToolSearchResult(AccessModel):
    """One full tool definition, as returned by the deferred-search endpoint."""

    id: str
    namespace: str
    label: str
    compact_hint: str
    description: str
    action_class: str
    risk: str
    read_only: bool
    callable_now: bool
    parameter_names: list[str]
    input_examples: list[str]
    score: float


@router.get("/tool-search")
def tool_search(
    q: str = "",
    limit: int = 5,
    x_session_token: Annotated[str | None, Header(alias="X-Session-Token")] = None,
) -> dict[str, Any]:
    """Deferred tool search: full definitions for at most ``limit`` matching capabilities.

    GATED, unlike its neighbours. ``/capabilities``, ``/agents``, ``/log`` and ``/calls``
    are deliberately public catalogue and roster reads. This route is not one of them: a
    ranked search over descriptions and parameter names discloses catalogue SHAPE -- what
    exists, what it is called, and what it reaches -- which is the same recon-sensitive
    argument that gated ``GET /api/access/servers`` (main.py::_is_server_inventory_request).

    The gate is enforced HERE rather than there because ``main.py``'s app-level dependency
    matches literal paths for reads, and this route is not one of them. Checking it in the
    handler keeps the decision next to the thing being protected; adding a literal path in
    main.py would work equally well and is the alternative if that file is being touched
    anyway.

    The token check runs BEFORE any catalog work, so a probe learns nothing -- not even
    timing -- about whether the catalogue loaded.
    """
    from omniagentos.sessions.token import verify_token

    if not verify_token(x_session_token):
        raise ApiError(401, "unauthorized", "invalid or missing session token")

    limit = max(1, min(limit, 25))

    if not q.strip():
        return {"query": q, "results": [], "count": 0}

    try:
        catalog = default_catalog()
        ctx = ExposureContext(lane="api")
        # THE OPERATOR'S VIEW, NOT AN AGENT'S. The caller has already proven it holds the
        # local session token, and is therefore already entitled to the whole catalogue
        # through the public /capabilities route -- so the grant passed here is the whole
        # catalogue and nothing is hidden. An agent-facing search MUST NOT copy this line:
        # it has to pass that agent's real grant, which is what makes the exposure filter
        # hide what the agent may not see.
        decision = compute_exposure(ctx, grants=sorted(catalog))

        hits = search_tools(q, decision, catalog=catalog, limit=limit)

        results = []
        for hit in hits:
            results.append(
                ToolSearchResult(
                    id=hit.entry.id,
                    namespace=hit.entry.namespace,
                    label=hit.entry.label,
                    compact_hint=hit.entry.compact_hint,
                    description=hit.entry.description,
                    action_class=hit.entry.action_class.value,
                    risk=hit.entry.risk,
                    read_only=hit.entry.read_only,
                    callable_now=hit.entry.callable_now,
                    parameter_names=list(hit.entry.parameter_names),
                    input_examples=list(hit.entry.input_examples),
                    score=round(hit.score, 4),
                )
            )
        return {
            "query": q,
            "results": [r.model_dump() for r in results],
            "count": len(results),
        }
    except Exception:  # noqa: BLE001 - a search outage must not 500 the control plane
        # The API-side mirror of exposure.py's full-allowed-list fallback: losing search
        # degrades discovery, never availability. `degraded` is returned explicitly so a
        # caller can tell "nothing matched" from "search is down" -- an empty result list
        # that silently means the second is how a broken index gets mistaken for a small
        # catalogue.
        return {"query": q, "results": [], "count": 0, "degraded": True}


def _access_payload(agent_id: str, agent_name: str, granted: list[str]) -> dict[str, Any]:
    return {
        "agent_id": agent_id,
        "agent_name": agent_name,
        "granted": granted,
        "summary": describe_grant(granted),
    }


@router.get("/agents")
def list_agent_access(caps: CapStoreDep, collab: CollabStoreDep) -> dict[str, Any]:
    """Every agent with its grant summary -- the roster view."""
    grants = caps.grants_for_all()
    agents = collab.list_agents()
    return {
        "agents": [
            _access_payload(
                str(agent["id"]),
                str(agent.get("name", "")),
                grants.get(str(agent["id"]), []),
            )
            for agent in agents
        ]
    }


@router.get("/agents/{agent_id}")
def get_agent_access(agent_id: str, caps: CapStoreDep, collab: CollabStoreDep) -> dict[str, Any]:
    """One agent's grant, plus the blast-radius summary the UI renders."""
    agent = collab.get_agent(agent_id)
    if agent is None:
        raise ApiError(404, "not_found", f"No agent {agent_id!r}")
    return _access_payload(agent_id, str(agent.get("name", "")), caps.get_grant(agent_id))


@router.put("/agents/{agent_id}")
def set_agent_access(
    agent_id: str,
    body: SetGrantRequest,
    caps: CapStoreDep,
    collab: CollabStoreDep,
) -> dict[str, Any]:
    """Replace an unscoped grant or add lifecycle-scoped capabilities.

    Granting a consequential capability (a refund, an ad launch, a DNS change) is
    permitted and is what the approval queue exists for -- but it can never run
    unattended. The broker refuses it in code at call time regardless of policy
    config, so a grant here buys the agent the right to *ask*, not to act.
    """
    agent = collab.get_agent(agent_id)
    if agent is None:
        raise ApiError(404, "not_found", f"No agent {agent_id!r}")
    try:
        scoped = (
            body.mode != "read"
            or body.expires_at is not None
            or body.issued_by is not None
            or body.request_id is not None
        )
        if scoped:
            granted = caps.issue_scoped_grant(
                agent_id,
                body.granted,
                mode=body.mode,
                expires_at=body.expires_at,
                issued_by=body.issued_by or "operator",
                request_id=body.request_id,
                actor="operator",
                note=body.note,
            )
        else:
            granted = caps.set_grant(agent_id, body.granted, actor="operator", note=body.note)
    except ConnectorError as exc:
        raise ApiError(422, "unknown_capability", str(exc)) from exc
    return _access_payload(agent_id, str(agent.get("name", "")), granted)


@router.get("/log")
def get_grant_log(caps: CapStoreDep, agent_id: str | None = None) -> dict[str, Any]:
    """Append-only audit of grant changes: who gave what to whom, and when."""
    return {"entries": caps.grant_log(agent_id)}


@router.get("/calls")
def get_call_log(caps: CapStoreDep, run_id: str | None = None) -> dict[str, Any]:
    """Every brokered call an agent made -- allowed and denied.

    This is the "what did my agents do overnight?" view, which is the question that
    matters once loops are running unattended.
    """
    return {"entries": caps.call_log(run_id)}


class BrokerCallRequest(AccessModel):
    capability: str
    method: str = "GET"
    path: str = "/"
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any = None


@router.post("/call")
def broker_call(
    body: BrokerCallRequest,
    caps: CapStoreDep,
    authorization: Annotated[str, Header()] = "",
) -> dict[str, Any]:
    """Execute a capability on behalf of a run. THE ONLY DOOR AN AGENT GETS.

    The caller presents a per-run bearer token, not a grant. We resolve
    token -> run -> agent -> grant from the database, so an agent cannot describe
    its own powers to us: it can only prove which agent it is, and we look up what
    that agent may do. An agent that rewrites its own environment gains nothing,
    because its environment never contained the grant.

    Every call is logged, allowed or denied, before the response is returned.

    AUDIT OWNERSHIP (K5). U-A1 moved the attempt record INTO ``broker.call``,
    but this route kept writing its own row on top, so an API-originated call to
    a credentialed capability produced up to THREE rows (broker intent, broker
    terminal, route legacy) and any count over ``broker_calls`` double-counted
    it. Worse, the route passed no ``audit_context``, so the two rows the broker
    DID own were attributed to the default holder ``"system"`` with a synthetic
    run id -- the spine said "who did this: system" on exactly the calls where a
    real, authenticated agent was identified.

    Both halves are fixed by naming the actor once: the route hands the broker a
    canonical holder (``lane:api``, the door itself) plus the run and the real
    ``agent_id``, and writes its own row ONLY for the capabilities the broker
    structurally cannot audit -- the credential-free and unknown ones, which
    take the unlogged path. So every call is recorded exactly once, by whichever
    layer can actually see it.
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise ApiError(401, "unauthorized", "a broker token is required")

    identity = caps.resolve_token(token)
    if identity is None:
        raise ApiError(401, "unauthorized", "broker token is unknown, expired, or revoked")

    run_id, agent_id = identity["run_id"], identity["agent_id"]
    granted = caps.get_grant(agent_id)  # authoritative; NOT supplied by the caller

    # Asked BEFORE the call so the answer is the same on the denied and the
    # allowed path, and so a broker that starts auditing more never produces a
    # duplicate here.
    spine_owns_the_record = broker.audits_own_calls(body.capability)
    audit_context = broker.AuditContext(
        holder=API_BROKER_HOLDER,
        run_id=run_id,
        request_id=run_id,
        correlation_id=agent_id,
    )

    def _log_if_unaudited(*, allowed: bool, reason: str = "", status: int | None = None) -> None:
        if spine_owns_the_record:
            return
        caps.log_call(
            run_id,
            agent_id,
            body.capability,
            method=body.method,
            path=body.path,
            allowed=allowed,
            reason=reason,
            status=status,
            holder=API_BROKER_HOLDER,
            decision="allowed" if allowed else "denied",
        )

    try:
        result = broker.call(
            body.capability,
            granted,
            method=body.method,
            path=body.path,
            query=body.query,
            body=body.body,
            grant_store=caps,
            agent_id=agent_id,
            # The route already holds the control-plane store this request is
            # served from. Letting the broker open a SECOND handle to the same
            # database would put two writers on one file for one logical call,
            # and ``log_call`` deliberately runs with ``busy_timeout=0``.
            audit_store=caps,
            audit_context=audit_context,
        )
    except BrokerDenied as exc:
        _log_if_unaudited(allowed=False, reason=exc.reason)
        raise ApiError(
            403,
            exc.reason,
            exc.next_action or exc.detail or str(exc),
            exc.payload(),
        ) from exc

    _log_if_unaudited(allowed=True, status=int(result["status"]))
    return result


__all__ = ["router"]
