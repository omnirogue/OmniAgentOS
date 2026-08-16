"""Telegram Bot API poller — long-polls ``getUpdates``.

Credential: ``TELEGRAM_BOT_TOKEN`` (env var name is fixed; the token itself never
appears in code, config files, or logs).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx

from omniagentos.comms.normalize import normalize_telegram
from omniagentos.contracts import utc_now_iso
from omniagentos.steward.store import StewardStore

__all__ = ["poll_once"]

logger = logging.getLogger(__name__)

KIND = "telegram"
TOKEN_ENV = "TELEGRAM_BOT_TOKEN"
_API_BASE = "https://api.telegram.org"

# Matches the token as it appears in the request path (`/bot<digits>:<token>/...`)
# so it can be redacted even if a caller forgets to pass the literal token in.
_BOT_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")


def _sanitize(message: str, token: str | None) -> str:
    """Strip the bot token (literal and pattern-matched) from an error message.

    The token is embedded directly in the ``getUpdates`` request PATH
    (``/bot<token>/getUpdates``), so ANY exception whose text echoes the
    request URL — most notably ``httpx.HTTPStatusError.__str__`` — leaks it.
    This is applied to every error message this module ever stores/logs, not
    just ones we know reference the URL, since a future httpx version or an
    unanticipated exception type could embed it just as easily.
    """
    redacted = message
    if token:
        redacted = redacted.replace(token, "[REDACTED]")
    redacted = _BOT_TOKEN_RE.sub("bot[REDACTED]", redacted)
    return redacted


def _error_detail(exc: Exception) -> str:
    """A fixed, non-URL-bearing shape for the exception — never ``str(exc)``
    for an HTTP-status failure, since that embeds the full request URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Telegram API error (HTTP {exc.response.status_code})"
    if isinstance(exc, httpx.TimeoutException):
        return "Telegram API error (timeout)"
    if isinstance(exc, httpx.RequestError):
        return f"Telegram API error (request failed: {type(exc).__name__})"
    return str(exc)


def _stored_offset(steward: StewardStore, name: str) -> int:
    sources = {row["name"]: row for row in steward.list_comms_sources()}
    config = (sources.get(name) or {}).get("config") or {}
    try:
        return int(config.get("offset") or 0)
    except (TypeError, ValueError):
        return 0


def poll_once(
    steward: StewardStore,
    name: str = "telegram",
    *,
    timeout: int = 25,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    """Fetch one batch of updates via ``getUpdates`` and store each as a comms message."""
    token = os.environ.get(TOKEN_ENV)
    if not token:
        error = f"missing environment variable: {TOKEN_ENV}"
        steward.upsert_comms_source(name, KIND, status="pending_setup", last_error=error)
        return {
            "source": name,
            "status": "pending_setup",
            "error": error,
            "fetched": 0,
            "created": 0,
        }

    offset = _stored_offset(steward, name)
    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout + 10)
    fetched = 0
    created = 0
    try:
        response = http_client.get(
            f"{_API_BASE}/bot{token}/getUpdates",
            params={"offset": offset, "timeout": timeout},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok", False):
            raise RuntimeError(f"Telegram API error: {payload.get('description')}")

        for update in payload.get("result", []):
            fetched += 1
            message = normalize_telegram(update)
            _row, was_created = steward.insert_comms_message(message)
            if was_created:
                created += 1
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                offset = max(offset, update_id + 1)
    except Exception as exc:
        error = _sanitize(f"telegram poll failed: {_error_detail(exc)}", token)
        logger.warning(error)
        steward.upsert_comms_source(name, KIND, status="error", last_error=error)
        return {
            "source": name,
            "status": "error",
            "error": error,
            "fetched": fetched,
            "created": created,
        }
    finally:
        if owns_client:
            http_client.close()

    steward.upsert_comms_source(
        name,
        KIND,
        status="active",
        config={"offset": offset},
        last_poll_at=utc_now_iso(),
        last_error="",
    )
    return {"source": name, "status": "active", "error": "", "fetched": fetched, "created": created}
