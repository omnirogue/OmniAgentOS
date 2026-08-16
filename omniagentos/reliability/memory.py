"""Improvement search + lessons writeback (§3 memory module).

Search prior attempts by signature/title/root-cause. Flag failures/rejections.
Writeback lessons on terminal transitions. Propose confirmed fixes as skills.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from omniagentos.reliability.contracts import ReliabilityStore
from omniagentos.reliability.taxonomy import ImprovementStatus


def _reflexion_var_root() -> Path:
    """Resolve the isolated var root for lesson/skill writeback (L-15).

    Prefer explicit runtime isolation knobs. Never fall back to a cwd-relative
    ``"var"`` path — that leaked fabricated test lessons into the operator
    checkout's gitignored corpus whenever tests ran without env setup.
    """
    for key in ("OMNIAGENTOS_VAR", "OMNIAGENTOS_VAR_DIR"):
        raw = os.environ.get(key)
        if raw:
            return Path(raw).expanduser().resolve()
    home = os.environ.get("OMNIAGENTOS_HOME")
    if home:
        return Path(home).expanduser().resolve() / "var"
    # Package-anchored default (independent of process cwd).
    return Path(__file__).resolve().parents[2] / "var"


def search_improvements(
    store: ReliabilityStore,
    signature: str = "",
    title_keywords: list[str] | None = None,
    root_cause: str = "",
) -> list[dict[str, Any]]:
    """Search improvements by signature/title keywords/root-cause.

    Returns prior attempts with failure/rejection flags set.

    Args:
        store: ReliabilityStore
        signature: event signature to match against title/root_cause
        title_keywords: OR'd keywords to search in title + summary
        root_cause: root-cause category to match

    Returns:
        List of {id, title, root_cause, status, created_at, failed_before, rejected_before}
    """
    if title_keywords is None:
        title_keywords = []

    results = []
    seen_ids: set[str] = set()

    # Build query based on provided filters
    query_parts = ["1=1"]
    params: list[str] = []

    if signature:
        # Match signature in title or root_cause
        query_parts.append("(title LIKE ? OR root_cause LIKE ?)")
        params.extend([f"%{signature}%", f"%{signature}%"])

    if title_keywords:
        # OR all keywords
        keyword_conditions = " OR ".join(["(title LIKE ? OR summary LIKE ?)"] * len(title_keywords))
        query_parts.append(f"({keyword_conditions})")
        for kw in title_keywords:
            params.extend([f"%{kw}%", f"%{kw}%"])

    if root_cause:
        query_parts.append("root_cause LIKE ?")
        params.append(f"%{root_cause}%")

    # Fallback: if no filters, search recent improvements
    if not (signature or title_keywords or root_cause):
        limit = 20
    else:
        limit = 100

    params.append(str(limit))

    try:
        improvements = store.list_improvements(limit=limit)
    except Exception:
        improvements = []

    # Filter results in memory
    for imp in improvements:
        if seen_ids and imp.id in seen_ids:
            continue
        seen_ids.add(imp.id)

        # Apply filters
        matches = True
        if signature:
            matches = signature.lower() in (imp.title.lower() or "") or signature.lower() in (
                imp.root_cause.lower() or ""
            )
        if matches and title_keywords:
            matches = any(
                kw.lower() in (imp.title.lower() or "") or kw.lower() in (imp.summary.lower() or "")
                for kw in title_keywords
            )
        if matches and root_cause:
            matches = root_cause.lower() in (imp.root_cause.lower() or "")

        if not matches:
            continue

        # Check for failure/rejection flags
        failed_before = imp.status in (
            ImprovementStatus.ROLLED_BACK.value,
            ImprovementStatus.FAILED.value,
        )
        rejected_before = imp.status == ImprovementStatus.REJECTED.value

        results.append(
            {
                "id": imp.id,
                "title": imp.title,
                "root_cause": imp.root_cause,
                "status": imp.status,
                "created_at": imp.created_at,
                "failed_before": failed_before,
                "rejected_before": rejected_before,
            }
        )

    return results


def writeback_lessons(store: ReliabilityStore, improvement_id: str) -> None:
    """Writeback lessons from a confirmed improvement.

    On terminal transition (confirmed, rolled_back, etc.), extract lessons
    and cross-reference prior attempts.

    Args:
        store: ReliabilityStore
        improvement_id: improvement.id
    """
    imp = store.get_improvement(improvement_id)
    if imp is None:
        return

    if imp.status != ImprovementStatus.CONFIRMED.value:
        # Only writeback on confirmed; other terminal states are handled separately
        return

    # Extract lessons from proposal_json + evidence
    lessons: dict[str, Any] = {
        "improvement_id": improvement_id,
        "title": imp.title,
        "root_cause": imp.root_cause,
        "confirmed_at": imp.updated_at,
    }

    # If this improvement references prior attempts, attach them
    if imp.memory_refs_json:
        lessons["prior_attempts"] = imp.memory_refs_json

    # Durable writeback: append-only JSONL under <var>/reflexion (never raises).
    try:
        import json

        path = _reflexion_var_root() / "reflexion" / "lessons.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(lessons, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 — writeback must never break the pipeline
        pass


def propose_confirmed_fix(store: ReliabilityStore, improvement_id: str) -> str | None:
    """Propose a confirmed fix as a reusable skill.

    When an improvement reaches confirmed status and kind='fix', extract
    the fix and propose it as a skill via the existing skills API.

    Args:
        store: ReliabilityStore
        improvement_id: improvement.id

    Returns:
        skill_id if proposed; None if not eligible
    """
    imp = store.get_improvement(improvement_id)
    if imp is None or imp.status != ImprovementStatus.CONFIRMED.value:
        return None

    if imp.kind != "fix":
        # Only propose fixes as skills
        return None

    # Map risk level to skill risk
    risk_map = {
        1: "low",
        2: "medium",
        3: "high",
        4: "critical",
    }

    skill_risk = risk_map.get(imp.risk_level, "medium")

    skill_proposal = {
        "improvement_id": improvement_id,
        "title": imp.title,
        "description": getattr(imp, "summary", None) or imp.title,
        "root_cause": imp.root_cause,
        "risk": skill_risk,
        "evidence": {
            "proposal_json": imp.proposal_json,
            "monitor_until": getattr(imp, "monitor_until", None),
        },
    }

    # Best-effort skills API; absence is not a failure of the reliability path.
    try:
        from omniagentos import skills as skills_mod

        propose = getattr(skills_mod, "propose_update", None) or getattr(
            skills_mod, "propose_skill", None
        )
        if callable(propose):
            result = propose(skill_proposal)
            if isinstance(result, str) and result:
                return result
            if isinstance(result, dict) and result.get("id"):
                return str(result["id"])
    except Exception:  # noqa: BLE001
        pass

    # Durable pending proposal for a human/curator to promote (G10: no self-promote).
    try:
        import json
        from uuid import uuid4

        path = _reflexion_var_root() / "reflexion" / "skill_proposals.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        proposal_id = f"skprop_{uuid4().hex[:12]}"
        skill_proposal["id"] = proposal_id
        skill_proposal["status"] = "pending_review"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(skill_proposal, sort_keys=True, default=str) + "\n")
        return proposal_id
    except Exception:  # noqa: BLE001
        return None
