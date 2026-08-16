"""Decision Center / Recommended Next Action capability contract (M-15).

The Executive Decision Center (``omniagentos/edc/**``, migration 130) is the real,
versioned product this capability now points at: a deterministic-first triage
pipeline that turns email (and any future ``SourceAdapter``) into owner-scoped
Decisions with a concrete recommended next action, an audited resolution lifecycle
(reply / execute / delegate / defer / snooze / dismiss), outcome-verified
completion, and an owner-approved rules + learning layer.

This module exposes the **truthful implemented contract** for that surface. It was
a `not_implemented` placeholder through M-15; EDC P0–P4 built the producer, so the
contract is flipped to `implemented` and points at the live ``/api/decisions``
surfaces. Keep it honest: it advertises only what is wired end-to-end.
"""

from __future__ import annotations

from typing import Any, Final

CAPABILITY_ID: Final[str] = "decision_center"
ALIASES: Final[tuple[str, ...]] = ("recommended_next_action", "recommended-next-action")

#: The live owner-scoped surfaces the capability resolves to (P1–P4). Every one is
#: wired end-to-end; nothing aspirational is listed here (spec §25 honesty rule).
SURFACES: Final[tuple[str, ...]] = (
    "/api/decisions",
    "/api/decisions/{decision_id}",
    "/api/decisions/{decision_id}/decide",
)

DECISION_CENTER_CONTRACT: Final[dict[str, Any]] = {
    "capability": CAPABILITY_ID,
    "aliases": list(ALIASES),
    "status": "implemented",
    "implemented": True,
    "http_status": 200,
    "producer": "omniagentos.edc",
    "surfaces": list(SURFACES),
    "message": (
        "Decision Center / Recommended Next Action is implemented by the Executive "
        "Decision Center (omniagentos/edc). Owner-scoped triage, recommended next "
        "action, audited resolution + outcome-verified completion, and an "
        "owner-approved rules/learning layer are live at /api/decisions."
    ),
}


def decision_center_contract() -> dict[str, Any]:
    """Return a fresh copy of the implemented capability contract."""
    # Shallow-copy so callers cannot mutate the module constant.
    contract = dict(DECISION_CENTER_CONTRACT)
    contract["aliases"] = list(DECISION_CENTER_CONTRACT["aliases"])
    contract["surfaces"] = list(DECISION_CENTER_CONTRACT["surfaces"])
    return contract
