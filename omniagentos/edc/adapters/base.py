"""The source-adapter boundary — the entire future-sources surface (synthesis §3).

A ``SourceEvent`` is a normalized, owner-stamped record ready for classification;
a ``SourceAdapter`` yields the pending events a source has produced since the
last triage. The pipeline is adapter-agnostic: it never imports a concrete
adapter, and adding a source (slack, billing, agent, …) is a new adapter plus a
row in the account map — zero core change. Email is adapter #1 (a SELECT over
owner-stamped ``comms_messages``, built in a later phase); this ~20-line contract
is deliberately the only speculative investment made up front.
"""

from __future__ import annotations

from typing import Any, Protocol, TypedDict, runtime_checkable


class SourceEvent(TypedDict):
    """One normalized, owner-stamped source record awaiting classification.

    ``owner_employee_id``/``company_slug`` are stamped by the ADAPTER from the
    account map — never guessed downstream. ``source_ref`` is the stable id
    within the source (``comms_messages.id`` for email); together with
    ``source`` and the owner it is the idempotency key on ``decisions``.
    """

    source: str  # 'email' | 'rule_proposal' | future 'slack','billing','agent'
    source_ref: str  # stable id within the source (e.g. comms_messages.id)
    source_account: str  # e.g. 'gmail_ownera'
    owner_employee_id: str
    company_slug: str
    occurred_at: str
    title: str
    body: str
    counterparty: str
    # Sender authentication result, derived by the adapter from the source's own
    # trust signals (email: DKIM/SPF alignment in Authentication-Results). Fail
    # CLOSED to ``False`` when the source cannot prove the sender — an unverified
    # sender can never reach URGENT on wording alone. Optional for non-email
    # sources / older callers; classify reads it via ``.get(...)`` defaulting False.
    sender_verified: bool
    metadata: dict[str, Any]


@runtime_checkable
class SourceAdapter(Protocol):
    """Yields the events a source has produced that are not yet decisions.

    ``store`` is the DAL the adapter reads from (kept untyped here to avoid a
    core→concrete-store import in the boundary module). Implementations MUST be
    read-only over the source and MUST NOT perform provider I/O — ingestion is
    someone else's job (the imap poller / broker gmail pollers for email).
    """

    name: str

    def pending_events(self, store: Any) -> list[SourceEvent]: ...


__all__ = ["SourceAdapter", "SourceEvent"]
