"""Durable approval-token validation against the campaign grant store.

H-02 / F-11: a caller-supplied string is not proof of human approval. Hard-human
and money/customer-write gates may only pass when a grant store can prove the
token is live, correctly generation-fenced, unexpired, unrevoked, and scoped to
the intended capability / connector / tool / target / arguments.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from omniagentos.grants.store import is_grant_live


@dataclass(frozen=True, slots=True)
class ApprovalValidation:
    """Outcome of one durable approval-token check."""

    ok: bool
    reason: str = ""
    grant: dict[str, Any] | None = None


def _tool_name(capability: str) -> str:
    _connector, sep, tool = capability.partition(".")
    return tool if sep else ""


def _connector_name(capability: str) -> str:
    connector, sep, _tool = capability.partition(".")
    return connector if sep else capability


def _as_int(value: Any, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _metadata(grant: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = grant.get("metadata")
    return raw if isinstance(raw, Mapping) else {}


def _scoped_args_match(
    allowed: Any,
    presented: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Require every grant-pinned scoped arg to be present and equal.

    Ambiguous shapes (non-mapping pins, non-mapping presentation when pins
    exist) fail closed. Extra presented keys are allowed; missing or unequal
    required keys are not.
    """
    if allowed is None or allowed == {}:
        return True, ""
    if not isinstance(allowed, Mapping):
        return False, "scoped_args_ambiguous"
    if presented is None:
        return False, "scoped_args_missing"
    if not isinstance(presented, Mapping):
        return False, "scoped_args_ambiguous"
    for key, expected in allowed.items():
        if key not in presented:
            return False, "scoped_args_missing"
        if presented[key] != expected:
            return False, "scoped_args_mismatch"
    return True, ""


def _target_allowed(grant: Mapping[str, Any], target: str | None) -> tuple[bool, str]:
    """Enforce target_set membership. Non-list / falsy non-list shapes fail closed.

    ``None`` or missing is treated as an empty list (no audience restriction).
    Empty list is unrestricted. Any other non-list value (including ``""``, ``0``,
    ``False``, mappings) is ``target_set_ambiguous`` — never coerced to empty.
    """
    if "target_set" not in grant:
        target_set: Any = None
    else:
        target_set = grant.get("target_set")
    if target_set is None:
        target_set = []
    if not isinstance(target_set, list):
        return False, "target_set_ambiguous"
    if not target_set:
        # Empty set means "no audience restriction" at the grant layer.
        return True, ""
    if target is None:
        # Non-empty target set with unknown audience cannot be proven in-set.
        return False, "target_ambiguous"
    if target not in set(target_set):
        return False, "target_breakout"
    return True, ""


# Recipient-count growth beyond the approval-time snapshot that a single send
# is still allowed to absorb before being refused as drift. Zero by default:
# an approved audience snapshot is exact, so any measurable growth in the live
# call's own recipient surface is refused rather than silently admitted. A
# grant that legitimately needs a larger audience must be re-approved (re-
# minted with a new snapshot), never have its existing bound stretched at
# send time.
_AUDIENCE_DRIFT_TOLERANCE = 0


def _audience_bound_ok(
    grant: Mapping[str, Any],
    *,
    capability: str,
    scoped_args: Mapping[str, Any] | None,
) -> tuple[bool, str]:
    """Re-check the live recipient surface against the grant's audience snapshot.

    Only customer-message capabilities carry a real audience (see
    :func:`omniagentos.connectors.extractors.is_broadcast_capability`); every
    other capability is a no-op here, preserving existing grant behavior.

    Fail closed: a broadcast-capable grant that reaches this check without a
    recorded ``max_recipients``/``audience_snapshot_hash`` pair is refused
    outright. ``create_grant`` no longer mints one missing this pair, but this
    is the defense-in-depth arm for any row that exists anyway (a pre-existing
    row, or one written directly rather than through the DAL).

    Runs against ``scoped_args`` -- the caller's outbound request body/form
    (``authorize_with_grant`` / ``try_consume`` receive it derived straight
    from the request, never a separate caller assertion) -- so a grant
    approved against N recipients cannot be exercised unmodified against a
    materially larger or substituted recipient set: the live count may not
    exceed ``max_recipients``, may not drift past the snapshot count by more
    than the configured tolerance, and (when the live recipients are fully
    known) must hash-match the approved snapshot exactly.

    Empty/undeterminable live recipients (``extract_call_bounds`` could not
    parse the body, or it carried none) is treated as UNKNOWN, never as
    "verified zero" -- the extractor's own contract (see the extractors.py
    module docstring) is that empty recipients means unknown audience, and an
    unparseable or malformed broadcast payload must fail closed exactly like
    ``_target_allowed`` already does for an unknown single ``target``, rather
    than sail through every numeric/hash check for want of anything to
    compare against.

    v1 limitation (accepted, not fixed here): for
    ``customerio*.trigger_broadcast``, ``recipient_count``/``recipients`` are
    the segment ids and explicit addresses NAMED in the call body, not that
    segment's live membership -- a segment can grow after approval with no
    local signal of it (see ``extractors.py``'s
    ``_customerio_trigger_broadcast_bounds`` docstring). This function bounds
    which segments/addresses a grant may name and how many, and detects a
    substituted or added segment/address; it does NOT detect a named segment's
    membership growing externally. Fast-follow: query the live segment size
    at send time and re-check it against the approval-time snapshot too.
    """
    # Local import: connectors.extractors imports connectors.broker, which
    # imports this package at module load time -- a module-level import here
    # would be circular.
    from omniagentos.connectors.extractors import (
        audience_snapshot_hash,
        extract_call_bounds,
        is_broadcast_capability,
    )

    if not is_broadcast_capability(capability):
        return True, ""

    meta = _metadata(grant)
    max_recipients = _as_int(meta.get("max_recipients"), default=None)
    snapshot_hash = meta.get("audience_snapshot_hash")
    if max_recipients is None or not isinstance(snapshot_hash, str) or not snapshot_hash:
        # Same reason ``is_grant_live`` uses for this fact (it runs first, via
        # ``validate_approval_token``, and refuses here); kept as its own
        # reachable check for callers of ``_audience_bound_ok`` outside that
        # path.
        return False, "no_audience_bound"
    snapshot_count = _as_int(meta.get("audience_snapshot_count"), default=0) or 0

    bounds = extract_call_bounds(capability, "", "", dict(scoped_args) if scoped_args else {})
    if not bounds.recipients:
        # Empty/unparseable is UNKNOWN, not "zero, therefore trivially within
        # any bound" -- fail closed rather than let a malformed or empty
        # broadcast payload skip every check below for want of a recipient to
        # compare.
        return False, "audience_unverifiable"
    if bounds.recipient_count > max_recipients:
        return False, "max_recipients_exceeded"
    if bounds.recipient_count > snapshot_count + _AUDIENCE_DRIFT_TOLERANCE:
        return False, "audience_drift"
    if audience_snapshot_hash(bounds.recipients) != snapshot_hash:
        return False, "audience_snapshot_mismatch"

    return True, ""


def _generation_match(grant: Mapping[str, Any], generation: int | None) -> tuple[bool, str]:
    """Require an explicit durable generation pin on the grant and a matching present.

    Missing, non-int, or unparseable pins fail closed as ``generation_ambiguous``.
    Callers may not omit the generation when a pin exists (no silent default to 0).
    """
    meta = _metadata(grant)
    if "generation" not in meta:
        return False, "generation_ambiguous"
    expected = _as_int(meta.get("generation"), default=None)
    if expected is None:
        return False, "generation_ambiguous"
    if generation is None:
        return False, "generation_ambiguous"
    presented = _as_int(generation, default=None)
    if presented is None:
        return False, "generation_ambiguous"
    if presented != expected:
        return False, "generation_mismatch"
    return True, ""


def _pins_match(
    grant: Mapping[str, Any],
    *,
    capability: str,
    action_class: str | None,
    connector: str | None,
    tool: str | None,
) -> tuple[bool, str]:
    """Check capability identity plus optional metadata pins and derived names."""
    grant_cap = str(grant.get("capability") or "")
    if capability != grant_cap:
        return False, "capability_mismatch"

    derived_connector = _connector_name(grant_cap)
    derived_tool = _tool_name(grant_cap)

    if connector is not None and connector != derived_connector:
        return False, "connector_mismatch"
    if tool is not None and tool != derived_tool:
        return False, "tool_mismatch"

    meta = _metadata(grant)
    pinned_ac = meta.get("action_class")
    if pinned_ac is None:
        return False, "action_class_ambiguous"
    if action_class is None:
        return False, "action_class_ambiguous"
    if str(pinned_ac) != str(action_class):
        return False, "action_class_mismatch"

    pinned_connector = meta.get("connector")
    if pinned_connector is not None and str(pinned_connector) != derived_connector:
        return False, "connector_mismatch"
    if pinned_connector is not None and connector is not None:
        if str(pinned_connector) != str(connector):
            return False, "connector_mismatch"

    pinned_tool = meta.get("tool")
    if pinned_tool is not None and str(pinned_tool) != derived_tool:
        return False, "tool_mismatch"
    if pinned_tool is not None and tool is not None:
        if str(pinned_tool) != str(tool):
            return False, "tool_mismatch"

    return True, ""


def validate_approval_token(
    token: str | None,
    *,
    grant_store: Any | None,
    capability: str,
    action_class: str | None = None,
    connector: str | None = None,
    tool: str | None = None,
    target: str | None = None,
    generation: int | None = None,
    scoped_args: Mapping[str, Any] | None = None,
    spend_usd: float = 0,
) -> ApprovalValidation:
    """Validate one approval token against the durable grant store. Fail closed.

    The token MUST be a campaign-grant id that loads from ``grant_store``. Any
    missing store, malformed token, load error, or ambiguous scope check denies.
    """
    if grant_store is None:
        return ApprovalValidation(False, "grant_store_unavailable")
    if token is None:
        return ApprovalValidation(False, "token_missing")
    if not isinstance(token, str):
        return ApprovalValidation(False, "token_malformed")
    token = token.strip()
    if not token:
        return ApprovalValidation(False, "token_malformed")

    try:
        get_grant = getattr(grant_store, "get_grant", None)
        if get_grant is None:
            return ApprovalValidation(False, "grant_store_unavailable")
        row = get_grant(token)
    except Exception:  # noqa: BLE001 -- store faults deny rather than open the gate.
        return ApprovalValidation(False, "grant_store_error")

    if row is None:
        return ApprovalValidation(False, "token_not_found")
    if not isinstance(row, Mapping):
        return ApprovalValidation(False, "token_malformed")
    if str(row.get("id") or "") != token:
        return ApprovalValidation(False, "token_not_found")

    live, reason = is_grant_live(
        dict(row),
        capability=capability,
        spend_usd=spend_usd,
    )
    if not live:
        return ApprovalValidation(False, reason or "not_live", dict(row))

    ok, reason = _pins_match(
        row,
        capability=capability,
        action_class=action_class,
        connector=connector,
        tool=tool,
    )
    if not ok:
        return ApprovalValidation(False, reason, dict(row))

    ok, reason = _generation_match(row, generation)
    if not ok:
        return ApprovalValidation(False, reason, dict(row))

    ok, reason = _target_allowed(row, target)
    if not ok:
        return ApprovalValidation(False, reason, dict(row))

    ok, reason = _audience_bound_ok(row, capability=capability, scoped_args=scoped_args)
    if not ok:
        return ApprovalValidation(False, reason, dict(row))

    meta = _metadata(row)
    ok, reason = _scoped_args_match(meta.get("scoped_args"), scoped_args)
    if not ok:
        return ApprovalValidation(False, reason, dict(row))

    return ApprovalValidation(True, "ok", dict(row))


def normalize_grant_ref(
    approval_token: str | None,
    grant_id: str | None,
) -> tuple[str | None, str]:
    """Resolve approval_token / grant_id to one grant reference.

    Returns ``(ref, reason)``. ``reason`` is empty on success. A free-form
    truthy string with no store is still a ref — the store check happens later.
    Conflicting non-equal refs fail closed.
    """
    token = approval_token.strip() if isinstance(approval_token, str) else approval_token
    gid = grant_id.strip() if isinstance(grant_id, str) else grant_id

    if token is not None and not isinstance(token, str):
        return None, "token_malformed"
    if gid is not None and not isinstance(gid, str):
        return None, "token_malformed"

    if isinstance(token, str):
        token = token.strip()
        if not token:
            return None, "token_malformed"
    if isinstance(gid, str):
        gid = gid.strip()
        if not gid:
            return None, "token_malformed"

    if token and gid and token != gid:
        return None, "token_grant_mismatch"
    ref = token or gid
    return (ref if ref else None), ""


__all__ = [
    "ApprovalValidation",
    "normalize_grant_ref",
    "validate_approval_token",
]
