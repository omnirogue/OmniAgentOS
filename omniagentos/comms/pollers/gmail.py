"""Gmail poller — reads the operator's real Gmail into ``comms_messages`` for the EDC.

Unlike the Titan mailboxes (which speak IMAP and reach the box directly through
:mod:`omniagentos.comms.pollers.imap`), the Gmail accounts are only reachable
through the CAPABILITY BROKER: an agent never holds the OAuth refresh token, it
names a read-only capability and the broker resolves the credential, enforces the
GET-on-``/gmail/v1/users/me/...`` allowlist, performs the call and returns only
the response body. This poller therefore issues exactly two brokered reads per
new message and NEVER mutates the mailbox (no label/read/delete — Gmail's
``users.messages.get`` does not change ``UNREAD`` the way an IMAP ``RFC822``
fetch would set ``\\Seen``):

1. ``<name>.search``  → ``GET /gmail/v1/users/me/messages?q=<window>`` (ids only)
2. ``<name>.get_message`` → ``GET /gmail/v1/users/me/messages/{id}?format=full``

Each mailbox is its OWN source (``gmail_ownera``, ``gmail_initech``, …); the
poller stamps that per-mailbox ``source`` + the owner from ``edc.accounts`` via
:func:`stamp_message_owner`, so two owners who receive the same ``Message-ID`` can
never collide on ``comms_messages``' ``UNIQUE(source, external_id)`` key (F01).

Durability mirrors the IMAP poller. A monotonic ``internal_date_cursor``
(Gmail ``internalDate``, epoch ms) is persisted in the source config and used as
the ``after:`` search bound, so a steady state is O(new). The cursor advances
ONLY after a message's ``insert_comms_message`` has durably succeeded, and
messages are inserted oldest-first, so a transient store error freezes the cursor
BEFORE the un-stored message (the next poll re-fetches it) instead of skipping it.
Message-ID dedupe on top of the cursor makes a re-fetch a harmless no-op. All
errors are sanitized to the exception TYPE only — never ``str(exc)`` — so no
OAuth material or URL can reach ``last_error`` → the /comms API → the dashboard.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

from omniagentos.comms.normalize import normalize_gmail
from omniagentos.contracts import utc_now_iso
from omniagentos.edc.accounts import accounts_map
from omniagentos.edc.ingest import stamp_message_owner
from omniagentos.steward.store import StewardStore

__all__ = ["poll_once", "KIND"]

logger = logging.getLogger(__name__)

KIND = "gmail"
_SEARCH_PATH = "/gmail/v1/users/me/messages"
_DEFAULT_SINCE_DAYS = 7
#: Hard bound on search pagination so a runaway ``nextPageToken`` loop (or a
#: pathological first-run window) cannot spin forever inside one poll.
_MAX_SEARCH_PAGES = 25


class _Broker(Protocol):
    """The one broker method this poller needs; the real module satisfies it.

    Injected so tests exercise the poller with a fake (no live network) while
    production passes :mod:`omniagentos.connectors.broker`.
    """

    def call(
        self,
        cap_id: str,
        granted: list[str] | None = ...,
        *,
        method: str = ...,
        path: str = ...,
        query: dict[str, Any] | None = ...,
    ) -> dict[str, Any]: ...


class _GmailHttpError(RuntimeError):
    """A brokered Gmail read returned a non-2xx status. Carries only the code.

    The status code is not secret; the response body (which could contain email
    content) never enters the message, and callers render only ``type(exc)``.
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"gmail read returned HTTP {status}")
        self.status = status


def _search_cap(name: str) -> str:
    return f"{name}.search"


def _message_cap(name: str) -> str:
    return f"{name}.get_message"


def _resolve_config(steward: StewardStore, name: str) -> dict[str, Any]:
    sources = {row["name"]: row for row in steward.list_comms_sources()}
    return dict((sources.get(name) or {}).get("config") or {})


def _cursor_ms(config: Mapping[str, Any]) -> int:
    try:
        return int(config.get("internal_date_cursor") or 0)
    except (TypeError, ValueError):
        return 0


def _build_query(cursor_ms: int, since_days: int) -> str:
    """Gmail search window: an ``after:`` epoch once we have a cursor, else a
    bounded ``newer_than`` first-run window so a cold start is not the whole box.

    ``after:`` takes second-granular unix time; the cursor is ms, so a message in
    the boundary second may be re-listed — Message-ID dedupe makes that a no-op,
    which is the safe direction (never skip, only re-see)."""
    if cursor_ms > 0:
        return f"after:{cursor_ms // 1000}"
    return f"newer_than:{max(1, int(since_days))}d"


def _result_body(result: Any) -> dict[str, Any]:
    """Extract the JSON body from a ``broker.call`` result, or raise on failure."""
    if not isinstance(result, Mapping):
        raise _GmailHttpError(0)
    try:
        status = int(result.get("status") or 0)
    except (TypeError, ValueError):
        status = 0
    if not result.get("ok", False) or not (200 <= status < 300):
        raise _GmailHttpError(status)
    body = result.get("body")
    return dict(body) if isinstance(body, Mapping) else {}


def _search_ids(broker: _Broker, cap: str, query: str) -> list[str]:
    """List new message ids for ``query``, following ``nextPageToken``."""
    ids: list[str] = []
    page_token: str | None = None
    for _ in range(_MAX_SEARCH_PAGES):
        params: dict[str, Any] = {"q": query}
        if page_token:
            params["pageToken"] = page_token
        body = _result_body(
            broker.call(cap, granted=[cap], method="GET", path=_SEARCH_PATH, query=params)
        )
        for item in body.get("messages") or []:
            if isinstance(item, Mapping):
                mid = str(item.get("id") or "")
                if mid:
                    ids.append(mid)
        next_token = body.get("nextPageToken")
        if not next_token:
            break
        page_token = str(next_token)
    return ids


def _get_message(broker: _Broker, cap: str, message_id: str) -> dict[str, Any]:
    """Fetch one full message (headers + body). Read-only: never mutates state."""
    return _result_body(
        broker.call(
            cap,
            granted=[cap],
            method="GET",
            path=f"{_SEARCH_PATH}/{message_id}",
            query={"format": "full"},
        )
    )


def _internal_ms(message: Mapping[str, Any]) -> int:
    try:
        return int(message.get("internalDate") or 0)
    except (TypeError, ValueError):
        return 0


def poll_once(
    steward: StewardStore,
    name: str,
    *,
    broker: _Broker | None = None,
    since_days: int = _DEFAULT_SINCE_DAYS,
) -> dict[str, Any]:
    """Poll one Gmail mailbox through the broker for messages after the cursor.

    ``name`` is the per-mailbox source (e.g. ``gmail_ownera``); it selects both
    the broker capabilities (``<name>.search`` / ``<name>.get_message``) and the
    ``edc.accounts`` owner. Read-only and idempotent (durable cursor + Message-ID
    dedupe). ``broker`` defaults to the real credential broker; tests inject a
    fake so nothing touches the network.
    """
    if broker is None:
        # Imported lazily so a fake-broker unit test never drags in the real
        # credential module (and its store/registry imports).
        from omniagentos.connectors import broker as real_broker

        broker = real_broker

    config = _resolve_config(steward, name)
    cursor_ms = _cursor_ms(config)
    highest = cursor_ms
    accounts = accounts_map()
    query = _build_query(cursor_ms, since_days)

    fetched = 0
    created = 0

    # Network phase: list ids, then fetch every message. A failure here (broker
    # denial, transport error, non-2xx) freezes the cursor at its prior value —
    # nothing is inserted, so the next poll re-lists and re-fetches (no loss).
    try:
        message_ids = _search_ids(broker, _search_cap(name), query)
        fetched_messages: list[tuple[int, dict[str, Any]]] = []
        for message_id in message_ids:
            raw_message = _get_message(broker, _message_cap(name), message_id)
            fetched += 1
            fetched_messages.append((_internal_ms(raw_message), raw_message))
        # Insert oldest-first so the cursor only ever moves forward and an insert
        # gap freezes it BEFORE the newer, still-unstored messages.
        fetched_messages.sort(key=lambda item: item[0])
    except Exception as exc:  # noqa: BLE001 -- sanitize: type only, never str(exc).
        return _fail(steward, name, config, highest, exc, fetched, created)

    # Store phase: advance the durable cursor ONLY past a message once its row is
    # committed. A transient store error freezes ``highest`` at the last success.
    try:
        for internal_ms, raw_message in fetched_messages:
            message = normalize_gmail(raw_message)
            # Per-mailbox source identity + owner stamp (F01 + §8) BEFORE insert.
            message, _mapped = stamp_message_owner(message, name, accounts)
            _row, was_created = steward.insert_comms_message(message)
            if was_created:
                created += 1
            if internal_ms > highest:
                highest = internal_ms
    except Exception as exc:  # noqa: BLE001 -- same sanitize discipline as above.
        return _fail(steward, name, config, highest, exc, fetched, created)

    steward.upsert_comms_source(
        name,
        KIND,
        status="active",
        config={**config, "internal_date_cursor": highest},
        last_poll_at=utc_now_iso(),
        last_error="",
    )
    return {"source": name, "status": "active", "error": "", "fetched": fetched, "created": created}


def _fail(
    steward: StewardStore,
    name: str,
    config: dict[str, Any],
    highest: int,
    exc: Exception,
    fetched: int,
    created: int,
) -> dict[str, Any]:
    """Persist an error outcome without ever leaking secret material.

    Only the exception TYPE is recorded (matching the IMAP/Slack/Telegram
    pollers): an OAuth token or a full request URL must never reach
    ``last_error`` → the /comms API → the dashboard.
    """
    error = f"gmail poll failed: {type(exc).__name__}"
    logger.warning(error)
    steward.upsert_comms_source(
        name,
        KIND,
        status="error",
        config={**config, "internal_date_cursor": highest},
        last_error=error,
    )
    return {
        "source": name,
        "status": "error",
        "error": error,
        "fetched": fetched,
        "created": created,
    }
