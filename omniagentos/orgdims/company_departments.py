"""Per-department health review, migrated to orgdims."""

from __future__ import annotations

import logging
import time
from typing import Any

from omniagentos.contracts import ResultStatus
from omniagentos.orgdims.company_init import (
    DEFAULT_BUDGET,
    AdapterFn,
    adapter_text,
    default_adapter_fn,
    parse_json_maybe,
)
from omniagentos.reliability.contracts import Agent, OrgUnit, ReliabilityStore

logger = logging.getLogger(__name__)

_VALID_KINDS = {"fix", "optimization", "architecture", "new_agent", "skill", "docs", "config"}
_VALID_RISK = {1, 2, 3, 4}

_CONTEXT_SCORECARD_LIMIT = 5
_CONTEXT_EVENT_LIMIT = 15
_CONTEXT_IMPROVEMENT_LIMIT = 50
_CONTEXT_IMPROVEMENT_SHOWN = 5

_REVIEW_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "kind": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "risk_hint": {"type": "integer"},
                    "expected_impact": {"type": "string"},
                    "plan": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["title"],
            },
        }
    },
    "required": ["proposals"],
}


def _archdocs_context(focus_terms: list[str]) -> str:
    """Living-arch-doc context for the prompt."""
    try:
        from omniagentos.archdocs.context import load_arch_context

        return load_arch_context(focus_terms, max_tokens=600)
    except Exception:  # pragma: no cover
        return ""


def _due(manager: Agent, audit_kind: str, force: bool) -> bool:
    if force:
        return True
    schedule = manager.schedule_json or {}
    if schedule.get("callable") is False:
        return False
    cadence = schedule.get("cadence", "twice_daily")
    if cadence == "twice_daily":
        return audit_kind in ("twice_daily", "on_demand")
    return cadence == audit_kind or audit_kind == "on_demand"


def build_department_context(store: ReliabilityStore, dept: OrgUnit) -> str:
    """Scoped context for *dept*."""
    lines: list[str] = [f"## Department: {dept.name}", "", dept.charter or "(no charter set)", ""]

    scorecards = store.list_scorecards(
        subject_type="department", subject_id=dept.id, limit=_CONTEXT_SCORECARD_LIMIT
    )
    lines.append("### Recent scorecards")
    if scorecards:
        for sc in scorecards:
            lines.append(f"- {sc.window} {sc.period_start}: {sc.metrics_json}")
    else:
        lines.append("- (none yet)")

    open_events = store.list_events(status="open", limit=_CONTEXT_EVENT_LIMIT)
    lines.append("")
    lines.append("### Open reliability events (system-wide feed; pick what's in your domain)")
    if open_events:
        for evt in open_events:
            lines.append(
                f"- [{evt.severity}] {evt.failure_class} (source={evt.source}, sig={evt.signature})"
            )
    else:
        lines.append("- (none open)")

    recent = [
        imp
        for imp in store.list_improvements(limit=_CONTEXT_IMPROVEMENT_LIMIT)
        if isinstance(imp.proposal_json, dict) and imp.proposal_json.get("department") == dept.name
    ][:_CONTEXT_IMPROVEMENT_SHOWN]
    lines.append("")
    lines.append("### This department's recent proposals")
    if recent:
        for imp in recent:
            lines.append(f"- [{imp.status}] {imp.title}")
    else:
        lines.append("- (none yet)")

    arch = _archdocs_context([dept.name])
    if arch:
        lines.append("")
        lines.append(arch)

    return "\n".join(lines)


def _build_review_prompt(store: ReliabilityStore, dept: OrgUnit, manager: Agent) -> str:
    context = build_department_context(store, dept)
    return (
        f"You are {manager.title} ({manager.name}), the {dept.name} department manager "
        "in an AI engineering company that improves itself.\n\n"
        f"{context}\n\n"
        "Run a health review of your domain. Respond with STRICT JSON only, matching:\n"
        '{"proposals": [{"title": str, "summary": str, "kind": '
        '"fix|optimization|architecture|new_agent|skill|docs|config", '
        '"root_cause": str, "risk_hint": 1-4, "expected_impact": str, "plan": [str, ...]}]}\n'
        "Rank proposals by importance (most important first). Return an empty proposals list "
        "if nothing actionable was found. Do not include any prose outside the JSON object."
    )


def _sanitize_kind(kind: Any) -> str:
    if isinstance(kind, str) and kind in _VALID_KINDS:
        return kind
    return "optimization"


def _sanitize_risk_hint(risk_hint: Any) -> int | None:
    if isinstance(risk_hint, int) and risk_hint in _VALID_RISK:
        return risk_hint
    if isinstance(risk_hint, str) and risk_hint.isdigit() and int(risk_hint) in _VALID_RISK:
        return int(risk_hint)
    return None


def _create_improvement_from_proposal(
    store: ReliabilityStore, dept: OrgUnit, manager: Agent, proposal: dict[str, Any]
) -> str | None:
    title = proposal.get("title")
    if not isinstance(title, str) or not title.strip():
        logger.warning("company.departments: skipping proposal without a title from %s", dept.name)
        return None
    kind = _sanitize_kind(proposal.get("kind"))
    risk_hint = _sanitize_risk_hint(proposal.get("risk_hint"))
    proposal_json: dict[str, Any] = {
        "change_type": "docs" if kind == "docs" else "files",
        "files": [],
        "plan": proposal.get("plan") if isinstance(proposal.get("plan"), list) else [],
        "restart_required": False,
        "expected_impact": proposal.get("expected_impact", ""),
        "repro": "",
        "department": dept.name,
        "risk_hint": risk_hint,
        "manager": manager.name,
    }
    return store.create_improvement(
        origin="department",
        kind=kind,
        title=title.strip(),
        summary=str(proposal.get("summary", ""))[:2000],
        root_cause=str(proposal.get("root_cause", ""))[:2000],
        proposal_json=proposal_json,
        created_by=f"company.departments:{manager.name}",
    )


def run_department_reviews(
    store: ReliabilityStore,
    adapter_fn: AdapterFn | None = None,
    budget: dict[str, Any] | None = None,
    *,
    department: str | None = None,
    audit_kind: str = "twice_daily",
) -> dict[str, Any]:
    """Run the health review for each enabled department due per its manager's schedule."""
    fn = adapter_fn or default_adapter_fn

    budget = budget or DEFAULT_BUDGET
    force = department is not None

    summary: dict[str, Any] = {"reviewed": [], "skipped": [], "errors": [], "improvement_ids": []}

    managers = [a for a in store.list_agents(org_role="manager", enabled=1)]
    for manager in managers:
        if manager.org_unit_id is None:
            continue
        dept = store.get_org_unit(manager.org_unit_id)
        if dept is None or dept.kind != "department":
            continue
        if department is not None and dept.name != department:
            continue
        if not _due(manager, audit_kind, force):
            summary["skipped"].append(dept.name)
            continue

        started = time.monotonic()
        try:
            prompt = _build_review_prompt(store, dept, manager)
            result = fn(
                manager.harness or "cli-claude",
                prompt,
                output_schema=_REVIEW_OUTPUT_SCHEMA,
                budget=budget,
            )
            if result.status != ResultStatus.OK:
                summary["errors"].append(
                    {
                        "department": dept.name,
                        "error": result.error or f"adapter status={result.status}",
                    }
                )
                continue
            text = adapter_text(result)
            parsed = parse_json_maybe(text)
            if parsed is None:
                logger.warning("company.departments: unparseable review output for %s", dept.name)
                summary["errors"].append(
                    {"department": dept.name, "error": "unparseable adapter output"}
                )
                continue
            proposals = parsed.get("proposals")
            if not isinstance(proposals, list):
                summary["errors"].append(
                    {"department": dept.name, "error": "proposals field missing/invalid"}
                )
                continue
            for proposal in proposals:
                if not isinstance(proposal, dict):
                    continue
                imp_id = _create_improvement_from_proposal(store, dept, manager, proposal)
                if imp_id:
                    summary["improvement_ids"].append(imp_id)
            summary["reviewed"].append(dept.name)
        except Exception as exc:  # pragma: no cover
            logger.exception("company.departments: review failed for %s", dept.name)
            summary["errors"].append({"department": dept.name, "error": str(exc)})
        finally:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info("company.departments: %s review took %dms", dept.name, elapsed_ms)

    return summary
