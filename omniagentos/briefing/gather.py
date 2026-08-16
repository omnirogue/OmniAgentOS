"""Collect the prior day's signals for a truthful daily briefing."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time, timedelta
from datetime import date as Date
from typing import Any

from omniagentos.db.store import SqliteStore
from omniagentos.steward.config import StewardConfig
from omniagentos.steward.quoting import quote_untrusted
from omniagentos.steward.store import StewardStore


@dataclass(slots=True)
class GatherResult:
    """JSON-friendly briefing inputs and explicit degraded-state metadata."""

    comms_count: int = 0
    comms_highlights: list[dict[str, Any]] = field(default_factory=list)
    metric_deltas: list[dict[str, Any]] = field(default_factory=list)
    runs_summary: dict[str, Any] = field(
        default_factory=lambda: {"completed": 0, "failed": 0, "cost_usd": 0.0}
    )
    promoted_facts: list[str] = field(default_factory=list)
    knowledge_unavailable: bool = False
    open_approvals: int = 0
    # The store's EXACT open-alert count, which is not len(open_alerts):
    # open_alerts is a deliberately bounded page (100 rows) so the briefing
    # stays readable and the LLM prompt stays small. Anything that reports
    # "how many alerts are open" must read this field -- len() of the page
    # saturates at the page size and renders a flood as a modest, plausible
    # number, which is the favourable-absence failure exactly.
    open_alert_total: int = 0
    open_alerts: list[dict[str, Any]] = field(default_factory=list)
    open_suggestions: list[dict[str, Any]] = field(default_factory=list)
    reliability: dict[str, Any] = field(default_factory=dict)
    empty: bool = True
    briefing_date: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _window(target: Date) -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    if target == now.date():
        end = now
    else:
        end = datetime.combine(target + timedelta(days=1), time.min, tzinfo=UTC)
    return end - timedelta(hours=24), end


def _within(row: dict[str, Any], keys: tuple[str, ...], start: datetime, end: datetime) -> bool:
    stamp = next(
        (
            parsed
            for key in keys
            if row.get(key)
            if (parsed := _parse_timestamp(row.get(key))) is not None
        ),
        None,
    )
    return stamp is not None and start <= stamp <= end


def _comms(
    store: StewardStore, cfg: StewardConfig, start: datetime, end: datetime
) -> tuple[int, list[dict[str, Any]]]:
    rows = [
        row
        for row in store.list_comms_messages(limit=500)
        if _within(row, ("sent_at", "created_at"), start, end)
    ]
    vip_senders = {sender.casefold() for sender in cfg.alerts.vip_senders}

    def sort_key(row: dict[str, Any]) -> tuple[bool, datetime]:
        sender = str(row.get("sender") or "").casefold()
        stamp = _parse_timestamp(
            row.get("sent_at") or row.get("created_at")
        ) or datetime.min.replace(tzinfo=UTC)
        return sender in vip_senders, stamp

    rows.sort(key=sort_key, reverse=True)
    highlights: list[dict[str, Any]] = []
    for row in rows[:10]:
        body = str(row.get("body_text") or "")[:500]
        highlights.append(
            {
                "sender": str(row.get("sender") or "(unknown sender)"),
                "subject": str(row.get("subject") or "(no subject)"),
                "quoted": quote_untrusted(
                    body,
                    source=f"comms:{row.get('source') or 'unknown'}:{row.get('external_id') or row.get('id')}",
                    max_chars=500,
                ),
            }
        )
    return len(rows), highlights


def _metrics(
    store: StewardStore, start: datetime, end: datetime
) -> tuple[bool, list[dict[str, Any]]]:
    found_in_window = False
    deltas: list[dict[str, Any]] = []
    for goal in store.list_goals(status="active"):
        north_star = goal.get("north_star")
        if not isinstance(north_star, dict):
            continue
        source = str(north_star.get("source") or "")
        metric = str(north_star.get("metric") or "")
        if not metric:
            continue
        series = store.snapshot_series(
            metric, goal_id=str(goal["id"]), source=source or None, days=30
        )
        eligible = [
            row
            for row in series
            if (stamp := _parse_timestamp(row.get("captured_at"))) is not None and stamp <= end
        ]
        if not eligible:
            continue
        latest = eligible[-1]
        latest_at = _parse_timestamp(latest.get("captured_at"))
        if latest_at is None or latest_at < start:
            continue
        found_in_window = True
        previous = eligible[-2] if len(eligible) > 1 else None
        latest_value = float(latest.get("value") or 0.0)
        previous_value = float(previous.get("value") or 0.0) if previous is not None else None
        delta_pct = (
            ((latest_value - previous_value) / abs(previous_value)) * 100.0
            if previous_value not in (None, 0.0)
            else None
        )
        deltas.append(
            {
                "goal": str(goal.get("name") or goal["id"]),
                "metric": metric,
                "latest": latest_value,
                "previous": previous_value,
                "delta_pct": round(delta_pct, 2) if delta_pct is not None else None,
            }
        )
    return found_in_window, deltas


def _runs(store: SqliteStore, start: datetime, end: datetime) -> tuple[bool, dict[str, Any]]:
    rows = [
        row
        for row in store.list_runs({}, limit=500)
        if _within(row, ("created_at", "queued_at"), start, end)
    ]
    return bool(rows), {
        "completed": sum(row.get("state") == "completed" for row in rows),
        "failed": sum(row.get("state") == "failed" for row in rows),
        "cost_usd": round(sum(float(row.get("cost_usd") or 0.0) for row in rows), 6),
    }


def _reliability(sst: SqliteStore, start: datetime, end: datetime) -> dict[str, Any]:
    """V2 reliability digest for the briefing; {} when the 042 tables are absent."""
    window = (start.strftime("%Y-%m-%dT%H:%M:%SZ"), end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    try:
        conn = sst._connection
        open_events = conn.execute(
            "SELECT severity, COUNT(*) AS n FROM reliability_events WHERE status IN ('open','recovering') GROUP BY severity"
        ).fetchall()
        awaiting = conn.execute(
            "SELECT COUNT(*) AS n FROM improvements WHERE status IN ('awaiting_human','panel_blocked')"
        ).fetchone()
        applied = conn.execute(
            "SELECT COUNT(*) AS n FROM improvements WHERE applied_at >= ? AND applied_at < ?",
            window,
        ).fetchone()
        rolled_back = conn.execute(
            "SELECT COUNT(*) AS n FROM improvements WHERE status = 'rolled_back' AND updated_at >= ? AND updated_at < ?",
            window,
        ).fetchone()
    except Exception:  # noqa: BLE001 - pre-042 DB must never fail the briefing
        return {}
    return {
        "open_events": {str(row["severity"]): int(row["n"]) for row in open_events},
        "improvements_awaiting_decision": int(awaiting["n"]) if awaiting else 0,
        "improvements_applied": int(applied["n"]) if applied else 0,
        "improvements_rolled_back": int(rolled_back["n"]) if rolled_back else 0,
    }


def _promoted_facts() -> tuple[list[str], bool]:
    try:
        from omniagentos.knowledge import config as knowledge_config
        from omniagentos.knowledge.store import KnowledgeStore

        enabled = getattr(knowledge_config, "enabled", knowledge_config.knowledge_enabled)
        if not enabled():
            return [], False
        knowledge = KnowledgeStore(dsn=knowledge_config.dsn())
        try:
            snapshot = knowledge.graph_snapshot(limit_nodes=500)
            active = [
                str(fact.get("statement") or "")
                for fact in snapshot.get("facts", [])
                if fact.get("status") == "active" and fact.get("statement")
            ]
            return active[:5], False
        finally:
            knowledge.close()
    except Exception:
        return [], True


def _has_critical_reliability_signal(reliability: dict[str, Any]) -> bool:
    """True when open critical events or improvements-awaiting-decision exist.

    codex #19: a day can be quiet on comms/metrics/runs while a critical
    reliability event (or an improvement stuck awaiting a human decision) is
    open — that must never be reported as an empty "nothing to report" day.
    """
    open_events = reliability.get("open_events") or {}
    open_critical = int(open_events.get("critical") or 0)
    awaiting = int(reliability.get("improvements_awaiting_decision") or 0)
    return open_critical > 0 or awaiting > 0


def gather(
    store: StewardStore,
    sst: SqliteStore,
    cfg: StewardConfig,
    *,
    date: Date,
) -> GatherResult:
    """Gather the 24-hour briefing window without allowing knowledge outages to fail it."""
    start, end = _window(date)
    comms_count, highlights = _comms(store, cfg, start, end)
    has_snapshots, metric_deltas = _metrics(store, start, end)
    has_runs, runs_summary = _runs(sst, start, end)
    promoted_facts, knowledge_unavailable = _promoted_facts()
    reliability = _reliability(sst, start, end)
    empty = (
        not comms_count
        and not has_snapshots
        and not has_runs
        and not _has_critical_reliability_signal(reliability)
    )
    return GatherResult(
        comms_count=comms_count,
        comms_highlights=highlights,
        metric_deltas=metric_deltas,
        runs_summary=runs_summary,
        promoted_facts=promoted_facts,
        knowledge_unavailable=knowledge_unavailable,
        open_approvals=len(sst.list_approvals(state="pending", limit=500)),
        open_alert_total=store.open_alert_count(),
        open_alerts=store.list_alerts(state="open", limit=100),
        open_suggestions=store.list_suggestions(state="open", limit=100),
        reliability=reliability,
        empty=empty,
        briefing_date=date.isoformat(),
    )


__all__ = ["GatherResult", "gather"]
