"""Best-effort outbound notification helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx

from omniagentos.connectors import broker

_PIEDPIPER_EMAIL_CAPABILITY = "piedpiper_acmeuni.email_send"


@dataclass(frozen=True, slots=True)
class NotifyResult:
    """Result of a best-effort notification send.

    ``provider_id`` carries the provider-assigned message id for this send,
    when the provider's response carries one (e.g. PiedPiper's ``messageId``).
    ``None`` when the channel has no such id (Slack incoming webhooks return
    no message identifier) or the send failed before a provider id was ever
    issued. Callers that persist delivery results (``deliveries_json`` etc.)
    MUST use this field rather than inferring an id from ``detail``, which
    only ever holds a human-readable status string.
    """

    ok: bool
    channel: str
    detail: str
    provider_id: str | None = None


def _sanitized_detail(channel: str, exc: Exception) -> str:
    """Build an error detail that can never contain a secret URL/token.

    ``channel``'s own credential (the Slack webhook URL, or the PiedPiper bearer
    token echoed back into an httpx-formatted URL/message) MUST NEVER be
    persisted or logged: ``NotifyResult.detail`` flows into
    ``deliveries_json`` -> the vault briefing note -> the ``/briefings`` API,
    and ``str(exc)`` for an ``httpx.HTTPStatusError`` embeds the full request
    URL (``"... for url '<secret-bearing-url>'"``) verbatim. So this NEVER
    calls ``str(exc)``: only a fixed, non-secret-bearing shape (status code or
    exception type name) is returned, regardless of exception type — a raw
    connection error, timeout, or any future httpx internals change is just as
    safe as an HTTPStatusError.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"{channel} webhook error: HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return f"{channel} webhook error: timeout"
    if isinstance(exc, httpx.RequestError):
        return f"{channel} webhook error: request failed ({type(exc).__name__})"
    return f"{channel} webhook error: {type(exc).__name__}"


def send_slack(
    text: str, *, webhook_env: str = "SLACK_WEBHOOK_URL", timeout: float = 10.0
) -> NotifyResult:
    """Post a Slack webhook message without allowing delivery errors to escape.

    The webhook URL IS the secret (anyone holding it can post as this
    integration); see :func:`_sanitized_detail` for why the error detail never
    contains it.
    """
    webhook_url = os.environ.get(webhook_env)
    if not webhook_url:
        return NotifyResult(False, "slack", "not configured")
    try:
        response = httpx.post(webhook_url, json={"text": text}, timeout=timeout)
        response.raise_for_status()
        return NotifyResult(True, "slack", "sent")
    except Exception as exc:
        return NotifyResult(False, "slack", _sanitized_detail("slack", exc))


def send_piedpiper_email(
    to_email: str, subject: str, html: str, *, timeout: float = 20.0
) -> NotifyResult:
    """Upsert a PiedPiper contact and send email through the always-human broker gate."""
    location_id = os.environ.get("PIEDPIPER_ACMEUNI_LOCATION_ID")
    if not location_id:
        return NotifyResult(False, "piedpiper_email", "not configured")

    try:
        contact_response = broker.call(
            _PIEDPIPER_EMAIL_CAPABILITY,
            [_PIEDPIPER_EMAIL_CAPABILITY],
            method="POST",
            path="/contacts/upsert",
            body={"locationId": location_id, "email": to_email},
            timeout=timeout,
        )
        if not contact_response.get("ok"):
            status = int(contact_response.get("status") or 0)
            detail = (
                f"piedpiper_email broker error: HTTP {status}" if status else "piedpiper_email broker error"
            )
            return NotifyResult(False, "piedpiper_email", detail)

        payload: Any = contact_response.get("body")
        contact = payload.get("contact") if isinstance(payload, dict) else None
        contact_id = contact.get("id") if isinstance(contact, dict) else None
        if not contact_id:
            return NotifyResult(False, "piedpiper_email", "contact upsert returned no contact id")

        message_response = broker.call(
            _PIEDPIPER_EMAIL_CAPABILITY,
            [_PIEDPIPER_EMAIL_CAPABILITY],
            method="POST",
            path="/conversations/messages",
            body={
                "type": "Email",
                "contactId": contact_id,
                "subject": subject,
                "html": html,
            },
            timeout=timeout,
        )
        if not message_response.get("ok"):
            status = int(message_response.get("status") or 0)
            detail = (
                f"piedpiper_email broker error: HTTP {status}" if status else "piedpiper_email broker error"
            )
            return NotifyResult(False, "piedpiper_email", detail)
        message_body: Any = message_response.get("body")
        provider_id = None
        if isinstance(message_body, dict):
            raw_id = message_body.get("messageId") or message_body.get("id")
            provider_id = str(raw_id) if raw_id else None
        return NotifyResult(True, "piedpiper_email", "sent", provider_id)
    except broker.BrokerDenied as exc:
        return NotifyResult(False, "piedpiper_email", exc.reason)
    except Exception as exc:
        return NotifyResult(False, "piedpiper_email", f"piedpiper_email broker error: {type(exc).__name__}")


__all__ = ["NotifyResult", "send_piedpiper_email", "send_slack"]
