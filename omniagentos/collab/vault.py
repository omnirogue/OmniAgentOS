"""Vault rendering helpers for collaboration conversation logs.

The curator (L08) calls these formatters; this module does not write notes itself.
"""

from __future__ import annotations

from typing import Any

from omniagentos.collab.contracts import conversation_digest


def render_conversation_log(
    channel: dict[str, Any], messages: list[dict[str, Any]]
) -> tuple[str, str]:
    """Return (title, body) for a vault note rendering a channel's conversation.

    Body is a markdown note with channel name, member list, and digested messages.
    """
    name = str(channel.get("name") or channel.get("id") or "channel")
    kind = str(channel.get("kind") or "")
    channel_id = str(channel.get("id") or "")
    members = channel.get("members") or []
    if isinstance(members, list):
        member_list = ", ".join(str(m) for m in members) if members else "(none)"
    else:
        member_list = str(members)

    title = f"Conversation: {name}"
    digest = conversation_digest(messages)
    if not digest:
        digest = "_(no messages)_"

    body_lines = [
        f"# {title}",
        "",
        f"**Channel ID:** `{channel_id}`",
        f"**Name:** {name}",
        f"**Kind:** {kind}",
        f"**Members:** {member_list}",
        "",
        "## Messages",
        "",
        digest,
        "",
    ]
    return title, "\n".join(body_lines)
