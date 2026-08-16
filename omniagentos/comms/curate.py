"""Curation policy: which stored comms rows earn a slot in the knowledge graph.

This module makes NO network calls and touches NO LLM — it is pure substring/keyword
matching over an already-stored row, run by the offline batch job
(:mod:`omniagentos.comms.extract_batch`). Selection is deliberately conservative:
only VIP senders, explicit operator flags, or a configured goal's keywords earn
extraction; everything else stays Tier-1-only (stored, never summarized/embedded).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omniagentos.steward.config import StewardConfig

__all__ = ["select_for_extraction"]


def select_for_extraction(
    row: Mapping[str, Any],
    cfg: StewardConfig,
    goals: Sequence[Mapping[str, Any]],
) -> tuple[bool, str, str | None]:
    """Decide whether a stored comms row should be extracted into the knowledge graph.

    Returns ``(selected, reason, discipline)``. Checked in order:

    1. Operator flag — an operator (or a prior curate pass) already marked the row
       ``kb_status == "selected"``; always honored regardless of the VIP/keyword toggles.
    2. VIP sender — case-insensitive substring match against ``cfg.alerts.vip_senders``,
       gated by ``cfg.curation.extract_vip``.
    3. Goal keywords — case-insensitive substring match of any active goal's keyword
       against the subject/body, gated by ``cfg.curation.extract_goal_keywords``; the
       matched goal's ``discipline_id`` is returned so the episode lands in that discipline.

    Anything that matches none of the above is left Tier-1-only: ``(False, "not
    selected", None)``.
    """
    if str(row.get("kb_status") or "") == "selected":
        return True, "operator flagged", None

    sender = str(row.get("sender") or "").lower()
    if cfg.curation.extract_vip:
        for vip in cfg.alerts.vip_senders:
            token = str(vip).strip().lower()
            if token and token in sender:
                return True, f"vip sender match ({vip})", None

    if cfg.curation.extract_goal_keywords:
        haystack = f"{row.get('subject') or ''} {row.get('body_text') or ''}".lower()
        for goal in goals:
            for keyword in goal.get("keywords") or []:
                term = str(keyword).strip().lower()
                if term and term in haystack:
                    discipline = goal.get("discipline_id")
                    return (
                        True,
                        f"goal keyword match ({keyword})",
                        (str(discipline) if discipline is not None else None),
                    )

    return False, "not selected", None
