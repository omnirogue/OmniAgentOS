"""Request-body extractors: the field-level boundary the path allowlist cannot see.

WHY THIS EXISTS -- the allowlist in ``configs/connectors.yaml`` bounds WHERE a
capability call can go (method + path), but it cannot see WHAT the call carries.
Two gaps follow, and both close here:

    * Recipients. A send capability (``gmail*.send``, ``piedpiper_*.conversation_send``,
      ``customerio_*.trigger_broadcast``) is only as safe as the audience it
      reaches. The grant layer needs the recipient surface -- who, and how many --
      pulled out of the request body before the call is approved or audited.

    * Shared path shapes. Meta object update is a bare ``POST /{object-id}``, so
      ``meta_*.launch`` and ``meta_*.budget_change`` have IDENTICAL method+path
      shape and no path allowlist can ever tell them apart. The boundary between
      "flip a campaign live" and "set its budget" is FIELD-level, and it lives in
      this module: launch may set only ``{status}``, budget_change only
      ``{daily_budget, lifetime_budget, bid_amount}``, and any other body key is
      refused with ``BrokerDenied("body_field_not_allowed")`` before the request
      is ever issued. Widening either set is a reviewable code change here, not
      a config edit.

THE CONTRACT (``broker.call`` wires this in immediately after validate_request):

    * Unknown capability -> empty :class:`CallBounds`. Extraction never blocks a
      call the registry already allows; it only OBSERVES -- except the strict
      meta extractors above, which are fail-closed BY DESIGN because there the
      field set IS the security boundary.
    * Parse failures -> empty fields, NEVER an exception. In particular an
      undecodable gmail MIME yields EMPTY recipients: that means "unknown", and
      the grant layer must treat unknown recipients as outside any allowed target
      set -- absence of evidence is not evidence of safety.
    * Bounds are facts ABOUT the request, not the request: recipient addresses,
      counts, a spend figure, object ids, and the names of mutated fields. The
      message text itself is never extracted, so bounds are safe to render into
      a result dict or an audit payload.
"""

from __future__ import annotations

import base64
import email
import email.utils
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from omniagentos.connectors.broker import BrokerDenied


@dataclass(frozen=True)
class CallBounds:
    """The bounded facts a request body states about one capability call.

    Everything defaults to empty/None so an unknown capability or an unparseable
    body yields a truthful "we know nothing" instead of an error.
    """

    recipients: tuple[str, ...] = ()
    recipient_count: int = 0
    spend_usd: float | None = None
    account_id: str | None = None
    resource_ids: tuple[str, ...] = ()
    mutates_fields: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe rendering for broker.call's result and the audit trail."""
        return {
            "recipients": list(self.recipients),
            "recipient_count": self.recipient_count,
            "spend_usd": self.spend_usd,
            "account_id": self.account_id,
            "resource_ids": list(self.resource_ids),
            "mutates_fields": list(self.mutates_fields),
        }


def is_broadcast_capability(cap_id: str) -> bool:
    """True for the three capability shapes whose body carries a real audience.

    ``gmail*.send``, ``piedpiper*.conversation_send``, and ``customerio*.trigger_broadcast``
    are the capability shapes :func:`extract_call_bounds` pulls real recipient
    data out of -- this predicate MUST stay in lockstep with the dispatch at the
    top of that function (both accept the bare connector name AND any
    ``<connector>_<suffix>`` form; a bare/prefixed asymmetry here would let a
    differently-shaped capability id -- e.g. the real, currently-registered
    ``customerio.trigger_broadcast`` -- classify as non-broadcast and skip the
    audience bound entirely). The grant layer (``grants/store.py``,
    ``grants/validation.py``) uses it to decide which campaign grants require an
    audience bound (``max_recipients`` + an approval-time snapshot hash) and
    which capabilities keep the older, unbounded ``target_set`` semantics
    unchanged. Kept as a narrow, explicit allow-list, never a heuristic: a
    capability missing here would mint an unbounded customer-message grant, and
    the meta strict-field capabilities (which mutate campaign objects, not
    audiences) are deliberately excluded.
    """
    connector, sep, action = cap_id.partition(".")
    if not sep:
        return False
    if action == "send" and (connector == "gmail" or connector.startswith("gmail_")):
        return True
    if action == "conversation_send" and (connector == "piedpiper" or connector.startswith("piedpiper_")):
        return True
    return action == "trigger_broadcast" and (
        connector == "customerio" or connector.startswith("customerio_")
    )


def audience_snapshot_hash(recipients: Iterable[str]) -> str:
    """Deterministic, order-independent content hash of a recipient set.

    Recorded on a campaign grant at approval time (the audience snapshot,
    ``grants/store.py``) and recomputed from the live outbound recipients at
    send time (``grants/validation.py``) so a recipient-set substitution is
    detectable as a single string mismatch rather than a fragile list diff.
    Values are stringified, deduplicated, and sorted before hashing, so hash
    equality is exactly set equality -- independent of call order, repeats, or
    dict-vs-list shape drift between mint time and send time.

    Encoding is length-prefixed (``"<len>:<value>|"`` per item, netstring-
    style) rather than separator-joined: a plain separator join would let two
    DIFFERENT recipient sets hash equal whenever a recipient value happens to
    contain the separator byte (e.g. ``{"a\\x1fb"}`` vs ``{"a", "b"}``). A
    length prefix makes the boundary between items unambiguous regardless of
    what bytes an individual recipient contains, so hash equality can never be
    forced by a separator-collision in attacker-influenced recipient data.
    """
    normalized = sorted({str(item) for item in recipients if item})
    encoded = "".join(f"{len(item)}:{item}|" for item in normalized)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def extract_call_bounds(
    cap_id: str,
    method: str,
    path: str,
    body: Any,
    form: Any = None,
) -> CallBounds:
    """Pull the bounded fields out of one capability call's request body.

    Dispatches on the capability id. An unrecognised capability returns empty
    bounds and never blocks; the meta strict-field extractors are the ONLY
    arm allowed to raise, and they raise BrokerDenied -- a refusal, not a crash.
    ``method`` is part of the contract for future extractors whose bounds depend
    on it (e.g. PUT-vs-POST semantics); no current extractor needs it.

    Each of the three send/broadcast branches accepts both the bare connector
    name and any ``<connector>_<suffix>`` form, in lockstep with
    :func:`is_broadcast_capability` -- an asymmetry here would classify a
    capability as broadcast-capable (requiring an audience bound to mint) while
    never actually extracting its recipients, permanently refusing every send
    under it.
    """
    connector, _, action = cap_id.partition(".")

    if action == "send" and (connector == "gmail" or connector.startswith("gmail_")):
        return _gmail_send_bounds(body)
    if action == "conversation_send" and (connector == "piedpiper" or connector.startswith("piedpiper_")):
        return _piedpiper_conversation_send_bounds(body)
    if action == "trigger_broadcast" and (
        connector == "customerio" or connector.startswith("customerio_")
    ):
        return _customerio_trigger_broadcast_bounds(body)
    if connector.startswith("meta_"):
        if action == "launch":
            fields = _meta_strict_fields(cap_id, body, form, _META_LAUNCH_FIELDS)
            return CallBounds(
                account_id=_meta_account_id(path),
                resource_ids=(path.strip("/"),),
                mutates_fields=fields,
            )
        if action == "budget_change":
            fields = _meta_strict_fields(cap_id, body, form, _META_BUDGET_FIELDS)
            return CallBounds(
                spend_usd=_meta_budget_spend_usd(body, form),
                account_id=_meta_account_id(path),
                resource_ids=(path.strip("/"),),
                mutates_fields=fields,
            )
    return CallBounds()


# --- gmail -----------------------------------------------------------------


def _gmail_send_bounds(body: Any) -> CallBounds:
    """To/Cc/Bcc out of a gmail.send body (``{"raw": "<base64url MIME>"}``).

    Best-effort by contract: any decode or parse failure returns EMPTY
    recipients and NEVER raises. Empty recipients means "unknown audience",
    which the grant layer must treat as outside any allowed target set.
    ``threadId`` and every other body key are ignored -- only the recipient
    headers are bounded facts.
    """
    if not isinstance(body, dict):
        return CallBounds()
    raw = body.get("raw")
    if not isinstance(raw, str) or not raw:
        return CallBounds()
    try:
        # Gmail's `raw` is base64url, commonly unpadded -- restore the padding.
        mime_bytes = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        message = email.message_from_bytes(mime_bytes)
        # get_all, NOT get: a MIME may repeat To/Cc/Bcc, and Gmail delivers to
        # every one of them. Reading only the first header hides recipients from
        # the grant target-set check, which is a send-to-anyone bypass.
        # Only non-empty header values: some email versions fail the WHOLE parse
        # when empty Cc/Bcc values are joined into the address list.
        headers = [
            value
            for name in ("to", "cc", "bcc")
            for value in (message.get_all(name) or ())
            if value
        ]
        addresses = tuple(addr for _display_name, addr in email.utils.getaddresses(headers) if addr)
    except Exception:  # noqa: BLE001 -- a send body must never break the broker.
        return CallBounds()
    return CallBounds(recipients=addresses, recipient_count=len(addresses))


# --- piedpiper -------------------------------------------------------------------


def _piedpiper_conversation_send_bounds(body: Any) -> CallBounds:
    """Recipient + mutated field names out of a conversation_send body.

    The body is ``{type, contactId, message, ...}``: the contact being messaged
    is the recipient surface, and the sorted key set records WHICH fields the
    call carried (never their values -- the message text stays out of bounds).
    """
    if not isinstance(body, dict):
        return CallBounds()
    contact_id = body.get("contactId")
    recipients = (str(contact_id),) if contact_id else ()
    return CallBounds(
        recipients=recipients,
        recipient_count=len(recipients),
        mutates_fields=tuple(sorted(str(key) for key in body)),
    )


# --- customer.io -----------------------------------------------------------


def _customerio_trigger_broadcast_bounds(body: Any) -> CallBounds:
    """Audience out of a trigger_broadcast body.

    ``recipients.segment.ids`` names the audience segments being fired at and
    ``emails`` names explicit addresses; both (stringified) are the recipient
    surface. A segment id stands for a whole audience, so recipient_count is a
    LOWER bound on reach -- the grant layer must price that in.
    """
    if not isinstance(body, dict):
        return CallBounds()
    segment_ids: list[str] = []
    recipients_block = body.get("recipients")
    if isinstance(recipients_block, dict):
        segment = recipients_block.get("segment")
        if isinstance(segment, dict):
            ids = segment.get("ids")
            if isinstance(ids, list):
                segment_ids = [str(item) for item in ids]
    emails = body.get("emails")
    email_list = [str(item) for item in emails] if isinstance(emails, list) else []
    recipients = tuple(segment_ids + email_list)
    return CallBounds(recipients=recipients, recipient_count=len(recipients))


# --- meta ------------------------------------------------------------------

# The field-level boundary between meta_*.launch and meta_*.budget_change.
# Both are a bare POST /{object-id}; these sets are the ONLY thing that keeps
# "launch" from setting a budget and "budget_change" from flipping a status.
_META_LAUNCH_FIELDS = frozenset({"status"})
_META_BUDGET_FIELDS = frozenset({"daily_budget", "lifetime_budget", "bid_amount"})


def _meta_strict_fields(
    cap_id: str,
    body: Any,
    form: Any,
    allowed: frozenset[str],
) -> tuple[str, ...]:
    """Field names a meta_* write carries, refusing any outside ``allowed``.

    Fail-closed in both directions: a key outside the allowed set is refused,
    and so is a non-mapping body we cannot inspect at all -- for the strict meta
    boundary "cannot see the fields" means "cannot allow the call". Fields may
    arrive as JSON (body) or form-encoded (form); Meta accepts both.
    """
    keys: set[str] = set()
    for payload in (body, form):
        if payload is None:
            continue
        if not isinstance(payload, dict):
            raise BrokerDenied(
                "body_field_not_allowed",
                cap_id,
                "body is not inspectable as fields; strict field checks need a mapping",
            )
        keys.update(str(key) for key in payload)
    unexpected = keys - allowed
    if unexpected:
        raise BrokerDenied(
            "body_field_not_allowed",
            cap_id,
            f"field(s) {sorted(unexpected)} outside this capability's allowed set "
            f"{sorted(allowed)}",
        )
    return tuple(sorted(keys))


def _meta_account_id(path: str) -> str | None:
    """Ad-account id when the path names one (``/act_<id>/...``), else None.

    Meta ad accounts are env-backed (``*_META_AD_ACCOUNT_IDS``) and canonically
    ``act_<digits>``; the bare digits are returned so the value compares cleanly
    against configured account lists. An object-id path (``POST /{object-id}``)
    carries no account signal -- ownership of an object id cannot be derived
    locally, so None is the truthful answer there.
    """
    first = path.strip("/").split("/", 1)[0]
    if first.startswith("act_") and first[4:]:
        return first[4:]
    return None


def _meta_budget_spend_usd(body: Any, form: Any) -> float | None:
    """The budget a budget_change sets, in USD. None when no budget key is set.

    Meta budgets are MINOR UNITS (cents): 5000 means $50.00, hence the /100.
    daily_budget wins over lifetime_budget when both are present; bid_amount is
    a bid ceiling, not a budget, and does not count as spend. A non-numeric
    value yields None (unknown spend) rather than raising -- the grant layer
    treats unknown spend as outside any spend bound. A NEGATIVE value is
    nonsense as a budget and also yields None: reporting it as real spend would
    let a spend-bounded grant be topped up by consuming negative amounts.
    """
    for payload in (body, form):
        if not isinstance(payload, dict):
            continue
        for key in ("daily_budget", "lifetime_budget"):
            if key in payload:
                try:
                    value = float(payload[key]) / 100.0
                except (TypeError, ValueError):
                    return None
                return value if value >= 0 else None
    return None


__all__ = [
    "CallBounds",
    "audience_snapshot_hash",
    "extract_call_bounds",
    "is_broadcast_capability",
]
