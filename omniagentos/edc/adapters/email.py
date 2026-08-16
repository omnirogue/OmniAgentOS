"""Email source adapter — adapter #1 (synthesis §3, P1).

Reads owner-stamped ``comms_messages`` and yields the ones not yet triaged, as
normalized :class:`SourceEvent`s. It performs NO provider I/O — ingestion stays
with the imap poller / broker gmail pollers (which write ``comms_messages`` via
``StewardStore.insert_comms_message``); this adapter only SELECTs.

Two idempotency layers, per the plan:

* **Fast path (F1 watermark).** Per ``(source='email', owner)`` the adapter
  advances ``edc_source_cursor`` to the highest ``comms_messages.id`` it has
  turned into a Decision and reads only ``id > watermark`` (``list_owner_comms_
  after``). Triage cost is O(new messages), never O(history).
* **Backstop (D3).** ``decisions.UNIQUE(source, source_ref, owner)`` makes a
  re-scan of an already-decisioned message a no-op even if the cursor is behind
  (e.g. a crash between the durable write and the cursor advance).

Advancing the watermark is the PIPELINE's job (``edc/main.py``), done only AFTER
a message's Decision row is durably written — the adapter is read-only.
"""

from __future__ import annotations

import re
from typing import Any

from omniagentos.edc.accounts import SourceOwner, accounts_map
from omniagentos.edc.adapters.base import SourceEvent

__all__ = ["EmailAdapter", "event_from_comms_row", "sender_verified_from_headers"]

# OPUS-MAJOR sender-credibility gate. A message may only reach URGENT if its
# sender is authenticated (or on the provider allowlist, checked in classify).
# Here we derive the authentication boolean from the RFC 8601
# ``Authentication-Results`` (and ``ARC-Authentication-Results``) header the
# gmail/imap ingest already preserves in ``raw['headers']``. A DKIM or SPF
# ``pass`` counts ONLY when it is ALIGNED to the From domain — an attacker can
# obtain a DKIM pass for their OWN domain, so an unaligned pass proves nothing
# about a spoofed From. Absent/unparseable headers fail CLOSED to ``False``.

_AUTH_HEADER_NAMES = ("authentication-results", "arc-authentication-results")


def _domain_of(addr: str) -> str:
    """The lowercased domain of an email address (or a bare domain), else ''."""
    text = str(addr or "")
    match = re.search(r"@([A-Za-z0-9.\-]+)", text)
    domain = (match.group(1) if match else text).strip().strip("<>").lower()
    return domain if "." in domain else ""


def _aligned(auth_domain: str, from_domain: str) -> bool:
    """Whether an auth-result domain aligns to the From domain (org-level)."""
    a = auth_domain.strip().lstrip("@").rstrip(".").lower()
    f = from_domain.strip().rstrip(".").lower()
    if not a or not f:
        return False
    # Relaxed alignment: exact, or one is a subdomain of the other
    # (mg.freshworks.com aligns to freshworks.com and vice versa).
    return a == f or f.endswith("." + a) or a.endswith("." + f)


def sender_verified_from_headers(headers: Any, sender: str) -> bool:
    """True iff a DKIM/SPF ``pass`` in the auth headers aligns to the From domain.

    Fail-closed: no headers, no From domain, or no aligned pass ⇒ ``False``.
    """
    if not isinstance(headers, dict) or not headers:
        return False
    from_domain = _domain_of(sender)
    if not from_domain:
        return False
    values = [
        str(value)
        for name, value in headers.items()
        if str(name).strip().lower() in _AUTH_HEADER_NAMES
    ]
    if not values:
        return False
    # Each method result is ';'-separated; within a result the properties
    # (header.d=, header.i=, smtp.mailfrom=) are space-separated on the segment.
    for segment in re.split(r"[;\n]", " ; ".join(values)):
        low = segment.lower()
        if "dkim=pass" in low:
            for match in re.finditer(r"header\.(?:d|i)=@?([A-Za-z0-9.\-]+)", segment, re.I):
                if _aligned(match.group(1), from_domain):
                    return True
        if "spf=pass" in low:
            for match in re.finditer(
                r"smtp\.(?:mailfrom|helo)=@?([A-Za-z0-9.@\-]+)", segment, re.I
            ):
                candidate = _domain_of(match.group(1)) or match.group(1)
                if _aligned(candidate, from_domain):
                    return True
    return False


#: The stable adapter/source name. It is what the Decision row's ``source`` and
#: the ``edc_source_cursor`` are keyed on — NOT the per-mailbox comms ``source``
#: (that per-mailbox name is the F01 ingest identity fix and lands in
#: ``source_account`` instead).
EMAIL_SOURCE = "email"


def event_from_comms_row(
    row: dict[str, Any], accounts: dict[str, SourceOwner]
) -> SourceEvent | None:
    """Map one owner-stamped ``comms_messages`` row to a :class:`SourceEvent`.

    Returns ``None`` when the row is not owner-stamped (an unmapped source is
    skipped loudly upstream; a defensive skip here keeps a stray NULL out of the
    pipeline). Shared with the reclassify pass so both build events identically.
    """
    owner = row.get("owner_employee_id")
    if not owner:
        return None
    mailbox = str(row.get("source") or "")
    account = accounts.get(mailbox)
    company_slug = account.company_slug if account is not None else ""
    source_account = account.source_account if account is not None else mailbox

    metadata: dict[str, Any] = {
        "comms_source": mailbox,
        "thread_id": row.get("thread_id", ""),
        "external_id": row.get("external_id", ""),
        "kb_status": row.get("kb_status"),
    }
    headers: Any = None
    raw = row.get("raw")
    if isinstance(raw, dict):
        headers = raw.get("headers")
        if isinstance(headers, dict):
            metadata["headers"] = headers
            unsubscribe = headers.get("List-Unsubscribe") or headers.get("list-unsubscribe")
            if unsubscribe:
                metadata["list_unsubscribe"] = str(unsubscribe)
        if raw.get("list_unsubscribe"):
            metadata["list_unsubscribe"] = str(raw["list_unsubscribe"])

    sender = str(row.get("sender") or "")
    return SourceEvent(
        source=EMAIL_SOURCE,
        source_ref=str(row.get("id")),
        source_account=source_account,
        owner_employee_id=str(owner),
        company_slug=company_slug,
        occurred_at=str(row.get("sent_at") or row.get("created_at") or ""),
        title=str(row.get("subject") or ""),
        body=str(row.get("body_text") or ""),
        counterparty=sender,
        sender_verified=sender_verified_from_headers(headers, sender),
        metadata=metadata,
    )


class EmailAdapter:
    """Yields undecisioned owner-stamped email as :class:`SourceEvent`s."""

    name = EMAIL_SOURCE

    def __init__(
        self,
        *,
        accounts: dict[str, SourceOwner] | None = None,
        batch_limit: int = 500,
    ) -> None:
        self._accounts = accounts
        self._batch_limit = batch_limit

    def _resolved_accounts(self) -> dict[str, SourceOwner]:
        return self._accounts if self._accounts is not None else accounts_map()

    def pending_events(self, store: Any) -> list[SourceEvent]:
        """New owner-stamped email beyond each owner's watermark, oldest first.

        ``store`` is the :class:`~omniagentos.edc.store.DecisionStore` (for the
        F1 cursor); comms rows are read through a :class:`StewardStore` composed
        over the SAME underlying connection. Ordered ``(owner, id asc)`` so the
        pipeline can advance the watermark monotonically per owner.
        """
        from omniagentos.steward.store import StewardStore

        steward = StewardStore(store._store)
        accounts = self._resolved_accounts()
        owners = sorted({owner.owner_employee_id for owner in accounts.values()})

        events: list[SourceEvent] = []
        for owner in owners:
            cursor = store.get_source_cursor(self.name, owner)
            after_id = _as_int(cursor.get("last_message_id")) if cursor else 0
            rows = steward.list_owner_comms_after(
                owner_employee_id=owner, after_id=after_id, limit=self._batch_limit
            )
            for row in rows:
                event = event_from_comms_row(row, accounts)
                if event is not None:
                    events.append(event)
        return events


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
