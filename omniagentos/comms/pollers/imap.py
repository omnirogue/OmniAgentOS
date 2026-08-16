"""IMAP BODY.PEEK poller (e.g. Gmail with an app password) over SSL.

Each configured mailbox is a named row in ``comms_sources`` whose ``config`` JSON
names the THREE environment variables holding the connection details:
``{"host_env": ..., "user_env": ..., "password_env": ...}`` — never the secrets
themselves. If a source has no explicit config yet, a per-name convention
(``IMAP_HOST_<NAME>``, ``IMAP_USER_<NAME>``, ``IMAP_PASSWORD_<NAME>``) is assumed
and persisted on first run, so operators only need to export three env vars named
after the source (see ``scripts/comms/SETUP.md``).

Fetching uses ``UID FETCH ... (BODY.PEEK[])`` rather than plain ``FETCH (RFC822)``:
the latter has the side effect of setting ``\\Seen`` on the operator's real mailbox
just by us having read it, which is never acceptable for an inbox we don't own.
Progress is tracked via a durable highest-processed-UID cursor persisted in
``config["uid_cursor"]`` (IMAP UIDs, unlike sequence numbers, are stable across
sessions for a given mailbox) — advanced ONLY after each message's
``insert_comms_message`` call actually succeeds, so a transient store error can't
silently lose a message: the next poll re-fetches it (and anything after it in
this cycle) instead of skipping past it. Message-ID dedupe (``insert_comms_message``'s
``(source, external_id)`` uniqueness) remains as belt-and-braces on top of the cursor.
"""

from __future__ import annotations

import imaplib
import logging
import os
from collections.abc import Callable
from typing import Any

from omniagentos.comms.normalize import normalize_imap, parse_rfc822
from omniagentos.contracts import utc_now_iso
from omniagentos.edc.accounts import accounts_map
from omniagentos.edc.ingest import stamp_message_owner
from omniagentos.steward.store import StewardStore

__all__ = ["poll_once"]

logger = logging.getLogger(__name__)

KIND = "imap"
_REQUIRED_KEYS = ("host_env", "user_env", "password_env")
_FETCH_SPEC = "(BODY.PEEK[])"


def _default_config(name: str) -> dict[str, str]:
    slug = name.upper().replace("-", "_").replace(" ", "_")
    return {
        "host_env": f"IMAP_HOST_{slug}",
        "user_env": f"IMAP_USER_{slug}",
        "password_env": f"IMAP_PASSWORD_{slug}",
    }


def _resolve_config(steward: StewardStore, name: str) -> dict[str, Any]:
    sources = {row["name"]: row for row in steward.list_comms_sources()}
    existing = dict((sources.get(name) or {}).get("config") or {})
    for key, value in _default_config(name).items():
        existing.setdefault(key, value)
    return existing


def _stored_uid_cursor(config: dict[str, Any]) -> int:
    try:
        return int(config.get("uid_cursor") or 0)
    except (TypeError, ValueError):
        return 0


def _raw_message_bytes(msg_data: list[Any]) -> bytes | None:
    for part in msg_data:
        if isinstance(part, tuple) and len(part) == 2:
            return part[1]
    return None


def poll_once(
    steward: StewardStore,
    name: str,
    *,
    mailbox: str = "INBOX",
    connection_factory: Callable[[str], Any] = imaplib.IMAP4_SSL,
) -> dict[str, Any]:
    """Poll one IMAP mailbox for messages with UID greater than the durable cursor.

    Never marks anything ``\\Seen`` (``BODY.PEEK[]``); idempotent via both the
    UID cursor and Message-ID dedupe.
    """
    config = _resolve_config(steward, name)
    missing = [key for key in _REQUIRED_KEYS if not os.environ.get(config[key])]
    if missing:
        error = f"missing environment variable: {config[missing[0]]}"
        steward.upsert_comms_source(
            name, KIND, status="pending_setup", config=config, last_error=error
        )
        return {
            "source": name,
            "status": "pending_setup",
            "error": error,
            "fetched": 0,
            "created": 0,
        }

    host = os.environ[config["host_env"]]
    user = os.environ[config["user_env"]]
    password = os.environ[config["password_env"]]

    # Loaded once per poll (not per message): the static edc.accounts map used to
    # stamp per-mailbox identity + owner on each ingested row (review F01 + §8).
    accounts = accounts_map()

    cursor = _stored_uid_cursor(config)
    highest_processed = cursor
    fetched = 0
    created = 0
    # Soft UID FETCH failures (non-OK status / empty or unreadable payload) do
    # not raise — they used to fall through to status=active with last_error=""
    # while the cursor froze on the gap. A permanently unfetchable UID then
    # looked healthy forever (non-result presented as favourable). Track the
    # gap explicitly and report it as error, matching the insert-failure path.
    fetch_gap_error: str | None = None
    try:
        with connection_factory(host) as connection:
            connection.login(user, password)
            connection.select(mailbox)
            status, data = connection.uid("search", None, f"UID {cursor + 1}:*")
            if status != "OK":
                raise RuntimeError(f"IMAP UID SEARCH failed: {status}")
            raw_uids = data[0].split() if data and data[0] else []
            uids = sorted({int(raw) for raw in raw_uids if raw.strip()})
            for uid in uids:
                if uid <= cursor:
                    # Defensive: some servers' `N:*` UID ranges are inclusive of
                    # boundary values in surprising ways; never re-process what
                    # the cursor already covers.
                    continue
                status, msg_data = connection.uid("fetch", str(uid), _FETCH_SPEC)
                if status != "OK" or not msg_data:
                    # Freeze the cursor at the FIRST gap (don't `continue` past a
                    # transiently-unfetchable UID): advancing over it would lose it
                    # permanently. Stop the cycle; `UID {cursor+1}:*` re-fetches it
                    # next poll, matching the insert-failure semantics below.
                    # Do NOT claim active/healthy — the measure failed.
                    fetch_gap_error = f"imap fetch gap at UID {uid}: status={status!r}"
                    break
                raw_bytes = _raw_message_bytes(msg_data)
                if raw_bytes is None:
                    fetch_gap_error = f"imap fetch gap at UID {uid}: unreadable response"
                    break
                fetched += 1
                message = normalize_imap(parse_rfc822(raw_bytes))
                # Stamp per-mailbox source identity + owner BEFORE insert (F01 +
                # §8). Per-mailbox `source` keeps the (source, external_id) dedupe
                # from collapsing two owners' mail with a shared Message-ID.
                message, _mapped = stamp_message_owner(message, name, accounts)
                # The cursor only advances past this UID once the row is
                # durably stored below — if insert_comms_message raises (e.g. a
                # transient DB error), `highest_processed` freezes here and the
                # next poll's `UID {cursor + 1}:*` search re-fetches this exact
                # message instead of silently skipping it.
                _row, was_created = steward.insert_comms_message(message)
                if was_created:
                    created += 1
                highest_processed = uid
    except Exception as exc:
        # Sanitize like the telegram/slack pollers: store only the exception TYPE,
        # never str(exc), so no credential (IMAP password / connection URI) can
        # reach last_error -> the /comms API -> the dashboard.
        error = f"imap poll failed: {type(exc).__name__}"
        logger.warning(error)
        steward.upsert_comms_source(
            name,
            KIND,
            status="error",
            config={**config, "uid_cursor": highest_processed},
            last_error=error,
        )
        return {
            "source": name,
            "status": "error",
            "error": error,
            "fetched": fetched,
            "created": created,
        }

    if fetch_gap_error is not None:
        logger.warning(fetch_gap_error)
        steward.upsert_comms_source(
            name,
            KIND,
            status="error",
            config={**config, "uid_cursor": highest_processed},
            last_error=fetch_gap_error,
        )
        return {
            "source": name,
            "status": "error",
            "error": fetch_gap_error,
            "fetched": fetched,
            "created": created,
        }

    steward.upsert_comms_source(
        name,
        KIND,
        status="active",
        config={**config, "uid_cursor": highest_processed},
        last_poll_at=utc_now_iso(),
        last_error="",
    )
    return {"source": name, "status": "active", "error": "", "fetched": fetched, "created": created}
