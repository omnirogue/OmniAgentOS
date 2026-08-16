"""Read models for the goal dashboard."""

from __future__ import annotations

from typing import Any

from omniagentos.db.store import SqliteStore
from omniagentos.steward.store import StewardStore

_SOURCE_METRICS = {
    "stripe": ("net_revenue_usd", "payment_failures"),
    "meta": ("spend_usd", "roas"),
}


def _metrics(goal: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    north_star = goal.get("north_star", {})
    source = str(north_star.get("source", "")) if isinstance(north_star, dict) else ""
    metric = str(north_star.get("metric", "")) if isinstance(north_star, dict) else ""
    values = _SOURCE_METRICS.get(source, (metric,) if metric else ())
    return source, values


def goal_summary(steward: StewardStore, goal: dict[str, Any]) -> dict[str, Any]:
    """Return one goal with current and fourteen-day metric views."""
    result = dict(goal)
    source, metrics = _metrics(goal)
    goal_id = str(goal["id"])
    result["latest"] = {
        metric: steward.latest_snapshot(source, metric, goal_id) for metric in metrics
    }
    result["trend"] = {
        metric: steward.snapshot_series(metric, goal_id=goal_id, source=source, days=14)
        for metric in metrics
    }
    return result


def _fact_views(steward: StewardStore, goal_id: str) -> list[dict[str, Any]]:
    links = steward.goal_fact_links(goal_id)
    if not links:
        return []
    try:
        from omniagentos.knowledge import config as knowledge_config
        from omniagentos.knowledge.store import KnowledgeStore

        enabled = getattr(knowledge_config, "enabled", knowledge_config.knowledge_enabled)
        if not enabled():
            raise RuntimeError("knowledge is disabled")
        knowledge = KnowledgeStore(dsn=knowledge_config.dsn())
        try:
            result = []
            for link in links:
                fact_id = int(link["fact_id"])
                fact = knowledge.get_fact(fact_id)
                statement = fact.statement if fact is not None else "(fact unavailable)"
                result.append({"fact_id": fact_id, "statement": statement})
            return result
        finally:
            knowledge.close()
    except Exception:
        return [
            {"fact_id": int(link["fact_id"]), "statement": "(fact unavailable)"} for link in links
        ]


def goal_detail(
    steward: StewardStore, goal: dict[str, Any], store: SqliteStore | None = None
) -> dict[str, Any]:
    """Expand a goal summary with goal-specific knowledge and operational context."""
    database = store or steward._store
    result = goal_summary(steward, goal)
    goal_id = str(goal["id"])
    result["facts"] = _fact_views(steward, goal_id)
    result["suggestions"] = [
        suggestion
        for suggestion in steward.list_suggestions(state="open")
        if suggestion.get("goal_id") == goal_id
    ]
    discipline_id = goal.get("discipline_id")
    result["recent_runs"] = (
        database.list_runs({"discipline_id": discipline_id}, limit=10) if discipline_id else []
    )
    return result
