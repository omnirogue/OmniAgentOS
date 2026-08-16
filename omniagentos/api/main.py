"""ASGI application for OmniAgentOS's localhost control plane."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import threading
from collections.abc import AsyncGenerator, Iterable, Mapping
from typing import Annotated, Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omniagentos.api.boot_receipt import boot_receipt
from omniagentos.api.middleware.chokepoint import ChokePointMiddleware
from omniagentos.api.routes.access import router as access_router
from omniagentos.api.routes.accounts import router as accounts_router
from omniagentos.api.routes.alerts import router as alerts_router
from omniagentos.api.routes.artifacts_preview import router as artifacts_preview_router
from omniagentos.api.routes.autonomy import router as autonomy_router
from omniagentos.api.routes.banking import router as banking_router
from omniagentos.api.routes.board_files import router as board_files_router
from omniagentos.api.routes.briefings import router as briefings_router
from omniagentos.api.routes.categories import router as categories_router
from omniagentos.api.routes.cbm import router as cbm_router
from omniagentos.api.routes.chats import router as chats_router
from omniagentos.api.routes.collab import router as collab_router
from omniagentos.api.routes.comms import router as comms_router
from omniagentos.api.routes.company_goals import employees_router
from omniagentos.api.routes.company_goals import router as company_goals_router
from omniagentos.api.routes.compute import router as compute_router
from omniagentos.api.routes.connections import router as connections_router
from omniagentos.api.routes.control import ApiError, _authorized, router
from omniagentos.api.routes.decisions import router as decisions_router
from omniagentos.api.routes.employee_transcripts import employee_transcripts_router
from omniagentos.api.routes.engine import router as engine_router
from omniagentos.api.routes.filesearch import router as filesearch_router
from omniagentos.api.routes.goals import router as goals_router
from omniagentos.api.routes.graph import router as graph_router
from omniagentos.api.routes.grok_ops import router as grok_ops_router
from omniagentos.api.routes.hierarchy import router as hierarchy_router
from omniagentos.api.routes.improvements import router as improvements_router
from omniagentos.api.routes.intake import router as intake_router
from omniagentos.api.routes.jira import router as jira_router
from omniagentos.api.routes.knowledge import router as knowledge_router
from omniagentos.api.routes.memlife import router as memlife_router
from omniagentos.api.routes.metacog import router as metacog_router
from omniagentos.api.routes.models import router as models_router
from omniagentos.api.routes.models_formation import router as models_formation_router
from omniagentos.api.routes.mounts import router as mounts_router
from omniagentos.api.routes.notifications import router as notifications_router
from omniagentos.api.routes.org import router as org_router
from omniagentos.api.routes.orgdims import router as orgdims_router
from omniagentos.api.routes.projects import router as projects_router
from omniagentos.api.routes.provision import router as provision_router
from omniagentos.api.routes.pulse import router as pulse_router
from omniagentos.api.routes.reflection import router as reflection_router
from omniagentos.api.routes.reliability import router as reliability_router
from omniagentos.api.routes.revenue import router as revenue_router
from omniagentos.api.routes.routines import router as routines_router
from omniagentos.api.routes.semsearch import router as semsearch_router
from omniagentos.api.routes.session_events import router as session_events_router
from omniagentos.api.routes.session_ledger import router as session_ledger_router
from omniagentos.api.routes.sessions import router as sessions_router
from omniagentos.api.routes.suggestions import router as suggestions_router
from omniagentos.api.routes.swarm import router as swarm_router
from omniagentos.api.routes.system import router as system_router
from omniagentos.api.routes.system_jobs import router as system_jobs_router
from omniagentos.api.routes.team import router as team_router
from omniagentos.api.routes.testobs import router as testobs_router
from omniagentos.api.routes.today import router as today_router
from omniagentos.api.routes.voice import router as voice_router
from omniagentos.api.routes.workfs import router as workfs_router
from omniagentos.connectors.secrets_env import load_secrets_env
from omniagentos.lab.api.routes import router as lab_router
from omniagentos.simgate import assert_startup_coherence
from omniagentos.skills.router import router as skills_router

LOG = logging.getLogger(__name__)

_MAX_JSON_REQUEST_BYTES = 16 * 1024 * 1024
_MAX_MULTIPART_REQUEST_BYTES = 500 * 1024 * 1024
# The three routers that actually accept multipart. The budget is granted per
# router PREFIX, not per upload endpoint, so every entry here also hands the
# 500MB budget to that router's plain-JSON routes — add nothing speculatively.
_MULTIPART_PATHS = (
    "/api/employee-transcripts",
    "/api/board/",
    "/api/projects",
)
_MAX_VALIDATION_ERRORS = 100
_MAX_VALIDATION_MESSAGE_CHARS = 512
_MAX_VALIDATION_LOC_CHARS = 200
_MAX_VALIDATION_LOC_ITEMS = 10
_HTTP_ERROR_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "request_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    503: "unavailable",
}


class _BodySizeLimitMiddleware:
    """Reject oversized HTTP request bodies before FastAPI parses or dispatches them.

    Multipart uploads retain their route-specific 100 MB/file and 500 MB/request
    limits. JSON and non-multipart requests use a 16 MiB backstop, leaving routes
    with tighter streaming limits in control of their own request streams.
    """

    def __init__(self, app: ASGIApp, max_body_bytes: int = _MAX_JSON_REQUEST_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        bytes_seen = 0
        body_too_large = False
        response_started = False
        max_body_bytes = self._limit_for_scope(scope)

        async def limited_receive() -> Message:
            nonlocal body_too_large, bytes_seen
            message = await receive()
            if message["type"] == "http.request":
                bytes_seen += len(message.get("body", b""))
                if bytes_seen > max_body_bytes:
                    body_too_large = True
                    # Do not forward the over-limit chunk. The invalid sentinel
                    # prevents a valid prefix from reaching the route handler.
                    return {"type": "http.request", "body": b"\x00", "more_body": False}
            return message

        async def limited_send(message: Message) -> None:
            nonlocal response_started
            if not body_too_large:
                if message["type"] == "http.response.start":
                    response_started = True
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not body_too_large:
                raise
            LOG.warning("suppressed downstream error on over-limit body", exc_info=True)
        if body_too_large:
            if response_started:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            else:
                await self._reject(scope, receive, send, max_body_bytes)

    @staticmethod
    def _content_type(scope: Scope) -> str:
        content_type = (
            next(
                (
                    value.decode("latin-1")
                    for name, value in scope.get("headers", [])
                    if name == b"content-type"
                ),
                "",
            )
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        return content_type

    def _limit_for_scope(self, scope: Scope) -> int:
        if self._content_type(scope) == "multipart/form-data" and scope.get("path", "").startswith(
            _MULTIPART_PATHS
        ):
            return _MAX_MULTIPART_REQUEST_BYTES
        return self.max_body_bytes

    async def _reject(
        self, scope: Scope, receive: Receive, send: Send, max_body_bytes: int
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "error": {
                    "code": "request_too_large",
                    "message": "request body exceeds the allowed size",
                    "detail": {"limit_bytes": max_body_bytes},
                }
            },
        )
        await response(scope, receive, send)


# JG-1: load var/secrets/*.env into os.environ only when unset (never logs values).
load_secrets_env()

# Product-scoped CORS (C-08). Grok dashboard defaults to :3003 (launcher
# OMNIAGENTOS_DASH_PORT); Omni legacy :3000 is NOT implied. Origins are an explicit
# allow-list — never "*" with credentials.
_DEFAULT_DASHBOARD_PORT = 3003
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
# Full origin: scheme + host [+ port], no path/query/userinfo/fragment.
_ORIGIN_RE = re.compile(
    r"^https?://(127\.0\.0\.1|localhost)(:\d{1,5})?$",
    re.IGNORECASE,
)


def _canonical_dashboard_port(env: Mapping[str, str] | None = None) -> int:
    """Resolve the product dashboard port (OMNIAGENTOS_DASH_PORT / OMNIAGENTOS_DASH_PORT)."""
    environ = env if env is not None else os.environ
    raw = (
        environ.get("OMNIAGENTOS_DASH_PORT")
        or environ.get("OMNIAGENTOS_DASH_PORT")
        or str(_DEFAULT_DASHBOARD_PORT)
    )
    try:
        port = int(str(raw).strip())
    except (TypeError, ValueError):
        LOG.warning("invalid dashboard port %r; using %s", raw, _DEFAULT_DASHBOARD_PORT)
        return _DEFAULT_DASHBOARD_PORT
    if not (1 <= port <= 65535):
        LOG.warning("dashboard port out of range %s; using %s", port, _DEFAULT_DASHBOARD_PORT)
        return _DEFAULT_DASHBOARD_PORT
    return port


def validate_cors_origin(origin: str) -> str | None:
    """Return a normalized loopback origin, or None if invalid / non-product.

    Product policy (fail closed):
    * http or https only
    * host must be 127.0.0.1 or localhost
    * no path, query, fragment, or userinfo
    * never ``*`` (wildcard credentials are forbidden)
    * port, when present, must be in 1..65535 (urlparse raises on out-of-range)
    """
    if not isinstance(origin, str):
        return None
    candidate = origin.strip().rstrip("/")
    if not candidate or candidate == "*":
        return None
    if not _ORIGIN_RE.match(candidate):
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname not in _LOOPBACK_HOSTS:
        return None
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        return None
    if parsed.username or parsed.password:
        return None
    host = parsed.hostname
    # urlparse(...).port raises ValueError for ports outside 0..65535 (e.g. 99999).
    # Catch that and also reject 0 so invalid env entries never crash module import.
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is not None:
        if not (1 <= port <= 65535):
            return None
        return f"{parsed.scheme}://{host}:{port}"
    return f"{parsed.scheme}://{host}"


def _origins_for_port(port: int) -> list[str]:
    return [f"http://{host}:{port}" for host in _LOOPBACK_HOSTS]


def _parse_cors_origins_env(raw: str) -> list[str]:
    origins: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        validated = validate_cors_origin(part)
        if validated is None:
            LOG.warning("ignoring invalid CORS origin %r", part.strip())
            continue
        if validated not in seen:
            seen.add(validated)
            origins.append(validated)
    return origins


def allowed_cors_origins(
    *,
    env: Mapping[str, str] | None = None,
    dashboard_port: int | None = None,
) -> list[str]:
    """Validated product-scoped CORS allow-list (never includes ``*``).

    Resolution order:
    1. ``OMNIAGENTOS_CORS_ORIGINS`` — comma-separated full origins (each validated)
    2. else ``http://127.0.0.1:<port>`` and ``http://localhost:<port>`` where
       ``port`` is ``OMNIAGENTOS_DASH_PORT`` / ``OMNIAGENTOS_DASH_PORT`` / 3003

    Extra loopback ports may be appended via ``OMNIAGENTOS_CORS_EXTRA_PORTS``
    (comma-separated integers) for explicitly configured alternate dash ports
    (e.g. 3011 for a local secondary dashboard instance).
    """
    environ = env if env is not None else os.environ
    explicit = (environ.get("OMNIAGENTOS_CORS_ORIGINS") or "").strip()
    origins: list[str] = []
    if explicit:
        origins = _parse_cors_origins_env(explicit)
        if not origins:
            LOG.warning(
                "OMNIAGENTOS_CORS_ORIGINS set but no valid origins; falling back to dashboard port"
            )

    if not origins:
        port = dashboard_port if dashboard_port is not None else _canonical_dashboard_port(environ)
        origins = _origins_for_port(port)
    seen = set(origins)

    extra_raw = (environ.get("OMNIAGENTOS_CORS_EXTRA_PORTS") or "").strip()
    if extra_raw:
        for part in extra_raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                extra_port = int(part)
            except ValueError:
                LOG.warning("ignoring invalid CORS extra port %r", part)
                continue
            if not (1 <= extra_port <= 65535):
                LOG.warning("ignoring out-of-range CORS extra port %s", extra_port)
                continue
            for origin in _origins_for_port(extra_port):
                if origin not in seen:
                    seen.add(origin)
                    origins.append(origin)
    return origins


def cors_middleware_kwargs(origins: Iterable[str] | None = None) -> dict[str, Any]:
    """kwargs for CORSMiddleware — credentials never combined with wildcard."""
    allow = list(origins) if origins is not None else allowed_cors_origins()
    # Defense in depth: strip any wildcard that slipped past validation.
    allow = [o for o in allow if o and o != "*"]
    return {
        "allow_origins": allow,
        "allow_credentials": False,
        "allow_methods": ["*"],
        "allow_headers": ["*"],
    }


def _resolve_resume_orphaned_orchestrations() -> Any:
    """Lazy resolver for resume_orphaned_orchestrations — patchable for tests."""
    from omniagentos.intake.service import resume_orphaned_orchestrations

    return resume_orphaned_orchestrations


# HTTP methods that mutate state. Deny-by-default auth (below) gates EVERY one of
# them. Most reads stay public, but project-file reads are capabilities over the
# local filesystem and are explicitly gated as well.
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The SMALL explicit allowlist of mutating routes that are genuinely public or
# carry their OWN authentication, so the session-token gate must NOT also apply:
#
#   * POST /api/comms/inbound — the inbound-webhook boundary. It authenticates
#     every request with a per-source ``X-Comms-Token`` (see comms.inbound); it is
#     called by EXTERNAL webhook senders that cannot hold the local session token.
#
#   * POST /api/sessions/hook-eval — carries its OWN authentication (AC-policy
#     hook-auth): either the full session token OR a session's narrowly-scoped
#     hook-eval credential bound to that session's own row (see
#     sessions.hook_token / routes.sessions._hook_eval_authorized). A sandboxed
#     bridge session cannot read the full token (var/secrets is OS-sandbox-denied
#     to it) but CAN read its own scoped credential, so this route must evaluate
#     that itself rather than being rejected here first. The scoped credential
#     authorizes ONLY hook-eval (and the sibling attention hook below) for ONLY
#     the session it was minted for; every OTHER mutating route (including every
#     other /api/sessions/* route) still requires the real session token via this
#     same gate, unchanged.
#
#   * POST /api/session-events/hook — same AC-policy hook-auth as hook-eval
#     (``_hook_eval_authorized``). Claude Code Notification/Stop/SessionEnd
#     hooks POST here with the scoped credential; the app-level gate must not
#     reject them first.
#
# Everything else that mutates fails closed. A NEW mutating route is gated the
# moment it is added unless its exact path is added here on purpose (fail-closed
# by default — the Round-7 gap was that new/existing mutating routes were open).
_PUBLIC_MUTATION_PATHS = frozenset(
    {"/api/comms/inbound", "/api/sessions/hook-eval", "/api/session-events/hook"}
)

# A caller that presents only the machine-wide session bearer has no human or
# per-principal proof.  Name that subject ``system`` rather than letting it
# inherit the operator's authority by omission.  Omitting the header is
# deliberately the least-privileged identity.
#
# This is the SAME header name and derivation as the dashboard's own trusted
# hop uses elsewhere in the estate: the dashboard's server-side proxy resolves
# a signed, host-only browser-credential cookie (never a client-supplied
# header) and forwards ONLY that verified result as this header — see
# ``dashboard/src/lib/serverProxy.ts``'s human-decision path and
# ``dashboard/src/lib/browserCredential.ts::resolveBrowserCredential``.
# ``Tailscale-User-Login`` is a DIFFERENT header in a different hop: Caddy
# injects it only into the dashboard's own ``/api/auth/login`` route, which
# uses it solely to mint that signed cookie; it never reaches this FastAPI
# process, so it cannot be this constant's source. Do not substitute it here.
#
# As of this commit that dashboard-side forwarding exists for the
# second-factor route class only (PUT /api/autonomy, improvement decisions);
# cross-principal-read and approval-parked routes have no producer wired yet
# anywhere in the estate. This deny-list is therefore defence in depth against
# a caller holding only the shared session token, not yet a full identity
# boundary — FastAPI cannot itself distinguish the dashboard proxy from any
# other local caller presenting the same header. See PR#15's review thread for
# the operator's open ruling on shipping this now vs. holding for the
# dashboard-side producer to cover every class.
_AUTHENTICATED_PRINCIPAL_HEADER = "X-Omni-Authenticated-Principal"
_SYSTEM_PRINCIPAL = "system"

# These are policy *classes*, not a permissive fallback.  Keeping the deny-list
# explicit means a new sensitive route must opt into a class below; it cannot
# silently acquire the machine bearer just because it is session-token-gated.
#
# ``approval_parked`` maps the API actions that create, grant, or decide the
# money/customer/production-delete work AD-15 parks for a human.  ``system`` is
# an execution identity, never that human.
_SYSTEM_DENY_RULES = frozenset(
    {
        "second_factor",
        "cross_principal_read",
        "approval_parked",
    }
)


def _request_principal(x_omni_authenticated_principal: str | None) -> str:
    """Resolve the request subject; a raw machine bearer is always ``system``.

    The header is intentionally only a transport assertion at this layer.  The
    trusted dashboard proxy is responsible for deriving it from its signed
    browser credential; callers that present no assertion cannot accidentally
    become an operator merely by holding ``sessions-token``.

    Mirrors the shape validation the dashboard-side producer applies before it
    ever mints this header (empty, over-length, or control-character payloads
    are refused there too): a malformed assertion is not a softer form of
    "human" — it is treated exactly like an absent one, ``system``, rather than
    being accepted at face value.
    """
    principal = (x_omni_authenticated_principal or "").strip()
    if not principal or len(principal) > 256:
        return _SYSTEM_PRINCIPAL
    if any(ord(char) < 32 or ord(char) == 127 for char in principal):
        return _SYSTEM_PRINCIPAL
    return principal


def _path_parts(request: Request) -> tuple[str, ...]:
    """The router-visible path segments, shared by system deny predicates."""
    return tuple(request.scope["path"].strip("/").split("/"))


def _is_second_factor_route(request: Request) -> bool:
    """Return whether this route changes autonomy or an improvement decision."""
    parts = _path_parts(request)
    if request.method == "PUT" and parts == ("api", "autonomy"):
        return True
    if request.method == "PATCH" and len(parts) == 4 and parts[:2] == ("api", "goals"):
        return parts[3] == "target"
    # Create and pause are human-only mutations too, same deny class as the
    # target PATCH above -- a raw machine bearer must not reach any of the
    # three goal-mutation routes.
    if request.method == "POST" and parts == ("api", "goals"):
        return True
    if request.method == "POST" and len(parts) == 4 and parts[:2] == ("api", "goals"):
        return parts[3] == "pause"
    return (
        request.method == "POST"
        and len(parts) == 4
        and parts[:2] == ("api", "improvements")
        and parts[3] in {"approve", "reject", "apply", "rollback", "pull"}
    )


def _is_approval_parked_route(request: Request) -> bool:
    """Return whether this is a human approval, grant, or high-risk decision."""
    if request.method not in _MUTATING_METHODS:
        return False
    parts = _path_parts(request)
    if request.method == "PUT" and len(parts) == 4 and parts[:2] == ("api", "access"):
        # Replacing an agent's capability grant can include consequential and
        # irreversible powers, which remain human-gated at the broker.
        return parts[2] == "agents"
    if request.method != "POST":
        return False
    return (
        (len(parts) == 4 and parts[:2] == ("api", "approvals") and parts[3] == "decision")
        or (len(parts) == 4 and parts[:2] == ("api", "suggestions") and parts[3] == "approve")
        or (
            len(parts) == 4
            and parts[:2] == ("api", "reflection")
            and parts[3] in {"approve", "reject"}
        )
        or (
            len(parts) == 5
            and parts[:3] == ("api", "org", "agent-requests")
            and parts[4] in {"approve", "reject"}
        )
        or (parts == ("api", "grok", "grants"))
        or (len(parts) == 5 and parts[:3] == ("api", "grok", "grants") and parts[4] == "revoke")
    )


def _is_cross_principal_read(request: Request) -> bool:
    """Return whether this reads another principal's private machine state."""
    return any(
        (
            _is_project_file_read(request),
            _is_mounts_request(request),
            _is_workfs_request(request),
            _is_server_inventory_request(request),
            _is_employee_transcript_read(request),
            _is_session_ledger_read(request),
            _is_gated_read_namespace(request),
        )
    )


def _system_route_is_denied(request: Request) -> bool:
    """Apply the explicit policy classes to a raw machine-token caller."""
    return (
        ("second_factor" in _SYSTEM_DENY_RULES and _is_second_factor_route(request))
        or ("cross_principal_read" in _SYSTEM_DENY_RULES and _is_cross_principal_read(request))
        or ("approval_parked" in _SYSTEM_DENY_RULES and _is_approval_parked_route(request))
    )


def _is_project_file_read(request: Request) -> bool:
    """Return whether this is a GET/HEAD of a project filesystem endpoint."""
    if request.method not in {"GET", "HEAD"}:
        return False
    # Parse scope["path"] -- the exact string the router matches on -- NOT
    # request.url.path, which Starlette truncates at a literal '?'/'#'. Reading
    # url.path here would let "/api/projects/{id}%3F/files" skip the gate while
    # still reaching the file handler (gate/router split-brain).
    parts = request.scope["path"].strip("/").split("/")
    return len(parts) >= 4 and parts[:2] == ["api", "projects"] and parts[3] == "files"


def _is_mounts_request(request: Request) -> bool:
    """Return whether this is a GET/HEAD of the mounts filesystem-browse API.

    Mirrors ``_is_project_file_read``: ``/api/mounts/*`` (list mounts, browse a
    directory, preview a file) reads this machine's real filesystem -- an even
    broader capability than the per-project grants -- so it is gated the same
    way despite being GET/HEAD (P2 mounts browse API).
    """
    if request.method not in {"GET", "HEAD"}:
        return False
    parts = request.scope["path"].strip("/").split("/")
    return len(parts) >= 2 and parts[:2] == ["api", "mounts"]


def _is_workfs_request(request: Request) -> bool:
    """Return whether this request reads or writes the local WORK tree.

    WorkFS exposes both directory names (tree) and mkdir capability (ensure),
    so it follows the same session-token policy as the adjacent mounts
    filesystem API even though the tree endpoint is a GET.
    """
    parts = request.scope["path"].strip("/").split("/")
    return len(parts) >= 2 and parts[:2] == ["api", "workfs"]


def _is_server_inventory_request(request: Request) -> bool:
    """Return whether this is a GET/HEAD of the server-fleet inventory route.

    ``GET /api/access/servers`` returns live hosts/users/ports/purposes/status
    and SSH-key *paths* for the whole fleet -- recon-sensitive even though it
    never returns key material -- so it is gated the same way as project-file
    and mounts reads (finding 5). This is a LITERAL-path match: only the exact
    ``/api/access/servers`` route is gated, not the rest of ``/api/access/*``
    (``/api/access/capabilities`` and the other GETs there are intentionally
    public catalogue/roster reads and must stay reachable without a token).
    """
    if request.method not in {"GET", "HEAD"}:
        return False
    return request.scope["path"] == "/api/access/servers"


def _is_employee_transcript_read(request: Request) -> bool:
    """Return whether this is a GET/HEAD of the employee-transcript surface.

    ``/api/employee-transcripts`` (list) and ``/api/employee-transcripts/{id}``
    (metadata) return the operator-supplied transcript FILENAMES, the SHA-256 of
    every stored transcript, and the on-disk ``storage_path`` layout of the
    contained transcript root -- i.e. a directory listing of a private HR
    corpus, plus the exact leaf names an attacker would need to aim at that
    root. That is the same recon tier as ``/api/workfs/*`` (directory names) and
    ``GET /api/access/servers`` (fleet layout), so it is gated the same way even
    though both routes are GET. The POST upload is already gated by the
    mutating-method rule; this is a PREFIX match over the router's own prefix,
    so any future read added under it is gated the moment it exists rather than
    the moment someone remembers.
    """
    if request.method not in {"GET", "HEAD"}:
        return False
    # scope["path"], not url.path -- see _is_project_file_read for why (a literal
    # '?' in a transcript id must not create a gate/router split-brain).
    parts = request.scope["path"].strip("/").split("/")
    return len(parts) >= 2 and parts[:2] == ["api", "employee-transcripts"]


def _is_session_ledger_read(request: Request) -> bool:
    """Return whether this is a GET/HEAD of the session-ledger relay routes.

    ``GET /api/ledger/claims`` and ``GET /api/ledger/tail`` shell out to the
    estate session-ledger CLI (``~/.omniagentos/ops/bin/ledger``) and relay
    claim/session history across every project on the machine -- cross-
    project recon, the same tier as the other gated read namespaces below --
    so they require the session token even though most GETs in this app are
    public by convention (session-ledger integration brief, S1). This is a
    LITERAL two-path match, sibling under the SAME ``/api/ledger`` prefix to
    the pre-existing PUBLIC ``GET /api/ledger`` (an unrelated cognitive-flow
    manifest read, see ``routes.control.ledger``): gating the whole
    ``("api", "ledger")`` namespace the way ``_GATED_READ_NAMESPACES`` gates
    accounts/provision below would silently 401 that unrelated route, so this
    stays a predicate over the two specific sub-paths instead.
    """
    if request.method not in {"GET", "HEAD"}:
        return False
    parts = request.scope["path"].strip("/").split("/")
    return len(parts) == 3 and parts[:2] == ["api", "ledger"] and parts[2] in {"claims", "tail"}


# GET/HEAD namespaces that are gated as a WHOLE, declared as path segments
# rather than as one more hand-written predicate. The predicates above match on
# path SHAPE (a ``files`` segment in third position, one literal route); these
# match on the namespace a route lives in, which is what a new sibling route
# inherits automatically:
#
#   * ``/api/accounts/*`` — the provider-credential roster. ``GET /api/accounts``
#     returns per-account email, ``config_dir`` path, ``auth_type`` and status
#     detail, and ``GET /api/accounts/usage`` reads each config dir off disk;
#     credential-adjacent recon for a confined agent, which is exactly what the
#     session token exists to deny (I-13).
#
#   * ``/api/provision/*`` — ``GET /api/provision/{project_id}`` returns the
#     directories and connector capabilities a project has been granted: the
#     same filesystem-capability inventory shape as the project-file and mounts
#     reads gated above, and the read half of the scope-escape surface whose
#     write half (``POST .../request``) is already gated as a mutation.
#
# ``tests/api/test_auth_posture.py`` enumerates every GET route and fails unless
# it is either gated or named in that file's explicit public allowlist, so a new
# sensitive GET cannot default to public silently.
#   * ``/api/decisions/*`` — the Executive Decision Center (migration 130). Every
#     row is a PRIVATE, owner-scoped decision; a machine principal owns no queue,
#     so the whole namespace is gated (session token required) and the raw
#     ``system`` principal is denied here via ``_is_cross_principal_read`` — the
#     same posture as ``/api/team``. The route ALSO derives the owner from the
#     principal and 403s ``system`` in-handler (defence in depth, and it covers
#     the POST decide path this GET-only gate does not).
#   * ``/api/ops/*`` — the boot composition receipt (dsh-audit C-21). It reports
#     this machine's own boot state, including swallowed exception text, which is
#     the same content class as ``/api/workfs/tree`` (directory names) and
#     ``GET /api/access/servers`` (SSH-key paths). Declared as a NAMESPACE, not a
#     literal path, so a second ``/api/ops`` read cannot mount itself public: the
#     launchd/topology inventories this lane ships as scripts are the obvious next
#     candidates. Namespace membership also puts it in ``_is_cross_principal_read``,
#     so a raw machine bearer with no human principal is denied here exactly as it
#     is on its siblings — which is what the dashboard proxy's ``"ops"`` prefix
#     entry then mirrors correctly by construction.
_GATED_READ_NAMESPACES = frozenset(
    {
        ("api", "accounts"),
        ("api", "provision"),
        ("api", "team"),
        ("api", "decisions"),
        ("api", "ops"),
    }
)


def _is_gated_read_namespace(request: Request) -> bool:
    """Return whether this GET/HEAD lands in a wholly-gated namespace."""
    if request.method not in {"GET", "HEAD"}:
        return False
    # scope["path"], not url.path — same gate/router split-brain reason as
    # _is_project_file_read.
    parts = tuple(request.scope["path"].strip("/").split("/"))
    return parts[:2] in _GATED_READ_NAMESPACES


def require_session_token(
    request: Request,
    x_session_token: str | None = Header(None, alias="X-Session-Token"),
) -> None:
    """App-level deny-by-default gate for EVERY mutating request (AC-policy fix7).

    Requires the local session token (``var/secrets/sessions-token``, 0600, provably
    unreadable by a confined agent) on every POST/PUT/PATCH/DELETE outside the tiny
    public allowlist. This closes the Round-7 control-plane escape: before it, only
    ``control.py``/``sessions.py`` carried ``_authorized`` while the SCOPE-DEFINING and
    RUN-AUTHORING routes (``projects.create_project``, ``provision.request_scope``,
    ``routines``, ``suggestions.approve``, intake dispatch, …) were open, so an agent
    could loopback-POST to self-grant scope and author runs inside it.

    Project-file GET/HEAD requests are gated too because they expose filesystem
    capabilities -- and so are ``/api/mounts/*`` GET/HEAD requests (P2), which
    read this machine's real filesystem directly, the literal
    ``GET /api/access/servers`` route (finding 5), which exposes the live
    server fleet (hosts/users/ports/status/SSH-key paths), and
    ``/api/employee-transcripts/*`` GET/HEAD requests (JG3-BE major 6), which
    expose transcript filenames and the contained store's on-disk leaf names,
    and ``GET /api/ledger/claims``/``GET /api/ledger/tail`` (session-ledger
    integration brief, S1), which relay cross-project claim/session history
    off the estate-wide ledger.
    The namespaces in ``_GATED_READ_NAMESPACES`` (``/api/accounts/*``,
    ``/api/provision/*``) are gated the same way, declared as data rather than
    as another predicate (I-13). Other reads pass through untouched. The
    dashboard reaches gated routes through its same-origin Next.js proxy,
    which attaches the token server-side.
    Registered as an app-level FastAPI dependency so it applies to every
    included router and is overridable in tests via
    ``app.dependency_overrides``.
    """
    if (
        request.method not in _MUTATING_METHODS
        and not _is_project_file_read(request)
        and not _is_mounts_request(request)
        and not _is_workfs_request(request)
        and not _is_server_inventory_request(request)
        and not _is_employee_transcript_read(request)
        and not _is_session_ledger_read(request)
        and not _is_gated_read_namespace(request)
    ):
        return
    if request.scope["path"] in _PUBLIC_MUTATION_PATHS:
        return
    from omniagentos.sessions.token import verify_token

    if not verify_token(x_session_token):
        raise ApiError(401, "unauthorized", "invalid or missing session token")
    # This is an internal trusted-proxy assertion, not a public API parameter:
    # declaring it as a FastAPI Header would add it to every globally gated
    # route's OpenAPI operation.  The dashboard's signed browser-credential
    # resolver is its producer (see the constant's definition above for the
    # exact derivation and its current route-class coverage); this deny-list
    # merely treats an absent or malformed assertion as the machine subject.
    asserted_principal = request.headers.get(_AUTHENTICATED_PRINCIPAL_HEADER)
    if _request_principal(asserted_principal) == _SYSTEM_PRINCIPAL and _system_route_is_denied(
        request
    ):
        raise ApiError(
            403,
            "system_principal_forbidden",
            "the machine principal may not access this route",
        )


def _resume_orchestrations_daemon() -> None:
    """Background daemon: resume orphaned orchestrations on startup.

    Runs in a daemon thread with a configurable delay; logs exceptions but
    never crashes startup.
    """
    delay = float(os.environ.get("OMNIAGENTOS_ORCH_RESUME_STARTUP_DELAY_SECONDS", "3"))
    LOG.info(f"Startup resume: waiting {delay}s for app to bind...")
    import time

    time.sleep(delay)
    LOG.info("Startup resume: calling resume_orphaned_orchestrations...")
    try:
        # Lazy import to avoid import-order sensitivity between api and intake
        from omniagentos.api.deps import get_store
        from omniagentos.api.routes.collab import get_collab_store

        store = get_store()
        collab_store = get_collab_store()
        resume_func = _resolve_resume_orphaned_orchestrations()
        resumed = resume_func(store=store, collab_store=collab_store, limit=8)
        LOG.info(f"Startup resume: resumed {len(resumed)} orchestrations")
    except Exception as exc:
        LOG.exception(f"Startup resume: exception: {exc}")


def _assert_explicit_control_plane_db() -> str:
    """Refuse to serve against an implicitly-resolved control-plane database.

    The B2 defect class, generalised. When ``OMNIAGENTOS_DB`` is unset,
    :func:`omniagentos.contracts.default_db_path` silently falls back to
    ``<repo>/var/omniagentos.db`` -- a file that exists, carries the current
    migration head, and holds a plausible-looking handful of rows. Nothing
    fails; the process just serves a decoy. All 16 rendered launchd jobs ran
    that way for 1300 consecutive ticks, exit code 0, reporting healthy, while
    the API served ``var/runtime/state.sqlite3`` -- which is why seeded
    routines sat at ``total_runs=0`` forever.

    A row-count heuristic does not catch this: the decoy holds 26 board_tasks
    against production's 1097. The only reliable signal is *provenance*, so the
    path must be stated explicitly. ``tests/conftest.py`` already pins
    ``OMNIAGENTOS_DB`` for every test, and the launcher exports it in
    ``scripts/launch-env.sh``; set ``OMNIAGENTOS_ALLOW_DEFAULT_DB=1`` for the
    rare deliberate use of the repo-root default.
    """
    from omniagentos.contracts import default_db_path

    explicit = bool(os.environ.get("OMNIAGENTOS_DB", "").strip())
    resolved = default_db_path()
    if explicit:
        LOG.info("Control plane DB: %s (source=OMNIAGENTOS_DB)", resolved)
        return resolved
    if os.environ.get("OMNIAGENTOS_ALLOW_DEFAULT_DB") == "1":
        LOG.warning(
            "Control plane DB: %s (source=repo-root default, "
            "permitted by OMNIAGENTOS_ALLOW_DEFAULT_DB=1)",
            resolved,
        )
        return resolved
    raise RuntimeError(
        "REFUSING TO START: OMNIAGENTOS_DB is unset, so the control-plane "
        f"database resolved implicitly to {resolved}. That fallback is how a "
        "process ends up serving a decoy database that looks healthy and does "
        "nothing. Start via scripts/launch-omniagentos.sh (which sources "
        "scripts/launch-env.sh and exports the canonical path), or set "
        "OMNIAGENTOS_DB explicitly. To use the repo-root default on purpose, "
        "set OMNIAGENTOS_ALLOW_DEFAULT_DB=1."
    )


def _assert_migration_inventory() -> None:
    """Refuse to serve when the migration directory is ambiguous (fail closed).

    Incident 2026-08-04: a stray duplicate ``NNN_*.sql`` made the migration file
    SCAN raise, and because a dozen modules call ``migrate_connection()`` while
    handling a REQUEST (every DAL re-migrates the connection it is handed), the
    fault surfaced as a 500 on every endpoint instead of as a startup refusal.
    Verifying (and memoizing) the inventory here means the ambiguity is reported
    ONCE, loudly, at boot -- and that request paths reuse this verified
    inventory afterwards rather than re-scanning and re-raising per request.
    """
    from omniagentos.db.migrate import DuplicateMigrationVersion, verify_migration_files

    try:
        verified = verify_migration_files(refresh=True)
    except DuplicateMigrationVersion as exc:
        raise RuntimeError(
            "REFUSING TO START: the packaged migration directory is ambiguous "
            f"({exc}). Two files claim the same version prefix, so the applied "
            "schema would depend on filesystem ordering. Remove or renumber the "
            "stray file (migrations are append-only; the highest existing NNN + "
            "1 is the only valid new number), then restart."
        ) from exc
    head = verified[-1][0] if verified else 0
    LOG.info("Migration inventory verified: %d files, head=%03d", len(verified), head)


def _mint_session_token_on_first_boot() -> None:
    """First-boot mint: create the control-plane session token if absent.

    ``sessions/token.py`` split verify from mint: a missing token is now an
    authentication FAILURE (:func:`~omniagentos.sessions.token.verify_token`
    reads only), never a mint event, so an unauthenticated request can no
    longer mint the production credential as a side effect of a failed check.

    That correctly closed the old leak, but it left a fresh var root with no
    legitimate way to ever get a first token: every mutating route --
    including the ones that would spawn the first session -- now requires a
    token that does not exist yet, and the only other minter
    (``sessions/hook_client.py``) runs only from INSIDE an already-spawned
    session, which nothing can spawn without a token already. A brand-new
    tree is otherwise permanently bricked.

    This is the one legitimate first-boot exception: the (unsandboxed,
    control-plane-trusted) API process itself, at its own startup, mints
    exactly once. ``load_or_create_token()`` already reads before it writes
    (``_load_token()``) and creates via ``O_EXCL``, so calling it here is a
    no-op whenever a token already exists and it never re-mints over one.
    Set ``OMNIAGENTOS_MINT_TOKEN_ON_STARTUP=0`` to disable (tests pin this
    off by default; see ``tests/conftest.py``).
    """
    if os.environ.get("OMNIAGENTOS_MINT_TOKEN_ON_STARTUP", "1") != "1":
        return
    try:
        from omniagentos.sessions.token import load_or_create_token, token_path

        load_or_create_token()
        LOG.info("Startup token mint: ensured %s", token_path())
    except Exception:  # noqa: BLE001 -- minting must never block API startup.
        LOG.exception("Startup token mint failed")


#: Vault playbook notes are ``*.md``; a basename is the only token from the
#: aggregated failure text this process is willing to relay.
_VAULT_NOTE_NAME_RE = re.compile(r"\b[\w][\w.-]{0,80}\.md\b")
_MAX_VAULT_NOTE_NAMES = 20


def _vault_index_failure_summary(exc: BaseException) -> str:
    """Structured, ALLOW-LISTED summary of an aggregated vault-index failure.

    ``index_vault_playbook`` raises one ``RuntimeError`` whose message joins
    ``f"{path.name}: {type(exc).__name__}: {exc}"`` for every bad note. That
    inner ``{exc}`` is third-party text this process does not author, and
    PyYAML's ``ScannerError`` embeds a snippet of the offending SOURCE LINE — so
    relaying the message would serve the contents of a vault note out of
    ``GET /api/ops/boot-receipt`` (Class-A review A3). Pattern redaction catches
    credential SHAPES, not arbitrary file content, so nothing is relayed here.

    Instead this extracts only what matches the allow-list: note BASENAMES, plus
    how many there were. Everything else — including the exception's own message
    — is dropped. The full text is still in the API log, which has a different
    reader set than an HTTP endpoint.
    """
    try:
        raw = str(exc)
    except Exception:  # noqa: BLE001 -- a hostile __str__ must not break boot
        return "notes=? (failure text unreadable)"
    names = sorted({match.group(0) for match in _VAULT_NOTE_NAME_RE.finditer(raw)})
    shown = names[:_MAX_VAULT_NOTE_NAMES]
    more = f" (+{len(names) - len(shown)} more)" if len(names) > len(shown) else ""
    listed = ", ".join(shown) if shown else "none identified"
    return f"failing notes={len(names)}: {listed}{more}"


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """FastAPI lifespan hook: startup and shutdown events.

    On startup, assert the control-plane DB was chosen explicitly and that the
    migration inventory is unambiguous, mint the session token on a fresh tree,
    then resume orphaned orchestrations and stale swarm runs.

    Every optional step below is fail-open and stays that way. What changed
    (dsh-audit C-21) is that each outcome is also RECORDED in the process-wide
    boot receipt (:mod:`omniagentos.api.boot_receipt`), readable at
    ``GET /api/ops/boot-receipt``, so a half-composed process is a fact an
    operator can read rather than a line in a log nobody tails. Recording never
    raises and never changes which exceptions are swallowed.
    """
    receipt = boot_receipt()
    receipt.start()
    # Startup. This runs FIRST and is the one startup step allowed to abort
    # boot: every step below it operates on the database it validates.
    _assert_explicit_control_plane_db()
    # Second refusal guard, before anything opens a connection: an ambiguous
    # migration directory must stop the boot, not 500 every request later.
    _assert_migration_inventory()
    assert_startup_coherence()
    # Third: the one legitimate first-boot mint (see docstring) — only after
    # both refusal guards, so an incoherent boot never touches the token
    # path. Never blocks boot and never re-mints over an existing token.
    _mint_session_token_on_first_boot()
    orch_enabled = os.environ.get("OMNIAGENTOS_ORCH_RESUME_ON_STARTUP", "1")
    if orch_enabled == "1":
        thread = threading.Thread(
            target=_resume_orchestrations_daemon,
            name="api-orch-resume-startup",
            daemon=True,
        )
        thread.start()
        # Recorded because the receipt answers "what was composed here", and a
        # started resume thread is part of that composition -- but it is a
        # THREAD: its own failures are logged inside the daemon and cannot be
        # attributed to this step. Said plainly rather than rendered as health.
        receipt.record_ok(
            "orch-resume",
            "resume thread started; the thread's own outcome is not captured here",
        )
    else:
        receipt.record_disabled("orch-resume", "OMNIAGENTOS_ORCH_RESUME_ON_STARTUP")
    if os.environ.get("OMNIAGENTOS_SWARM_RESUME_ON_STARTUP", "1") == "1":
        try:
            from omniagentos.api.routes.swarm import get_swarm_dal
            from omniagentos.swarm.scheduler import resume_stale_swarms

            summary = resume_stale_swarms(db_path=get_swarm_dal().db_path)
            LOG.info("Startup swarm resume: summary=%s", summary)
            # COUNTS ONLY. The summary carries ``db_path`` (the canonicalised
            # ABSOLUTE control-plane DB path), swarm run ids, and up to four more
            # absolute paths under ``db_mismatch`` — so recording it verbatim made
            # the HEALTHY path a filesystem disclosure, not just the degraded one
            # (Class-A review A2). The log line above still has the full detail;
            # this endpoint is read by more callers than the log is.
            receipt.record_ok(
                "swarm-resume",
                "reconciled={} resumed={} skipped_fresh={} candidates={} errors={}".format(
                    len(summary.get("reconciled") or {}),
                    len(summary.get("resumed") or []),
                    len(summary.get("skipped_fresh") or []),
                    summary.get("candidates") or 0,
                    len(summary.get("errors") or []),
                ),
            )
        except Exception as exc:  # noqa: BLE001 -- crash recovery must never block API startup.
            LOG.exception("Startup swarm resume failed")
            receipt.record_degraded("swarm-resume", exc, "startup swarm resume failed")
    else:
        receipt.record_disabled("swarm-resume", "OMNIAGENTOS_SWARM_RESUME_ON_STARTUP")
    if os.environ.get("OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP", "1") == "1":
        store = None
        try:
            from omniagentos.api.deps import get_store
            from omniagentos.db.store import SqliteStore

            store = get_store()
            # get_store() is typed as Store; routine seeding requires the concrete
            # SQLite DAL. Fail closed if a non-SQLite implementation is wired.
            if not isinstance(store, SqliteStore):
                raise TypeError(
                    f"startup routine seed requires SqliteStore, got {type(store).__name__}"
                )
            receipt.record_ok("routine-seed-store", "SqliteStore resolved")
        except Exception as exc:  # noqa: BLE001 -- seeding must never block API startup.
            LOG.exception("store unavailable; no routines seeded")
            receipt.record_degraded("routine-seed-store", exc, "store unavailable")
            store = None
        if store is not None:
            try:
                from omniagentos.improve.dispatcher import ensure_improve_dispatcher_routine
                from omniagentos.lab.jobs import ensure_lab_jobs_routine
                from omniagentos.memlife.dream import ensure_dream_cycle_routine

                ensure_improve_dispatcher_routine(store)
                ensure_lab_jobs_routine(store)
                ensure_dream_cycle_routine(store)
                LOG.info(
                    "Startup routine seed: improve-dispatcher + lab-jobs-drain + "
                    "memlife-dream-cycle ensured"
                )
                receipt.record_ok(
                    "routine-seed",
                    "improve-dispatcher + lab-jobs-drain + memlife-dream-cycle ensured",
                )
            except Exception as exc:  # noqa: BLE001 -- seeding must never block API startup.
                LOG.exception("Startup routine seed failed")
                receipt.record_degraded("routine-seed", exc, "startup routine seed failed")
            try:
                from omniagentos.scheduler.loop_seeding import ensure_w3_health_monitor_routine

                ensure_w3_health_monitor_routine(store)
                receipt.record_ok("w3-health-monitor", "routine ensured")
            except Exception as exc:  # noqa: BLE001 -- W3 failure must never block API startup.
                LOG.exception("w3-health-monitor NOT seeded")
                receipt.record_degraded("w3-health-monitor", exc, "w3-health-monitor NOT seeded")
        else:
            # The seeds below never ran. That is not "ok" and it is not their
            # own failure either -- their precondition was not met.
            reason = "store unavailable; seed did not run"
            receipt.record_skipped("routine-seed", reason)
            receipt.record_skipped("w3-health-monitor", reason)
    else:
        for subsystem in ("routine-seed-store", "routine-seed", "w3-health-monitor"):
            receipt.record_disabled(subsystem, "OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP")
    if os.environ.get("OMNIAGENTOS_SEED_EMPLOYEES_ON_STARTUP", "1") == "1":
        try:
            from omniagentos.api.deps import get_store
            from omniagentos.company_goals.seed_employees import seed_employees
            from omniagentos.company_goals.store import CompanyGoalsStore
            from omniagentos.db.store import SqliteStore

            store = get_store()
            # Same fail-closed guard as the routine seed: the employee roster is
            # written through the concrete SQLite DAL (migration 098).
            if not isinstance(store, SqliteStore):
                raise TypeError(
                    f"startup employee seed requires SqliteStore, got {type(store).__name__}"
                )
            counts = seed_employees(CompanyGoalsStore(store))
            LOG.info(
                "Startup employee seed: created=%d existing=%d",
                counts["created"],
                counts["existing"],
            )
            receipt.record_ok(
                "employee-seed",
                f"created={counts['created']} existing={counts['existing']}",
            )
        except Exception as exc:  # noqa: BLE001 -- seeding must never block API startup.
            LOG.exception("Startup employee seed failed")
            receipt.record_degraded("employee-seed", exc, "startup employee seed failed")
    else:
        receipt.record_disabled("employee-seed", "OMNIAGENTOS_SEED_EMPLOYEES_ON_STARTUP")
    if os.environ.get("OMNIAGENTOS_INDEX_VAULT_ON_STARTUP", "1") == "1":
        try:
            from omniagentos.skills import index_vault_playbook, sync_playbook_from_repo

            synced = sync_playbook_from_repo()
            indexed = index_vault_playbook()
            LOG.info("Startup vault index: synced %d playbook notes, indexed %d", synced, indexed)
            receipt.record_ok("vault-index", f"synced={synced} indexed={indexed}")
        except Exception as exc:  # noqa: BLE001 -- indexing must never block API startup.
            LOG.exception("Startup vault index failed")
            receipt.record_degraded(
                "vault-index",
                exc,
                "startup vault index failed",
                message_override=_vault_index_failure_summary(exc),
            )
    else:
        receipt.record_disabled("vault-index", "OMNIAGENTOS_INDEX_VAULT_ON_STARTUP")
    receipt.complete()
    try:
        yield
    finally:
        # The production scheduler owns a process-lifetime router limits DAL.
        # Stop its workers before closing that shared connection.
        from omniagentos.swarm.scheduler import shutdown_default_schedulers

        shutdown_default_schedulers()


app = FastAPI(
    title="OmniAgentOS Control Plane",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    dependencies=[Depends(require_session_token)],
    lifespan=lifespan,
)
app.add_middleware(ChokePointMiddleware)
app.add_middleware(_BodySizeLimitMiddleware)
app.add_middleware(CORSMiddleware, **cors_middleware_kwargs())


def _validation_error_detail(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Return bounded diagnostics without retaining Pydantic's raw input values."""
    detail: list[dict[str, Any]] = []
    for error in exc.errors()[:_MAX_VALIDATION_ERRORS]:
        raw_loc = error.get("loc", ())
        loc = [
            item if isinstance(item, int) else str(item)[:_MAX_VALIDATION_LOC_CHARS]
            for item in raw_loc[:_MAX_VALIDATION_LOC_ITEMS]
        ]
        detail.append(
            {
                "loc": loc,
                "msg": str(error.get("msg", "invalid request"))[:_MAX_VALIDATION_MESSAGE_CHARS],
                "type": str(error.get("type", "validation_error"))[:_MAX_VALIDATION_MESSAGE_CHARS],
            }
        )
    return detail


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(
            {"error": {"code": exc.code, "message": exc.message, "detail": exc.detail}}
        ),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=jsonable_encoder(
            {
                "error": {
                    "code": "validation",
                    "message": "request validation failed",
                    "detail": _validation_error_detail(exc),
                }
            }
        ),
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_ERROR_CODES.get(exc.status_code, "internal" if exc.status_code >= 500 else "error")
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": str(exc.detail), "detail": None}},
    )


@app.exception_handler(Exception)
async def internal_error_handler(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "internal", "message": "internal server error", "detail": None}},
    )


@app.get("/api/ops/boot-receipt")
def get_boot_receipt(_: Annotated[None, Depends(_authorized)] = None) -> dict[str, Any]:
    """The boot composition receipt for THIS API process (dsh-audit C-21).

    Read-only, in-process, no DB and no filesystem: it reports which of the
    lifespan's optional subsystems came up (``ok``), which raised and were
    swallowed (``degraded``, with the exception type and a bounded message),
    which never ran because a precondition failed (``skipped``), and which were
    switched off by their ``OMNIAGENTOS_*_ON_STARTUP`` flag (``disabled``).

    Gated twice, deliberately, the same way ``/api/decisions`` is: ``/api/ops``
    is in ``_GATED_READ_NAMESPACES``, which requires the session token AND denies
    a raw machine principal via ``_is_cross_principal_read`` — a receipt reports
    this machine's own state, the content class its siblings are gated for. The
    route-level ``Depends(_authorized)`` below it is defence in depth, so the
    endpoint stays gated even if the namespace entry is ever edited out.

    This is an operator surface, not a public health probe (``/health`` remains
    that).
    """
    del _
    return boot_receipt().snapshot()


app.include_router(router)
app.include_router(lab_router)
app.include_router(collab_router)
app.include_router(decisions_router)
app.include_router(categories_router)
app.include_router(chats_router)
app.include_router(intake_router)
app.include_router(access_router)
app.include_router(knowledge_router)
app.include_router(comms_router)
app.include_router(goals_router)
app.include_router(revenue_router)
# hierarchy_router owns GET /api/projects/tree (L-01). It MUST register before
# the flat projects router so the literal path wins over /api/projects/{id}.
# The flat projects router no longer registers a second /tree handler.
app.include_router(hierarchy_router)
app.include_router(projects_router)
app.include_router(mounts_router)
app.include_router(workfs_router)
app.include_router(provision_router)
app.include_router(banking_router)
app.include_router(briefings_router)
app.include_router(alerts_router)
app.include_router(routines_router)
app.include_router(suggestions_router)
app.include_router(voice_router)
app.include_router(sessions_router)
app.include_router(session_events_router)
# GET /api/ledger/claims + GET /api/ledger/tail -- session-ledger relay for
# the board header's open-claims strip and the card drawer's event timeline.
# Sibling to (never inside) control.py's pre-existing GET /api/ledger.
app.include_router(session_ledger_router)
app.include_router(notifications_router)
app.include_router(today_router)
app.include_router(accounts_router)
app.include_router(filesearch_router)
app.include_router(semsearch_router)
app.include_router(skills_router, prefix="/api", tags=["skills"])
app.include_router(board_files_router)
app.include_router(reliability_router)
app.include_router(improvements_router)
app.include_router(reflection_router)
app.include_router(memlife_router)
app.include_router(org_router)
app.include_router(orgdims_router)
app.include_router(autonomy_router)
app.include_router(swarm_router)
app.include_router(engine_router)
app.include_router(graph_router)
app.include_router(cbm_router)
app.include_router(system_router)
app.include_router(artifacts_preview_router)
app.include_router(metacog_router)
app.include_router(grok_ops_router)
app.include_router(models_router)
app.include_router(models_formation_router)
app.include_router(pulse_router)
app.include_router(connections_router)
app.include_router(system_jobs_router)
app.include_router(jira_router)
app.include_router(company_goals_router)
app.include_router(employees_router)
app.include_router(team_router)
app.include_router(employee_transcripts_router)
app.include_router(testobs_router)
app.include_router(compute_router)
