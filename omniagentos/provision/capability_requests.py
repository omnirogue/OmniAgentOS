"""U-E1 — the typed CapabilityRequest spine.

One immutable envelope carries every mid-run capability ask from every lane::

    CapabilityRequest {
      request_id, from_agent_id, from_lane,
      session_id?, run_id?, task_id?, requested_at, rationale,
      request: tool{tool_id} | skill{skill_id} | key_scope{group}
             | extra_agent{formation, count, objective, owned_paths?},
      inherited_floor?
    }

WHAT THE EMITTER MAY NOT SUPPLY
Not ``granted_by``, not risk, not action class, not a credential, not a grant
id, not an expiry, and not its own effective grants. None of those are fields on
this dataclass and none are columns on migration 113, so there is nowhere for a
forged one to land. Identity is the one field an emitter DOES state, and the
service rejects an envelope whose claimed ``from_agent_id`` differs from the
authenticated channel principal -- the emitter names itself, it does not get to
be believed.

THE TWO VALIDATION BOUNDARIES ARE DIFFERENT ON PURPOSE
* **Pre-ledger (no row).** Malformed uuid, non-canonical identity, empty or
  oversized rationale, an impossible union, an identity that does not match the
  channel, and an id that is not ADDRESSABLE -- ``fake.tool`` names a connector
  namespace that does not exist, so there is no system for the request to be
  about. These emit a bounded validation event (no payload echoed) and create no
  row, because a durable ledger of unaddressable strings is a denial-of-service
  surface, not an audit trail.
* **Post-ledger (durable ``hard_rejected``).** A WELL-FORMED, addressable
  request the policy refuses -- ``echo.unknown`` names a registered connector and
  an unknown capability. That is a real ask about a real system and it earns a
  durable decision with a reason code and a next action.

``key_scope`` IS REQUEST-TIME CONVENIENCE ONLY
A worker names a group; the SERVER expands it to that group's callable, read-only,
unmetered, non-danger capability ids. Zero expansion is a typed denial, never a
silent no-op and never a pretend grant. A mixed group grants only the read ids
and NAMES every excluded member in the receipt. A key name never crosses this
boundary in either direction.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

from omniagentos.connectors import ConnectorRegistry, load_registry
from omniagentos.provision.capability_policy import (
    CapabilityPolicy,
    Clock,
    PolicyReceipt,
    SystemClock,
)

LOG = logging.getLogger(__name__)

MAX_RATIONALE_CHARS = 500
MAX_OWNED_PATHS = 32
MAX_EXTRA_AGENTS = 4

#: Formations a worker may name. Resolved server-side to a manifest (U-E6); a
#: worker can never submit prompts, providers, accounts, or launch argv.
FORMATIONS: frozenset[str] = frozenset(
    {"coding", "creative", "research", "marketing", "operations", "prediction"}
)

#: The ONE identity grammar, anchored, matched with ``fullmatch``. Kept byte-equal
#: to ``reliability/store.py:_CANONICAL_IDENTITY_RE`` and to migration 113's
#: GLOB CHECK; ``test_the_three_canonical_identity_enforcers_agree`` fails if any
#: of the three drifts.
_CANONICAL_IDENTITY = re.compile(r"system|(?:lane|loop|job|human):[A-Za-z0-9][A-Za-z0-9._-]*")

#: ``<connector>.<capability>``. Syntax only -- addressability is a registry
#: question, asked separately and answered differently.
_CAPABILITY_ID = re.compile(r"[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*")
_SKILL_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_SCOPE_GROUP = re.compile(r"[a-z][a-z0-9_]{0,31}")


class EnvelopeRejected(ValueError):
    """A request was refused BEFORE the ledger. Carries no payload content.

    ``subject`` is the bounded identifier that failed (a capability id, a group,
    a formation) and never the rationale, a body, or a credential -- an audit
    event that echoes arbitrary emitter text is an injection surface aimed at
    whoever reads it.
    """

    def __init__(self, reason_code: str, detail: str, subject: str = "") -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail
        self.subject = subject[:64]

    def as_event(self) -> dict[str, str]:
        """The bounded validation event. Three fields, all closed-vocabulary."""
        return {
            "event": "validation_rejected",
            "reason_code": self.reason_code,
            "subject": self.subject,
        }


# --------------------------------------------------------------- the union


@dataclass(frozen=True, slots=True)
class ToolRequest:
    """A connector capability id, e.g. ``echo.ping``."""

    tool_id: str

    kind = "tool"

    @property
    def subject_id(self) -> str:
        return self.tool_id

    def validate(self) -> None:
        if not _CAPABILITY_ID.fullmatch(self.tool_id):
            raise EnvelopeRejected(
                "malformed_capability_id",
                "a tool id must be <connector>.<capability>",
                self.tool_id,
            )

    def as_dict(self) -> dict[str, Any]:
        return {"tool": {"tool_id": self.tool_id}}


@dataclass(frozen=True, slots=True)
class SkillRequest:
    """A released, signed, eval-passed skill (S7-6). Operator-decided here."""

    skill_id: str

    kind = "skill"

    @property
    def subject_id(self) -> str:
        return self.skill_id

    def validate(self) -> None:
        if not _SKILL_ID.fullmatch(self.skill_id):
            raise EnvelopeRejected("malformed_skill_id", "a skill id is bounded", self.skill_id)

    def as_dict(self) -> dict[str, Any]:
        return {"skill": {"skill_id": self.skill_id}}


@dataclass(frozen=True, slots=True)
class KeyScopeRequest:
    """A registry GROUP. The server expands it; the worker never names a key."""

    group: str

    kind = "key_scope"

    @property
    def subject_id(self) -> str:
        return self.group

    def validate(self) -> None:
        if not _SCOPE_GROUP.fullmatch(self.group):
            raise EnvelopeRejected("malformed_scope_group", "a scope group is bounded", self.group)

    def as_dict(self) -> dict[str, Any]:
        return {"key_scope": {"group": self.group}}


@dataclass(frozen=True, slots=True)
class ExtraAgentRequest:
    """Teammates as an escalation move. Operator-decided by default (S7-3)."""

    formation: str
    count: int
    objective: str
    owned_paths: tuple[str, ...] = ()

    kind = "extra_agent"

    @property
    def subject_id(self) -> str:
        return self.formation

    def validate(self) -> None:
        if self.formation not in FORMATIONS:
            raise EnvelopeRejected(
                "unknown_formation", "formation is not configured", self.formation
            )
        if not 1 <= self.count <= MAX_EXTRA_AGENTS:
            raise EnvelopeRejected(
                "count_out_of_range",
                f"count must be 1..{MAX_EXTRA_AGENTS}",
                str(self.count),
            )
        if not self.objective.strip() or len(self.objective) > MAX_RATIONALE_CHARS:
            raise EnvelopeRejected("malformed_objective", "objective is bounded and non-empty")
        if len(self.owned_paths) > MAX_OWNED_PATHS:
            raise EnvelopeRejected("too_many_owned_paths", "owned_paths is bounded")
        for path in self.owned_paths:
            if not path or "\x00" in path or ".." in path.replace("\\", "/").split("/"):
                raise EnvelopeRejected("unsafe_owned_path", "owned_paths must not escape", path)

    def as_dict(self) -> dict[str, Any]:
        return {
            "extra_agent": {
                "formation": self.formation,
                "count": self.count,
                "objective": self.objective,
                "owned_paths": list(self.owned_paths),
            }
        }


RequestPayload = ToolRequest | SkillRequest | KeyScopeRequest | ExtraAgentRequest


@dataclass(frozen=True, slots=True)
class ExecutionFloorHint:
    """A rung's requested floor, carried as EVIDENCE OF NEED, never as authority."""

    tier: str = ""
    effort: str = ""
    capabilities: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "effort": self.effort,
            "capabilities": list(self.capabilities),
        }


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    """The immutable envelope. Frozen: what was asked cannot be rewritten later."""

    from_agent_id: str
    from_lane: str
    rationale: str
    request: RequestPayload
    request_id: str = ""
    requested_at: str = ""
    session_id: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    inherited_floor: ExecutionFloorHint | None = field(default=None)

    @classmethod
    def new(
        cls,
        *,
        from_agent_id: str,
        from_lane: str,
        rationale: str,
        request: RequestPayload,
        clock: Clock | None = None,
        **optional: Any,
    ) -> CapabilityRequest:
        """Mint a fresh envelope. The emitter never picks its own request id."""
        now = (clock or SystemClock()).now()
        return cls(
            from_agent_id=from_agent_id,
            from_lane=from_lane,
            rationale=rationale,
            request=request,
            request_id=str(uuid.uuid4()),
            requested_at=now.astimezone(UTC).isoformat(),
            **optional,
        )

    def validate(self) -> None:
        """Structural validation. Raises :class:`EnvelopeRejected`; writes nothing."""
        try:
            uuid.UUID(self.request_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise EnvelopeRejected(
                "malformed_request_id", "request_id must be a uuid", str(self.request_id)[:64]
            ) from exc
        if not _CANONICAL_IDENTITY.fullmatch(self.from_agent_id or ""):
            raise EnvelopeRejected(
                "non_canonical_identity",
                "from_agent_id must be system or lane:/loop:/job:/human:<subject>",
                self.from_agent_id or "",
            )
        if not _CANONICAL_IDENTITY.fullmatch(self.from_lane or ""):
            raise EnvelopeRejected(
                "non_canonical_lane",
                "from_lane must be system or lane:/loop:/job:/human:<subject>",
                self.from_lane or "",
            )
        if not self.rationale.strip() or len(self.rationale) > MAX_RATIONALE_CHARS:
            raise EnvelopeRejected(
                "malformed_rationale",
                f"rationale must be non-empty and <= {MAX_RATIONALE_CHARS} chars",
            )
        if not isinstance(self.request, RequestPayload):
            raise EnvelopeRejected("impossible_union", "request is not one of the four variants")
        try:
            _parse_iso(self.requested_at)
        except ValueError as exc:
            raise EnvelopeRejected(
                "malformed_timestamp", "requested_at must be ISO 8601", str(self.requested_at)[:64]
            ) from exc
        self.request.validate()

    def as_row(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "from_agent_id": self.from_agent_id,
            "from_lane": self.from_lane,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "requested_at": self.requested_at,
            "rationale": self.rationale,
            "request_kind": self.request.kind,
            "request_json": json.dumps(self.request.as_dict(), separators=(",", ":"), sort_keys=True),
            "inherited_floor_json": (
                None
                if self.inherited_floor is None
                else json.dumps(
                    self.inherited_floor.as_dict(), separators=(",", ":"), sort_keys=True
                )
            ),
        }


#: The complete emitter-supplied field set. A field added here that the emitter
#: must NOT control (granted_by, action_class, expiry, a grant id) fails
#: ``test_the_envelope_has_no_field_an_emitter_may_not_supply``.
ENVELOPE_FIELDS = frozenset(f.name for f in fields(CapabilityRequest))


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------- the service


@dataclass(frozen=True, slots=True)
class ScopeExpansion:
    """What a ``key_scope`` group resolved to, server-side."""

    group: str
    granted: tuple[str, ...]
    excluded: tuple[str, ...]


class CapabilityRequestService:
    """The one server-side entry: authenticate, validate, persist, decide.

    Constructed per authenticated channel (loop AF_UNIX seam, scheduler/session
    service call, runner service call). Every transport normalises into the same
    envelope before any policy runs, so there is exactly one place where a
    request becomes a decision.
    """

    def __init__(
        self,
        store: Any,
        *,
        policy: CapabilityPolicy | None = None,
        clock: Clock | None = None,
        registry_loader: Callable[[], ConnectorRegistry] = load_registry,
        max_validation_events: int = 100,
    ) -> None:
        self._store = store
        self._clock = clock or SystemClock()
        self._policy = policy or CapabilityPolicy(clock=self._clock, registry_loader=registry_loader)
        self._registry_loader = registry_loader
        #: Bounded ring of pre-ledger refusals. The annex requires an observable
        #: reason for a request that never earns a row; a bounded in-process ring
        #: plus a log line gives one without a table an attacker can grow.
        self.validation_events: deque[dict[str, str]] = deque(maxlen=max_validation_events)

    # -- reads ---------------------------------------------------------------

    @property
    def _conn(self) -> sqlite3.Connection:
        connection: sqlite3.Connection = self._store._connection
        return connection

    def get(self, request_id: str) -> dict[str, Any] | None:
        """The stored envelope, or None."""
        row = self._conn.execute(
            "SELECT * FROM capability_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def expand_key_scope(self, group: str) -> ScopeExpansion:
        """Expand a group to its CALLABLE READ capability ids, server-side.

        Eligible means callable now, read-only, unmetered, and not in a danger
        group -- the subset of the floor that is a property of the capability
        rather than of the caller. Everything else in the group is EXCLUDED and
        named, so a mixed group produces an honest partial grant instead of a
        silent one.
        """
        registry = self._registry_loader()
        if group not in registry.groups:
            raise EnvelopeRejected("unknown_scope_group", "no such registry group", group)
        danger = registry.groups[group].danger
        granted: list[str] = []
        excluded: list[str] = []
        for capability in sorted(registry.capabilities.values(), key=lambda cap: cap.id):
            if capability.group != group:
                continue
            eligible = (
                capability.callable_now
                and capability.resolved_read_only
                and not self._policy.is_paid(capability)
                and not danger
            )
            (granted if eligible else excluded).append(capability.id)
        return ScopeExpansion(group=group, granted=tuple(granted), excluded=tuple(excluded))

    # -- the submission ------------------------------------------------------

    def submit(
        self,
        envelope: CapabilityRequest,
        *,
        channel_principal: str,
        project_id: str | None = None,
        in_approved_scope: bool = True,
        lane_allowed: bool = True,
    ) -> PolicyReceipt:
        """Validate, persist, and decide one request. Returns its receipt.

        ``channel_principal`` comes from the authenticated transport, never from
        the payload. An envelope claiming a different identity is refused before
        anything is written: forging attribution is the one thing a request
        ledger must not make possible.

        ``project_id`` (C11/D-31, migration 115) is the project the request is
        made FOR, and it arrives the same way ``channel_principal`` does — from
        the authenticated channel, not from the envelope. That is why it is not
        a field on :class:`CapabilityRequest`: a worker that could name the
        project on its own ask could ask for project A's send grant while
        working in project B, and the grant this request produces
        (``cas_grant`` copies the binding onto the row) is the thing the broker
        then enforces. ``None`` records no binding, which is honest and
        fail-closed — the resulting grant authorizes no project-scoped call.
        """
        try:
            envelope.validate()
            if not _CANONICAL_IDENTITY.fullmatch(channel_principal or ""):
                raise EnvelopeRejected(
                    "non_canonical_principal",
                    "the channel principal is not a canonical identity",
                    channel_principal or "",
                )
            if envelope.from_agent_id != channel_principal:
                raise EnvelopeRejected(
                    "identity_mismatch",
                    "the envelope claims an identity the channel did not authenticate",
                    envelope.from_agent_id,
                )
            subject = self._resolve_subject(envelope)
            # Idempotent: a replayed delivery re-reads its envelope rather than
            # writing a second one, and the decision below re-reads a terminal
            # decision rather than making a new one. A reused id carrying a
            # DIFFERENT ask is refused here, and inside this block so that the
            # smuggling attempt leaves the same bounded event every other
            # pre-ledger refusal does.
            self._persist(envelope, project_id=project_id)
        except EnvelopeRejected as rejected:
            self._record_validation_event(rejected)
            raise

        if envelope.request.kind in {"skill", "extra_agent"}:
            receipt = self._policy.park(
                self._store,
                envelope.request_id,
                holder_agent_id=envelope.from_agent_id,
                subject_kind=envelope.request.kind,
                subject_id=envelope.request.subject_id,
            )
        else:
            receipt = self._policy.decide(
                self._store,
                envelope.request_id,
                holder_agent_id=envelope.from_agent_id,
                subject_kind=envelope.request.kind,
                subject_id=envelope.request.subject_id,
                capability_ids=subject.granted,
                excluded_ids=subject.excluded,
                in_approved_scope=in_approved_scope,
                lane_allowed=lane_allowed,
            )
        if not receipt.terminal:
            # A parked request that has already outlived the wall (a replay, or a
            # caller that reconnected) is released here rather than parked twice.
            receipt = self._policy.enforce_caller_wall(self._store, envelope.request_id)
        return receipt

    def await_decision(self, request_id: str) -> PolicyReceipt:
        """Read the current decision, releasing the caller at the 90-second wall."""
        return self._policy.enforce_caller_wall(self._store, request_id)

    # -- internals -----------------------------------------------------------

    def _resolve_subject(self, envelope: CapabilityRequest) -> ScopeExpansion:
        """Turn the union into the capability ids the policy will decide on.

        Raises :class:`EnvelopeRejected` for anything UNADDRESSABLE -- a request
        about a system that does not exist. An unknown capability inside a known
        connector is addressable and gets a durable decision instead.
        """
        payload = envelope.request
        if isinstance(payload, ToolRequest):
            registry = self._registry_loader()
            connector = payload.tool_id.split(".", 1)[0]
            if connector not in registry.connectors:
                raise EnvelopeRejected(
                    "unaddressable_capability",
                    "no such connector namespace; there is no system to ask about",
                    payload.tool_id,
                )
            return ScopeExpansion(group="", granted=(payload.tool_id,), excluded=())
        if isinstance(payload, KeyScopeRequest):
            return self.expand_key_scope(payload.group)
        # skill / extra_agent carry no connector capability and park.
        return ScopeExpansion(group="", granted=(), excluded=())

    def _persist(
        self,
        envelope: CapabilityRequest,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Insert the envelope. Returns the EXISTING row on a replayed request id."""
        row = {**envelope.as_row(), "project_id": project_id}
        with self._store._lock:
            existing = self.get(envelope.request_id)
            if existing is not None:
                self._assert_replay_matches(existing, row)
                return existing
            try:
                self._conn.execute(
                    "INSERT INTO capability_requests "
                    "(request_id, from_agent_id, from_lane, session_id, run_id, task_id, "
                    "requested_at, rationale, request_kind, request_json, inherited_floor_json, "
                    "project_id) "
                    "VALUES (:request_id, :from_agent_id, :from_lane, :session_id, :run_id, "
                    ":task_id, :requested_at, :rationale, :request_kind, :request_json, "
                    ":inherited_floor_json, :project_id)",
                    row,
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                existing = self.get(envelope.request_id)
                if existing is None:
                    raise
                self._assert_replay_matches(existing, row)
                return existing
        return None

    @staticmethod
    def _assert_replay_matches(existing: dict[str, Any], row: dict[str, Any]) -> None:
        """A reused request id must carry the SAME ask, or it is not a replay.

        ``project_id`` is part of the ask (C11/D-31). Re-submitting a decided
        request id under a different project would otherwise read the FIRST
        envelope back and hand its receipt to work in a second project — a
        replay used as a project-crossing primitive.
        """
        for column in ("from_agent_id", "from_lane", "request_kind", "request_json", "project_id"):
            if str(existing.get(column) or "") != str(row.get(column) or ""):
                raise EnvelopeRejected(
                    "request_id_reuse",
                    "this request id already records a different ask",
                    str(row["request_id"]),
                )

    def _record_validation_event(self, rejected: EnvelopeRejected) -> None:
        event = rejected.as_event()
        self.validation_events.append(event)
        LOG.warning(
            "capability request rejected before the ledger: %s (%s)",
            event["reason_code"],
            event["subject"],
        )


__all__ = [
    "ENVELOPE_FIELDS",
    "FORMATIONS",
    "MAX_EXTRA_AGENTS",
    "MAX_OWNED_PATHS",
    "MAX_RATIONALE_CHARS",
    "CapabilityRequest",
    "CapabilityRequestService",
    "EnvelopeRejected",
    "ExecutionFloorHint",
    "ExtraAgentRequest",
    "KeyScopeRequest",
    "RequestPayload",
    "ScopeExpansion",
    "SkillRequest",
    "ToolRequest",
]
