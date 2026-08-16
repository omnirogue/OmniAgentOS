"""Owner + per-mailbox identity stamping for comms ingest (EDC tenancy).

Two jobs, both at the moment a poller hands a normalized message to
``StewardStore.insert_comms_message``:

1. **Per-mailbox source identity (review F01 — a privacy/loss BLOCKER).**
   ``comms_messages`` dedupes on ``UNIQUE(source, external_id)`` and
   ``normalize_imap`` hard-codes ``source='imap'`` for EVERY mailbox. Two owners
   who receive mail with the same ``Message-ID`` (a forwarded thread, a shared
   list, the same message to two mailboxes) therefore collide on that key: the
   second ``INSERT OR IGNORE`` no-ops and that owner SILENTLY LOSES the row while
   the poller's cursor advances past it. The fix is at the identity level —
   stamp the per-mailbox source NAME (e.g. ``gmail_ownera``) so the dedupe key
   is per-mailbox, and since distinct mailboxes map to distinct owners it is
   per-owner. Nothing keys behavior off the literal ``'imap'`` string.

2. **Owner stamping (synthesis §8).** Resolve the source's owner from the static
   ``edc.accounts`` map and stamp ``owner_employee_id``. An UNMAPPED source is
   left owner-NULL on purpose — EDC skips it loudly, never guesses.
"""

from __future__ import annotations

import logging
from typing import Any

from omniagentos.edc.accounts import SourceOwner, accounts_map, owner_for_source

logger = logging.getLogger(__name__)

__all__ = ["stamp_message_owner"]


def stamp_message_owner(
    message: dict[str, Any],
    source_name: str,
    accounts: dict[str, SourceOwner] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Return ``(stamped_message, mapped)`` for one normalized message.

    ``source_name`` is the per-mailbox comms source (the poller's ``name``). The
    returned message carries ``source = source_name`` (the F01 identity fix) and,
    when the source is in the account map, ``owner_employee_id`` +
    ``source_account``. ``mapped`` is False for an unmapped source — the message
    is still ingested (owner NULL) so no mail is lost, but EDC will skip it.
    """
    table = accounts if accounts is not None else accounts_map()
    stamped = dict(message)
    # F01: per-mailbox identity so the (source, external_id) dedupe never
    # collapses two owners' mail with a shared Message-ID.
    stamped["source"] = source_name

    owner: SourceOwner | None = owner_for_source(source_name, table)
    if owner is None:
        logger.info(
            "edc.ingest: source %r has no owner in edc.accounts; leaving owner NULL", source_name
        )
        return stamped, False

    stamped["owner_employee_id"] = owner.owner_employee_id
    return stamped, True
