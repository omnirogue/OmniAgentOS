"""Credential broker: the only module that touches secret material.

WHY THIS EXISTS -- the load-bearing design decision of the capability system.

The obvious way to give an agent connector access is to inject the connector's
environment variables into its subprocess: grant ``stripe_acmeuni.read``, inject
``ACMEUNI_STRIPE_PRIMARY_SECRET_KEY``. That design is worthless as a security
boundary, because a Stripe secret key that can read can also refund. The same is
true of the NMI security keys, the PayPal write client, the Vandelay login,
the Cloudflare token, and DATABASE_URL. Env injection cannot separate read from
write, so an agent holding a "read-only" grant would in fact hold the power to
move money, and the grant list would cheerfully report it as compliant.

So the broker is a proxy, not a vending machine:

    * Agents never receive credentials. Not for reads, not ever.
    * An agent names a capability; the broker resolves the credential, checks the
      grant, enforces an HTTP method+path allowlist derived from that capability,
      performs the call, and returns only the response body.
    * Read-only is enforced by the allowlist (GET on permitted prefixes), not by
      trusting the agent to only read.

TWO INDEPENDENT GATES protect consequential actions:

    1. policy.yaml pins ``consequential.always_human: true``.
    2. HARD_HUMAN_CLASSES below refuses them in code, with no config input.

Both must be removed by hand. A config edit alone cannot make an agent able to
issue a refund, launch a paid ad, drop a DNS record, or write to Postgres.
The intentional second authorization path is a live, bounded campaign grant:
it preserves the code gate while allowing pre-approved campaign actions without
reusing or weakening one-shot approval consumption.

Model completion remains a separately governed, budget-bounded path; connector
credentials are never exposed as an environment mapping by this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from omniagentos.connectors import Capability, ConnectorError, load_registry
from omniagentos.connectors.oauth import OAuthTokenError, mint_bearer
from omniagentos.connectors.secret_catalog import resolution_denial
from omniagentos.contracts import ActionClass, default_db_path, digest, new_id
from omniagentos.grants import normalize_grant_ref, validate_approval_token
from omniagentos.security.secret_storage import (
    PermissionViolation,
    StoragePermissionGuard,
    get_permission_guard,
    register_permission_guard,
)

# Refused in code, unconditionally, no matter what policy.yaml says. Removing an
# entry here is a deliberate, reviewable source change -- which is the point.
# IRREVERSIBLE is included (AC-policy): the ONE hard-stop class must be refused at
# Gate 2 too, so a money capability declared ``irreversible`` (not just
# ``consequential``) still cannot execute unattended.
HARD_HUMAN_CLASSES: frozenset[ActionClass] = frozenset(
    {ActionClass.CONSEQUENTIAL, ActionClass.IRREVERSIBLE}
)

# Money / bank groups (AC-policy). A connector in one of these groups may only be
# READ in auto mode: any MUTATING HTTP method is money-movement and hard-stops
# unless an explicit approval token is supplied. This is a GROUP-level guarantee
# layered on top of the per-capability action_class gate (HARD_HUMAN_CLASSES), so
# even a payment write that was somehow declared with a softer class still cannot
# execute unattended. Payment/bank READS (GET/HEAD) stay auto.
PAYMENT_GROUPS: frozenset[str] = frozenset({"payments"})
READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD", "OPTIONS"})

#: One next-action per DISTINGUISHABLE denial (U-R3). The point of the taxonomy
#: is that a caller can route on the code alone, so no two entries may describe
#: the same remedy:
#:
#:   * ``env_name_out_of_scope``    — the name belongs to another connector.
#:   * ``credential_missing``       — this ONE name is absent; siblings are set.
#:   * ``capability_unprovisioned`` — the connector has NO name set at all.
#:   * ``credential_unavailable``   — the name may well exist, but the secret
#:     STORE holding it failed the U-S1 permission guard in ``enforce`` mode.
#:     Deliberately separate from ``credential_missing``: "we refused to read
#:     it" and "it is not there" have different operators and different fixes,
#:     and collapsing them would let a permissions defect masquerade as a
#:     provisioning gap (which is how the guard's first rollout mis-reported a
#:     100% refusal rate as ``file_missing``).
#:   * ``audit_unavailable``        — nothing left this process. The durable
#:     intent could not be written, so no credential was read and no request
#:     was built. Retrying is safe.
#:   * ``audit_finalization_failed`` — the outbound request WAS issued and its
#:     terminal row could not be written. A blind retry may buy the same effect
#:     twice, so its remedy is reconciliation, never a retry (K1).
#:   * ``credential_quarantined`` — U-S2. The U-S2 catalog holds a row for this
#:     name in state ``quarantined``. The value may well be present and
#:     readable; we are declining to use a name that nothing in the repo
#:     references until a NAMED OWNER dispositions it. Reversible, and its
#:     metadata stays readable while it is refused. It cannot fold into
#:     ``credential_missing`` (which says "provision it" about a name the
#:     operator already has) or into ``credential_unavailable`` (whose remedy is
#:     a chmod, not a judgement call).
#:   * ``credential_revoked`` — U-S2. The catalog row is ``revoked``: the
#:     credential is dead at the provider. Terminal, and deliberately distinct
#:     from quarantine, because the one remedy quarantine permits — reinstate
#:     it — is the exact action a revoked credential must never receive. The
#:     same rule governs rollback: a failed canary never resurrects a revoked
#:     version. One code for both states would put "turn it back on" on the
#:     remedy path of a key that cannot come back.
#:   * ``grant_project_mismatch`` — C11/D-31. The durable grant is bound to a
#:     DIFFERENT project than the one this call names. The remedy is not to
#:     repair anything: it is to obtain a grant for the project the work
#:     belongs to, and to ask why this caller reached across a project
#:     boundary at all. This is the containment breach, and it gets its own
#:     code precisely so it is greppable in the audit spine.
#:   * ``grant_project_unbound`` — C11/D-31. Nothing durable binds this call to
#:     a project: either the standing row predates migration 115 (NULL
#:     project) or no store-backed grant record was presented at all. The
#:     remedy is a FLEET action — reissue/backfill the binding — and it is
#:     deliberately not ``grant_project_mismatch``, because "your grants
#:     predate project binding" and "this agent is reaching into another
#:     project" are different events with different responses, and collapsing
#:     them would let a real breakout hide inside a migration backlog.
#:   * ``call_project_unknown`` — C11/D-31. The mirror image: a PROJECT-BOUND
#:     grant, presented by a caller that did not say which project it is
#:     working in. Fixing this is a change to the CALLER (thread the project
#:     through), never to the grant, so it cannot share a next action with
#:     either code above.
_DENIAL_NEXT_ACTIONS: dict[str, str] = {
    "not_granted": "request via CapabilityRequest",
    "env_name_out_of_scope": "request the capability for the connector that owns this name",
    "credential_missing": "operator must provision this credential name",
    "capability_unprovisioned": "operator must provision this connector",
    "credential_unavailable": "operator must repair secret-store permissions for this credential",
    "credential_quarantined": (
        "a named owner must disposition this quarantined credential before it is used again"
    ),
    "credential_revoked": "rotate a new version; a revoked credential is never reinstated",
    "no_call_path": "no http path reviewed for this capability",
    "audit_unavailable": "retry after the local audit store recovers",
    "audit_finalization_failed": (
        "the call was already issued; reconcile the effect before retrying"
    ),
    # web.fetch-specific reason codes
    "invalid_url": "provide a valid http(s) URL with a hostname",
    "unsupported_scheme": "use http:// or https://; no file://, gopher://, etc.",
    "ssrf_refused": "target is a local/private/reserved host; choose a public URL",
    "host_resolution_failed": "hostname could not be resolved or resolved to a private IP",
    "method_not_allowed": "web.fetch permits GET or POST only",
    "request_headers_refused": "authorization and credential headers are not permitted",
    "transport_error": "network error or connection refused",
    "redirect_limit_exceeded": "redirect chain exceeds maximum (5 hops)",
    "grant_project_mismatch": (
        "request this capability for the project this work belongs to; "
        "a grant issued for another project never crosses over"
    ),
    "grant_project_unbound": (
        "reissue this grant bound to a project; an unbound grant cannot authorize "
        "project-scoped work"
    ),
    "call_project_unknown": (
        "identify the calling project; a project-bound grant never authorizes an "
        "unscoped call"
    ),
    "invalid_dry_run_flag": (
        "pass dry_run=True or dry_run=False; whether a call is a pre-flight or a "
        "live send is never inferred from a non-boolean value"
    ),
}

#: Human-readable, value-free detail for each catalog refusal.
_CATALOG_DENIAL_DETAIL: dict[str, str] = {
    "credential_quarantined": (
        "the secret catalog holds this name in quarantine; its metadata stays readable"
    ),
    "credential_revoked": "the secret catalog records this credential as revoked",
}

_LOG = logging.getLogger(__name__)
_CANONICAL_HOLDER = re.compile(r"^(?:lane|loop|job|human):[^\s:]+(?:\.[^\s:]+)*$|^system$")
_UNAVAILABLE_AUDIT_REASONS = frozenset(
    {"credential_missing", "credential_unavailable", "capability_unprovisioned"}
)


@dataclass(frozen=True, slots=True)
class AuditContext:
    """Metadata-only identity and correlation carried into broker audit rows."""

    holder: str = "system"
    run_id: str = ""
    task_id: str = ""
    session_id: str = ""
    request_id: str = ""
    grant_id: str = ""
    grant_issuer: str = ""
    grant_expires_at: str = ""
    budget_receipt_id: str = ""
    correlation_id: str = ""

    def normalized(self, *, grant_id: str = "") -> AuditContext:
        """Fill correlation identifiers and reject non-canonical holder spellings."""
        if not _CANONICAL_HOLDER.fullmatch(self.holder):
            raise BrokerDenied(
                "invalid_holder",
                "",
                "audit holder must use a canonical lane:/loop:/job:/human: spelling",
            )
        request_id = self.request_id or new_id("req")
        return replace(
            self,
            run_id=self.run_id or request_id,
            request_id=request_id,
            grant_id=self.grant_id or grant_id,
        )


def _danger_groups() -> frozenset[str]:
    """Connector groups the registry itself flags ``danger`` (payments/ads/infra).

    Sourced from the registry so the boundary tracks the catalogue: adding a new
    danger group in connectors.yaml automatically hard-stops its mutating calls.
    Fails closed to the payments floor if the registry cannot be read (authorize()
    would already have raised on a broken registry before this is reached)."""
    try:
        registry = load_registry()
        return frozenset(g for g, spec in registry.groups.items() if spec.danger)
    except Exception:  # noqa: BLE001 -- keep money protected even if the registry breaks.
        return PAYMENT_GROUPS


def is_read_only_query_post(cap: Capability, method: str, path: str) -> bool:
    """Whether a request is one of the two reviewed NMI query-only POSTs.

    NMI exposes transaction lookup only through ``query.php`` as a form POST.
    That route is semantically read-only, unlike NMI's ``transact.php`` endpoint.
    Keep this exception explicit and exact so it cannot become a general payment
    POST bypass.
    """
    return (
        cap.id in {"nmi_fortpoint.read", "nmi_glacier.read"}
        and method.upper() == "POST"
        and path == "/api/query.php"
    )


def is_money_write(cap: Capability, method: str, path: str = "/") -> bool:
    """True when this call MUTATES a payment/bank system (i.e. moves money).

    Frozen boundary: a payment/bank connector call is a money-move iff it targets
    a payments-group connector with a non-read HTTP method. Reads are auto.
    """
    return (
        cap.group in PAYMENT_GROUPS
        and method.upper() not in READ_METHODS
        and not is_read_only_query_post(cap, method, path)
    )


def is_dangerous_write(cap: Capability, method: str, path: str = "/") -> bool:
    """True when this call MUTATES any registry-declared DANGER group.

    Broader, future-proof superset of ``is_money_write`` (MEDIUM/fix4): a mutating
    (non-read) method on ANY danger group -- payments, ads, infra, or a future one
    -- hard-stops regardless of the capability's declared action_class, so a
    mis-declared money/ads/infra WRITE in a new group cannot slip past the
    per-group denylist. Payments keep their dedicated money-write message; this
    catches everything else.
    """
    return (
        cap.group in _danger_groups()
        and method.upper() not in READ_METHODS
        and not is_read_only_query_post(cap, method, path)
    )


class BrokerDenied(PermissionError):
    """A capability call was refused. Carries the reason for the audit event."""

    def __init__(self, reason: str, cap_id: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.cap_id = cap_id
        self.detail = detail
        self.next_action = _DENIAL_NEXT_ACTIONS.get(reason, "")

    def payload(self) -> dict[str, str]:
        """Return the machine-routable, secret-free denial envelope."""
        return {
            "capability_id": self.cap_id,
            "reason_code": self.reason,
            "next_action": self.next_action,
        }


def _request_schema_digest(body: Any, form: Mapping[str, Any] | None) -> str:
    """Hash request shape only; values and payload text never enter audit metadata."""

    def shape(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {str(key): shape(item) for key, item in sorted(value.items(), key=str)}
        if isinstance(value, (list, tuple)):
            return [shape(item) for item in value[:16]]
        return type(value).__name__

    shaped = {"body": shape(body), "form": shape(form)}
    return digest(repr(shaped))


def _audit_capability(cap_id: str) -> Capability | None:
    """Best-effort registry hint used before authorization, never as authority."""
    try:
        return load_registry().capability(cap_id)
    except Exception:  # noqa: BLE001 -- authorize() emits the typed denial.
        return None


def _is_credentialed(capability: Capability | None) -> bool:
    return bool(capability and capability.http and capability.http.auth)


def _is_inprocess(capability: Capability | None) -> bool:
    """Whether a reviewed capability is dispatched locally instead of over a socket."""
    return bool(
        capability
        and capability.http
        and capability.http.base_url == "inprocess://echo"
        and capability.id == "echo.ping"
    )


def audits_own_calls(cap_id: str) -> bool:
    """Whether :func:`call` writes its own U-A1 intent/terminal rows for ``cap_id``.

    U-R10's argument — a caller attesting to its own call is self-vouching —
    applies to routes as well as to the loop seam, so the broker owns the record
    wherever it can. But it can only own the record where it audits: a
    credential-free capability (and an unknown id) takes the unlogged path and
    writes nothing, and a caller that stopped logging those would delete them
    from the "what did my agents do overnight" view entirely.

    So the dedup rule is one question, asked by the caller BEFORE the call and
    answered here rather than guessed: the broker records the calls it audits,
    and the caller records only the ones it structurally cannot.
    """
    capability = _audit_capability(cap_id)
    return _is_credentialed(capability) or _is_inprocess(capability)


@contextmanager
def _audit_store_scope(audit_store: Any) -> Any:
    """Yield an explicit store or a short-lived handle to the control-plane DB."""
    if audit_store is not None:
        yield audit_store
        return

    from omniagentos.connectors.store import CapabilityStore
    from omniagentos.db.store import SqliteStore

    store = SqliteStore(default_db_path())
    try:
        yield CapabilityStore(store)
    finally:
        store.close()


def _audit_row(
    audit_store: Any,
    context: AuditContext,
    capability: Capability,
    *,
    call_id: str,
    method: str,
    decision: str,
    reason_code: str = "",
    latency_ms: int = 0,
    status: int | None = None,
    request_schema_hash: str = "",
    agent_id: str = "",
    path: str | None = None,
    target_host: str | None = None,
) -> None:
    """Append one row through the unified metadata-only store contract.

    ``holder`` is the canonical lane identity that the grant belongs to;
    ``agent_id`` is the concrete actor when the caller knows one (an API run's
    ``agt_*``). They are two columns because they answer two questions, and
    filling both from ``holder`` made the spine say ``system`` on exactly the
    calls where a real, authenticated agent was identified (K5). When no actor
    is supplied, the holder is still the most specific thing known.

    ``path`` and ``target_host`` default to the registry's own facts, which is
    right for every capability whose host is pinned by ``base_url``. A
    capability that chooses its TARGET per call (``web.fetch``) must override
    both, or the spine cannot answer "what did this agent actually reach?" —
    the one question a network-facing capability exists to be asked. Only the
    host and the path are recorded; query values, headers and bodies stay out
    of the row exactly as they do for every other capability.
    """
    spec = capability.http
    if target_host is None:
        target_host = urlsplit(spec.base_url).hostname or "" if spec else ""
    env_names = ",".join(_declared_env_names(capability))
    action_mode = "read" if capability.resolved_read_only else "write"
    status_class = f"{status // 100}xx" if status is not None and status >= 100 else ""
    response_size = capability.resolved_result_size_class.value
    audit_store.log_call(
        context.run_id,
        agent_id or context.holder,
        capability.id,
        method=method.upper(),
        path=capability.id if path is None else path,
        allowed=decision == "allowed",
        reason=reason_code,
        status=status,
        call_id=call_id,
        request_id=context.request_id,
        task_id=context.task_id,
        session_id=context.session_id,
        holder=context.holder,
        action_mode=action_mode,
        connector=capability.connector,
        env_name=env_names,
        credential_id=capability.resolved_credential_scope,
        grant_id=context.grant_id,
        grant_issuer=context.grant_issuer,
        grant_expires_at=context.grant_expires_at,
        target_host=target_host,
        request_schema_hash=request_schema_hash,
        decision=decision,
        reason_code=reason_code,
        upstream_status_class=status_class,
        latency_ms=latency_ms,
        response_size_class=response_size,
        budget_receipt_id=context.budget_receipt_id,
        correlation_id=context.correlation_id,
    )


def _emit_audit_health(call_id: str, capability_id: str) -> None:
    """Emit a local, value-free health signal when the audit sink is unavailable."""
    _LOG.error(
        "broker_audit_unavailable call_id=%s capability_id=%s",
        call_id,
        capability_id,
    )


def _terminal_decision(exc: BaseException) -> tuple[str, str]:
    if not isinstance(exc, BrokerDenied):
        return "unknown", "internal_error"
    if exc.reason == "transport_error":
        return "failed", exc.reason
    if exc.reason in _UNAVAILABLE_AUDIT_REASONS:
        return "unavailable", exc.reason
    return "denied", exc.reason


def _reached_provider(exc: BaseException) -> bool:
    """Whether the failure that ended ``_call_unlogged`` may have left this process.

    Every ``BrokerDenied`` except ``transport_error`` is decided locally BEFORE
    the outbound request is built — unknown capability, not granted, no reviewed
    call path, the money/danger gates, a missing credential name. Those provably
    issued nothing. A transport error, or any unclassified exception (which can
    just as easily come from parsing a response as from building a request), may
    have reached the provider and been billed.
    """
    return not (isinstance(exc, BrokerDenied) and exc.reason != "transport_error")


def _audit_failure_reason(reached: bool) -> str:
    """Name the audit-write failure by whether a provider may already have billed.

    U-A1 raised one ``audit_unavailable`` at every position, and the loop seam
    reads that code as "decided locally, provably unbilled" — correct for the
    intent write (no credential has been touched yet) and WRONG for the
    finalization write, which runs after a 201 whose prediction the provider is
    already rendering. Classifying that as unbilled releases the loop's budget
    reservation and licenses the next tick to buy the same effect again, which
    is precisely the defect class ``_after_billable_work`` exists to kill one
    layer up. So the position gets its own reason and the seam fails closed on
    it (K1).
    """
    return "audit_finalization_failed" if reached else "audit_unavailable"


def effective_grant(agent_caps: list[str], task_caps: list[str] | None) -> list[str]:
    """Intersect an agent's standing grant with a task's requested grant.

    The AGENT grant is the ceiling. A task may narrow it -- request fewer
    capabilities than the agent holds -- but may never widen it. This mirrors the
    existing task/step rule in runner/core.py (a step narrows a task, never
    self-grants) and extends it one level up, so the chain is:

        agent grant  >=  task grant  >=  step grant

    A task requesting a capability its agent does not hold gets silently narrowed
    rather than erroring, so a broadly-scoped task template can be reused by a
    narrowly-scoped agent without blowing up. The denial surfaces at call time,
    where it is audited against a specific capability.
    """
    held = set(agent_caps)
    if task_caps is None:
        return sorted(held)
    return sorted(held & set(task_caps))


def _tool_from_cap(cap_id: str) -> str:
    _connector, sep, tool = cap_id.partition(".")
    return tool if sep else ""


def _scoped_args_from_request(
    body: Any,
    form: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Derive scoped-arg surface from the actual outbound request.

    Grant pins are checked against this mapping, never against a caller-supplied
    ``scoped_args`` claim that could diverge from ``body``/``form``. Body keys
    win over form keys when both are present.
    """
    merged: dict[str, Any] = {}
    if isinstance(form, Mapping):
        merged.update(dict(form))
    if isinstance(body, Mapping):
        merged.update(dict(body))
    return merged if merged else None


# Budget keys the Meta extractors treat as spend (see extractors._meta_budget_spend_usd).
# Presence of either means the call claims a budget amount; if extraction could not
# price it, spend is unknown — never free.
_BUDGET_AMOUNT_KEYS: frozenset[str] = frozenset({"daily_budget", "lifetime_budget"})


def _request_claims_budget_amount(
    body: Any,
    form: dict[str, Any] | None,
) -> bool:
    """True when the request carries a budget amount field (JSON body or form).

    Presence alone matters: a null / non-numeric / negative value still *claims*
    a budget, and the extractor reports spend_usd=None (unknown). That is not
    the same as a bid-only or non-spend call with no budget key at all.
    """
    for payload in (body, form):
        if isinstance(payload, Mapping) and _BUDGET_AMOUNT_KEYS.intersection(payload):
            return True
    return False


# --------------------------------------------------------------------------
# C11 / D-31 — project binding.
#
# A grant answers "who may do this". D-31 requires it to answer "who may do
# this, FOR WHICH PROJECT", because a fleet that shares one agent identity
# across projects otherwise shares its reach too: a send grant issued for
# project A is, without this, a send grant for every project at once.
#
# The binding is read ONLY from durable store rows — `agent_capabilities`
# (migration 115) and `campaign_grants` (migration 058). A caller states the
# project it is working IN; it can never state the project a grant is bound TO.
# That asymmetry is the whole mechanism: if the caller could assert both sides
# of the comparison, the comparison would be theatre.
# --------------------------------------------------------------------------

#: Which durable record produced a binding. Carried into the denial detail so
#: an operator reading the audit spine knows WHICH row to fix.
_STANDING_GRANT_SOURCE = "standing"
_CAMPAIGN_GRANT_SOURCE = "campaign"


def _row_project(row: Any) -> str | None:
    """The project a durable grant row is bound to, or ``None`` for unbound.

    Blank is NULL. A grant row carrying ``project_id = ''`` records the absence
    of a binding just as much as a NULL does, and treating the empty string as a
    project id would make it match a caller that also passed ``''`` — an
    authorization created out of two pieces of missing information.
    """
    if not isinstance(row, Mapping):
        return None
    value = row.get("project_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _standing_project(grant_store: Any, holder: str, cap_id: str, *, required: bool) -> str | None:
    """Project binding on the standing ``agent_capabilities`` row for a holder.

    ``required`` is True once the CALL has named a project: from that point the
    store must be able to answer, and a store that cannot is a fail-closed
    ``grant_store_unavailable`` rather than an assumed-unbound pass. When no
    project is named there is nothing to prove, so a store without the reader
    (a narrow test double, a legacy shim) is left alone instead of being turned
    into a denial for every existing caller.
    """
    reader = getattr(grant_store, "get_grant_row", None) if grant_store is not None else None
    if reader is None:
        if required:
            raise BrokerDenied(
                "grant_store_unavailable",
                cap_id,
                "grant store cannot answer the project-binding question for this holder",
            )
        return None
    try:
        row = reader(holder, cap_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable binding must fail closed.
        raise BrokerDenied(
            "grant_store_unavailable",
            cap_id,
            "grant store failed while loading the standing project binding",
        ) from exc
    return _row_project(row)


def _campaign_project(grant_store: Any, grant_id: str, cap_id: str) -> str | None:
    """Project binding on the bounded ``campaign_grants`` row, if one is readable."""
    reader = getattr(grant_store, "get_grant", None)
    if reader is None:
        return None
    try:
        return _row_project(reader(grant_id))
    except Exception as exc:  # noqa: BLE001 -- an unreadable binding must fail closed.
        raise BrokerDenied(
            "grant_store_unavailable",
            cap_id,
            f"grant store failed while loading the project binding of {grant_id!r}",
        ) from exc


def _enforce_project_binding(
    cap_id: str,
    *,
    project_id: str | None,
    bindings: list[tuple[str, str | None]],
) -> None:
    """Refuse any call whose project is not the one its grants were issued for.

    Three refusals, three different people fix three different things (U-R3):

    * ``grant_project_mismatch`` — the grant belongs to another project. The
      containment breach. Nothing here is broken; a boundary was crossed.
    * ``grant_project_unbound`` — no durable record binds this call to a
      project (a pre-115 row, or no store-backed grant at all). The grant is
      what needs fixing.
    * ``call_project_unknown`` — a project-BOUND grant presented by a caller
      that did not say where it is working. The caller is what needs fixing.

    The unscoped-caller case is a denial and not a pass on purpose: "I did not
    say which project I am in" cannot be allowed to mean "therefore any of
    them". A call that names no project and holds no bound grant is untouched,
    which is what keeps this landing ahead of the D-31 switch instead of
    flag-daying every existing caller.
    """
    if project_id is None:
        bound = [(name, bound_to) for name, bound_to in bindings if bound_to is not None]
        if bound:
            bound_source, bound_to = bound[0]
            raise BrokerDenied(
                "call_project_unknown",
                cap_id,
                f"the {bound_source} grant is bound to project {bound_to!r} and this call "
                "names no project; an unscoped call cannot prove it belongs to it",
            )
        return

    if not bindings:
        raise BrokerDenied(
            "grant_project_unbound",
            cap_id,
            f"no durable grant record binds {cap_id} to project {project_id!r}; "
            "a project-scoped call needs a store-backed, project-bound grant",
        )

    for source, project in bindings:
        if project is None:
            raise BrokerDenied(
                "grant_project_unbound",
                cap_id,
                f"the {source} grant for {cap_id} carries no project binding, so it "
                f"cannot authorize work in project {project_id!r}",
            )
        if project != project_id:
            raise BrokerDenied(
                "grant_project_mismatch",
                cap_id,
                f"the {source} grant for {cap_id} is bound to project {project!r}; "
                f"this call belongs to project {project_id!r}",
            )


def authorize(
    cap_id: str,
    granted: list[str],
    *,
    approval_token: str | None = None,
    grant_id: str | None = None,
    grant_store: Any = None,
    agent_id: str | None = None,
    method: str = "GET",
    path: str = "/",
    grant_checker: Callable[..., bool] | None = None,
    target: str | None = None,
    generation: int | None = None,
    scoped_args: dict[str, Any] | None = None,
    project_id: str | None = None,
    grant_holder: str | None = None,
) -> Capability:
    """Decide whether one capability call may proceed. Fails closed.

    Raises BrokerDenied on any of: unknown capability, capability not in the
    grant, a hard-human action without a *store-backed* durable approval token
    or campaign grant, a call whose project is not the one its grants were
    issued for (C11/D-31), or a capability whose call path has not been
    reviewed.

    ``approval_token`` is never accepted on truthiness alone (H-02 / F-11). It
    must validate against ``grant_store`` for existence, generation, expiry,
    revocation, action class, connector/tool, target set, and scoped arguments.

    ``grant_row`` is intentionally not accepted: a caller-supplied dict is not a
    database fact. Identified agent calls load their lifecycle row only via
    ``grant_store.get_grant_row``; durable approval checks load their own grant
    records via ``grant_store.get_grant``.
    ``grant_checker`` remains as a compatibility-only keyword but can never
    authorize a hard-human action: a boolean callback cannot prove the complete
    durable scope or participate in atomic consumption/replay protection.

    ``project_id`` is the project the CALL belongs to — a statement about the
    caller, never about the grant. The project a grant is bound to is read from
    the durable row and nowhere else, so naming a project can only ever narrow
    what is permitted.

    ``grant_holder`` is the standing-grant subject when no ``agent_id`` was
    supplied. It is used for ONE thing: locating the ``agent_capabilities`` row
    whose project binding is checked. It deliberately does NOT enable the
    lifecycle (mode / expiry) checks above — see ``authorize_with_grant`` for
    why deriving ``agent_id`` from the holder would impose a read-only ceiling
    nobody chose.
    """
    registry = load_registry()

    try:
        cap = registry.capability(cap_id)
    except ConnectorError as exc:
        raise BrokerDenied("unknown_capability", cap_id, str(exc)) from exc

    if cap_id not in set(granted):
        raise BrokerDenied(
            "not_granted",
            cap_id,
            f"agent holds {len(granted)} capabilities; {cap_id!r} is not among them",
        )

    # The caller's list is only the membership snapshot.  When an identified
    # agent call reaches the broker, fetch the authoritative current row to
    # enforce lifecycle scope at call time (not merely at grant issuance).
    grant_row: dict[str, Any] | None = None
    if agent_id is not None:
        if grant_store is None:
            raise BrokerDenied("grant_store_unavailable", cap_id, "capability grant store is required")
        try:
            grant_row = grant_store.get_grant_row(agent_id, cap_id)
        except Exception as exc:  # noqa: BLE001 -- a failed lifecycle lookup must fail closed.
            raise BrokerDenied(
                "grant_store_unavailable",
                cap_id,
                "capability grant store failed while loading lifecycle scope",
            ) from exc
        if grant_row is None:
            raise BrokerDenied("not_granted", cap_id, "capability grant row no longer exists")

        raw_expiry = grant_row.get("expires_at")
        if raw_expiry:
            try:
                expires_at = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
                expired = expires_at.tzinfo is None or expires_at <= datetime.now(tz=UTC)
            except ValueError:
                expired = True
            if expired:
                raise BrokerDenied("grant_expired", cap_id, f"grant expired at {raw_expiry}")

        mode = str(grant_row.get("mode") or "read")
        if mode == "read" and (
            is_money_write(cap, method, path) or is_dangerous_write(cap, method, path)
        ):
            raise BrokerDenied(
                "mode_denied",
                cap_id,
                "grant is read-only; capability requires write",
            )

    # C11 / D-31. Containment is checked BEFORE the approval gates below: a
    # call reaching into another project is refused on that fact alone, and
    # never gets as far as presenting an approval token for work it has no
    # business doing. The bindings come from durable rows only.
    bindings: list[tuple[str, str | None]] = []
    if grant_row is not None:
        bindings.append((_STANDING_GRANT_SOURCE, _row_project(grant_row)))
    elif grant_holder is not None:
        holder_project = _standing_project(
            grant_store, grant_holder, cap_id, required=project_id is not None
        )
        bindings.append((_STANDING_GRANT_SOURCE, holder_project))
    if grant_id is not None and grant_store is not None:
        bindings.append((_CAMPAIGN_GRANT_SOURCE, _campaign_project(grant_store, grant_id, cap_id)))
    _enforce_project_binding(cap_id, project_id=project_id, bindings=bindings)

    if cap.action_class in HARD_HUMAN_CLASSES:
        durable_ok, durable_reason = _durable_hard_human_ok(
            cap,
            approval_token=approval_token,
            grant_id=grant_id,
            grant_store=grant_store,
            target=target,
            generation=generation,
            scoped_args=scoped_args,
        )
        if not durable_ok:
            # Gate 2. Reached even if policy.yaml has been edited to say otherwise.
            # Prefer a precise invalid-token reason when a token was presented.
            ref, norm_reason = normalize_grant_ref(approval_token, grant_id)
            if ref is not None or norm_reason or approval_token is not None or grant_id is not None:
                raise BrokerDenied(
                    "invalid_approval_token",
                    cap_id,
                    norm_reason
                    or durable_reason
                    or "approval token failed durable grant-store validation",
                )
            raise BrokerDenied(
                "requires_human_approval",
                cap_id,
                f"{cap_id} is {cap.action_class.value}; it can never run unattended",
            )

    if not cap.callable_now:
        # Declared in the catalogue, but no reviewed HTTP path yet. Fail closed:
        # an unreviewed call path is not a safe one.
        raise BrokerDenied(
            "no_call_path",
            cap_id,
            f"{cap_id} is declared but has no reviewed http spec; refusing",
        )

    return cap


def _durable_hard_human_ok(
    cap: Capability,
    *,
    approval_token: str | None,
    grant_id: str | None,
    grant_store: Any,
    target: str | None,
    generation: int | None,
    scoped_args: dict[str, Any] | None,
) -> tuple[bool, str]:
    """Return (ok, reason). True only when the complete durable store approves."""
    ref, reason = normalize_grant_ref(approval_token, grant_id)
    if reason:
        return False, reason
    if ref is None:
        return False, "token_missing"

    if grant_store is not None:
        result = validate_approval_token(
            ref,
            grant_store=grant_store,
            capability=cap.id,
            action_class=cap.action_class.value,
            connector=cap.connector,
            tool=_tool_from_cap(cap.id),
            target=target,
            generation=generation,
            scoped_args=scoped_args,
        )
        return result.ok, result.reason

    # No callback may stand in for the full store contract. The actual execution
    # path goes through authorize_with_grant(), whose try_consume() transaction
    # re-validates and consumes the bounded grant exactly once.
    return False, "grant_store_unavailable"


def _require_dry_run_flag(value: Any, cap_id: str) -> bool:
    """Return *value* as a strict bool, refusing anything else.

    A pre-flight flag that is silently misread is a real send. Only the literal
    booleans are accepted: every other value (``None``, ``0``, ``"false"``, a
    sentinel object) refuses BEFORE authorization, secret resolution, grant
    consumption, or dispatch, rather than guessing which way the caller meant.
    Guessing has exactly one dangerous direction -- a caller that asked for a
    pre-flight and got a live money write -- so this fails closed on both.
    """
    if value is True:
        return True
    if value is False:
        return False
    raise BrokerDenied(
        "invalid_dry_run_flag",
        cap_id,
        "dry_run must be a bool; refusing rather than guessing whether this "
        "call is a pre-flight or a live send",
    )


def _validate_grant_without_consuming(
    cap: Capability,
    cap_id: str,
    grant_store: Any,
    grant_id: str,
    *,
    target: str | None,
    spend_usd: float,
    generation: int | None,
    scoped_args: dict[str, Any] | None,
) -> None:
    """Run :func:`try_consume`'s scope check as a pure read, consuming nothing.

    ``GrantsStore.try_consume`` validates the durable grant with
    ``validate_approval_token`` (generation / target set / scoped-arg pins /
    live-ness / spend headroom) and only then mutates ``actions_used`` and
    ``spend_used_usd``. The authorize-only pre-flight runs that same validator
    against the same store and maps its refusals to the same
    ``grant_broke_out`` / ``grant_refused`` denials, so a dry run cannot certify
    a call the real path would refuse at consume time.

    Two honest limits, stated rather than hidden: this is point-in-time, not a
    reservation (headroom can be spent by another caller a millisecond later),
    and any failure to *reach* a verdict -- an unimportable validator, a
    malformed result object, a store that answers nothing -- refuses. Absence of
    an answer is never read as permission.
    """
    try:
        from omniagentos.grants.validation import validate_approval_token

        validation = validate_approval_token(
            grant_id,
            grant_store=grant_store,
            capability=cap_id,
            action_class=cap.action_class.value,
            connector=cap.connector,
            tool=_tool_from_cap(cap_id),
            target=target,
            generation=generation,
            scoped_args=scoped_args,
            spend_usd=spend_usd,
        )
    except Exception as exc:  # noqa: BLE001 -- an unreachable verdict denies.
        raise BrokerDenied(
            "grant_refused",
            cap_id,
            "dry-run grant validation could not be completed",
        ) from exc
    if getattr(validation, "ok", False) is not True:
        reason = str(getattr(validation, "reason", "") or "grant_validation_unavailable")
        if reason in {"target_breakout", "target_ambiguous"}:
            raise BrokerDenied("grant_broke_out", cap_id, reason)
        raise BrokerDenied("grant_refused", cap_id, reason)


def authorize_with_grant(
    cap_id: str,
    granted: list[str] | None,
    grant_store: Any,
    grant_id: str | None = None,
    *,
    grant_holder: str | None = None,
    target: str | None = None,
    spend_usd: float = 0,
    generation: int | None = None,
    scoped_args: dict[str, Any] | None = None,
    agent_id: str | None = None,
    method: str = "GET",
    path: str = "/",
    project_id: str | None = None,
    consume: bool = True,
) -> Capability:
    """Authorize from a store-backed standing or bounded campaign grant.

    ``grant_holder`` selects the standing-capability path. It loads the complete
    capability list from ``grant_store`` and rejects any caller-supplied
    ``granted`` list, so the capability cannot vouch for itself.

    ``grant_id`` selects the existing bounded campaign-grant path. That path
    always loads and atomically consumes the durable grant; it never trusts a
    caller-supplied dict as proof of a live campaign grant.

    ``consume=False`` is the authorize-only pre-flight form (see ``dry_run`` on
    :func:`call`): the durable grant is loaded and validated against THIS
    request exactly as a real call validates it, and nothing is consumed. Only
    the literal ``False`` selects it -- any other value consumes, because the
    dangerous direction of a misread flag here is a dispatched call whose
    bounded grant was never spent.

    ``agent_id`` is U-R6's lifecycle handle and is threaded through BOTH paths:
    when a caller names one, the current ``agent_capabilities`` row is re-read
    at call time and its mode/expiry enforced. It is deliberately NOT derived
    from ``grant_holder``: holder identity proves membership, not lifecycle
    scope, and inferring one from the other would apply an unasked-for read-only
    ceiling to every standing grant.

    Concretely, migration 106 defaults a grant row's ``mode`` to ``'read'`` and
    the lifecycle check blocks a read-mode grant from performing an
    ``is_money_write`` or ``is_dangerous_write`` call. So deriving ``agent_id``
    from ``grant_holder`` would be harmless today — the one live loop capability
    is ``replicate.generate``, group ``ai``, ``danger: false``, and its POST is
    neither — and would become a hard stop the day a loop is granted a write in
    a danger group (payments/ads/infra), for a ceiling nobody chose. That is the
    trap, and it is recorded in RESIDUAL-RISKS.md rather than only here.

    ``project_id`` (C11/D-31) is the project the CALL belongs to, and it is
    threaded through BOTH paths — the standing row and the campaign row are each
    checked against it by :func:`authorize`. The holder path additionally passes
    ``grant_holder`` down, because that is the only handle that locates the
    standing row when no ``agent_id`` was named: without it, a holder-backed
    call would silently skip the one check this whole change exists for.
    """
    if grant_store is None:
        raise BrokerDenied(
            "grant_store_unavailable",
            cap_id,
            "database grant store is required for broker authorization",
        )

    if grant_holder is not None:
        if granted is not None:
            raise BrokerDenied(
                "caller_supplied_grant",
                cap_id,
                "grant-backed callers may not supply their own capability list",
            )
        if grant_id is not None:
            raise BrokerDenied(
                "invalid_grant_reference",
                cap_id,
                "standing and campaign grant references cannot be combined",
            )
        try:
            stored_grant = grant_store.get_grant(grant_holder)
        except Exception as exc:  # noqa: BLE001
            raise BrokerDenied(
                "grant_store_unavailable",
                cap_id,
                f"grant store failed while loading holder {grant_holder!r}",
            ) from exc
        if not isinstance(stored_grant, list):
            raise BrokerDenied(
                "grant_store_unavailable",
                cap_id,
                "standing grant store returned an invalid capability list",
            )
        return authorize(
            cap_id,
            stored_grant,
            grant_store=grant_store,
            agent_id=agent_id,
            method=method,
            path=path,
            project_id=project_id,
            grant_holder=grant_holder,
        )

    if grant_id is None or granted is None:
        raise BrokerDenied(
            "invalid_grant_reference",
            cap_id,
            "campaign authorization requires a grant id and capability ceiling",
        )
    try:
        grant = grant_store.get_grant(grant_id)
    except Exception as exc:  # noqa: BLE001
        raise BrokerDenied(
            "grant_store_unavailable",
            cap_id,
            f"grant store failed while loading {grant_id!r}",
        ) from exc
    if grant is None:
        raise BrokerDenied("grant_not_found", cap_id, f"campaign grant {grant_id!r} not found")
    cap = authorize(
        cap_id,
        granted,
        grant_id=grant_id,
        grant_store=grant_store,
        agent_id=agent_id,
        method=method,
        path=path,
        target=target,
        generation=generation,
        scoped_args=scoped_args,
        project_id=project_id,
    )
    if consume is False:
        _validate_grant_without_consuming(
            cap,
            cap_id,
            grant_store,
            grant_id,
            target=target,
            spend_usd=spend_usd,
            generation=generation,
            scoped_args=scoped_args,
        )
        return cap
    result = grant_store.try_consume(
        grant_id,
        target=target,
        spend_usd=spend_usd,
        capability=cap_id,
        generation=generation,
        scoped_args=scoped_args,
        action_class=cap.action_class.value,
        connector=cap.connector,
        tool=_tool_from_cap(cap_id),
    )
    if not result.ok:
        if result.outcome == "broke_out":
            raise BrokerDenied("grant_broke_out", cap_id, result.reason)
        raise BrokerDenied("grant_refused", cap_id, result.reason)
    return cap


def validate_request(cap: Capability, method: str, path: str) -> None:
    """Enforce the capability's HTTP allowlist.

    This is what actually makes a read-only grant read-only. The credential behind
    it is fully privileged; the allowlist is the boundary.
    """
    spec = cap.http
    if spec is None:  # pragma: no cover -- authorize() already refused this.
        raise BrokerDenied("no_call_path", cap.id)

    if method.upper() not in {m.upper() for m in spec.methods}:
        raise BrokerDenied(
            "method_not_allowed",
            cap.id,
            f"{method.upper()} not permitted by {cap.id} (allows {', '.join(spec.methods)})",
        )

    # Defense-in-depth on the credential boundary: reject dot-segment traversal
    # BEFORE the prefix/regex allowlist. Prefix matching is startswith-based, so a
    # crafted '/v1/charges/../../admin' would satisfy an allowed prefix here yet
    # httpx normalizes it on the wire to a different, unvalidated path. No real API
    # route contains a '.' or '..' path segment, so rejecting them costs nothing and
    # closes the validate-here / normalize-on-send gap for every connector at once.
    if any(seg in {"..", "."} for seg in path.split("/")):
        raise BrokerDenied(
            "path_not_allowed",
            cap.id,
            f"{path!r} contains a dot-segment; traversal is refused at the broker boundary",
        )

    if spec.path_prefixes and not any(path.startswith(p) for p in spec.path_prefixes):
        raise BrokerDenied(
            "path_not_allowed",
            cap.id,
            f"{path!r} is outside the paths permitted by {cap.id}",
        )

    # A prefix cannot pin a suffix when the path has a variable id in the middle:
    # '/contacts/' + POST would admit /contacts/{id}/tasks as readily as
    # /contacts/{id}/notes. Write capabilities therefore carry a full-match regex,
    # and it is checked in ADDITION to the prefix -- never instead of it.
    if spec.path_regex and not re.fullmatch(spec.path_regex, path):
        raise BrokerDenied(
            "path_not_allowed",
            cap.id,
            f"{path!r} does not match the exact subresource {cap.id} is scoped to",
        )


#: Three-rung rollout control for the secret-store permission guard.
#:
#: ``off``     — the guard never runs.
#: ``shadow``  — the guard runs and LOGS what it would have refused, but every
#:               resolution proceeds. This is the default: the guard's store
#:               mapping is still a U-S3 placeholder, and a mapping mistake in
#:               enforce mode refuses every outbound call on the box.
#: ``enforce`` — a violation becomes ``credential_unavailable``.
STORE_PERMISSION_GUARD_ENV = "OMNIAGENTOS_STORE_PERMISSION_GUARD"
_STORE_PERMISSION_GUARD_MODES = ("off", "shadow", "enforce")
_DEFAULT_STORE_PERMISSION_GUARD_MODE = "shadow"

#: The one local secret store this process can name until U-S3 lands catalog
#: resolution.
_LOCAL_SECRET_STORE = "~/.config/omni/connections.env"


def _store_permission_guard_mode() -> str:
    """Resolve the guard's rung, read at call time so a preset always wins.

    Module-internal on purpose: the OPERATOR surface for the rung is the
    :data:`STORE_PERMISSION_GUARD_ENV` preset, and the only readers of the
    resolved value are the two arming/enforcement sites below. A public reader
    would read as a wired capability while having no caller outside this file.
    """
    raw = (os.environ.get(STORE_PERMISSION_GUARD_ENV) or "").strip().lower()
    if raw in _STORE_PERMISSION_GUARD_MODES:
        return raw
    if raw:
        _LOG.warning(
            "%s=%r is not one of %s; using %r",
            STORE_PERMISSION_GUARD_ENV,
            raw,
            _STORE_PERMISSION_GUARD_MODES,
            _DEFAULT_STORE_PERMISSION_GUARD_MODE,
        )
    return _DEFAULT_STORE_PERMISSION_GUARD_MODE


def _get_store_path_for_env(env_name: str) -> str | None:
    """Return the local store backing this credential, or ``None`` if unmapped.

    U-S3 will replace this single-store placeholder with catalog resolution.
    Until then the only store this process can name is the shared connections
    file — and it can only be named when it is actually there. Claiming it for
    every credential on a machine that has no such file made the guard refuse
    100% of outbound calls with ``file_missing``, which is a mapping failure
    reported as a security violation.

    ``lexists`` and not ``exists``: a symlink that dangles outside the store is
    exactly the case the guard's ``symlink_escape`` check exists for, so a
    broken link must still map and still be judged.
    """
    del env_name
    candidate = os.path.expanduser(_LOCAL_SECRET_STORE)
    return candidate if os.path.lexists(candidate) else None


def _arm_permission_guard() -> StoragePermissionGuard | None:
    """The guard to consult for this resolution, or ``None`` when disabled.

    U-S1 registered the guard only from its own tests, so production resolution
    never consulted it at all. Arming happens here, from the credential path, so
    the rung is re-read on every call and an explicitly registered guard (an
    operator override, or a test double) still wins.
    """
    if _store_permission_guard_mode() == "off":
        return None
    guard = get_permission_guard()
    if guard is None:
        guard = StoragePermissionGuard()
        register_permission_guard(guard)
    return guard


def _declared_env_names(capability: Capability) -> tuple[str, ...]:
    """Return the exact credential-name scope declared by ``capability``'s connector."""
    registry = load_registry()
    connector = registry.connectors.get(capability.connector)
    if connector is None:
        raise BrokerDenied(
            "env_name_out_of_scope",
            capability.id,
            f"connector {capability.connector!r} is not present in the registry",
        )
    return tuple(connector.env)


def _resolve_secret(env_name: str, *, capability: Capability) -> str:
    """Read one credential. The only place in the codebase that does this.

    Module-private on purpose (U-R4): the PUBLIC resolution boundary is
    :func:`resolve_for`, which is capability-addressed. A caller can no longer
    name an arbitrary environment variable, so the connector that owns a name is
    the only capability that can reach it.

    The only sanctioned exception to this path is the MODEL_COMPLETE
    capability's unbrokered GEMINI_API_KEY read (U-R9 / T4.8 owner), which
    bypasses this module entirely in ``omniagentos/llm/client.py`` and
    ``omniagentos/scheduler/loop_effects.py``. That exception is bounded by the
    ``llm/`` BudgetGuard spend limit and is the sole place in ``omniagentos/``
    where a credential is read outside this broker module.

    Never log, never return through an API, never write to an event payload. The
    return value is passed straight into an outbound request header.
    """
    declared = _declared_env_names(capability)
    if env_name not in declared:
        raise BrokerDenied(
            "env_name_out_of_scope",
            capability.id,
            f"{env_name!r} is not declared by connector {capability.connector!r}",
        )

    # U-S2: consult the name-only catalog BEFORE anything else touches this
    # name. A quarantined or revoked credential is refused on DISPOSITION, and
    # that fact outranks both the store's permission state and whether the
    # value happens to be present -- reporting a parked name as "missing" would
    # send the operator to provision a credential they already have.
    #
    # ``resolution_denial`` fails OPEN on every catalog fault by design (see its
    # docstring): a broken catalog degrades to pre-catalog behaviour with a loud
    # log rather than refusing every healthy name on the box.
    catalog_denial = resolution_denial(env_name)
    if catalog_denial:
        raise BrokerDenied(
            catalog_denial,
            env_name,
            _CATALOG_DENIAL_DETAIL[catalog_denial],
        )

    # U-S1/Phase-0: arm the store permission guard HERE, on the live resolution
    # path. The U-R4 rewrite made this function private but did not move it off
    # the credential path, so the guard still runs on every resolved name — and
    # its refusal is a fifth, distinct denial code (``credential_unavailable``)
    # that never collapses into the four scope/provisioning codes below.
    guard = _arm_permission_guard()
    if guard is not None:
        store_path = _get_store_path_for_env(env_name)
        if store_path is None:
            # Unmapped is NOT a violation. Nothing about this credential's
            # storage has been examined, so there is nothing to refuse; saying
            # so distinctly keeps "we did not check" out of the denial counts.
            _LOG.debug(
                "secret-store guard: no local store maps %s; resolution passes through",
                env_name,
            )
        else:
            try:
                guard.check_store_access(store_path, env_name)
            except PermissionViolation as exc:
                if _store_permission_guard_mode() == "enforce":
                    raise BrokerDenied(
                        "credential_unavailable",
                        env_name,
                        f"Store access denied: {exc.reason}",
                    ) from exc
                _LOG.warning(
                    "secret-store guard (shadow): would refuse %s: %s",
                    env_name,
                    exc.reason,
                )

    value = os.environ.get(env_name, "")
    if not value:
        reason = "credential_missing"
        denied_id = env_name
        detail = f"{env_name} is not present in the broker environment"
        if not any(os.environ.get(name, "") for name in declared):
            reason = "capability_unprovisioned"
            denied_id = capability.id
            detail = f"{capability.connector} has no provisioned credential names"
        raise BrokerDenied(
            reason,
            denied_id,
            detail,
        )
    return value


def _resolve_for_unlogged(capability: Capability) -> dict[str, str]:
    return {
        env_name: _resolve_secret(env_name, capability=capability)
        for env_name in _declared_env_names(capability)
    }


def resolve_for(
    capability: Capability,
    *,
    audit_store: Any = None,
    audit_context: AuditContext | None = None,
) -> dict[str, str]:
    """Resolve exactly the credential names declared by one capability's connector.

    This is the public credential-resolution boundary. Callers select a capability,
    never an arbitrary environment-variable name; the returned mapping cannot
    contain names owned by another connector.  A durable intent is written before
    reading any value and a terminal row follows every outcome.
    """
    call_id = new_id("bcl")
    context = (audit_context or AuditContext()).normalized()
    started = time.monotonic()
    try:
        with _audit_store_scope(audit_store) as sink:
            try:
                _audit_row(
                    sink,
                    context,
                    capability,
                    call_id=call_id,
                    method="RESOLVE",
                    decision="intent",
                )
            except Exception as exc:  # noqa: BLE001 -- fail closed before value access.
                _emit_audit_health(call_id, capability.id)
                raise BrokerDenied(
                    "audit_unavailable",
                    capability.id,
                    "durable broker intent could not be written",
                ) from exc

            try:
                resolved = _resolve_for_unlogged(capability)
            except BaseException as exc:
                decision, reason = _terminal_decision(exc)
                try:
                    _audit_row(
                        sink,
                        context,
                        capability,
                        call_id=call_id,
                        method="RESOLVE",
                        decision=decision,
                        reason_code=reason,
                        latency_ms=round((time.monotonic() - started) * 1000),
                    )
                except Exception as audit_exc:  # noqa: BLE001
                    _emit_audit_health(call_id, capability.id)
                    raise BrokerDenied(
                        "audit_unavailable",
                        capability.id,
                        "broker finalization could not be written",
                    ) from audit_exc
                raise

            try:
                _audit_row(
                    sink,
                    context,
                    capability,
                    call_id=call_id,
                    method="RESOLVE",
                    decision="allowed",
                    latency_ms=round((time.monotonic() - started) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                _emit_audit_health(call_id, capability.id)
                raise BrokerDenied(
                    "audit_unavailable",
                    capability.id,
                    "broker finalization could not be written",
                ) from exc
            return resolved
    except BrokerDenied:
        raise
    except Exception as exc:  # noqa: BLE001 -- store creation/migration failure.
        _emit_audit_health(call_id, capability.id)
        raise BrokerDenied(
            "audit_unavailable",
            capability.id,
            "broker audit store is unavailable",
        ) from exc


def resolve_one_for(
    capability: Capability,
    env_name: str,
    *,
    audit_store: Any = None,
    audit_context: AuditContext | None = None,
) -> str:
    """Resolve one declared credential through the durable audit boundary.

    This is the narrow counterpart to :func:`resolve_for` for callers which
    need to preserve optional configuration semantics.  In particular, a
    connector may declare descriptive or alternate-provider names that are not
    needed by a given call; resolving the whole connector merely to determine
    whether one auth value is available would turn those optional names into an
    all-or-nothing provisioning requirement.

    The caller still supplies a capability, never an arbitrary connector or
    environment namespace, and every attempt writes the same RESOLVE
    intent/finalization audit pair as ``resolve_for``.
    """
    call_id = new_id("bcl")
    context = (audit_context or AuditContext()).normalized()
    started = time.monotonic()
    try:
        with _audit_store_scope(audit_store) as sink:
            try:
                _audit_row(
                    sink,
                    context,
                    capability,
                    call_id=call_id,
                    method="RESOLVE",
                    decision="intent",
                )
            except Exception as exc:  # noqa: BLE001 -- fail closed before value access.
                _emit_audit_health(call_id, capability.id)
                raise BrokerDenied(
                    "audit_unavailable",
                    capability.id,
                    "durable broker intent could not be written",
                ) from exc

            try:
                resolved = _resolve_secret(env_name, capability=capability)
            except BaseException as exc:
                decision, reason = _terminal_decision(exc)
                try:
                    _audit_row(
                        sink,
                        context,
                        capability,
                        call_id=call_id,
                        method="RESOLVE",
                        decision=decision,
                        reason_code=reason,
                        latency_ms=round((time.monotonic() - started) * 1000),
                    )
                except Exception as audit_exc:  # noqa: BLE001
                    _emit_audit_health(call_id, capability.id)
                    raise BrokerDenied(
                        "audit_unavailable",
                        capability.id,
                        "broker finalization could not be written",
                    ) from audit_exc
                raise

            try:
                _audit_row(
                    sink,
                    context,
                    capability,
                    call_id=call_id,
                    method="RESOLVE",
                    decision="allowed",
                    latency_ms=round((time.monotonic() - started) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                _emit_audit_health(call_id, capability.id)
                raise BrokerDenied(
                    "audit_unavailable",
                    capability.id,
                    "broker finalization could not be written",
                ) from exc
            return resolved
    except BrokerDenied:
        raise
    except Exception as exc:  # noqa: BLE001 -- store creation/migration failure.
        _emit_audit_health(call_id, capability.id)
        raise BrokerDenied(
            "audit_unavailable",
            capability.id,
            "broker audit store is unavailable",
        ) from exc


def _scoped_resolver(capability: Capability) -> Callable[[str], str]:
    """Return a name-addressed callable fenced to one connector's declared names.

    Resolves ONE name per request, not the connector's whole declared set. The
    fence is what U-R4 is for — a name outside the connector's declaration is
    refused — but resolving every declared name eagerly makes provisioning
    all-or-nothing: Teller declares six names and its mTLS scheme reads three,
    so an unset ``TELLER_APPLICATION_ID`` would have failed every live bank
    read with ``credential_missing``. Names a scheme never asks for are not
    this call's business.

    :func:`resolve_for` remains the API for the complete mapping, where
    resolving everything is the caller's explicit request rather than a side
    effect of needing one header.
    """
    allowed = frozenset(_declared_env_names(capability))
    resolved: dict[str, str] = {}

    def resolve(env_name: str) -> str:
        if env_name not in allowed:
            raise BrokerDenied(
                "env_name_out_of_scope",
                capability.id,
                f"{env_name!r} is not declared by connector {capability.connector!r}",
            )
        if env_name not in resolved:
            resolved[env_name] = _resolve_secret(env_name, capability=capability)
        return resolved[env_name]

    return resolve


def _ovh_body_str(body: Any) -> str:
    """Serialise an OVH request body exactly as httpx will place it on the wire.

    OVH signs the literal request body bytes, so the signer must see the same
    string httpx emits for ``json=body``. httpx 0.28 encodes JSON with
    ``ensure_ascii=False, separators=(",", ":"), allow_nan=False`` -- match that
    here so the signature covers the real payload. A body-less call (GET/DELETE)
    signs the empty string, as OVH's own client does.
    """
    if body is None:
        return ""
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _ovh_signature(
    app_secret: str,
    consumer_key: str,
    method: str,
    url: str,
    body_str: str,
    timestamp: str,
) -> str:
    """Compute an OVH request signature.

    ``"$1$" + sha1_hex(secret + "+" + consumer + "+" + METHOD + "+" + URL + "+"
    + BODY + "+" + TS)`` -- OVH authenticates every request with this per-call
    digest rather than a bearer token. The application secret feeds the SHA1 but
    is one-way through it: the digest is returned, the secret never is. Keeping
    this pure (no clock, no env read) makes the signature deterministic for a
    known input, which the unit tests pin.
    """
    raw = "+".join([app_secret, consumer_key, method.upper(), url, body_str, timestamp])
    return "$1$" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _auth_headers(
    spec: Any,
    capability: Capability,
    *,
    method: str = "GET",
    url: str = "",
    body: Any = None,
    form: dict[str, Any] | None = None,
) -> tuple[dict[str, str], dict[str, str], tuple[str, str] | None]:
    """Build auth headers, query params, and client cert from a capability's auth scheme.

    Returns (headers, query_params, cert). The cert tuple is (cert_path, key_path) when
    using mutual-TLS, or None otherwise. Credentials are read here and passed
    straight into the outbound request -- they are never returned to a caller, logged,
    or written to an event payload.

    ``method``/``url``/``body``/``form`` describe the ACTUAL outbound request and
    are only consulted by signing schemes (OVH), which sign over the method/url/body
    so the signature covers the real request rather than a caller assertion. The
    OVH scheme signs the JSON body only and refuses a ``form`` payload (it would be
    sent but signed as empty -- a mismatch OVH rejects), failing closed before any
    network hop. Non-signing schemes ignore all four, so existing two-arg callers
    are unaffected.
    """
    headers: dict[str, str] = dict(spec.headers)
    params: dict[str, str] = {}
    cert: tuple[str, str] | None = None
    scheme = spec.auth
    if not scheme:
        return headers, params, cert

    kind, _, rest = scheme.partition(":")

    resolve = _scoped_resolver(capability)

    if kind == "bearer":
        headers["Authorization"] = f"Bearer {resolve(rest)}"
    elif kind == "basic":
        import base64

        user_env, _, pw_env = rest.partition(":")
        raw = f"{resolve(user_env)}:{resolve(pw_env)}".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
    elif kind == "query":
        param, _, env_name = rest.partition(":")
        params[param] = resolve(env_name)
    elif kind == "header":
        # "header:<Header-Name>:<ENV_VAR>" -- for APIs that authenticate with a
        # bare API-key header (e.g. Slash's X-Api-Key) rather than Authorization.
        header_name, _, env_name = rest.partition(":")
        if not header_name or not env_name:
            raise BrokerDenied("bad_auth_scheme", scheme, "header scheme needs header:<Name>:<ENV>")
        headers[header_name] = resolve(env_name)
    elif kind == "oauth2":
        # "oauth2:<REFRESH_ENV>:<CLIENT_ID_ENV>:<CLIENT_SECRET_ENV>" -- Google-style
        # OAuth2. The broker exchanges the long-lived refresh token for a short-lived
        # access token (POST to Google's token endpoint, cached in-process), then sets
        # it as a Bearer. The three env vars are read HERE and never leave the broker:
        # The broker never exposes connector credentials to an agent subprocess.
        # A token-endpoint error fails closed -- no token, no API call.
        parts = rest.split(":")
        if len(parts) != 3 or not all(parts):
            raise BrokerDenied(
                "bad_auth_scheme",
                scheme,
                "oauth2 scheme needs oauth2:<RefreshEnv>:<ClientIdEnv>:<ClientSecretEnv>",
            )
        refresh_env, client_id_env, client_secret_env = parts
        try:
            token = mint_bearer(refresh_env, client_id_env, client_secret_env, resolve)
        except OAuthTokenError as exc:
            # Fail closed: refuse the call rather than issue it without a valid token.
            raise BrokerDenied("oauth_token_error", scheme, str(exc)) from exc
        headers["Authorization"] = f"Bearer {token}"
    elif kind == "ovh":
        # "ovh:<APP_KEY_ENV>:<APP_SECRET_ENV>:<CONSUMER_KEY_ENV>" -- OVH does NOT
        # carry a bearer token; it SIGNS every request. The three env values are
        # read HERE and never leave the broker: the application key and consumer
        # key ride out as X-Ovh-* headers, but the application SECRET only ever
        # feeds the SHA1 digest -- it is never a header, a return value, or a log
        # line. The signature covers the ACTUAL outbound method/url/body passed in
        # by the dispatcher (not a caller assertion), so a tampered request cannot
        # reuse a signature. A missing env value fails closed via resolve().
        parts = rest.split(":")
        if len(parts) != 3 or not all(parts):
            raise BrokerDenied(
                "bad_auth_scheme",
                scheme,
                "ovh scheme needs ovh:<AppKeyEnv>:<AppSecretEnv>:<ConsumerKeyEnv>",
            )
        # The OVH API these capabilities use is JSON-only, and the signature folds
        # in the JSON body ONLY. A form payload would be transmitted (data=...) but
        # signed as an empty body -- a signature mismatch OVH rejects with 401.
        # Fail closed here rather than issue a request that cannot authenticate.
        if form:
            raise BrokerDenied(
                "form_not_supported",
                capability.id,
                "ovh scheme does not support form data; it signs the JSON body only",
            )
        app_key_env, app_secret_env, consumer_key_env = parts
        app_key = resolve(app_key_env)
        app_secret = resolve(app_secret_env)
        consumer_key = resolve(consumer_key_env)
        timestamp = str(int(time.time()))
        signature = _ovh_signature(
            app_secret,
            consumer_key,
            method,
            url,
            _ovh_body_str(body),
            timestamp,
        )
        headers["X-Ovh-Application"] = app_key
        headers["X-Ovh-Consumer"] = consumer_key
        headers["X-Ovh-Timestamp"] = timestamp
        headers["X-Ovh-Signature"] = signature
    elif kind == "mtls":
        # "mtls:<CERT_PATH_ENV>:<CERT_KEY_PATH_ENV>:<ACCESS_TOKEN_ENV>" -- mutual-TLS
        # client certificate plus HTTP Basic auth with the access token as username.
        # Used by Teller and similar APIs requiring client-cert authentication.
        parts = rest.split(":")
        if len(parts) < 3:
            raise BrokerDenied(
                "bad_auth_scheme", scheme, "mtls scheme needs mtls:<CertPath>:<KeyPath>:<TokenEnv>"
            )
        cert_path_env = parts[0]
        key_path_env = parts[1]
        token_env = parts[2]
        cert_path = resolve(cert_path_env)
        key_path = resolve(key_path_env)
        token = resolve(token_env)
        # Set up the client cert tuple for httpx
        cert = (cert_path, key_path)
        # Set up HTTP Basic auth with token as username, empty password
        import base64

        raw = f"{token}:".encode()
        headers["Authorization"] = f"Basic {base64.b64encode(raw).decode()}"
    elif kind == "form":
        # Resolved below by _auth_form() so form credentials never leave broker.py.
        pass
    else:
        raise BrokerDenied("bad_auth_scheme", scheme, f"unrecognised auth scheme {kind!r}")
    return headers, params, cert


def _auth_form(spec: Any, capability: Capability) -> dict[str, str]:
    """Build secret form fields for the small set of form-authenticated APIs."""
    scheme = spec.auth
    if not scheme or not scheme.startswith("form:"):
        return {}
    _, _, rest = scheme.partition(":")
    param, _, env_name = rest.partition(":")
    if not param or not env_name:
        raise BrokerDenied("bad_auth_scheme", scheme, "form scheme needs form:<param>:<ENV>")
    return {param: _scoped_resolver(capability)(env_name)}


def _call_unlogged(
    cap_id: str,
    granted: list[str] | None,
    *,
    method: str = "GET",
    path: str = "/",
    query: dict[str, Any] | None = None,
    body: Any = None,
    form: dict[str, Any] | None = None,
    approval_token: str | None = None,
    grant_id: str | None = None,
    grant_store: Any = None,
    grant_holder: str | None = None,
    agent_id: str | None = None,
    generation: int | None = None,
    project_id: str | None = None,
    timeout: float = 30.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute one capability call on an agent's behalf.

    This is the ONLY way an agent reaches an external system. It authorizes the
    grant, enforces the method/path allowlist, resolves the credential, makes the
    request, and returns the response body. The agent never sees the credential and
    cannot vary the host -- ``base_url`` comes from the registry, not the caller.

    When a durable grant reference (``grant_id`` or ``approval_token``) and
    ``grant_store`` are provided, the campaign grant is consumed atomically and
    recipients extracted from the body are checked against the grant's target set.
    Scoped-arg pins are checked against the actual ``body``/``form`` of this
    request (never a separate caller assertion).

    Extractor order (intentional):

    * **Durable path** (grant ref + store): ``extract_call_bounds`` runs *before*
      ``authorize_with_grant`` so target-set / spend consume see real recipients
      and so a mismatched body is refused without a network hop. Extraction is
      pure observation of the caller-supplied body (no side effects, no grant
      writes); authorize + atomic consume still gate the network.
    * **Non-durable path**: ``authorize`` runs first, then extractors attach
      bounds for the response/audit only.

    Free-form truthy ``approval_token`` strings never satisfy money or danger
    gates (H-02 / F-11); only a store-validated, consumed grant does.

    Cross-lane note: ``toolplane`` currently invokes authorize/call without a
    ``grant_store``. That path fails closed for hard-human / money / danger
    writes (no durable proof). Wiring a store into toolplane is intentionally
    out of L02 scope — see RESIDUAL-RISKS.md (toolplane no-store handoff).

    ``dry_run=True`` answers "would this exact call be authorized right now?"
    and nothing else. Every authorization obligation still runs — grant load and
    validity, lifecycle mode/expiry, project binding, target-set membership,
    spend measurability, the method/path allowlist, and the money-write and
    danger-group hard-stops — and a refusal raises the same
    :class:`BrokerDenied` a real call raises. What does NOT run is everything
    downstream of the verdict: no bounded-grant consumption, no secret
    resolution, no in-process echo dispatch, no HTTP request. The short-circuit
    sits between the last hard-stop and ``cap.http``, so there is no dispatch
    site it can be routed around; both live below it.
    """
    import httpx

    from omniagentos.connectors.extractors import extract_call_bounds

    dry_run = _require_dry_run_flag(dry_run, cap_id)

    grant_ref, norm_reason = normalize_grant_ref(approval_token, grant_id)
    if norm_reason:
        raise BrokerDenied("invalid_approval_token", cap_id, norm_reason)

    # Always derived from the outbound request — never a separate caller claim.
    request_scoped = _scoped_args_from_request(body, form)

    # Path 1: standing capability grant loaded by holder from the broker DB.
    # Path 2: durable campaign grant / approval token — authorize + consume.
    # Path 3: compatibility path with a caller-supplied capability ceiling.
    durable_ok = False
    if grant_holder is not None:
        if grant_ref is not None:
            raise BrokerDenied(
                "invalid_grant_reference",
                cap_id,
                "standing and campaign grant references cannot be combined",
            )
        cap = authorize_with_grant(
            cap_id,
            granted,
            grant_store,
            grant_holder=grant_holder,
            agent_id=agent_id,
            method=method,
            path=path,
            project_id=project_id,
        )
        bounds = extract_call_bounds(cap_id, method, path, body, form)
    elif grant_ref is not None and grant_store is not None:
        # Observe body bounds so the grant consume can enforce target-set.
        bounds = extract_call_bounds(cap_id, method, path, body, form)
        try:
            grant_row = grant_store.get_grant(grant_ref)
        except Exception as exc:  # noqa: BLE001
            raise BrokerDenied(
                "grant_store_unavailable",
                cap_id,
                f"grant store failed while loading {grant_ref!r}",
            ) from exc
        if grant_row is None:
            raise BrokerDenied("grant_not_found", cap_id, f"campaign grant {grant_ref!r} not found")
        raw_targets = grant_row.get("target_set")
        if raw_targets is None:
            raw_targets = []
        if not isinstance(raw_targets, list):
            raise BrokerDenied(
                "grant_refused",
                cap_id,
                "target_set_ambiguous",
            )
        allowed = set(raw_targets)
        if allowed:
            if not bounds.recipients:
                # Unknown audience cannot be proven in-set — fail closed.
                raise BrokerDenied(
                    "grant_broke_out",
                    cap_id,
                    "recipients unknown or empty; cannot prove target-set membership",
                )
            outsiders = [r for r in bounds.recipients if r not in allowed]
            if outsiders:
                raise BrokerDenied(
                    "grant_broke_out",
                    cap_id,
                    f"recipients outside grant target_set: {outsiders}",
                )
        primary = bounds.recipients[0] if bounds.recipients else None
        # Unknown spend must never become $0.00. ``bounds.spend_usd is None``
        # means the extractor could not price the call (unparseable / negative /
        # null budget field). Coercing None→0 via ``or 0`` records free consume
        # against a real max_spend_usd cap — the same class of defect as
        # unknown cost booked as 0.0. When a budget amount was claimed but not
        # measured, refuse before try_consume. Bid-only / non-spend calls leave
        # no budget key and legitimately consume $0.
        if bounds.spend_usd is None and _request_claims_budget_amount(body, form):
            raise BrokerDenied(
                "spend_unknown",
                cap_id,
                "budget amount present but unmeasurable; refusing rather than "
                "recording $0.00 against the grant spend cap",
            )
        spend = 0.0 if bounds.spend_usd is None else float(bounds.spend_usd)
        cap = authorize_with_grant(
            cap_id,
            granted,
            grant_store,
            grant_ref,
            target=primary,
            spend_usd=spend,
            generation=generation,
            scoped_args=request_scoped,
            agent_id=agent_id,
            method=method,
            path=path,
            project_id=project_id,
            consume=not dry_run,
        )
        durable_ok = True
    else:
        # Token without a store, or no token at all. authorize() fails closed for
        # hard-human classes when the token cannot be store-validated.
        cap = authorize(
            cap_id,
            granted or [],
            approval_token=approval_token,
            grant_id=grant_id,
            grant_store=grant_store,
            agent_id=agent_id,
            method=method,
            path=path,
            target=None,
            generation=generation,
            scoped_args=request_scoped,
            project_id=project_id,
        )
        bounds = extract_call_bounds(cap_id, method, path, body, form)
        durable_ok = False

    validate_request(cap, method, path)

    # Money-movement hard-stop (AC-policy): a WRITE to a payment/bank connector may
    # never execute unattended. Reads (GET/HEAD) fall through and run in auto mode.
    # This is checked here -- before the outbound request is ever issued -- so a
    # money-move is refused, not silently sent, when no *validated* approval exists.
    # Truthiness of approval_token / grant_id is intentionally not enough (H-02).
    if is_money_write(cap, method, path) and not durable_ok:
        raise BrokerDenied(
            "money_write_requires_approval",
            cap_id,
            f"{cap_id} is a payment/bank WRITE ({method.upper()}); money-movement "
            "hard-stops in auto mode and needs a store-validated durable approval",
        )

    # Danger-group hard-stop (MEDIUM/fix4): any MUTATING method on a registry-flagged
    # danger group (ads/infra/... beyond payments) is refused regardless of the
    # capability's declared class, so a mis-declared write in a new danger group
    # cannot ride a soft action_class into unattended execution.
    if is_dangerous_write(cap, method, path) and not durable_ok:
        raise BrokerDenied(
            "dangerous_write_requires_approval",
            cap_id,
            f"{cap_id} is a WRITE ({method.upper()}) on danger group {cap.group!r}; "
            "it hard-stops in auto mode and needs a store-validated durable approval",
        )

    # Authorize-only pre-flight terminates HERE: every hard-stop above has been
    # evaluated and none of the effects below have run. This must stay the last
    # statement before ``cap.http`` — every dispatch site (in-process echo and
    # both httpx forms) and the only secret resolution (_auth_headers) are below
    # it, so the pre-flight cannot be routed around by adding another sink.
    if dry_run:
        return {
            "capability": cap_id,
            "status": 0,
            "bounds": bounds.as_dict(),
            "ok": True,
            "dry_run": True,
            "body": None,
        }

    spec = cap.http
    assert spec is not None  # authorize() guarantees this.

    if _is_inprocess(cap):
        from omniagentos.provision.connectors.echo import EchoRequestError, _dispatch_echo

        try:
            payload = _dispatch_echo(body)
        except EchoRequestError as exc:
            raise BrokerDenied("validation_error", cap_id, str(exc)) from exc
        return {
            "capability": cap_id,
            "status": 200,
            "bounds": bounds.as_dict(),
            "ok": True,
            "body": payload,
        }

    # Build the exact URL the request will hit so signing auth schemes (OVH) sign
    # the real url+query, not a stale approximation. OVH adds no auth query params,
    # so the caller ``query`` is the whole query string; httpx.URL merges it the
    # same way the outbound request does below.
    request_url = f"{spec.base_url.rstrip('/')}{path}"
    if query:
        request_url = str(httpx.URL(request_url, params=query))
    headers, auth_params, cert = _auth_headers(
        spec, cap, method=method, url=request_url, body=body, form=form
    )
    params = {**(query or {}), **auth_params}
    form_data = {**(form or {}), **_auth_form(spec, cap)}

    try:
        # Use an httpx.Client when a client cert is needed (mutual-TLS, e.g. Teller);
        # otherwise call httpx.request directly. Both carry json body + form data.
        if cert:
            with httpx.Client(cert=cert, verify=True) as client:
                response = client.request(
                    method.upper(),
                    f"{spec.base_url.rstrip('/')}{path}",
                    headers=headers,
                    params=params,
                    json=body if body is not None else None,
                    data=form_data or None,
                    timeout=timeout,
                )
        else:
            response = httpx.request(
                method.upper(),
                f"{spec.base_url.rstrip('/')}{path}",
                headers=headers,
                params=params,
                json=body if body is not None else None,
                data=form_data or None,
                timeout=timeout,
            )
    except httpx.HTTPError as exc:
        raise BrokerDenied("transport_error", cap_id, str(exc)) from exc

    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text[:4000]}

    return {
        "capability": cap_id,
        "status": response.status_code,
        "bounds": bounds.as_dict(),
        "ok": response.is_success,
        "body": payload,
    }


def call(
    cap_id: str,
    granted: list[str] | None = None,
    *,
    method: str = "GET",
    path: str = "/",
    query: dict[str, Any] | None = None,
    body: Any = None,
    form: dict[str, Any] | None = None,
    approval_token: str | None = None,
    grant_id: str | None = None,
    grant_store: Any = None,
    grant_holder: str | None = None,
    agent_id: str | None = None,
    generation: int | None = None,
    project_id: str | None = None,
    timeout: float = 30.0,
    audit_store: Any = None,
    audit_context: AuditContext | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute one call with broker-owned intent and terminal audit rows.

    Credential-free capabilities keep running when the audit store is down.
    Credentialed capabilities persist intent before authorization or resolution;
    inability to do so fails closed before any authentication material is read.

    Production callers identify a ``grant_holder`` and thread the database
    ``grant_store``. They must leave ``granted`` unset; the broker loads the
    current standing grant itself. The list parameter remains only for legacy
    and bounded campaign callers until those surfaces are migrated separately.

    ``agent_id`` is U-R6's lifecycle handle: when present, the grant's current
    ``mode``/``expires_at`` row is re-read and enforced at call time. It is
    forwarded unchanged through both the audited and unaudited paths, so the
    audit spine cannot become a place where a lifecycle check is quietly lost.
    It is also the row's ``agent_id`` column when supplied, so a route that
    knows a real agent identity is not attributed to the ``holder`` lane (K5).

    ``project_id`` is C11/D-31's containment handle: the project this call is
    work for. It is forwarded unchanged through both the audited and unaudited
    paths for the same reason ``agent_id`` is — the audit spine must never
    become the place where a containment check is quietly lost.

    An audit-write failure is reported by POSITION, not by one blanket code:
    ``audit_unavailable`` means nothing left this process (safe to retry) and
    ``audit_finalization_failed`` means the request WAS issued and only its
    terminal row is missing (reconcile; a blind retry may buy the effect
    twice). The distinction is load bearing — the loop seam reads it to decide
    whether an open budget reservation is released or settled.

    ``dry_run=True`` is the authorize-only pre-flight: the full authorization
    verdict with zero secret resolution, zero dispatch and zero bounded-grant
    consumption (see :func:`_call_unlogged`). It is receipted in this same
    audit spine — intent, then a terminal ``decision='dry_run'`` row — so a
    pre-flight is never indistinguishable from a real send when the money spine
    is reconciled. A refused pre-flight raises ``BrokerDenied`` and records the
    ordinary terminal denial rows, so the spine answers both "would it run?"
    and "why not?". The flag must be a strict bool; anything else refuses
    before the intent row, having attempted nothing.

    A pre-flight verdict is point-in-time, not a reservation: it proves the
    call is authorized NOW, not that a later real send will be.
    """
    dry_run = _require_dry_run_flag(dry_run, cap_id)
    capability = _audit_capability(cap_id)
    if not (_is_credentialed(capability) or _is_inprocess(capability)):
        return _call_unlogged(
            cap_id,
            granted,
            method=method,
            path=path,
            query=query,
            body=body,
            form=form,
            approval_token=approval_token,
            grant_id=grant_id,
            grant_store=grant_store,
            grant_holder=grant_holder,
            agent_id=agent_id,
            generation=generation,
            project_id=project_id,
            timeout=timeout,
            dry_run=dry_run,
        )

    assert capability is not None
    call_id = new_id("bcl")
    context = (audit_context or AuditContext()).normalized(grant_id=grant_id or "")
    schema_hash = _request_schema_digest(body, form)
    started = time.monotonic()
    #: Flipped the moment bytes may have left this process. Read by
    #: :func:`_audit_failure_reason` so an audit-write failure can never be
    #: reported as "provably unbilled" once the provider has been asked.
    reached = False

    try:
        with _audit_store_scope(audit_store) as sink:
            try:
                _audit_row(
                    sink,
                    context,
                    capability,
                    call_id=call_id,
                    method=method,
                    decision="intent",
                    request_schema_hash=schema_hash,
                    agent_id=agent_id or "",
                )
            except Exception as exc:  # noqa: BLE001 -- no durable intent, no credential use.
                _emit_audit_health(call_id, cap_id)
                raise BrokerDenied(
                    "audit_unavailable",
                    cap_id,
                    "durable broker intent could not be written",
                ) from exc

            try:
                result = _call_unlogged(
                    cap_id,
                    granted,
                    method=method,
                    path=path,
                    query=query,
                    body=body,
                    form=form,
                    approval_token=approval_token,
                    grant_id=grant_id,
                    grant_store=grant_store,
                    grant_holder=grant_holder,
                    agent_id=agent_id,
                    generation=generation,
                    project_id=project_id,
                    timeout=timeout,
                    dry_run=dry_run,
                )
            except BaseException as exc:
                reached = _reached_provider(exc)
                decision, reason = _terminal_decision(exc)
                try:
                    _audit_row(
                        sink,
                        context,
                        capability,
                        call_id=call_id,
                        method=method,
                        decision=decision,
                        reason_code=reason,
                        latency_ms=round((time.monotonic() - started) * 1000),
                        request_schema_hash=schema_hash,
                        agent_id=agent_id or "",
                    )
                except Exception as audit_exc:  # noqa: BLE001
                    _emit_audit_health(call_id, cap_id)
                    raise BrokerDenied(
                        _audit_failure_reason(reached),
                        cap_id,
                        "broker finalization could not be written",
                    ) from audit_exc
                raise

            # The provider answered. Everything from here is bookkeeping about
            # work that has already been done (and, for a paid capability,
            # already been charged). A pre-flight is the one shape of success
            # that reached NOTHING: leaving ``reached`` False keeps a failed
            # finalization classified as "provably unbilled, safe to retry"
            # instead of sending the loop seam to reconcile a send that by
            # construction never happened.
            reached = not dry_run
            status = int(result["status"])
            try:
                _audit_row(
                    sink,
                    context,
                    capability,
                    call_id=call_id,
                    method=method,
                    decision="dry_run" if dry_run else "allowed",
                    latency_ms=round((time.monotonic() - started) * 1000),
                    status=status,
                    request_schema_hash=schema_hash,
                    agent_id=agent_id or "",
                )
            except Exception as exc:  # noqa: BLE001
                _emit_audit_health(call_id, cap_id)
                raise BrokerDenied(
                    _audit_failure_reason(reached),
                    cap_id,
                    "broker finalization could not be written",
                ) from exc
            return result
    except BrokerDenied:
        raise
    except Exception as exc:  # noqa: BLE001 -- store creation/migration failure.
        _emit_audit_health(call_id, cap_id)
        raise BrokerDenied(
            _audit_failure_reason(reached),
            cap_id,
            "broker audit store is unavailable",
        ) from exc


def describe_grant(granted: list[str]) -> dict[str, Any]:
    """Summarise what a grant actually reaches, for the UI and for audit.

    Returns counts by action class plus the blast-radius facts a human needs to
    approve an agent: can it write anything, can it touch money, can it reach a
    customer. Contains no secrets -- only env var *names*.
    """
    registry = load_registry()
    caps: list[Capability] = []
    unknown: list[str] = []
    for cap_id in granted:
        try:
            caps.append(registry.capability(cap_id))
        except ConnectorError:
            unknown.append(cap_id)

    by_class: dict[str, int] = {}
    for cap in caps:
        by_class[cap.action_class.value] = by_class.get(cap.action_class.value, 0) + 1

    danger_groups = {g for g, spec in registry.groups.items() if spec.danger}
    return {
        "total": len(caps),
        "unknown": unknown,
        "by_action_class": by_class,
        "connectors": sorted({c.connector for c in caps}),
        "groups": sorted({c.group for c in caps}),
        "writes": sorted(c.id for c in caps if c.action_class != ActionClass.READ_ONLY),
        "auto_writes": sorted(
            c.id for c in caps if c.is_auto and c.action_class == ActionClass.INTERNAL_REVERSIBLE
        ),
        "needs_approval": sorted(
            c.id for c in caps if c.action_class == ActionClass.EXTERNAL_REVERSIBLE
        ),
        "always_human": sorted(c.id for c in caps if c.action_class in HARD_HUMAN_CLASSES),
        "touches_danger_group": sorted({c.group for c in caps if c.group in danger_groups}),
        "not_yet_callable": sorted(c.id for c in caps if not c.callable_now),
        "env_names": sorted(registry.env_for([c.id for c in caps])),
    }


__all__ = [
    "HARD_HUMAN_CLASSES",
    "PAYMENT_GROUPS",
    "READ_METHODS",
    "AuditContext",
    "BrokerDenied",
    "audits_own_calls",
    "authorize",
    "authorize_with_grant",
    "call",
    "describe_grant",
    "effective_grant",
    "is_read_only_query_post",
    "is_dangerous_write",
    "is_money_write",
    "resolve_for",
    "resolve_one_for",
    "validate_request",
]
