"""Deploy-time preflight intelligence for board tasks."""

from __future__ import annotations

import re
from typing import Any

from omniagentos.formation.selector import select_formation_with_confidence
from omniagentos.orgdims.service import OrgDimsService
from omniagentos.skills import list_skills
from omniagentos.skills.select import select_skills


def extract_paths(text: str) -> list[str]:
    """Extract candidate files or directory paths from a task description/title."""
    # Match strings that look like relative paths
    candidates = re.findall(r"(?:[\w\-./]+/[\w\-./]+)", text)
    paths = []
    for c in candidates:
        c = c.strip(" .`\"'(),;*!?")
        if not c or c.startswith("http") or ".." in c:
            continue
        # normalize slashes
        normalized = c.replace("\\", "/").strip("/")
        if normalized and normalized not in paths:
            paths.append(normalized)
    return paths


def build_preflight(
    db_path: str,
    *,
    task_id: str,
    title: str,
    description: str,
    discipline: str | None = None,
    priority: str | None = None,
    wide: bool = False,
    pinned_formation: str | None = None,
) -> dict[str, Any]:
    """Run deploy-time classification, formation binding, and skill selection.

    Composes OrgDimsService.classify_board_task + formation selection + skill selection
    + owned paths + parallelism logic.
    """
    # 1. Classification
    org_svc = OrgDimsService(db_path=db_path)
    org_result = org_svc.classify_board_task(
        task_id=task_id,
        title=title,
        description=description,
        discipline=discipline,
        priority=priority,
        apply=True,
    )

    # 2. Extract classification details
    classification = org_result.bundle.classification
    risk_class = classification.risk_class or "none"
    domains = classification.domains or []
    primary_domain = domains[0] if domains else (discipline or "coding")

    # 3. Formation binding
    # If a pinned formation is specified, we must use it. Otherwise, we select with confidence.
    if pinned_formation:
        from omniagentos.formation.selector import _all

        formations = _all()
        # Find formation by id/slug
        pin_id = pinned_formation.strip().lower()
        form = formations.get(pin_id)
        if not form:
            # Fallback if unknown
            selection = select_formation_with_confidence(
                goal=description or title,
                task_class=primary_domain,
                risk_class=risk_class,
            )
        else:
            from omniagentos.formation.selector import FormationSelection

            selection = FormationSelection(
                formation=form,
                confidence=1.0,
                reason="pinned",
            )
    else:
        selection = select_formation_with_confidence(
            goal=description or title,
            task_class=primary_domain,
            risk_class=risk_class,
        )

    formation_id = selection.formation.id if selection.formation else "coding"
    confidence = selection.confidence

    # 4. Skill selection
    registry_rows = []
    try:
        registry_rows = list_skills(database=db_path)
    except Exception:
        pass

    hits = []
    if registry_rows:
        hits = select_skills(
            registry_rows,
            domain=primary_domain,
            risk_class=risk_class,
            max_skills=8,  # spec says: "skills: select 3–8"
            min_skills=3,
        )

    # Format hits to JSON serialize safely
    skills_list = [
        {
            "name": h.name,
            "version": h.version,
            "score": h.score,
            "reason": h.reason,
        }
        for h in hits
    ]

    # 5. Extract owned paths
    extracted = extract_paths(f"{title} {description}")

    # 6. Parallelism calculation
    parallelism = 2
    if wide:
        parallelism = 4

    # Build preflight dict
    preflight = {
        "workstream": classification.primary_workstream or "engineering",
        "domains": domains,
        "risk_class": risk_class,
        "priority": classification.priority or priority or "normal",
        "formation_id": formation_id,
        "confidence": confidence,
        "skills": skills_list,
        "owned_paths": extracted,
        "parallelism": parallelism,
        "wide": wide,
    }
    if pinned_formation:
        preflight["pinned_formation"] = pinned_formation

    return preflight
