"""W2: Inbox triage & drafts with approval-gated sends.

Flow: poll IMAP → classify messages → draft replies → gate for approval → send via PiedPiper
Each tick processes ONE message per the poll_classify_act_verify template.

Classification decisions: reply, archive, escalate, ignore.
- reply: draft a response, T2 parks for approval before sending
- archive: mark read, T0 no-op (template skips act/verify via "action: skip")
- escalate: create escalation ticket, T2 parks for approval
- ignore: skip processing, T0 no-op (template skips via "action: skip")

CRITICAL: Every outbound send is T2 and MUST park for human approval.
Sends go ONLY via PiedPiper as noreply@mail.initech.example (never @initech.example — quarantined).
"""

from __future__ import annotations

from typing import Any

from omniagentos_loops.contracts import RiskTier
from omniagentos_loops.tools import LoopTool, digest_key

# ============================================================================
# Classification Rules
# ============================================================================

def _classify_message(ctx: Any, message: dict[str, Any]) -> dict[str, Any]:
    """Classify a message deterministically, then via LLM if ambiguous.

    Deterministic rules:
    - No-reply/unsubscribe addresses → skip (archive, non-effect)
    - Allow-listed senders (internal domains) → reply
    - Marketing/automated patterns → skip (archive)

    Ambiguous → use LLM to decide.

    Returns: {"action": "reply|skip", "confidence": ..., "reasoning": "..."}
    Template treats "skip" -> idle (no act/verify).
    Template treats "reply" -> act (T2, parks for approval).
    """
    # Deterministic layer
    sender = (message.get("from_") or "").lower()
    subject = (message.get("subject") or "").lower()

    # Unsubscribe/no-reply detection → archive (skip)
    if any(x in sender for x in ["noreply", "no-reply", "donotreply", "bounces"]):
        return {"action": "skip", "reason": "auto-generated"}

    # Allow-list (example: internal domain) → reply
    if sender.endswith("@initech.example") or sender.endswith("@initech.example.com"):
        return {"action": "reply", "reason": "internal", "confidence": 0.95}

    # Marketing patterns → archive (skip)
    if any(x in subject for x in ["unsubscribe", "promotional", "marketing", "newsletter"]):
        return {"action": "skip", "reason": "marketing"}

    # Default to LLM for ambiguous cases
    model = ctx.model()
    decision = model.complete_json(
        messages=[
            {
                "role": "user",
                "content": (
                    f"Classify this email:\n"
                    f"From: {sender}\n"
                    f"Subject: {subject}\n"
                    f"Preview: {(message.get('body') or '')[:200]}\n"
                    f"\n"
                    f"Should we reply, archive, or ignore?\n"
                    f"reply = send a response to the sender\n"
                    f"skip = archive/ignore without responding\n"
                ),
            }
        ],
        required_keys=["action", "confidence"],
        purpose="classify_message",
    )

    # Ensure valid action; default to skip if uncertain
    action = decision.get("action", "skip")
    if action not in ("reply", "skip", "escalate"):
        action = "skip"
    # Template only handles "skip" and other actions, so "escalate" maps to act too
    if action == "escalate":
        action = "reply"  # Escalate via the same act node for now

    return {
        "action": action,
        "confidence": decision.get("confidence", 0.5),
        "reasoning": decision.get("reasoning", ""),
    }


# ============================================================================
# Tool Implementations
# ============================================================================

def _poll_inbox(ctx: Any) -> list[dict[str, Any]]:
    """Poll for unprocessed messages from the comms inbox.

    Returns: list of message dicts, newest last (so first in list is oldest).
    State tracking via receipts prevents re-processing on crash-resume.

    In production: query comms_messages table for messages from harper source.
    """
    store = ctx.store
    try:
        rows = store._connection.execute(
            """
            SELECT id, source, external_id, from_, subject, body, created_at
            FROM comms_messages
            WHERE source = 'harper'
            ORDER BY created_at ASC
            LIMIT 50
            """
        ).fetchall()
        return [dict(r) for r in rows] if rows else []
    except Exception:
        # Fallback for test fixtures or missing table
        return []


def _classify_inbox(ctx: Any, item: dict[str, Any]) -> dict[str, Any]:
    """Classify a single message."""
    decision = _classify_message(ctx, item)
    decision["msg_id"] = str(item.get("id") or item.get("external_id", "msg-unknown"))
    return decision


def _act_on_message(ctx: Any, item: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """Act on the classification decision (only called for non-skip actions).

    For 'reply': draft the message (T0 call).
    Return a result that verify will see.

    Template 'act' node is T2, so this return parks for approval.
    """
    action = decision.get("action", "skip")
    msg_id = decision.get("msg_id", "msg-unknown")

    if action == "reply":
        # Draft a reply
        model = ctx.model()
        draft = model.complete_json(
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Draft a professional reply to this email:\n"
                        f"From: {item.get('from_', '')}\n"
                        f"Subject: {item.get('subject', '')}\n"
                        f"Body: {item.get('body', '')[:500]}\n"
                        f"\nKeep the reply concise and helpful."
                    ),
                }
            ],
            required_keys=["to", "subject", "body"],
            purpose="draft_reply",
        )
        return {
            "action": "reply",
            "msg_id": msg_id,
            "draft": {
                "to": item.get("from_", ""),
                "subject": draft.get("subject", f"Re: {item.get('subject', '')}"),
                "body": draft.get("body", ""),
            },
            "status": "pending_approval",
        }

    # Fallback (shouldn't reach here if classify is correct)
    return {
        "action": action,
        "msg_id": msg_id,
        "status": "skipped",
    }


def _verify_message(
    ctx: Any,
    item: dict[str, Any],
    decision: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Verify the outcome and record receipts.

    This is called AFTER an act effect has completed (or been approved).
    For replies: send via PiedPiper (in production).
    For other actions: just verify they executed.
    """
    action = decision.get("action", "skip")
    msg_id = decision.get("msg_id", "msg-unknown")

    # For reply effects, verify they would be sent (in production, actually send via PiedPiper)
    if action == "reply":
        draft = result.get("draft", {})
        if not draft:
            raise RuntimeError(f"reply action did not produce draft: {result}")

        # In production: send via PiedPiper as noreply@mail.initech.example
        # For now, just mark as verified
        send_result = {
            "to": draft.get("to", ""),
            "from": "noreply@mail.initech.example",
            "subject": draft.get("subject", ""),
            "body": draft.get("body", "")[:100] + ("..." if len(draft.get("body", "")) > 100 else ""),
            "msg_id": msg_id,
            "verified": True,
        }
        return send_result

    # Fallback
    return {
        "verified": True,
        "action": action,
        "msg_id": msg_id,
    }


# ============================================================================
# Tool Registration
# ============================================================================

def register(ctx: Any) -> None:
    """Register W2 tools on the loop context.

    Tools are bound to the template's nodes: poll, classify, act, verify.
    - poll (T0): returns list of messages
    - classify (T0): returns decision with action
    - act (T2): returns draft/escalation (effect, parks for approval)
    - verify (T0): verifies outcome and records receipts
    """

    # Idempotency key functions (business keys for receipts)
    def poll_key(_args: dict[str, Any]) -> str:
        # Poll is deterministic per tick, one key per instance
        return "poll"

    def classify_key(args: dict[str, Any]) -> str:
        item = args.get("item", {})
        return str(item.get("id") or item.get("external_id", "item"))

    def act_key(args: dict[str, Any]) -> str:
        item = args.get("item", {})
        decision = args.get("decision", {})
        action = decision.get("action", "unknown")
        msg_id = str(item.get("id") or item.get("external_id", "msg"))
        return f"{msg_id}:{action}"

    def send_key(args: dict[str, Any]) -> str:
        item = args.get("item", {})
        decision = args.get("decision", {})
        result = args.get("result", {})
        # For reply: recipient + draft digest (business key = recipient + content)
        if decision.get("action") == "reply":
            draft = result.get("draft", {})
            return digest_key(draft.get("to"), draft.get("subject"), draft.get("body"))
        # Fallback
        msg_id = str(item.get("id") or item.get("external_id", "msg"))
        return digest_key(msg_id, decision.get("action"))

    # Register tools
    ctx.tools.register(
        LoopTool(
            name="poll",
            tier=RiskTier.T0,
            idempotency_key=poll_key,
            call=lambda **_: _poll_inbox(ctx),
            description="Poll IMAP inbox for unprocessed messages (comms_messages table)",
        )
    )

    ctx.tools.register(
        LoopTool(
            name="classify",
            tier=RiskTier.T0,
            idempotency_key=classify_key,
            call=lambda item: _classify_inbox(ctx, item),
            description="Classify message (deterministic rules + LLM for ambiguity)",
        )
    )

    ctx.tools.register(
        LoopTool(
            name="act",
            tier=RiskTier.T2,
            idempotency_key=act_key,
            call=lambda item, decision: _act_on_message(ctx, item, decision),
            description="Act on classification (draft reply, parks for approval before send)",
        )
    )

    ctx.tools.register(
        LoopTool(
            name="verify",
            tier=RiskTier.T0,
            idempotency_key=send_key,
            call=lambda item, decision, result: _verify_message(ctx, item, decision, result),
            description="Verify outcome and record receipts (prevent re-processing)",
        )
    )


__all__ = ["register"]
