"""Pure alert-rule evaluation over persisted Steward rows."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from omniagentos.steward.config import AlertsConfig
from omniagentos.steward.quoting import quote_untrusted


@dataclass(frozen=True, slots=True)
class AlertCandidate:
    """A pure rule result; borderline candidates retain their source message for triage."""

    rule: str
    severity: str
    title: str
    body: str
    evidence: dict[str, Any]
    cooldown_key: str
    message: dict[str, Any] | None = None
    # H12: a monotonic "how bad is this instance" value passed through to
    # store.create_alert so a worsening incident escalates through the
    # cooldown window even when severity doesn't change (e.g. ROAS sliding
    # from 0.9 to 0.2 while still "critical" both times). None when a rule has
    # no natural magnitude (vip_urgent, borderline/llm_triage).
    magnitude: float | None = None


def _number(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest(rows: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    return max(
        rows, key=lambda row: (str(row.get("captured_at", "")), int(row.get("id", 0))), default=None
    )


def _matching(rows: Iterable[dict[str, Any]], metric: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("metric") in (None, metric)]


def _matched_pattern(message: dict[str, Any], cfg: AlertsConfig) -> str | None:
    text = f"{message.get('subject') or ''}\n{message.get('body_text') or ''}".casefold()
    return next((pattern for pattern in cfg.urgent_patterns if pattern.casefold() in text), None)


def _message_body(message: dict[str, Any]) -> str:
    content = f"Subject: {message.get('subject') or ''}\n\n{message.get('body_text') or ''}"
    return quote_untrusted(
        content, source=f"comms:{message.get('source') or 'unknown'}", max_chars=1000
    )


def roas_floor(snapshots: Iterable[dict[str, Any]], cfg: AlertsConfig) -> list[AlertCandidate]:
    """Alert when the newest ROAS measurement is below the configured floor."""
    latest = _latest(_matching(snapshots, "roas"))
    value = _number(latest.get("value")) if latest is not None else None
    if latest is None or value is None or value >= cfg.roas_floor:
        return []
    return [
        AlertCandidate(
            rule="roas_floor",
            severity="critical",
            title="ROAS below configured floor",
            body=f"Latest ROAS is {value:.2f}, below the configured floor of {cfg.roas_floor:.2f}.",
            evidence={
                "metric": "roas",
                "value": value,
                "floor": cfg.roas_floor,
                "snapshot_id": latest.get("id"),
            },
            cooldown_key="roas_floor",
            # H12: shortfall below the floor -- a further ROAS slide escalates
            # through cooldown even though severity stays "critical".
            magnitude=max(0.0, cfg.roas_floor - value),
        )
    ]


def _snapshot_day(snapshot: dict[str, Any]) -> date | None:
    stamp = str(snapshot.get("captured_at") or "")
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _spend_by_day(series: Iterable[dict[str, Any]]) -> dict[date, dict[str, Any]]:
    """Latest spend_usd snapshot per calendar day (ignores rows without a value)."""
    by_day: dict[date, dict[str, Any]] = {}
    for row in _matching(series, "spend_usd"):
        day = _snapshot_day(row)
        if day is None or _number(row.get("value")) is None:
            continue
        prior = by_day.get(day)
        if prior is None or str(row.get("captured_at", "")) > str(prior.get("captured_at", "")):
            by_day[day] = row
    return by_day


def spend_spike(series: Iterable[dict[str, Any]], cfg: AlertsConfig) -> list[AlertCandidate]:
    """Alert when today's spend exceeds the recent daily baseline by the threshold."""
    by_day = _spend_by_day(series)
    if not by_day:
        return []
    today = max(by_day)
    current = _number(by_day[today].get("value"))
    assert current is not None
    prior_values = [
        value
        for day, row in by_day.items()
        if 0 < (today - day).days <= 7
        if (value := _number(row.get("value"))) is not None
    ]
    if len(prior_values) < 3:
        return []
    average = sum(prior_values) / len(prior_values)
    threshold = average * (1 + cfg.spend_spike_pct / 100)
    if current <= threshold:
        return []
    return [
        AlertCandidate(
            rule="spend_spike",
            severity="high",
            title="Advertising spend spike detected",
            body=(
                f"Today's spend is ${current:.2f}, above the ${average:.2f} average of "
                f"the prior {len(prior_values)} days by more than {cfg.spend_spike_pct:.0f}%."
            ),
            evidence={
                "metric": "spend_usd",
                "today": current,
                "prior_average": average,
                "prior_days": len(prior_values),
                "threshold_pct": cfg.spend_spike_pct,
            },
            cooldown_key="spend_spike",
            # H12: the excess spend over baseline -- a bigger spike escalates.
            magnitude=max(0.0, current - average),
        )
    ]


def spend_spike_intraday(
    series: Iterable[dict[str, Any]], cfg: AlertsConfig
) -> list[AlertCandidate]:
    """Alert when today's spend exceeds a configured absolute USD cap.

    Unlike ``spend_spike``, this rule does not need prior-day baseline history:
    a single day's ``spend_usd`` is enough. Fires only when
    ``spend_spike_absolute_cap_usd`` is set (non-None) and today's spend
    exceeds that cap. Own rule name / cooldown key so it never collides with
    the baseline-relative ``spend_spike`` path. Severity is critical — this
    is the signal intended to trip the spend circuit breaker on day one of a
    runaway campaign.
    """
    # Optional field: may be absent on older AlertsConfig instances. getattr
    # keeps this rule pure and callable without a config.py schema change.
    cap_raw = getattr(cfg, "spend_spike_absolute_cap_usd", None)
    if cap_raw is None:
        return []
    try:
        cap = float(cap_raw)
    except (TypeError, ValueError):
        return []
    by_day = _spend_by_day(series)
    if not by_day:
        return []
    today = max(by_day)
    current = _number(by_day[today].get("value"))
    assert current is not None
    if current <= cap:
        return []
    return [
        AlertCandidate(
            rule="spend_spike_intraday",
            severity="critical",
            title="Intraday advertising spend cap exceeded",
            body=(
                f"Today's spend is ${current:.2f}, above the configured absolute cap of "
                f"${cap:.2f} (no multi-day baseline required)."
            ),
            evidence={
                "metric": "spend_usd",
                "today": current,
                "absolute_cap_usd": cap,
                "snapshot_id": by_day[today].get("id"),
            },
            cooldown_key="spend_spike_intraday",
            magnitude=max(0.0, current - cap),
        )
    ]


def payment_failure_burst(
    snapshots: Iterable[dict[str, Any]], cfg: AlertsConfig
) -> list[AlertCandidate]:
    """Alert when the latest payment-failure count reaches the configured burst."""
    latest = _latest(_matching(snapshots, "payment_failures"))
    value = _number(latest.get("value")) if latest is not None else None
    if latest is None or value is None or value < cfg.payment_failure_burst:
        return []
    return [
        AlertCandidate(
            rule="payment_failures",
            severity="high",
            title="Payment failure burst detected",
            body=(
                f"Latest payment-failure count is {value:g}, meeting the configured burst "
                f"threshold of {cfg.payment_failure_burst}."
            ),
            evidence={
                "metric": "payment_failures",
                "value": value,
                "burst": cfg.payment_failure_burst,
                "snapshot_id": latest.get("id"),
            },
            cooldown_key="payment_failures",
            # H12: the failure count itself -- a bigger burst escalates.
            magnitude=value,
        )
    ]


def revenue_data_quality(
    rows: Iterable[dict[str, Any]], *, threshold: int, day: str
) -> list[AlertCandidate]:
    """Alert CRITICAL when a revenue SOURCE (not the revenue figure) has gone dark.

    This is the fail-loud counterpart to :func:`revenue_drop`: that rule fires on
    a bad NUMBER, this one fires on a source that has stopped producing any
    number at all -- Stripe unconfigured, a broker error, an unreadable response
    shape -- for ``threshold`` or more consecutive ET calendar days.

    ``rows`` are :meth:`omniagentos.revenue.store.RevenueStore.source_status_snapshot`
    shape: one dict per (vertical, source) with ``status_key``, ``last_day``,
    ``last_status`` ('ok' | 'failed' | 'unconfigured'), and ``consecutive_failures``.
    Only rows whose ``last_day`` matches ``day`` count -- a source's PAST streak
    that has since recovered (an 'ok' row from an earlier collection today) must
    not keep firing once the ledger has moved past it.
    """
    red = sorted(
        str(row["status_key"])
        for row in rows
        if row.get("last_day") == day
        and row.get("last_status") != "ok"
        and int(row.get("consecutive_failures") or 0) >= threshold
    )
    if not red:
        return []
    worst_streak = max(
        (int(row.get("consecutive_failures") or 0) for row in rows if row.get("status_key") in red),
        default=threshold,
    )
    return [
        AlertCandidate(
            rule="revenue_data_quality",
            severity="critical",
            title="Revenue data quality is RED",
            body=(
                f"{len(red)} revenue source(s) have been failed or unconfigured for "
                f"{threshold}+ consecutive days as of {day}: {', '.join(red)}. A source "
                "that writes nothing real can no longer look green."
            ),
            evidence={"day": day, "threshold": threshold, "red_sources": red},
            cooldown_key="revenue_data_quality",
            # H12-style: a longer streak is a worse instance of the same alert.
            magnitude=float(worst_streak),
        )
    ]


# H10/PROD-001: "a couple days" of prior net_revenue_usd history required
# before the percentage-drop check can fire -- mirrors spend_spike's fixed
# >=3-day guard shape, kept independent of the (larger, configurable)
# revenue_baseline_days averaging window so day-2 data doesn't misfire a
# baseline computed from a single point.
_MIN_REVENUE_BASELINE_DAYS = 2


def revenue_drop(series: Iterable[dict[str, Any]], cfg: AlertsConfig) -> list[AlertCandidate]:
    """Alert CRITICAL when revenue is at/below the floor, or crashes vs. baseline.

    This is the flagship "alert me if my revenue stops" signal (H10/PROD-001,
    the operator explicitly asked for it). Two independent triggers over the latest
    day's net_revenue_usd snapshot:

    - floor: fires from day ONE, no history required (default floor 0.0 means
      any zero-or-negative-revenue day alerts).
    - trailing-baseline pct drop: needs >= ``_MIN_REVENUE_BASELINE_DAYS`` of
      prior daily values (within the ``revenue_baseline_days`` window) before
      it can fire, since a single early data point can't establish a
      meaningful baseline (same shape as spend_spike's >=3-day guard).
    """
    by_day: dict[date, dict[str, Any]] = {}
    for row in _matching(series, "net_revenue_usd"):
        day = _snapshot_day(row)
        if day is None or _number(row.get("value")) is None:
            continue
        prior = by_day.get(day)
        if prior is None or str(row.get("captured_at", "")) > str(prior.get("captured_at", "")):
            by_day[day] = row
    if not by_day:
        return []
    today = max(by_day)
    latest_row = by_day[today]
    current = _number(latest_row.get("value"))
    assert current is not None

    prior_values = [
        value
        for day, row in by_day.items()
        if 0 < (today - day).days <= cfg.revenue_baseline_days
        if (value := _number(row.get("value"))) is not None
    ]
    baseline_avg = sum(prior_values) / len(prior_values) if prior_values else None

    drop_pct: float | None = None
    if baseline_avg is not None and baseline_avg > 0:
        drop_pct = (baseline_avg - current) / baseline_avg * 100

    floor_triggered = current <= cfg.revenue_floor_usd
    pct_triggered = (
        drop_pct is not None
        and len(prior_values) >= _MIN_REVENUE_BASELINE_DAYS
        and drop_pct >= cfg.revenue_drop_pct
    )
    if not floor_triggered and not pct_triggered:
        return []

    # H12: "the drop amount" -- prefer the trailing-baseline shortfall when we
    # have any prior history (more informative), else fall back to the
    # floor shortfall for a day-one alert. Either way, a bigger crash yields a
    # bigger magnitude so it can escalate through cooldown.
    magnitude = max(
        0.0,
        (baseline_avg - current) if baseline_avg is not None else (cfg.revenue_floor_usd - current),
    )

    if floor_triggered:
        body = (
            f"Latest net_revenue_usd is ${current:.2f}, at or below the configured floor of "
            f"${cfg.revenue_floor_usd:.2f}."
        )
    else:
        assert drop_pct is not None and baseline_avg is not None
        body = (
            f"Latest net_revenue_usd is ${current:.2f}, down {drop_pct:.0f}% from the "
            f"${baseline_avg:.2f} average of the prior {len(prior_values)} days "
            f"(threshold {cfg.revenue_drop_pct:.0f}%)."
        )

    return [
        AlertCandidate(
            rule="revenue_drop",
            severity="critical",
            title="Revenue crash detected",
            body=body,
            evidence={
                "metric": "net_revenue_usd",
                "value": current,
                "floor": cfg.revenue_floor_usd,
                "baseline_avg": baseline_avg,
                "drop_pct": drop_pct,
                "prior_days": len(prior_values),
                "snapshot_id": latest_row.get("id"),
                "floor_triggered": floor_triggered,
                "pct_triggered": pct_triggered,
            },
            cooldown_key="revenue_drop",
            magnitude=magnitude,
        )
    ]


def vip_urgent(messages: Iterable[dict[str, Any]], cfg: AlertsConfig) -> list[AlertCandidate]:
    """Create critical candidates for urgent-pattern messages from VIP senders."""
    vip_senders = {sender.casefold() for sender in cfg.vip_senders}
    candidates: list[AlertCandidate] = []
    for message in messages:
        sender = str(message.get("sender") or "")
        pattern = _matched_pattern(message, cfg)
        if sender.casefold() not in vip_senders or pattern is None:
            continue
        candidates.append(
            AlertCandidate(
                rule="vip_urgent",
                severity="critical",
                title="Urgent message from VIP sender",
                body=_message_body(message),
                evidence={"message_id": message.get("id"), "sender": sender, "pattern": pattern},
                cooldown_key=f"vip:{sender.casefold()}",
            )
        )
    return candidates


def borderline_urgent(
    messages: Iterable[dict[str, Any]], cfg: AlertsConfig
) -> list[AlertCandidate]:
    """Return non-VIP pattern hits for optional LLM triage, never direct alerts."""
    vip_senders = {sender.casefold() for sender in cfg.vip_senders}
    candidates: list[AlertCandidate] = []
    for message in messages:
        sender = str(message.get("sender") or "")
        pattern = _matched_pattern(message, cfg)
        if sender.casefold() in vip_senders or pattern is None:
            continue
        candidates.append(
            AlertCandidate(
                rule="borderline_urgent",
                severity="triage",
                title="Borderline urgent message",
                body=_message_body(message),
                evidence={"message_id": message.get("id"), "sender": sender, "pattern": pattern},
                cooldown_key=f"triage:{message.get('id') or message.get('external_id') or 'unknown'}",
                message=message,
            )
        )
    return candidates


# V2 reliability dead-man's switch (V2-DESIGN §11 M5). Lives HERE — outside
# omniagentos/reliability/** — so a change that blinds the reliability system
# cannot also silence the alarm that detects the blinding. Tier-S protected.
_WATCH_STALE_MINUTES = 45.0
_AUDIT_STALE_HOURS = 14.0


def reliability_deadman(
    watch_cursor: dict[str, Any] | None,
    latest_audit: dict[str, Any] | None,
    now: datetime,
) -> list[AlertCandidate]:
    """Alert when the reliability watch/audit jobs themselves have gone silent.

    No candidates when both rows are absent (pre-migration or never-deployed DB);
    once either subsystem has durable state, absent/corrupt/stale counterparts
    become explicit failures. Store read failures use ``_state=store_error`` from
    the monitor seam so they cannot masquerade as an empty, healthy database.
    """
    now = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    candidates: list[AlertCandidate] = []

    store_errors: list[tuple[str, str]] = []
    watch_store_error = bool(watch_cursor and watch_cursor.get("_state") == "store_error")
    audit_store_error = bool(latest_audit and latest_audit.get("_state") == "store_error")
    for component, row in (
        ("watch", watch_cursor),
        ("audit", latest_audit),
    ):
        if row and row.get("_state") == "store_error":
            store_errors.append((component, str(row.get("error_type") or "store_error")))
    if store_errors:
        components = sorted(component for component, _ in store_errors)
        error_types = sorted({error_type for _, error_type in store_errors})
        candidates.append(
            AlertCandidate(
                rule="reliability_deadman",
                severity="critical",
                title="Reliability state store is unavailable",
                body=(
                    "The Steward could not read reliability liveness state for "
                    f"{', '.join(components)}. Monitoring health is degraded until "
                    "the durable state store is readable."
                ),
                evidence={
                    "state": "unavailable",
                    "components": components,
                    "error_types": error_types,
                },
                cooldown_key="reliability_deadman_store",
            )
        )

    readable_watch = (
        watch_cursor if watch_cursor and watch_cursor.get("_state") != "store_error" else None
    )
    readable_audit = (
        latest_audit if latest_audit and latest_audit.get("_state") != "store_error" else None
    )
    if readable_watch is None and readable_audit is None:
        return candidates

    if readable_watch is None:
        if watch_store_error:
            return candidates
        candidates.append(
            AlertCandidate(
                rule="reliability_deadman",
                severity="critical",
                title="Reliability watch has never run",
                body=(
                    "Reliability audit state exists, but no watch heartbeat has ever "
                    "been recorded. The fast failure-detection loop is not running."
                ),
                evidence={"state": "never_run"},
                cooldown_key="reliability_deadman_watch",
            )
        )
        return candidates

    cursor_ts = _parse_utc(readable_watch.get("updated_at"))
    if cursor_ts is None:
        candidates.append(
            AlertCandidate(
                rule="reliability_deadman",
                severity="critical",
                title="Reliability watch state is corrupt",
                body=(
                    "The durable watch row has no valid heartbeat timestamp. "
                    "Watcher liveness cannot be established."
                ),
                evidence={
                    "state": "corrupt",
                    "watch_cursor_updated_at": readable_watch.get("updated_at"),
                },
                cooldown_key="reliability_deadman_watch_state",
            )
        )
    if cursor_ts is not None:
        stale_min = (now - cursor_ts).total_seconds() / 60.0
        if stale_min > _WATCH_STALE_MINUTES:
            candidates.append(
                AlertCandidate(
                    rule="reliability_deadman",
                    severity="critical",
                    title="Reliability watch has gone silent",
                    body=(
                        f"The reliability watch heartbeat has not updated in {stale_min:.0f} minutes "
                        f"(threshold {_WATCH_STALE_MINUTES:.0f}m). The failure-detection loop may be "
                        "dead, wedged, or unloaded — silence from the reliability system is itself a failure."
                    ),
                    evidence={
                        "state": "stale",
                        "watch_cursor_updated_at": readable_watch.get("updated_at"),
                        "stale_minutes": stale_min,
                    },
                    cooldown_key="reliability_deadman_watch",
                    magnitude=stale_min,
                )
            )

    watch_payload: dict[str, Any] | None = None
    try:
        decoded = json.loads(readable_watch.get("value_json") or "{}")
        if not isinstance(decoded, dict):
            raise ValueError("watch state is not an object")
        watch_payload = decoded
    except (TypeError, ValueError):
        candidates.append(
            AlertCandidate(
                rule="reliability_deadman",
                severity="critical",
                title="Reliability watch payload is corrupt",
                body=(
                    "The watch heartbeat row exists, but its durable state payload "
                    "cannot be decoded. Audit liveness may be unavailable."
                ),
                evidence={"state": "corrupt"},
                cooldown_key="reliability_deadman_watch_state",
            )
        )

    audit_state = "stale"
    audit_ts = None
    if audit_store_error:
        return candidates
    if readable_audit is not None:
        audit_ts = _parse_utc(readable_audit.get("started_at"))
        if audit_ts is None:
            candidates.append(
                AlertCandidate(
                    rule="reliability_deadman",
                    severity="critical",
                    title="Reliability audit state is corrupt",
                    body=(
                        "The newest reliability audit has no valid start timestamp. "
                        "Twice-daily audit liveness cannot be established."
                    ),
                    evidence={
                        "state": "corrupt",
                        "last_audit_id": readable_audit.get("id"),
                        "last_audit_started_at": readable_audit.get("started_at"),
                    },
                    cooldown_key="reliability_deadman_audit_state",
                )
            )
    else:
        # No audit has ever been recorded. Baseline only on the set-once
        # first_seen stamp; using the continuously refreshed heartbeat would
        # postpone this alarm forever.
        audit_state = "never_run"
        first_seen = (
            _parse_utc(watch_payload.get("first_seen")) if watch_payload is not None else None
        )
        if first_seen is None:
            candidates.append(
                AlertCandidate(
                    rule="reliability_deadman",
                    severity="critical",
                    title="Reliability audit baseline is corrupt",
                    body=(
                        "No reliability audit exists and the watch state has no "
                        "valid set-once first_seen timestamp."
                    ),
                    evidence={"state": "corrupt"},
                    cooldown_key="reliability_deadman_audit_state",
                )
            )
        audit_ts = first_seen

    if audit_ts is not None:
        stale_h = (now - audit_ts).total_seconds() / 3600.0
        if stale_h > _AUDIT_STALE_HOURS:
            last_audit_id = readable_audit.get("id") if readable_audit is not None else None
            last_audit_started_at = (
                readable_audit.get("started_at") if readable_audit is not None else None
            )
            candidates.append(
                AlertCandidate(
                    rule="reliability_deadman",
                    severity="critical",
                    title=(
                        "Reliability audit has never run"
                        if audit_state == "never_run"
                        else "Reliability twice-daily audit overdue"
                    ),
                    body=(
                        f"No reliability audit has started in {stale_h:.1f} hours "
                        f"(threshold {_AUDIT_STALE_HOURS:.0f}h; expected twice daily)."
                    ),
                    evidence={
                        "state": audit_state,
                        "last_audit_id": last_audit_id,
                        "last_audit_started_at": last_audit_started_at,
                        "stale_hours": stale_h,
                    },
                    cooldown_key="reliability_deadman_audit",
                    magnitude=stale_h,
                )
            )
    return candidates


def goal_limbo(rows: Iterable[dict[str, Any]]) -> list[AlertCandidate]:
    """Shape pre-selected board-task limbo rows into alert candidates.

    ``rows`` come from ``omniagentos.intake.board_sweep.goal_limbo_candidates``
    (already filtered to open/blocked/awaiting_approval cards with no
    ``updated_at`` touch inside the configured window) -- this function only
    shapes them, mirroring every other rule here.

    STABLE KEYS, reusing the already-established pattern instead of a
    parallel scheme: cooldown_key names the CONDITION (this specific card,
    bucketed by owner/goal), never a measurement like age -- same discipline
    as integrity.py's per-category episode keys (commit 380d68d98) and every
    other rule's cooldown_key in this module. ``store.create_alert``'s
    (rule, cooldown_key) case identity is the dedup mechanism itself: repeat
    firings while the card stays untouched collapse into the same open row
    (suppressed); once the card is touched (or archived, or leaves a limbo
    status) the rule stops reporting that key and the monitor's
    auto-resolve closes the case, so a later relapse into limbo opens a
    fresh alert -- this rule never re-invents that bookkeeping itself.
    """
    from omniagentos.intake.board_sweep import limbo_recommended_action

    candidates: list[AlertCandidate] = []
    for task in rows:
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        status = str(task.get("status") or "")
        owner = str(task.get("owner_employee_id") or "unassigned")
        goal = str(task.get("goal_id") or "no_goal")
        recommendation = limbo_recommended_action(task)
        severity = "high" if status in {"awaiting_approval", "blocked"} else "medium"
        updated_at_unknown = bool(task.get("updated_at_unknown"))
        touch = (
            "an UNREADABLE last-touch timestamp (instrument problem; treated as limbo)"
            if updated_at_unknown
            else "no recorded touch"
        )
        candidates.append(
            AlertCandidate(
                rule="goal_limbo",
                severity=severity,
                title=f"Board card stuck in {status}",
                body=(
                    f"Card {task_id} ({task.get('title') or 'untitled'}) has sat "
                    f"{status} with {touch}. owner={owner} goal={goal}. "
                    f"Recommended action: {recommendation}"
                ),
                evidence={
                    "task_id": task_id,
                    "status": status,
                    "owner_employee_id": task.get("owner_employee_id"),
                    "goal_id": task.get("goal_id"),
                    "blocked_reason": task.get("blocked_reason"),
                    "updated_at": task.get("updated_at"),
                    "updated_at_unknown": updated_at_unknown,
                    "recommended_action": recommendation,
                },
                cooldown_key=f"goal_limbo:{owner}:{goal}:{task_id}",
            )
        )
    return candidates


def _parse_utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# ---------------------------------------------------------------------------
# Executive Decision Center (EDC, migration 130) — pure classification
# primitives (synthesis §4). Same shape as vip_urgent/borderline_urgent: a
# message dict in, a structured verdict out, no I/O. They are the DETERMINISTIC
# BACKSTOP the round-1 review flagged as load-bearing, and CODEX F04 hinges on
# them: ``edc_consequence`` + ``edc_deadline`` + ``edc_active_harm`` are HARM
# detection and are computed BEFORE any suppression. Scary WORDING
# ("urgent"/"asap") is deliberately NOT a harm signal here — only structural
# notice phrasing (payment FAILED, account SUSPENDED, security BREACH) is. That
# is the difference between "URGENT!! 50% off" (noise) and "your payment failed,
# account suspended tonight" (a real emergency).
# ---------------------------------------------------------------------------


def _edc_text(message: dict[str, Any]) -> str:
    """Casefolded subject + body + sender, the surface every primitive scans."""
    parts = [
        str(message.get("subject") or ""),
        str(message.get("body_text") or message.get("body") or ""),
        str(message.get("sender") or message.get("counterparty") or ""),
    ]
    return " ".join(parts).casefold()


# Consequence buckets, evaluated in PRIORITY order (first match wins). Each
# pattern is STRUCTURAL — the phrasing a provider/system notice uses when a real
# consequence is in play — never a mere urgency adjective. A plain "invoice
# attached" or "urgent sale" matches nothing here (consequence == 'none'), which
# is exactly why a fake invoice / "URGENT" marketing blast stays suppressible.
_EDC_CONSEQUENCE_ORDER: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "security",
        re.compile(
            r"unauthori[sz]ed|suspicious (?:sign-?in|login|activity)|"
            r"security (?:alert|incident|breach|warning)|data breach|compromis(?:e|ed)|"
            r"unusual sign-?in|we (?:detected|noticed) (?:a |an |unusual |suspicious )|"
            r"verify your identity|password (?:was|has been) reset|two-factor"
        ),
    ),
    (
        "financial",
        re.compile(
            r"payment (?:failed|declined|was declined|could not be|couldn'?t be|"
            r"unsuccessful|did ?n'?t go through|has failed)|card (?:was )?declined|"
            r"unable to (?:process|charge|collect)|billing (?:problem|issue|failure|failed)|"
            r"past due|overdue (?:invoice|payment|balance)|charge (?:failed|was declined)|"
            r"insufficient funds|renewal (?:payment )?failed|update your payment(?: method)?|"
            r"payment method (?:on file )?(?:has )?expired|expired card"
        ),
    ),
    (
        "service_loss",
        re.compile(
            r"suspend(?:ed|ing)?|will be (?:cancell?ed|terminated|closed|deactivated|"
            r"shut ?down|disabled)|shutting down|shut ?down|service (?:interruption|"
            r"will end|termination)|downgrade(?:d)?|account (?:closure|termination|"
            r"deactivation|suspension)|lose access|deactivat(?:e|ed|ion)|terminat(?:e|ed|ion)"
        ),
    ),
    (
        "legal",
        re.compile(
            r"lawsuit|legal (?:action|notice|deadline|matter)|cease and desist|subpoena|"
            r"dmca|infringement|breach of contract|contract (?:expires|expir|renewal|"
            r"termination|dispute|issue|amendment)|compliance deadline|gdpr|"
            r"court (?:date|order)|litigation"
        ),
    ),
    (
        "account",
        re.compile(
            r"approval (?:required|needed|request(?:ed)?)|please approve|"
            r"awaiting your approval|needs? your (?:approval|sign-?off|decision|input|review)|"
            r"your (?:approval|decision|sign-?off) (?:is )?(?:required|needed|requested)|"
            r"requires your (?:authori[sz]ation|signature|approval)|"
            r"sign (?:the|this) (?:contract|agreement|document)|waiting (?:for|on) (?:your|you)|"
            r"vendor decision|which (?:vendor|option) (?:should|do you)"
        ),
    ),
)

# ACTIVE harm — present/imminent tense that makes a consequence urgent EVEN
# WITHOUT a parsed deadline (a breach is happening now; an account is already
# suspended). Distinct from a future request ("update within 7 days"), which is
# a consequence but not an emergency.
_EDC_ACTIVE_HARM = re.compile(
    r"unauthori[sz]ed (?:access|login|sign-?in|charge|transaction)|"
    r"we (?:detected|noticed) (?:unusual|suspicious)|"
    r"suspicious (?:sign-?in|login|activity)|security (?:breach|incident)|"
    r"(?:has|have) been (?:suspended|disabled|locked|compromised|deactivated|closed|terminated)|"
    r"account (?:is|was) (?:suspended|locked|disabled)|"
    r"payment (?:has )?(?:failed|declined|was declined)|"
    r"will be (?:suspended|cancell?ed|terminated|closed|deactivated|disabled|shut ?down)|"
    r"is being (?:suspended|terminated)"
)

# Suppression signals (IGNORE layer). Bulk/marketing/receipt/social shapes only.
# NOTE (F04): edc_suppress is NEVER consulted for a harm-bearing message — the
# classifier calls it only after consequence detection has returned 'none'.
_EDC_NOREPLY_SENDER = re.compile(
    r"(?:no-?reply|do-?not-?reply|donotreply|bounces?|mailer-daemon|postmaster|"
    r"notifications?|newsletter|news|marketing|digest|updates?|hello|team|info)@|"
    r"@(?:e|email|mail|news|marketing)\.|@(?:mailchimp|sendgrid|substack)(?:\.|$)"
)
_EDC_MARKETING = re.compile(
    r"unsubscribe|view (?:this|in) (?:email )?(?:in your )?browser|% off|\d+% off|"
    r"sale ends|limited time|special offer|coupon|discount code|flash sale|"
    r"book a (?:call|demo|meeting)|hop on a (?:quick )?call|grow your (?:revenue|business|sales)|"
    r"schedule a (?:demo|call)|free trial|quick question|new(?:s)?letter|webinar|"
    r"upgrade now|shop now|deal of the"
)
_EDC_RECEIPT = re.compile(
    r"receipt|your order|order confirmation|payment (?:received|successful|confirmation)|"
    r"thanks for your (?:payment|purchase|order)|invoice paid|subscription renewed|"
    r"your (?:invoice|statement) is ready|has shipped|out for delivery"
)
_EDC_SOCIAL = re.compile(
    r"liked your|commented on your|new follower|mentioned you|tagged you|"
    r"friend request|connection request|new message request|reacted to|"
    r"wants to connect|endorsed you"
)

# --- No-material-harm CONTEXT guards (calibration 2026-08-13) ----------------
# These narrow HARM detection so genuinely-consequence-free automated mail stops
# borrowing a consequence bucket. They only ever DOWNGRADE the weakest buckets
# (``account`` for scheduling/nudges, the identity-verify path of ``security``
# for OTPs) and never touch financial/service_loss/legal/hard-security — so a
# real payment/shutdown/breach notice is untouched (F04 harm-before-suppress
# stays intact: harm detection still runs first, it is just no longer fooled).

# Meeting / scheduling / calendar content. A meeting TIME is a deadline but not
# material harm; its agenda "decision / review / input / waiting-for-you" wording
# must not read as an approval request (the "Meeting Prep: X <> Y" false-URGENT).
_EDC_MEETING = re.compile(
    r"meeting prep|prep(?:aration)? for (?:your|the|our) (?:meeting|call|sync)|"
    r"(?:bi-?weekly|weekly|monthly|daily) sync|"
    r"calendar (?:invit|event|reminder)|meeting (?:invit|reminder|agenda|notes|recap|summary)|"
    r"\bagenda\b|reschedul|"
    r"(?:zoom|google meet|microsoft teams|webex) (?:link|call|meeting)|"
    r"\byou(?:'re| are) invited\b|join (?:the|your) (?:meeting|call|zoom)|"
    r"upcoming (?:meeting|call|sync)|your meeting (?:is|with|on)|\b1:1\b|<>"
)

# Verification-code / OTP delivery. A "verify your identity" line inside a code
# email is a login code, not a security incident.
_EDC_OTP = re.compile(
    r"verification code|one-?time (?:code|password|passcode|pin)|\botp\b|"
    r"security code|access code|login code|your code is|"
    r"(?:enter|use) (?:this|the following) code|confirm your email address|"
    r"is your (?:verification|security|login|access) code|magic link"
)

# Hard security signals that must survive even alongside OTP wording (a real
# breach alert that also mentions a code is still a real breach alert).
_EDC_SECURITY_HARD = re.compile(
    r"unauthori[sz]ed|suspicious (?:sign-?in|login|activity)|"
    r"security (?:breach|incident)|data breach|compromis(?:e|ed)|unusual sign-?in|"
    r"password (?:was|has been) reset"
)

# Hard legal signals — an actual formal/legal notice, distinct from soft
# contract-admin wording ("contract renewal", "amendment") that shows up in
# routine business correspondence (including meeting-bot agenda text) and is
# not, by itself, material harm.
_EDC_LEGAL_HARD = re.compile(
    r"lawsuit|cease and desist|subpoena|dmca|infringement|breach of contract|"
    r"court (?:date|order)|litigation"
)

# Sender classes that are INFORMATIONAL — marketing/newsletter/bulk-notice
# senders (reuses the suppress-layer no-reply/newsletter/marketing shape).
# Their content may TALK ABOUT a security/legal topic without describing harm
# actually happening to the recipient's own account — a newsletter about
# watermarks or a marketing blast about "security features" is not an
# incident. Real emails from informational-looking senders (no-reply@ breach
# notices are common) still get through because a genuinely grounded active-
# harm phrase (below) survives this gate.
_EDC_INFORMATIONAL_SENDER = _EDC_NOREPLY_SENDER

# Automated product/engagement nudges — no owner decision, no material harm.
_EDC_NUDGE = re.compile(
    r"don'?t (?:leave|forget)|you(?:'ve| have) (?:unread|\d+ new|new)|"
    r"complete your (?:profile|setup|onboarding|account)|finish (?:setting up|your setup)|"
    r"invite your (?:team|teammates|colleagues)|get started with|"
    r"you haven'?t (?:finished|completed|logged in|tried)|reminder to (?:complete|finish|try)|"
    r"waiting for you to|your team is waiting|pick up where you left off|"
    r"we miss you|come back to"
)

# Relative-deadline horizon. Business-generous "~24h": comfortably includes
# "tonight"/"today"/"tomorrow" and the explicit "within 24 hours" notice, and
# comfortably excludes "in 7 days" / "in 2 days" (those classify NEEDS_OWNER).
_EDC_IMMINENT_HOURS = 26.0
_EDC_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def edc_consequence(message: dict[str, Any]) -> str:
    """Structural consequence bucket for a message, or ``'none'`` (synthesis §4).

    One of ``security|financial|service_loss|legal|account|none``, from
    provider/system-notice structure — not adjectives. This is HARM detection:
    a non-``none`` result is NOT suppressible (F04).

    F05 grounding: a ``security``/``legal`` match must be grounded in harm to
    the RECIPIENT'S OWN account/service/legal standing, not a topical keyword
    mention. Two calibrated real false-positive shapes drove this: a
    meeting-notes bot's agenda boilerplate ("recording consent", "contract
    renewal") tripping ``legal``, and a marketing/newsletter blast tripping
    ``security`` off a bare topic word ("watermarks", "stay in control").
    ``edc_active_harm`` — present/imminent, account-directed phrasing like
    "payment has failed" / "account is suspended" / "we detected unusual
    activity" — is the grounding signal: it survives the gate because it
    names an action against the recipient's account, not a subject.
    """
    text = _edc_text(message)
    sender_text = str(message.get("sender") or message.get("counterparty") or "").casefold()
    otp = bool(_EDC_OTP.search(text))
    scheduling = bool(_EDC_MEETING.search(text))
    nudge = bool(_EDC_NUDGE.search(text))
    informational_sender = bool(_EDC_INFORMATIONAL_SENDER.search(sender_text))
    grounded = bool(_EDC_ACTIVE_HARM.search(text))
    for bucket, pattern in _EDC_CONSEQUENCE_ORDER:
        if not pattern.search(text):
            continue
        # OTP / verification-code delivery: an identity-verify line is a login
        # code, not a security incident — unless a hard-breach signal co-occurs.
        if bucket == "security" and otp and not _EDC_SECURITY_HARD.search(text):
            continue
        # Ungrounded security topic mention from an informational sender (a
        # newsletter/marketing blast merely discussing security) is not an
        # incident affecting the recipient — unless the text also grounds the
        # harm to the recipient's own account (active-harm phrasing).
        if bucket == "security" and informational_sender and not grounded:
            continue
        # Meeting/calendar-bot agenda boilerplate ("recording consent",
        # "contract renewal/amendment") and marketing/newsletter mentions of a
        # legal topic are not a legal notice unless it's a hard legal signal
        # (lawsuit/subpoena/DMCA/court date/...) or grounded harm.
        if (
            bucket == "legal"
            and (scheduling or informational_sender)
            and not _EDC_LEGAL_HARD.search(text)
            and not grounded
        ):
            continue
        # Meeting/scheduling & automated-nudge content: agenda "decision / review
        # / input / waiting-for-you" wording is a calendar/engagement matter, not
        # a material-harm approval request.
        if bucket == "account" and (scheduling or nudge):
            continue
        return bucket
    return "none"


def edc_active_harm(message: dict[str, Any]) -> bool:
    """Whether the message describes harm that is active/imminent right now.

    Makes a detected consequence URGENT even absent a parsed deadline (a live
    breach, an already-suspended account, a failed payment).
    """
    return bool(_EDC_ACTIVE_HARM.search(_edc_text(message)))


def edc_deadline(message: dict[str, Any], *, now: datetime | None = None) -> str | None:
    """Deterministically extract the earliest deadline, as ISO-Z, or ``None``.

    Handles explicit ISO dates, ``within/in N hours|days``, and the fuzzy
    business phrases (tonight/today/eod/tomorrow/by <weekday>). Relative phrases
    resolve against ``now`` so the result is testable with a fixed clock. Never
    infers a deadline from urgency wording.
    """
    now = now or datetime.now(UTC)
    text = _edc_text(message)
    candidates: list[datetime] = []

    for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        try:
            candidates.append(
                datetime(int(match[1]), int(match[2]), int(match[3]), 17, 0, tzinfo=UTC)
            )
        except ValueError:
            continue

    for match in re.finditer(r"\b(?:with ?in|in)\s+(\d+)\s*(hour|hr|day)s?\b", text):
        count = int(match[1])
        unit = match[2]
        delta = timedelta(hours=count) if unit in ("hour", "hr") else timedelta(days=count)
        candidates.append(now + delta)

    if re.search(
        r"\btonight\b|\bby end of (?:the )?day\b|\bend of day\b|\bby eod\b|\beod\b|"
        r"\bby (?:close of business|cob)\b",
        text,
    ):
        candidates.append(now + timedelta(hours=8))
    if re.search(r"\b(?:by )?today\b", text):
        candidates.append(now + timedelta(hours=10))
    if re.search(r"\b(?:by )?tomorrow\b", text):
        candidates.append(now + timedelta(hours=24))

    weekday_match = re.search(
        r"\bby (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", text
    )
    if weekday_match:
        target = _EDC_WEEKDAYS[weekday_match[1]]
        ahead = (target - now.weekday()) % 7
        ahead = ahead or 7  # "by monday" on a monday means next monday
        candidates.append((now + timedelta(days=ahead)).replace(hour=17, minute=0, second=0))

    if not candidates:
        return None
    return min(candidates).strftime("%Y-%m-%dT%H:%M:%SZ")


def edc_deadline_imminent(deadline_at: str | None, *, now: datetime | None = None) -> bool:
    """Whether ``deadline_at`` (ISO) falls within the ``~24h`` urgent horizon."""
    if not deadline_at:
        return False
    parsed = _parse_utc(deadline_at)
    if parsed is None:
        return False
    now = now or datetime.now(UTC)
    return parsed <= now + timedelta(hours=_EDC_IMMINENT_HOURS)


def edc_suppress(message: dict[str, Any], cfg: AlertsConfig | None = None) -> list[str]:
    """IGNORE-layer match ids for a message: newsletter/noreply/receipt/social.

    Returns the matched rule ids (empty = nothing to suppress). ``cfg`` is
    accepted for signature parity with the other primitives and future
    owner-tunable suppression; the deterministic net here needs no config.
    """
    del cfg  # reserved; deterministic net is config-free in P1
    sender = str(message.get("sender") or message.get("counterparty") or "").casefold()
    subject_body = (
        f"{message.get('subject') or ''} {message.get('body_text') or message.get('body') or ''}"
    ).casefold()
    list_unsub = str(message.get("list_unsubscribe") or "")
    headers = message.get("headers") or {}
    header_unsub = ""
    if isinstance(headers, dict):
        header_unsub = str(headers.get("list-unsubscribe") or headers.get("List-Unsubscribe") or "")

    matches: list[str] = []
    if list_unsub or header_unsub:
        matches.append("list_unsubscribe")
    if _EDC_NOREPLY_SENDER.search(sender):
        matches.append("noreply_sender")
    if _EDC_MARKETING.search(subject_body):
        matches.append("marketing")
    if _EDC_RECEIPT.search(subject_body):
        matches.append("routine_receipt")
    if _EDC_SOCIAL.search(subject_body):
        matches.append("social")
    if _EDC_OTP.search(subject_body):
        matches.append("verification_code")
    if _EDC_NUDGE.search(subject_body):
        matches.append("product_nudge")
    return matches


__all__ = [
    "AlertCandidate",
    "borderline_urgent",
    "edc_active_harm",
    "edc_consequence",
    "edc_deadline",
    "edc_deadline_imminent",
    "edc_suppress",
    "goal_limbo",
    "payment_failure_burst",
    "reliability_deadman",
    "revenue_drop",
    "roas_floor",
    "spend_spike",
    "spend_spike_intraday",
    "vip_urgent",
]
