"""PRIORITY 2: revenue_facts store — schema + UPSERT idempotency."""

from __future__ import annotations

from typing import Any

from omniagentos.revenue.store import RevenueFact, RevenueStore


def test_upsert_is_idempotent_for_null_campaign_stripe_rows(store: Any) -> None:
    rs = RevenueStore(store)
    rs.upsert_revenue_fact(
        RevenueFact(day="2026-07-10", vertical="AcmeUni", source="stripe", revenue_usd=100.0)
    )
    # Hourly re-collect with a refreshed figure (a late refund landed).
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10", vertical="AcmeUni", source="stripe", revenue_usd=90.0, refunds_usd=10.0
        )
    )
    rows = rs.query_day("2026-07-10")
    assert len(rows) == 1, "NULL-campaign row must UPSERT, not duplicate"
    assert rows[0]["revenue_usd"] == 90.0
    assert rows[0]["refunds_usd"] == 10.0
    assert rows[0]["campaign_id"] is None


def test_upsert_keys_on_day_vertical_source_campaign(store: Any) -> None:
    rs = RevenueStore(store)
    # Two brands, two campaigns each, same day -> all distinct rows.
    rs.upsert_revenue_fact(RevenueFact(day="2026-07-10", vertical="AcmeUni", source="stripe"))
    rs.upsert_revenue_fact(RevenueFact(day="2026-07-10", vertical="Initech", source="stripe"))
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10", vertical="AcmeUni", source="meta", campaign_id="c1", ad_spend_usd=5
        )
    )
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10", vertical="AcmeUni", source="meta", campaign_id="c2", ad_spend_usd=7
        )
    )
    rows = rs.query_day("2026-07-10")
    assert len(rows) == 4

    # Re-upsert one campaign -> refreshed in place, still 4 rows.
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10", vertical="AcmeUni", source="meta", campaign_id="c1", ad_spend_usd=9
        )
    )
    rows = rs.query_day("2026-07-10")
    assert len(rows) == 4
    c1 = next(r for r in rows if r.get("campaign_id") == "c1")
    assert c1["ad_spend_usd"] == 9.0


def test_meta_json_roundtrips_as_dict(store: Any) -> None:
    rs = RevenueStore(store)
    rs.upsert_revenue_fact(
        RevenueFact(
            day="2026-07-10",
            vertical="AcmeUni",
            source="meta",
            campaign_id="c1",
            meta={"revenue_attributed_usd": 42.5, "attribution": "derived:spend*purchase_roas"},
        )
    )
    row = rs.query_day("2026-07-10")[0]
    assert row["meta"]["revenue_attributed_usd"] == 42.5
    assert row["meta"]["attribution"] == "derived:spend*purchase_roas"


def test_query_day_isolates_days(store: Any) -> None:
    rs = RevenueStore(store)
    rs.upsert_revenue_fact(RevenueFact(day="2026-07-10", vertical="AcmeUni", source="stripe"))
    rs.upsert_revenue_fact(RevenueFact(day="2026-07-11", vertical="AcmeUni", source="stripe"))
    assert len(rs.query_day("2026-07-10")) == 1
    assert len(rs.query_day("2026-07-09")) == 0
