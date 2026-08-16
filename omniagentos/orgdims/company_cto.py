"""CTO daily quick review + weekly deep architecture review, migrated to orgdims."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from omniagentos.context.lanes import is_machine_identity
from omniagentos.contracts import (
    NoteType,
    ResultStatus,
    VaultFrontmatter,
    default_vault_dir,
    utc_now_iso,
)
from omniagentos.orgdims.company_init import (
    DEFAULT_BUDGET,
    AdapterFn,
    adapter_text,
    default_adapter_fn,
    parse_json_maybe,
)
from omniagentos.reliability.contracts import ReliabilityStore
from omniagentos.vault.frontmatter import render_frontmatter
from omniagentos.vault.write import write_note

logger = logging.getLogger(__name__)

OPEN_BACKLOG_STATUSES = ("proposed", "testing", "judging", "panel_blocked", "awaiting_human")

_KIND_WEIGHT = {
    "fix": 5,
    "optimization": 4,
    "architecture": 3,
    "skill": 3,
    "config": 3,
    "new_agent": 2,
    "docs": 1,
}
_MAX_AGE_BONUS_DAYS = 14

_DAILY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "new_proposals": {"type": "array"},
    },
    "required": ["narrative"],
}

_WEEKLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "narrative": {"type": "string"},
        "proposals": {"type": "array"},
    },
    "required": ["narrative"],
}


def _age_days(created_at: str) -> float:
    try:
        created = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return 0.0
    delta = datetime.now(UTC) - created
    return max(0.0, delta.total_seconds() / 86400.0)


def ranking_score_for(kind: str, risk_level: int, attempt: int, created_at: str) -> float:
    """Deterministic backlog ranking score."""
    kind_weight = _KIND_WEIGHT.get(kind, 2)
    age_bonus = min(_age_days(created_at), _MAX_AGE_BONUS_DAYS)
    return (kind_weight * 10) + age_bonus - (risk_level * 2) - (attempt * 3)


def daily_review(
    store: ReliabilityStore,
    adapter_fn: AdapterFn | None = None,
    budget: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-rank the open backlog and get the CTO's narrative."""
    fn = adapter_fn or default_adapter_fn
    budget = budget or DEFAULT_BUDGET

    ranked: list[dict[str, Any]] = []
    for status in OPEN_BACKLOG_STATUSES:
        for imp in store.list_improvements(status=status, limit=200):
            score = ranking_score_for(imp.kind, imp.risk_level, imp.attempt, imp.created_at)
            store.update_improvement_fields(imp.id, ranking_score=score)
            ranked.append(
                {"id": imp.id, "title": imp.title, "kind": imp.kind, "ranking_score": score}
            )
    ranked.sort(key=lambda r: r["ranking_score"], reverse=True)

    result: dict[str, Any] = {"ranked": ranked, "narrative": "", "new_improvement_ids": []}

    if not ranked:
        return result

    backlog_lines = "\n".join(
        f"- [{r['kind']}] {r['title']} (score={r['ranking_score']:.1f})" for r in ranked[:20]
    )
    prompt = (
        "You are the CTO of an AI engineering company that improves itself. Here is today's "
        f"open improvement backlog, ranked by a deterministic score:\n\n{backlog_lines}\n\n"
        "Respond with STRICT JSON only: "
        '{"narrative": str (a few sentences on today\'s priorities), '
        '"new_proposals": [{"title": str, "summary": str, "kind": '
        '"fix|optimization|architecture|new_agent|skill|docs|config", "root_cause": str, '
        '"expected_impact": str}]}. '
        "Only include new_proposals for genuinely new, obvious issues not already in the backlog above "
        "— an empty list is the expected common case."
    )
    try:
        agent_result = fn("cli-claude", prompt, output_schema=_DAILY_SCHEMA, budget=budget)
        if agent_result.status != ResultStatus.OK:
            logger.warning("company.cto: daily narrative call failed: %s", agent_result.error)
            return result
        parsed = parse_json_maybe(adapter_text(agent_result))
        if parsed is None:
            logger.warning("company.cto: unparseable daily narrative output")
            return result
        result["narrative"] = str(parsed.get("narrative", ""))
        new_proposals = parsed.get("new_proposals")
        if isinstance(new_proposals, list):
            for proposal in new_proposals:
                if not isinstance(proposal, dict):
                    continue
                title = proposal.get("title")
                if not isinstance(title, str) or not title.strip():
                    continue
                kind_raw = proposal.get("kind")
                kind = str(kind_raw) if kind_raw in _KIND_WEIGHT else "optimization"
                imp_id = store.create_improvement(
                    origin="cto",
                    kind=kind,
                    title=title.strip(),
                    summary=str(proposal.get("summary", ""))[:2000],
                    root_cause=str(proposal.get("root_cause", ""))[:2000],
                    proposal_json={
                        "change_type": "files",
                        "files": [],
                        "plan": [],
                        "restart_required": False,
                        "expected_impact": proposal.get("expected_impact", ""),
                        "repro": "",
                    },
                    created_by="company.cto:daily",
                )
                result["new_improvement_ids"].append(imp_id)
    except Exception:  # pragma: no cover
        logger.exception("company.cto: daily_review narrative step failed")

    return result


def _weekly_context(store: ReliabilityStore) -> str:
    lines = ["## Weekly architecture review context", ""]
    week_scorecards = store.list_scorecards(window="week", limit=30)
    lines.append("### Scorecards (week)")
    if week_scorecards:
        for sc in week_scorecards:
            lines.append(f"- {sc.subject_type}/{sc.subject_id}: {sc.metrics_json}")
    else:
        lines.append("- (none yet)")

    lines.append("")
    lines.append("### Applied/monitoring improvements (compounding-improvement signal)")
    recent_applied = [
        imp
        for imp in store.list_improvements(limit=100)
        if imp.status in ("applied", "monitoring", "confirmed")
    ][:10]
    if recent_applied:
        for imp in recent_applied:
            lines.append(f"- [{imp.status}] {imp.title}")
    else:
        lines.append("- (none yet)")

    lines.append("")
    lines.append(
        "### Agents (headcount/harness for the 'unnecessary agents / better models' question)"
    )
    # Filter at the QUERY, not at seed time. ``agents`` is also the identity
    # table broker grants foreign-key to, so since migration 108 it holds
    # machine holders (``loop:render_probe``, ``lane:api``) alongside the org
    # roster. They inherit ``enabled=1`` and ``org_role='specialist'`` from
    # migration 042's defaults and were being rendered here as headcount — two
    # rows that are not agents, polluting the one analysis that proposes
    # retiring agents. ``enabled`` cannot carry this distinction: migration 048
    # gave it PAUSE semantics, and grant administration deliberately needs these
    # holders visible.
    for agent in store.list_agents(enabled=1):
        if is_machine_identity(agent.id):
            continue
        lines.append(f"- {agent.name} ({agent.org_role}, harness={agent.harness})")

    return "\n".join(lines)


def weekly_review(
    store: ReliabilityStore,
    adapter_fn: AdapterFn | None = None,
    budget: dict[str, Any] | None = None,
    *,
    vault_dir: str | None = None,
    vault_autocommit: bool | None = None,
) -> dict[str, Any]:
    """Deep architecture review, migrated to orgdims."""
    fn = adapter_fn or default_adapter_fn
    budget = budget or DEFAULT_BUDGET

    result: dict[str, Any] = {"narrative": "", "new_improvement_ids": [], "vault_note_path": None}
    context = _weekly_context(store)
    prompt = (
        "You are the CTO of an AI engineering company that improves itself, running the weekly "
        f"deep architecture review.\n\n{context}\n\n"
        "Answer, in your narrative: are there unnecessary agents to retire? would a different model "
        "(per modelintel) serve any role better? what are the current bottlenecks? which improvements "
        "compound (make future improvements cheaper/safer)? "
        "Respond with STRICT JSON only: "
        '{"narrative": str, "proposals": [{"title": str, "summary": str, "kind": '
        '"fix|optimization|architecture|new_agent|skill|docs|config", "root_cause": str, '
        '"expected_impact": str}]}.'
    )
    try:
        agent_result = fn("cli-claude", prompt, output_schema=_WEEKLY_SCHEMA, budget=budget)
        if agent_result.status != ResultStatus.OK:
            logger.warning("company.cto: weekly review call failed: %s", agent_result.error)
        else:
            parsed = parse_json_maybe(adapter_text(agent_result))
            if parsed is None:
                logger.warning("company.cto: unparseable weekly review output")
            else:
                result["narrative"] = str(parsed.get("narrative", ""))
                proposals = parsed.get("proposals")
                if isinstance(proposals, list):
                    for proposal in proposals:
                        if not isinstance(proposal, dict):
                            continue
                        title = proposal.get("title")
                        if not isinstance(title, str) or not title.strip():
                            continue
                        kind_raw = proposal.get("kind")
                        kind = str(kind_raw) if kind_raw in _KIND_WEIGHT else "architecture"
                        imp_id = store.create_improvement(
                            origin="weekly",
                            kind=kind,
                            title=title.strip(),
                            summary=str(proposal.get("summary", ""))[:2000],
                            root_cause=str(proposal.get("root_cause", ""))[:2000],
                            proposal_json={
                                "change_type": "files",
                                "files": [],
                                "plan": [],
                                "restart_required": False,
                                "expected_impact": proposal.get("expected_impact", ""),
                                "repro": "",
                            },
                            created_by="company.cto:weekly",
                        )
                        result["new_improvement_ids"].append(imp_id)
    except Exception:  # pragma: no cover
        logger.exception("company.cto: weekly_review failed")

    vault = vault_dir or default_vault_dir()
    try:
        fm = VaultFrontmatter(
            id="cto-roadmap", type=NoteType.DECISION, created=utc_now_iso(), status="active"
        )
        body_lines = [
            "# CTO Roadmap",
            "",
            f"_Last weekly review: {utc_now_iso()}_",
            "",
            "## Narrative",
            "",
            result["narrative"] or "(no narrative this cycle)",
            "",
            "## New proposals this cycle",
            "",
        ]
        if result["new_improvement_ids"]:
            body_lines += [f"- {imp_id}" for imp_id in result["new_improvement_ids"]]
        else:
            body_lines.append("- (none)")
        body_lines += ["", "## Notes (human)", ""]
        note_path = write_note(
            vault,
            "org/cto/roadmap.md",
            render_frontmatter(fm) + "\n" + "\n".join(body_lines),
            autocommit=vault_autocommit,
        )
        result["vault_note_path"] = note_path
    except Exception:  # pragma: no cover
        logger.exception("company.cto: failed writing roadmap vault note")

    return result
