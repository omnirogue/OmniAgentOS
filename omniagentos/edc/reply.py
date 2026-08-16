"""Draft-only reply helpers. Draft authority is never send authority."""

from __future__ import annotations

from email.utils import parseaddr
from typing import Any, Protocol

from omniagentos.contracts import utc_now_iso
from omniagentos.edc.actions import draft_sha256
from omniagentos.edc.store import DecisionStore
from omniagentos.steward.quoting import quote_untrusted


class _JsonClient(Protocol):
    def complete_json(
        self,
        messages: list[dict[str, str]],
        required_keys: list[str],
        *,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        purpose: str = "default",
    ) -> dict[str, Any]: ...


def create_reply_draft(
    store: DecisionStore,
    decision: dict[str, Any],
    *,
    actor: str,
    intent: str,
    llm_client: _JsonClient | None = None,
    note: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Use the short-call client to create and persist a non-sendable draft."""
    client: _JsonClient
    if llm_client is None:
        from omniagentos.llm.client import ShortCallClient

        # The stable key is carried in the prompt/ledger purpose context. The
        # operation is draft-only; retries cannot send or gain authority (F12).
        client = ShortCallClient()
    else:
        client = llm_client
    key = f"edc-draft:{decision['id']}:{actor}"
    context = quote_untrusted(
        f"Subject: {decision.get('title') or ''}\n{decision.get('context') or ''}",
        source=f"edc:{decision['id']}",
    )
    result = client.complete_json(
        [
            {
                "role": "system",
                "content": (
                    "Draft an email reply with subject and body only. Never send it. "
                    f"Idempotency key: {key}"
                ),
            },
            {"role": "user", "content": f"Owner intent: {intent}\n\n{context}"},
        ],
        required_keys=["subject", "body"],
        purpose="edc_draft_reply",
        temperature=0.2,
        max_tokens=1000,
    )
    recipient = parseaddr(str(decision.get("counterparty") or ""))[1]
    if not recipient or "@" not in recipient:
        raise ValueError("reply recipient is missing or invalid")
    draft: dict[str, Any] = {
        "to": recipient,
        "subject": str(result.get("subject") or "").strip(),
        "body": str(result.get("body") or "").strip(),
        "idempotency_key": key,
        "created_at": utc_now_iso(),
    }
    if not draft["subject"] or not draft["body"]:
        raise ValueError("reply drafter returned an empty subject or body")
    draft["sha256"] = draft_sha256(draft)
    draft["approved_sha256"] = None
    draft["approved_at"] = None
    return store.resolve(
        decision["id"],
        actor=actor,
        resolution="reply",
        note=note,
        tags=tags,
        params={"draft": draft},
    )


def edit_reply_draft(
    store: DecisionStore,
    decision: dict[str, Any],
    *,
    actor: str,
    subject: str,
    body: str,
    note: str | None = None,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    """Owner edit creates a new SHA and always voids the prior approval."""
    draft = dict(decision.get("draft") or {})
    draft.update({"subject": subject.strip(), "body": body.strip()})
    if not draft.get("to") or not draft["subject"] or not draft["body"]:
        raise ValueError("draft edit requires recipient, subject, and body")
    draft["sha256"] = draft_sha256(draft)
    draft["approved_sha256"] = None
    draft["approved_at"] = None
    return store.resolve(
        decision["id"],
        actor=actor,
        resolution="edit",
        note=note,
        tags=tags,
        params={"draft": draft},
    )


__all__ = ["create_reply_draft", "edit_reply_draft"]
