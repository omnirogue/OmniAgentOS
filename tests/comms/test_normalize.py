"""Unit tests for every per-source normalizer -> the frozen message dict."""

from __future__ import annotations

from omniagentos.comms.normalize import (
    normalize_imap,
    normalize_slack,
    normalize_telegram,
    normalize_webhook,
    parse_rfc822,
    strip_html,
)

_FROZEN_KEYS = {
    "source",
    "external_id",
    "thread_id",
    "sender",
    "recipients",
    "sent_at",
    "subject",
    "body_text",
    "attachments",
    "raw",
}


def test_strip_html_extracts_visible_text_only() -> None:
    html = "<p>Hello <b>World</b></p><script>alert(1)</script>"
    text = strip_html(html)
    assert "Hello" in text and "World" in text
    # HTMLParser still emits <script> body as data; the point is tags themselves
    # never survive, not that we sanitize script content (that's a rendering
    # concern for whoever displays this text, not a storage concern here).
    assert "<" not in text and ">" not in text


def test_normalize_webhook_prefers_explicit_fields() -> None:
    message = normalize_webhook(
        "zapier",
        {
            "external_id": "ext-1",
            "thread_id": "thread-9",
            "sender": "vip@example.com",
            "recipients": ["owner@example.com"],
            "subject": "Hello",
            "body_text": "Explicit body",
            "attachments": [{"name": "a.pdf"}],
        },
    )
    assert set(message) == _FROZEN_KEYS
    assert message["source"] == "zapier"
    assert message["external_id"] == "ext-1"
    assert message["thread_id"] == "thread-9"
    assert message["sender"] == "vip@example.com"
    assert message["recipients"] == ["owner@example.com"]
    assert message["subject"] == "Hello"
    assert message["body_text"] == "Explicit body"
    assert message["attachments"] == [{"name": "a.pdf"}]
    assert message["raw"]["subject"] == "Hello"


def test_normalize_webhook_falls_back_to_zapier_email_shape() -> None:
    message = normalize_webhook(
        "zapier",
        {
            "from": "a@example.com",
            "to": "b@example.com, c@example.com",
            "subject": "Fallback",
            "body": "plain text body",
        },
    )
    assert message["sender"] == "a@example.com"
    assert message["recipients"] == ["b@example.com", "c@example.com"]
    assert message["body_text"] == "plain text body"


def test_normalize_webhook_strips_html_when_no_plain_body() -> None:
    message = normalize_webhook(
        "zapier", {"from": "a@example.com", "subject": "hi", "html": "<p>Hi <b>there</b></p>"}
    )
    assert "Hi" in message["body_text"] and "there" in message["body_text"]
    assert "<" not in message["body_text"]


def test_normalize_webhook_generates_stable_external_id_when_missing() -> None:
    payload = {"from": "a@example.com", "subject": "s", "body": "b"}
    first = normalize_webhook("zapier", payload)
    second = normalize_webhook("zapier", payload)
    assert first["external_id"] == second["external_id"]
    assert first["external_id"] != ""


def test_normalize_telegram_message() -> None:
    update = {
        "update_id": 42,
        "message": {
            "message_id": 7,
            "date": 1_700_000_000,
            "chat": {"id": -100123},
            "from": {"id": 55, "username": "vip_user"},
            "text": "hello there",
        },
    }
    message = normalize_telegram(update)
    assert set(message) == _FROZEN_KEYS
    assert message["source"] == "telegram"
    assert message["external_id"] == "7"
    assert message["thread_id"] == "-100123"
    assert message["sender"] == "vip_user"
    assert message["body_text"] == "hello there"
    assert message["sent_at"].endswith("Z")


def test_normalize_telegram_falls_back_to_update_id_without_message_id() -> None:
    update = {"update_id": 99, "message": {"chat": {"id": 1}, "text": "x"}}
    message = normalize_telegram(update)
    assert message["external_id"] == "99"


def test_normalize_slack_message_event() -> None:
    event = {"user": "U123", "text": "hi team", "ts": "1700000000.000100"}
    message = normalize_slack(event, channel="C999")
    assert set(message) == _FROZEN_KEYS
    assert message["source"] == "slack"
    assert message["thread_id"] == "C999"
    assert message["external_id"] == "1700000000.000100"
    assert message["sender"] == "U123"
    assert message["body_text"] == "hi team"
    assert message["sent_at"].endswith("Z")


def test_normalize_slack_history_item_uses_embedded_channel_without_override() -> None:
    event = {"channel": "C555", "user": "U1", "text": "hey", "ts": "1700000001.0001"}
    message = normalize_slack(event)
    assert message["thread_id"] == "C555"


def _rfc822(headers: dict[str, str], body: str) -> bytes:
    header_lines = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
    return (header_lines + "\r\n" + body).encode("utf-8")


def test_normalize_imap_plain_text() -> None:
    raw = _rfc822(
        {
            "Message-ID": "<abc123@mail.example.com>",
            "From": "Sender Name <sender@example.com>",
            "To": "owner@example.com",
            "Subject": "Plain email",
            "Date": "Mon, 01 Jan 2024 12:00:00 +0000",
        },
        "Hello from plain text",
    )
    message = normalize_imap(parse_rfc822(raw))
    assert set(message) == _FROZEN_KEYS
    assert message["source"] == "imap"
    assert message["external_id"] == "<abc123@mail.example.com>"
    assert message["thread_id"] == "<abc123@mail.example.com>"
    assert message["sender"] == "Sender Name <sender@example.com>"
    assert message["recipients"] == ["owner@example.com"]
    assert message["subject"] == "Plain email"
    assert "Hello from plain text" in message["body_text"]
    assert message["sent_at"] == "2024-01-01T12:00:00Z"


def test_normalize_imap_threading_uses_references_header() -> None:
    raw = _rfc822(
        {
            "Message-ID": "<reply-2@mail.example.com>",
            "References": "<orig-1@mail.example.com> <mid-1@mail.example.com>",
            "In-Reply-To": "<mid-1@mail.example.com>",
            "From": "a@example.com",
            "Subject": "Re: thread",
        },
        "reply body",
    )
    message = normalize_imap(parse_rfc822(raw))
    assert message["thread_id"] == "<orig-1@mail.example.com>"


def test_normalize_imap_missing_message_id_gets_stable_digest() -> None:
    raw = _rfc822({"From": "a@example.com", "Subject": "no id"}, "body")
    first = normalize_imap(parse_rfc822(raw))
    second = normalize_imap(parse_rfc822(raw))
    assert first["external_id"] == second["external_id"]
    assert first["external_id"] != ""


def test_normalize_imap_multipart_prefers_plain_and_lists_attachments() -> None:
    raw = (
        b"Message-ID: <multi-1@mail.example.com>\r\n"
        b"From: a@example.com\r\n"
        b"Subject: Multipart\r\n"
        b'Content-Type: multipart/mixed; boundary="BOUNDARY"\r\n'
        b"\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Plain part wins\r\n"
        b"--BOUNDARY\r\n"
        b"Content-Type: text/html\r\n\r\n"
        b"<p>HTML part loses</p>\r\n"
        b"--BOUNDARY\r\n"
        b'Content-Type: application/pdf; name="invoice.pdf"\r\n'
        b'Content-Disposition: attachment; filename="invoice.pdf"\r\n\r\n'
        b"%PDF-fake\r\n"
        b"--BOUNDARY--\r\n"
    )
    message = normalize_imap(parse_rfc822(raw))
    assert message["body_text"].strip() == "Plain part wins"
    assert message["attachments"] == [
        {"filename": "invoice.pdf", "content_type": "application/pdf"}
    ]


def test_normalize_imap_multipart_html_only_is_stripped() -> None:
    raw = (
        b"Message-ID: <html-only@mail.example.com>\r\n"
        b"From: a@example.com\r\n"
        b"Subject: HTML only\r\n"
        b'Content-Type: multipart/alternative; boundary="B2"\r\n'
        b"\r\n"
        b"--B2\r\n"
        b"Content-Type: text/html\r\n\r\n"
        b"<p>Only <i>HTML</i> here</p>\r\n"
        b"--B2--\r\n"
    )
    message = normalize_imap(parse_rfc822(raw))
    assert "Only" in message["body_text"] and "HTML" in message["body_text"]
    assert "<" not in message["body_text"]
