"""Build the frozen ``/api/revenue`` payload from ``revenue_facts`` rows.

Pure and side-effect free: it takes the day's facts (as returned by
:meth:`RevenueStore.query_day`) and folds them into the exact shape the dashboard
builds against. The honesty rules live here:

  * ``gross_contribution = revenue - ad_spend`` and is labelled NOT net profit.
  * Real (Stripe) revenue and Meta *attributed* conversion value are never
    conflated: attribution surfaces only under ``by_campaign.revenue_attributed_usd``
    and drives ``roas``; it is never added into ``revenue_usd``.
  * data_quality notes are derived honestly, including the ambiguity of an absent
    source (unconfigured vs. failed vs. genuinely zero — indistinguishable from
    stored facts alone).
"""

from __future__ import annotations

from typing import Any

from omniagentos.revenue.collect import BRANDS

TIMEZONE = "America/New_York"

GROSS_CONTRIBUTION_NOTE = (
    "gross contribution = revenue − ad spend. Excludes processing fees, "
    "chargebacks, and COGS (not tracked) — this is NOT net profit."
)

# Revenue/spend surfaces that exist in the business but are NOT yet wired into
# this collector. Listed so the dashboard never implies the P&L is complete.
NOT_WIRED = [
    "Google Ads",
    "Shopify",
    "COGS",
    "Stripe fees",
    "chargebacks",
    "PayPal/NMI/Vandelay/FanBasis/ChargeBlast",
]

# Deterministic vertical ordering: known brands first (config order), extras alpha.
_VERTICAL_ORDER = {brand.vertical: index for index, brand in enumerate(BRANDS)}


def _round2(value: float) -> float:
    return round(float(value), 2)


def _return_on_spend(revenue: float, spend: float) -> dict[str, float | None]:
    """Blended return metrics from REAL revenue and ad spend.

    ``blended_roas`` = revenue / ad spend (all collected revenue over all ad
    spend). ``roi`` = (revenue − ad spend) / ad spend (return on ad spend, a
    ratio: 0.5 == +50%). Both are NULL when there is no ad spend to divide by —
    never a fabricated 0, matching the honesty rule the attribution ``roas``
    already follows.

    Deliberately DISTINCT from ``roas`` (Meta-attributed conversion value ÷
    spend): this is BLENDED real revenue ÷ spend and is never conflated with
    attribution, per this module's contract. Excludes fees/COGS just like
    gross contribution — it is a spend-efficiency ratio, not a profit margin.
    """
    if not spend:
        return {"blended_roas": None, "roi": None}
    return {
        "blended_roas": _round2(revenue / spend),
        "roi": _round2((revenue - spend) / spend),
    }


def _attributed(fact: dict[str, Any]) -> float:
    meta = fact.get("meta") or {}
    try:
        return float(meta.get("revenue_attributed_usd", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _vertical_sort_key(vertical: str) -> tuple[int, str]:
    return (_VERTICAL_ORDER.get(vertical, len(_VERTICAL_ORDER)), vertical)


def build_revenue_report(
    day: str,
    facts: list[dict[str, Any]],
    *,
    generated_at: str,
    source_status: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Fold one day's facts into the frozen /api/revenue contract."""
    total_revenue = 0.0
    total_spend = 0.0
    total_failures = 0

    # Accumulators keyed by their grouping dimension.
    verticals: dict[str, dict[str, float]] = {}
    sources: dict[tuple[str, str], dict[str, float]] = {}
    campaigns: list[dict[str, Any]] = []

    for fact in facts:
        vertical = str(fact.get("vertical", ""))
        source = str(fact.get("source", ""))
        revenue = float(fact.get("revenue_usd", 0.0) or 0.0)
        spend = float(fact.get("ad_spend_usd", 0.0) or 0.0)
        failures = int(fact.get("payment_failures", 0) or 0)

        total_revenue += revenue
        total_spend += spend
        total_failures += failures

        vslot = verticals.setdefault(
            vertical, {"revenue": 0.0, "spend": 0.0, "failures": 0.0, "attributed": 0.0}
        )
        vslot["revenue"] += revenue
        vslot["spend"] += spend
        vslot["failures"] += failures

        sslot = sources.setdefault(
            (vertical, source), {"revenue": 0.0, "spend": 0.0, "failures": 0.0}
        )
        sslot["revenue"] += revenue
        sslot["spend"] += spend
        sslot["failures"] += failures

        if source == "meta":
            attributed = _attributed(fact)
            vslot["attributed"] += attributed
            roas = fact.get("roas")
            campaigns.append(
                {
                    "vertical": vertical,
                    "source": "meta",
                    "campaign_id": str(fact.get("campaign_id") or ""),
                    "campaign_name": str(fact.get("campaign_name") or ""),
                    "ad_spend_usd": _round2(spend),
                    "roas": None if roas is None else _round2(roas),
                    "revenue_attributed_usd": _round2(attributed),
                }
            )

    totals = {
        "revenue_usd": _round2(total_revenue),
        "ad_spend_usd": _round2(total_spend),
        "gross_contribution_usd": _round2(total_revenue - total_spend),
        **_return_on_spend(total_revenue, total_spend),
        "payment_failures": total_failures,
        "note": GROSS_CONTRIBUTION_NOTE,
    }

    by_vertical = [
        {
            "vertical": vertical,
            "revenue_usd": _round2(slot["revenue"]),
            "ad_spend_usd": _round2(slot["spend"]),
            "gross_contribution_usd": _round2(slot["revenue"] - slot["spend"]),
            # Attribution ROAS = Meta-attributed conversion value / ad spend. NULL
            # when there is no spend to divide by — never a fabricated 0. Kept
            # separate from the blended real-revenue metrics below.
            "roas": _round2(slot["attributed"] / slot["spend"]) if slot["spend"] else None,
            **_return_on_spend(slot["revenue"], slot["spend"]),
            "payment_failures": int(slot["failures"]),
        }
        for vertical, slot in sorted(verticals.items(), key=lambda kv: _vertical_sort_key(kv[0]))
    ]

    by_source = [
        {
            "vertical": vertical,
            "source": source,
            "revenue_usd": _round2(slot["revenue"]),
            "ad_spend_usd": _round2(slot["spend"]),
            "payment_failures": int(slot["failures"]),
        }
        for (vertical, source), slot in sorted(
            sources.items(), key=lambda kv: (_vertical_sort_key(kv[0][0]), kv[0][1])
        )
    ]

    by_campaign = sorted(
        campaigns,
        key=lambda c: (_vertical_sort_key(c["vertical"]), -c["ad_spend_usd"], c["campaign_name"]),
    )

    normalized_source_status = _source_status(source_status)
    return {
        "day": day,
        "timezone": TIMEZONE,
        "generated_at": generated_at,
        "totals": totals,
        "by_vertical": by_vertical,
        "by_source": by_source,
        "by_campaign": by_campaign,
        "data_quality": _data_quality(day, facts, sources, normalized_source_status),
        "coverage": _coverage(facts),
        "source_status": normalized_source_status,
    }


def _source_status(source_status: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Project persisted source outcomes into the additive API surface."""
    if source_status is None:
        return []

    statuses: list[dict[str, Any]] = []
    for row in source_status:
        vertical = row.get("vertical")
        source = row.get("source")
        status = row.get("last_status")
        if not all(isinstance(value, str) and value for value in (vertical, source, status)):
            continue
        if status not in {"ok", "failed", "unconfigured"}:
            continue
        try:
            consecutive_failures = max(0, int(row.get("consecutive_failures", 0)))
        except (TypeError, ValueError):
            consecutive_failures = 0
        statuses.append(
            {
                "vertical": vertical,
                "source": source,
                "status": status,
                "message": str(row.get("last_message") or ""),
                "consecutive_failures": consecutive_failures,
            }
        )
    return sorted(statuses, key=lambda row: (row["vertical"], row["source"]))


def _status_for_source(
    vertical: str, source: str, source_status: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return an exact status, with Meta-account outcomes covering Meta as a whole."""
    exact = next(
        (
            status
            for status in source_status
            if status["vertical"] == vertical and status["source"] == source
        ),
        None,
    )
    if exact is not None or source != "meta":
        return exact

    # Revenue collection persists account-level Meta outcomes as ``meta:<account>``.
    # A failed account must not disappear from the aggregate Meta quality note.
    account_statuses = [
        status
        for status in source_status
        if status["vertical"] == vertical and status["source"].startswith("meta:")
    ]
    if not account_statuses:
        return None
    return max(
        account_statuses,
        key=lambda status: (
            status["status"] == "failed",
            status["status"] == "unconfigured",
            status["consecutive_failures"],
        ),
    )


def _data_quality(
    day: str,
    facts: list[dict[str, Any]],
    sources: dict[tuple[str, str], dict[str, float]],
    source_status: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Derive honest data-quality notes from facts and persisted source outcomes."""
    notes: list[dict[str, str]] = []
    present = set(sources.keys())

    # 1) Expected (brand, source) pairs that produced no facts at all.
    for brand in BRANDS:
        for source in ("stripe", "meta"):
            if (brand.vertical, source) not in present:
                status = _status_for_source(brand.vertical, source, source_status)
                if status is not None:
                    message = status["message"] or "no collector detail recorded"
                    if status["status"] == "unconfigured":
                        notes.append(
                            {
                                "level": "info",
                                "message": (
                                    f"{brand.vertical} {source}: {message} — collection skipped."
                                ),
                            }
                        )
                    elif status["status"] == "failed":
                        notes.append(
                            {
                                "level": "warn",
                                "message": (
                                    f"{brand.vertical} {source} collection failed: {message} — "
                                    f"{status['consecutive_failures']} consecutive failure(s)."
                                ),
                            }
                        )
                    else:
                        notes.append(
                            {
                                "level": "info",
                                "message": (
                                    f"No {brand.vertical} {source} facts for {day} — collector "
                                    f"reported ok ({message})."
                                ),
                            }
                        )
                    continue
                notes.append(
                    {
                        "level": "warn",
                        "message": (
                            f"No {brand.vertical} {source} facts for {day} — source is "
                            "unconfigured, failed, or had genuinely no activity "
                            "(not distinguishable from stored facts alone)."
                        ),
                    }
                )

    # 2) Meta present but zero spend (usually a token/scope problem, not a real 0).
    for brand in BRANDS:
        slot = sources.get((brand.vertical, "meta"))
        if slot is not None and slot["spend"] == 0:
            notes.append(
                {
                    "level": "warn",
                    "message": (
                        f"Meta {brand.vertical} spend returned 0 for {day} — "
                        "verify token/account scope."
                    ),
                }
            )

    # 3) Surface Stripe payment failures per brand (independent of truncation).
    for fact in facts:
        if fact.get("source") != "stripe":
            continue
        failures = int(fact.get("payment_failures", 0) or 0)
        if failures > 0:
            level = "warn" if failures >= 10 else "info"
            notes.append(
                {
                    "level": level,
                    "message": (
                        f"{fact.get('vertical')} Stripe: {failures} failed payment(s) on {day}."
                    ),
                }
            )

    # 4) Surface Stripe pagination undercount whenever it applies (L-05).
    # Previously nested under failures > 0, so a truncated day with zero
    # payment_failures silently hid the UNDERCOUNT warning.
    for fact in facts:
        if fact.get("source") != "stripe":
            continue
        meta = fact.get("meta") or {}
        if meta.get("truncated"):
            notes.append(
                {
                    "level": "warn",
                    "message": (
                        f"{fact.get('vertical')} Stripe: charge pagination was truncated on "
                        f"{day} — revenue is an UNDERCOUNT."
                    ),
                }
            )

    return notes


def _coverage(facts: list[dict[str, Any]]) -> dict[str, list[str]]:
    revenue_live = sorted(
        {f"{f.get('vertical')} {f.get('source')}" for f in facts if f.get("source") == "stripe"}
    )
    spend_live = sorted(
        {f"{f.get('vertical')} {f.get('source')}" for f in facts if f.get("source") == "meta"}
    )
    return {
        "revenue_sources_live": revenue_live,
        "spend_sources_live": spend_live,
        "not_wired": list(NOT_WIRED),
    }


__all__ = ["build_revenue_report", "TIMEZONE", "NOT_WIRED"]
