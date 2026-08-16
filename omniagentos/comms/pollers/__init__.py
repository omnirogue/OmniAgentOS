"""Source pollers — Telegram, Slack, and IMAP — each writing through StewardStore.

Every poller is idempotent (dedupe on ``(source, external_id)`` in
``StewardStore.insert_comms_message``) and NEVER crashes on missing credentials:
a missing env var flips the source to ``pending_setup`` with a ``last_error``
naming the exact variable, and the poller returns normally (exit 0 at the CLI).
"""

from __future__ import annotations

__all__: list[str] = []
