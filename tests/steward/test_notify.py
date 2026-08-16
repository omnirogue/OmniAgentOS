from __future__ import annotations

import inspect
from typing import Any

import httpx
import pytest

from omniagentos.connectors import load_registry
from omniagentos.connectors.broker import BrokerDenied, validate_request
from omniagentos.contracts import ActionClass
from omniagentos.steward import notify


class _Response:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self.payload


def test_slack_missing_config_and_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    assert notify.send_slack("hello").detail == "not configured"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/slack")
    calls: list[tuple[str, dict[str, Any], float]] = []

    def post(url: str, *, json: dict[str, Any], timeout: float, **_: Any) -> _Response:
        calls.append((url, json, timeout))
        return _Response()

    monkeypatch.setattr(notify.httpx, "post", post)
    result = notify.send_slack("hello", timeout=3.0)
    assert result.ok is True and result.channel == "slack"
    assert calls == [("https://hooks.example/slack", {"text": "hello"}, 3.0)]


def test_piedpiper_without_broker_authorization_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIEDPIPER_ACMEUNI_LOCATION_ID", "location")

    def deny(*_: Any, **__: Any) -> dict[str, Any]:
        raise BrokerDenied(
            "requires_human_approval",
            "piedpiper_acmeuni.email_send",
            "email send requires a human approval",
        )

    monkeypatch.setattr(notify.broker, "call", deny)
    result = notify.send_piedpiper_email("to@example.com", "Subject", "<p>Hello</p>")

    assert result == notify.NotifyResult(False, "piedpiper_email", "requires_human_approval")


def test_piedpiper_broker_unavailable_has_no_direct_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIEDPIPER_ACMEUNI_LOCATION_ID", "location")
    attempts = 0

    def unavailable(*_: Any, **__: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("broker unavailable")

    monkeypatch.setattr(notify.broker, "call", unavailable)
    result = notify.send_piedpiper_email("to@example.com", "Subject", "<p>Hello</p>")

    assert attempts == 1
    assert result == notify.NotifyResult(False, "piedpiper_email", "piedpiper_email broker error: RuntimeError")


def test_piedpiper_authorized_two_step_send_uses_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PIEDPIPER_ACMEUNI_LOCATION_ID", "location")
    calls: list[dict[str, Any]] = []

    def call(cap_id: str, granted: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append({"cap_id": cap_id, "granted": granted, **kwargs})
        body = (
            {"contact": {"id": "contact-1"}}
            if kwargs["path"] == "/contacts/upsert"
            else {"messageId": "message-1"}
        )
        return {"ok": True, "status": 200, "body": body}

    monkeypatch.setattr(notify.broker, "call", call)
    result = notify.send_piedpiper_email("to@example.com", "Subject", "<p>Hello</p>", timeout=3.0)

    assert result == notify.NotifyResult(True, "piedpiper_email", "sent", "message-1")
    assert [entry["path"] for entry in calls] == [
        "/contacts/upsert",
        "/conversations/messages",
    ]
    assert all(entry["cap_id"] == "piedpiper_acmeuni.email_send" for entry in calls)
    assert all(entry["granted"] == ["piedpiper_acmeuni.email_send"] for entry in calls)
    assert all(entry["method"] == "POST" and entry["timeout"] == 3.0 for entry in calls)
    assert calls[0]["body"] == {"locationId": "location", "email": "to@example.com"}
    assert calls[1]["body"] == {
        "type": "Email",
        "contactId": "contact-1",
        "subject": "Subject",
        "html": "<p>Hello</p>",
    }


def test_piedpiper_email_has_no_direct_token_or_http_fallback() -> None:
    source = inspect.getsource(notify.send_piedpiper_email)

    assert "PIEDPIPER_ACMEUNI_TOKEN" not in source
    assert "httpx" not in source
    assert "Authorization" not in source
    assert "broker.call" in source


def test_piedpiper_email_registry_is_consequential_and_exactly_scoped() -> None:
    capability = load_registry().capability("piedpiper_acmeuni.email_send")

    assert capability.action_class is ActionClass.CONSEQUENTIAL
    assert capability.http is not None
    assert capability.http.auth == "bearer:PIEDPIPER_ACMEUNI_TOKEN"
    assert capability.http.headers == {"Version": "2021-07-28"}
    validate_request(capability, "POST", "/contacts/upsert")
    validate_request(capability, "POST", "/conversations/messages")
    with pytest.raises(BrokerDenied, match="path_not_allowed"):
        validate_request(capability, "POST", "/contacts/upsert/extra")
    with pytest.raises(BrokerDenied, match="method_not_allowed"):
        validate_request(capability, "GET", "/contacts/upsert")


def test_notify_exceptions_never_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/slack")

    def explode(*_: Any, **__: Any) -> _Response:
        raise RuntimeError("network down")

    monkeypatch.setattr(notify.httpx, "post", explode)
    result = notify.send_slack("hello")
    # An arbitrary exception's own text is never echoed (H7): only a fixed,
    # non-secret-bearing shape (the exception type name) is ever stored.
    assert result.ok is False
    assert "network down" not in result.detail
    assert result.detail == "slack webhook error: RuntimeError"

    monkeypatch.delenv("PIEDPIPER_ACMEUNI_LOCATION_ID", raising=False)
    assert notify.send_piedpiper_email("to@example.com", "Subject", "html").detail == "not configured"


def _raise_for_status(
    url: str, status_code: int, *, headers: dict[str, str] | None = None
) -> httpx.Response:
    """A REAL httpx.Response wired to a REAL httpx.Request for `url` — calling
    ``.raise_for_status()`` on it raises the genuine ``httpx.HTTPStatusError``
    whose ``str()`` embeds the full request URL, exactly as production code
    would see it. Only the network transport is faked; httpx's own exception
    formatting is not.
    """
    request = httpx.Request("POST", url, headers=headers)
    return httpx.Response(status_code, request=request)


def test_slack_non_2xx_error_detail_never_leaks_webhook_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_url = "https://hooks.slack.com/services/T00000/B00000/SUPER-SECRET-WEBHOOK-TOKEN"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", secret_url)

    def post(url: str, **_: Any) -> httpx.Response:
        return _raise_for_status(url, 403)

    monkeypatch.setattr(notify.httpx, "post", post)
    result = notify.send_slack("hello")
    assert result.ok is False
    assert "SUPER-SECRET-WEBHOOK-TOKEN" not in result.detail
    assert secret_url not in result.detail
    assert "hooks.slack.com" not in result.detail
    assert result.detail == "slack webhook error: HTTP 403"


def test_slack_timeout_error_detail_never_leaks_webhook_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_url = "https://hooks.slack.com/services/T00000/B00000/ANOTHER-SECRET-TOKEN"
    monkeypatch.setenv("SLACK_WEBHOOK_URL", secret_url)

    def post(url: str, **_: Any) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out talking to {url}", request=httpx.Request("POST", url))

    monkeypatch.setattr(notify.httpx, "post", post)
    result = notify.send_slack("hello")
    assert result.ok is False
    assert "ANOTHER-SECRET-TOKEN" not in result.detail
    assert secret_url not in result.detail
    assert result.detail == "slack webhook error: timeout"


def test_piedpiper_broker_error_detail_never_leaks_denial_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIEDPIPER_ACMEUNI_LOCATION_ID", "location")

    def deny(*_: Any, **__: Any) -> dict[str, Any]:
        raise BrokerDenied(
            "credential_unavailable",
            "piedpiper_acmeuni.email_send",
            "provider detail containing super-secret-piedpiper-bearer-token",
        )

    monkeypatch.setattr(notify.broker, "call", deny)
    result = notify.send_piedpiper_email("to@example.com", "Subject", "html")
    assert result.ok is False
    assert "super-secret-piedpiper-bearer-token" not in result.detail
    assert result.detail == "credential_unavailable"


def test_piedpiper_send_captures_provider_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The provider's message id must be captured in the structured result,
    not discarded in favour of the literal "sent"."""
    monkeypatch.setenv("PIEDPIPER_ACMEUNI_LOCATION_ID", "location")

    def call(cap_id: str, granted: list[str], **kwargs: Any) -> dict[str, Any]:
        body = (
            {"contact": {"id": "contact-1"}}
            if kwargs["path"] == "/contacts/upsert"
            else {"messageId": "message-1"}
        )
        return {"ok": True, "status": 200, "body": body}

    monkeypatch.setattr(notify.broker, "call", call)
    result = notify.send_piedpiper_email("to@example.com", "Subject", "<p>Hello</p>")

    assert result.provider_id == "message-1"


def test_slack_send_has_no_provider_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slack incoming webhooks return no message identifier — provider_id stays None."""
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.example/slack")

    def post(url: str, *, json: dict[str, Any], timeout: float, **_: Any) -> _Response:
        return _Response()

    monkeypatch.setattr(notify.httpx, "post", post)
    result = notify.send_slack("hello")
    assert result.ok is True
    assert result.provider_id is None


def test_piedpiper_send_with_no_message_id_in_body_leaves_provider_id_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PIEDPIPER_ACMEUNI_LOCATION_ID", "location")

    def call(cap_id: str, granted: list[str], **kwargs: Any) -> dict[str, Any]:
        body = (
            {"contact": {"id": "contact-1"}}
            if kwargs["path"] == "/contacts/upsert"
            else {}
        )
        return {"ok": True, "status": 200, "body": body}

    monkeypatch.setattr(notify.broker, "call", call)
    result = notify.send_piedpiper_email("to@example.com", "Subject", "<p>Hello</p>")
    assert result.ok is True
    assert result.provider_id is None
