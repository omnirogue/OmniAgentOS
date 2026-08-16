"""EDC morning-digest rendering — RENDERING ONLY (synthesis §11, P1).

``render_owner_section`` builds the per-owner Executive Decision Center block
appended to the existing per-person morning DM (``team/notify.run_morning``).
Transport and scheduling stay in ``notify.py`` — this module never posts, never
schedules, and never mutates. NEEDS_OWNER items go here (the non-interrupting daily
review); URGENT items are DM'd immediately at classification instead, so they are
deliberately NOT repeated here. Top MAYBE items get a short "review" tail.
"""

from __future__ import annotations

from typing import Any

from omniagentos.edc.store import DecisionStore

__all__ = ["render_owner_section"]

_MAYBE_LIMIT = 5


def _human_line(decision: dict[str, Any]) -> str:
    recommended = decision.get("recommended") or {}
    line = str(recommended.get("human_line") or "").strip()
    return line or "review and decide"


def _deadline_suffix(decision: dict[str, Any]) -> str:
    deadline = decision.get("deadline_at")
    return f" ⏰ {deadline}" if deadline else ""


def render_owner_section(decisions: DecisionStore, owner_employee_id: str) -> str | None:
    """The owner's EDC morning block, or ``None`` when there is nothing to show.

    Owner-scoped by construction (every store read requires the owner id). Lists
    open NEEDS_OWNER decisions each with its concrete recommended action, then a
    short count/preview of open MAYBE items awaiting review.
    """
    all_needs_owner = decisions.list_decisions(
        owner_employee_id=owner_employee_id, classification="needs_owner", status="open"
    )
    # Rule proposals are needs_owner decisions but get their OWN section (approve/
    # decline), so they are not double-listed in the general "needs you" block.
    proposals = [d for d in all_needs_owner if d.get("source") == "rule_proposal"]
    needs_owner = [d for d in all_needs_owner if d.get("source") != "rule_proposal"]
    maybe = [
        decision
        for decision in decisions.list_decisions(
            owner_employee_id=owner_employee_id, classification="maybe", status="open"
        )
    ]
    if not needs_owner and not maybe and not proposals:
        return None

    lines: list[str] = ["📨 Decisions for you:"]
    if needs_owner:
        for decision in needs_owner:
            title = str(decision.get("title") or "(no subject)")
            lines.append(
                f"• EDC-{decision.get('number')}: {title} — "
                f"{_human_line(decision)}{_deadline_suffix(decision)}"
            )
    else:
        lines.append("• (nothing needs you)")

    if maybe:
        preview = ", ".join(f"EDC-{decision.get('number')}" for decision in maybe[:_MAYBE_LIMIT])
        more = "" if len(maybe) <= _MAYBE_LIMIT else f" (+{len(maybe) - _MAYBE_LIMIT} more)"
        lines.append(f"• {len(maybe)} to review: {preview}{more}")

    if proposals:
        lines.append("🧠 Rule proposals awaiting approval:")
        for decision in proposals:
            lines.append(
                f"• EDC-{decision.get('number')}: {_human_line(decision)} — approve or decline"
            )

    return "\n".join(lines)
