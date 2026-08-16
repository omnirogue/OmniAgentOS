"""Per-source normalizers producing the frozen comms message dict.

Every normalizer returns exactly:
    {source, external_id, thread_id, sender, recipients: list, sent_at (ISO),
     subject, body_text, attachments: list, raw: dict}

``raw`` retains the untouched source payload (for audit/debugging); every other
field is derived from it. Normalizers never call an LLM and never touch the
network — they are pure functions over already-received bytes/JSON.
"""

from __future__ import annotations

import base64
import email
import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from omniagentos.contracts import utc_now_iso

__all__ = [
    "normalize_github",
    "normalize_gmail",
    "normalize_imap",
    "normalize_slack",
    "normalize_telegram",
    "normalize_webhook",
    "parse_rfc822",
    "process_github_event",
    "strip_html",
]


class _HTMLTextExtractor(HTMLParser):
    """Minimal, stdlib-only HTML -> text stripper (no third-party dependency)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def strip_html(value: str) -> str:
    """Best-effort HTML -> plain text extraction using only the standard library."""
    parser = _HTMLTextExtractor()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        # Malformed markup must never crash ingestion; fall back to the raw text.
        return value
    text = parser.text()
    # Collapse the run of blank lines HTMLParser tends to leave behind block tags.
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _digest(*parts: str) -> str:
    """Stable content-hash fallback external_id for sources with no natural id."""
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()[:24]


def _message(
    *,
    source: str,
    external_id: str,
    thread_id: str = "",
    sender: str = "",
    recipients: list[str] | None = None,
    sent_at: str | None = None,
    subject: str = "",
    body_text: str = "",
    attachments: list[dict[str, Any]] | None = None,
    raw: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source": source,
        "external_id": external_id,
        "thread_id": thread_id or "",
        "sender": sender or "",
        "recipients": recipients or [],
        "sent_at": sent_at or utc_now_iso(),
        "subject": subject or "",
        "body_text": body_text or "",
        "attachments": attachments or [],
        "raw": raw,
    }


def normalize_webhook(source: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a flexible inbound webhook payload (Zapier or a generic client).

    Explicit frozen-shape fields are preferred; otherwise falls back to the common
    Zapier "email relay" shape: ``{from, to, subject, body|text|html}``. HTML bodies
    are stripped to text when no plain-text field is present.
    """
    body = payload.get("body_text") or payload.get("body") or payload.get("text")
    html = payload.get("html")
    if not body and html:
        body = strip_html(str(html))

    subject = str(payload.get("subject") or "")
    sender = str(payload.get("sender") or payload.get("from") or "")

    recipients_raw = payload.get("recipients") or payload.get("to") or []
    if isinstance(recipients_raw, str):
        recipients = [addr.strip() for addr in recipients_raw.split(",") if addr.strip()]
    elif isinstance(recipients_raw, list):
        recipients = [str(item) for item in recipients_raw]
    else:
        recipients = []

    body_text = str(body or "")
    external_id = str(payload.get("external_id") or payload.get("id") or "").strip()
    if not external_id:
        external_id = _digest(source, sender, subject, body_text)

    sent_at = payload.get("sent_at")
    return _message(
        source=source,
        external_id=external_id,
        thread_id=str(payload.get("thread_id") or ""),
        sender=sender,
        recipients=recipients,
        sent_at=str(sent_at) if sent_at else None,
        subject=subject,
        body_text=body_text,
        attachments=list(payload.get("attachments") or []),
        raw=dict(payload),
    )


def _epoch_to_iso(value: Any) -> str | None:
    if not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def normalize_telegram(update: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a Telegram Bot API ``Update`` (from ``getUpdates``)."""
    message = _as_mapping(
        update.get("message") or update.get("edited_message") or update.get("channel_post")
    )
    chat = _as_mapping(message.get("chat"))
    sender_info = _as_mapping(message.get("from"))

    sender = (
        sender_info.get("username")
        or sender_info.get("first_name")
        or (str(sender_info.get("id")) if sender_info.get("id") is not None else "")
    )
    thread_id = str(chat.get("id")) if chat.get("id") is not None else ""
    external_id = (
        str(message.get("message_id"))
        if message.get("message_id") is not None
        else str(update.get("update_id", ""))
    )
    body_text = str(message.get("text") or message.get("caption") or "")

    attachments: list[dict[str, Any]] = []
    for key in ("document", "voice", "video", "audio", "sticker"):
        item = message.get(key)
        if isinstance(item, Mapping):
            attachments.append(
                {"type": key, "file_id": item.get("file_id"), "file_name": item.get("file_name")}
            )
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        largest = photos[-1]
        if isinstance(largest, Mapping):
            attachments.append({"type": "photo", "file_id": largest.get("file_id")})

    return _message(
        source="telegram",
        external_id=external_id,
        thread_id=thread_id,
        sender=str(sender),
        recipients=[thread_id] if thread_id else [],
        sent_at=_epoch_to_iso(message.get("date")),
        subject="",
        body_text=body_text,
        attachments=attachments,
        raw=dict(update),
    )


def normalize_slack(event: Mapping[str, Any], *, channel: str | None = None) -> dict[str, Any]:
    """Normalize a Slack ``message`` event (Events API) or history item.

    ``conversations.history`` items do not repeat the channel id, so callers pass it
    explicitly; the Events API payload embeds it on the event itself.
    """
    thread_id = str(channel if channel is not None else event.get("channel") or "")
    external_id = str(event.get("ts") or "")
    sender = str(event.get("user") or event.get("username") or event.get("bot_id") or "")
    body_text = str(event.get("text") or "")

    files = event.get("files")
    attachments = [
        {"type": "file", "id": item.get("id"), "name": item.get("name")}
        for item in (files or [])
        if isinstance(item, Mapping)
    ]

    return _message(
        source="slack",
        external_id=external_id,
        thread_id=thread_id,
        sender=sender,
        recipients=[thread_id] if thread_id else [],
        sent_at=_epoch_to_iso(float(external_id)) if _is_float(external_id) else None,
        subject="",
        body_text=body_text,
        attachments=attachments,
        raw=dict(event),
    )


def _is_float(value: str) -> bool:
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def parse_rfc822(raw: bytes) -> Message:
    """Parse RFC822 bytes (an IMAP ``FETCH ... RFC822`` response) into a Message."""
    return email.message_from_bytes(raw)


def _decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes | bytearray):
        value = part.get_payload()
        return value if isinstance(value, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return bytes(payload).decode(charset, errors="replace")
    except (LookupError, UnicodeDecodeError):
        return bytes(payload).decode("utf-8", errors="replace")


def _imap_body_and_attachments(msg: Message) -> tuple[str, list[dict[str, Any]]]:
    plain: str | None = None
    html: str | None = None
    attachments: list[dict[str, Any]] = []

    if msg.is_multipart():
        for part in msg.walk():
            if part.is_multipart():
                continue
            disposition = str(part.get_content_disposition() or "")
            content_type = part.get_content_type()
            if disposition == "attachment" or (
                part.get_filename() and content_type not in ("text/plain", "text/html")
            ):
                attachments.append(
                    {"filename": part.get_filename() or "", "content_type": content_type}
                )
                continue
            if content_type == "text/plain" and plain is None:
                plain = _decode_part(part)
            elif content_type == "text/html" and html is None:
                html = _decode_part(part)
    else:
        content_type = msg.get_content_type()
        if content_type == "text/html":
            html = _decode_part(msg)
        else:
            plain = _decode_part(msg)

    if plain is not None:
        return plain, attachments
    if html is not None:
        return strip_html(html), attachments
    return "", attachments


def normalize_imap(msg: Message) -> dict[str, Any]:
    """Normalize a parsed RFC822 email (``email.message.Message``)."""
    external_id = str(msg.get("Message-ID") or "").strip()
    if not external_id:
        external_id = _digest(
            "imap", str(msg.get("From", "")), str(msg.get("Subject", "")), str(msg.get("Date", ""))
        )

    references = str(msg.get("References") or msg.get("In-Reply-To") or "").strip()
    thread_id = references.split()[0] if references else external_id

    sender = str(msg.get("From") or "")
    to_header = str(msg.get("To") or "")
    recipients = [addr for _, addr in getaddresses([to_header]) if addr]
    subject = str(msg.get("Subject") or "")

    sent_at: str | None = None
    date_header = msg.get("Date")
    if date_header:
        try:
            sent_at = (
                parsedate_to_datetime(str(date_header))
                .astimezone(UTC)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except (TypeError, ValueError):
            sent_at = None

    body_text, attachments = _imap_body_and_attachments(msg)

    return _message(
        source="imap",
        external_id=external_id,
        thread_id=thread_id,
        sender=sender,
        recipients=recipients,
        sent_at=sent_at,
        subject=subject,
        body_text=body_text,
        attachments=attachments,
        raw={"headers": {key: str(value) for key, value in msg.items()}},
    )


# ---------------------------------------------------------------------------
# Gmail REST source adapter (users.messages.get ``format=full`` JSON).
#
# Gmail's ``format=full`` returns a structured JSON envelope (a ``payload`` with
# a ``headers`` array and a MIME ``parts`` tree whose leaf bodies are base64url)
# rather than the RFC822 bytes ``normalize_imap`` parses. The output shape is
# identical to ``normalize_imap`` on purpose: ``external_id`` is the RFC822
# ``Message-ID`` (so the per-mailbox ``(source, external_id)`` dedupe collapses a
# re-fetch but NEVER two owners who share a Message-ID — F01), and ``raw`` keeps
# the header map so the EDC email adapter can still read ``List-Unsubscribe``.
# ---------------------------------------------------------------------------


def _gmail_header_map(payload: Mapping[str, Any]) -> dict[str, str]:
    """Ordered original-case ``header name -> value`` map (first value wins)."""
    out: dict[str, str] = {}
    for header in payload.get("headers") or []:
        if not isinstance(header, Mapping):
            continue
        name = str(header.get("name") or "")
        if name and name not in out:
            out[name] = str(header.get("value") or "")
    return out


def _gmail_header(headers: Mapping[str, str], key: str) -> str:
    """Case-insensitive header lookup (Gmail preserves the sender's casing)."""
    lowered = key.lower()
    for name, value in headers.items():
        if name.lower() == lowered:
            return value
    return ""


def _b64url_decode(data: str) -> bytes:
    """Decode Gmail's URL-safe, unpadded base64 body payloads; never raises."""
    if not data:
        return b""
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded)
    except (ValueError, TypeError):
        # Malformed body must never crash ingestion — treat as empty text.
        return b""


def _gmail_body_and_attachments(payload: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Walk the MIME part tree; prefer text/plain, fall back to stripped HTML."""
    plain: str | None = None
    html: str | None = None
    attachments: list[dict[str, Any]] = []

    def walk(part: Mapping[str, Any]) -> None:
        nonlocal plain, html
        parts = part.get("parts")
        if isinstance(parts, list) and parts:
            for child in parts:
                if isinstance(child, Mapping):
                    walk(child)
            return
        mime = str(part.get("mimeType") or "")
        filename = str(part.get("filename") or "")
        if filename:
            attachments.append({"filename": filename, "content_type": mime})
            return
        body = part.get("body")
        data = str(body.get("data") or "") if isinstance(body, Mapping) else ""
        if not data:
            return
        text = _b64url_decode(data).decode("utf-8", errors="replace")
        if mime == "text/plain" and plain is None:
            plain = text
        elif mime == "text/html" and html is None:
            html = text

    walk(payload)
    if plain is not None:
        return plain, attachments
    if html is not None:
        return strip_html(html), attachments
    return "", attachments


def _gmail_internal_epoch(message: Mapping[str, Any]) -> float | None:
    """Gmail ``internalDate`` is epoch MILLISECONDS as a string → epoch seconds."""
    raw = message.get("internalDate")
    try:
        return int(raw) / 1000.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def normalize_gmail(message: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one Gmail ``users.messages.get`` (``format=full``) JSON message.

    ``external_id`` is the RFC822 ``Message-ID`` (matching ``normalize_imap``) so
    the same physical message keys identically no matter which mailbox fetched
    it. ``source`` is left as the generic ``"gmail"`` here — the poller restamps
    it to the per-mailbox source name via ``edc.ingest.stamp_message_owner``
    before insert, which is what actually satisfies F01.
    """
    payload = _as_mapping(message.get("payload"))
    headers = _gmail_header_map(payload)

    external_id = _gmail_header(headers, "Message-ID").strip()
    if not external_id:
        external_id = _digest(
            "gmail",
            str(message.get("id") or ""),
            _gmail_header(headers, "From"),
            _gmail_header(headers, "Subject"),
        )

    references = (
        _gmail_header(headers, "References") or _gmail_header(headers, "In-Reply-To")
    ).strip()
    thread_id = str(message.get("threadId") or "") or (
        references.split()[0] if references else external_id
    )

    sender = _gmail_header(headers, "From")
    recipients = [addr for _, addr in getaddresses([_gmail_header(headers, "To")]) if addr]
    subject = _gmail_header(headers, "Subject")

    sent_at: str | None = None
    date_header = _gmail_header(headers, "Date")
    if date_header:
        try:
            sent_at = (
                parsedate_to_datetime(date_header)
                .astimezone(UTC)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        except (TypeError, ValueError):
            sent_at = None
    if sent_at is None:
        sent_at = _epoch_to_iso(_gmail_internal_epoch(message))

    body_text, attachments = _gmail_body_and_attachments(payload)

    return _message(
        source="gmail",
        external_id=external_id,
        thread_id=thread_id,
        sender=sender,
        recipients=recipients,
        sent_at=sent_at,
        subject=subject,
        body_text=body_text,
        attachments=attachments,
        raw={"headers": headers},
    )


# ---------------------------------------------------------------------------
# GitHub source adapter (frozen message shape preserved)
# ---------------------------------------------------------------------------

_GITHUB_COMMENT_EVENTS = frozenset(
    {
        "issue_comment",
        "pull_request_review_comment",
        "pull_request_review",
    }
)
_GITHUB_CI_EVENTS = frozenset({"check_run", "workflow_run", "check_suite", "status"})


def normalize_github(
    payload: Mapping[str, Any],
    *,
    event_type: str | None = None,
) -> dict[str, Any] | None:
    """Normalize a GitHub webhook payload into the frozen comms message shape.

    Returns ``None`` for unknown/ignored event types (never raises).
    Supported:
    * issue_comment / pull_request_review_comment — PR comments
    * check_run / workflow_run failures — CI failures
    """
    kind = (event_type or str(payload.get("X-GitHub-Event") or "")).strip()
    if not kind:
        # Infer from payload shape.
        if "comment" in payload and ("issue" in payload or "pull_request" in payload):
            kind = "issue_comment"
        elif "check_run" in payload:
            kind = "check_run"
        elif "workflow_run" in payload:
            kind = "workflow_run"
        elif "pull_request" in payload and "review" in payload:
            kind = "pull_request_review"
        else:
            return None

    kind_l = kind.lower()
    if kind_l not in _GITHUB_COMMENT_EVENTS and kind_l not in _GITHUB_CI_EVENTS:
        return None

    if kind_l in _GITHUB_COMMENT_EVENTS:
        return _normalize_github_comment(payload, kind_l)
    return _normalize_github_ci(payload, kind_l)


def _normalize_github_comment(payload: Mapping[str, Any], kind: str) -> dict[str, Any] | None:
    comment = _as_mapping(payload.get("comment"))
    review = _as_mapping(payload.get("review"))
    body = str(comment.get("body") or review.get("body") or "")
    if not body and kind not in _GITHUB_COMMENT_EVENTS:
        return None
    user = comment.get("user") or review.get("user") or {}
    sender = ""
    if isinstance(user, Mapping):
        sender = str(user.get("login") or "")
    external_id = str(comment.get("id") or review.get("id") or "").strip()
    if not external_id:
        external_id = _digest("github", kind, sender, body)
    pr = _as_mapping(payload.get("pull_request"))
    issue = _as_mapping(payload.get("issue"))
    number = pr.get("number") or issue.get("number") or ""
    repo = _as_mapping(payload.get("repository"))
    repo_name = str(repo.get("full_name") or "")
    subject = f"GitHub {kind} on {repo_name}#{number}".strip()
    thread_id = f"{repo_name}#{number}" if repo_name or number else ""
    head_ref = ""
    head = _as_mapping(pr.get("head"))
    head_ref = str(head.get("ref") or "")
    return _message(
        source="github",
        external_id=str(external_id),
        thread_id=thread_id,
        sender=sender,
        recipients=[],
        sent_at=str(comment.get("created_at") or review.get("submitted_at") or "") or None,
        subject=subject,
        body_text=body,
        attachments=[],
        raw={
            "event_type": kind,
            "payload": dict(payload),
            "head_ref": head_ref,
            "repo": repo_name,
            "number": number,
        },
    )


def _normalize_github_ci(payload: Mapping[str, Any], kind: str) -> dict[str, Any] | None:
    node = payload.get(kind) if isinstance(payload.get(kind), Mapping) else None
    if node is None:
        # check_suite / status may nest differently
        for key in ("check_run", "workflow_run", "check_suite"):
            if isinstance(payload.get(key), Mapping):
                node = payload[key]  # type: ignore[assignment]
                kind = key
                break
    if not isinstance(node, Mapping):
        return None
    conclusion = str(node.get("conclusion") or node.get("state") or "").lower()
    status = str(node.get("status") or "").lower()
    # Only surface failures (or completed non-success).
    failed = conclusion in {"failure", "failed", "timed_out", "cancelled", "error"} or (
        status in {"failure", "error"} and not conclusion
    )
    if not failed and conclusion not in {"", "null", "none"}:
        # Successful CI is ignored for session routing.
        if conclusion in {"success", "neutral", "skipped"}:
            return None
    if not failed and status == "completed" and conclusion == "success":
        return None
    # If still running, ignore.
    if status in {"queued", "in_progress", "pending"} and not failed:
        return None

    name = str(node.get("name") or node.get("display_title") or kind)
    head_branch = str(node.get("head_branch") or node.get("head_ref") or "")
    head_sha = str(node.get("head_sha") or "")[:12]
    repo = _as_mapping(payload.get("repository"))
    repo_name = str(repo.get("full_name") or "")
    external_id = str(node.get("id") or node.get("run_id") or "").strip()
    if not external_id:
        external_id = _digest("github", kind, name, head_sha, conclusion)
    body = f"CI {kind} '{name}' conclusion={conclusion or status} branch={head_branch} sha={head_sha}"
    return _message(
        source="github",
        external_id=str(external_id),
        thread_id=f"{repo_name}@{head_branch or head_sha}",
        sender="github-ci",
        recipients=[],
        sent_at=str(node.get("completed_at") or node.get("updated_at") or "") or None,
        subject=f"GitHub CI failure: {name}",
        body_text=body,
        attachments=[],
        raw={
            "event_type": kind,
            "payload": dict(payload),
            "head_ref": head_branch,
            "head_sha": head_sha,
            "repo": repo_name,
            "conclusion": conclusion,
        },
    )


def process_github_event(
    payload: Mapping[str, Any],
    *,
    event_type: str | None = None,
    known_sessions: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    deliver: Any = None,
) -> dict[str, Any] | None:
    """Normalize + correlate a GitHub event under the mode flag.

    * ``off`` — returns None (inert).
    * ``shadow`` — normalizes + correlates, logs target, does **not** deliver.
    * ``enforce`` — normalizes + correlates and calls ``deliver(message, correlation)``
      when provided.

    Never raises on unknown event types.
    """
    from omniagentos.comms.correlation import attribute_github_event
    from omniagentos.comms.github_mode import github_comms_enforcing, github_comms_mode

    mode = github_comms_mode(env)
    if mode == "off":
        return None
    message = normalize_github(payload, event_type=event_type)
    if message is None:
        return None
    # Build a correlation view from message raw + body.
    corr_payload: dict[str, Any] = dict(message.get("raw") or {})
    corr_payload["body"] = message.get("body_text")
    corr_payload["head_ref"] = (message.get("raw") or {}).get("head_ref")
    if isinstance(payload.get("pull_request"), Mapping):
        corr_payload["pull_request"] = payload["pull_request"]
    if isinstance(payload.get("comment"), Mapping):
        corr_payload["comment"] = payload["comment"]
    correlation = attribute_github_event(corr_payload, known_sessions=known_sessions)
    result = {
        "message": message,
        "correlation": correlation.as_dict(),
        "mode": mode,
        "delivered": False,
    }
    if mode == "shadow":
        return result
    if github_comms_enforcing(env) and callable(deliver):
        deliver(message, correlation)
        result["delivered"] = True
    return result

