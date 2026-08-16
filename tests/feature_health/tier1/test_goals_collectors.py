"""Feature-health tier1 — goals collectors golden-fixture probe ($0, no network).

Feeds recorded Stripe/Meta-shaped broker payloads through
:func:`omniagentos.goals.collect.collect_once`, stubbing the broker boundary the
way ``tests/goals/test_collect.py`` does (``monkeypatch.setattr(collect.broker,
"call", ...)``). Covers:

* Stripe ``has_more`` pagination — the second page is requested with
  ``starting_after`` = the last charge id of the first page, and the summed
  snapshot reflects ALL pages;
* Meta ``act_``-prefix normalization in BOTH directions — an env value already
  carrying ``act_`` must not become ``act_act_`` (Graph 400), and a bare id
  still gains exactly one prefix.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from omniagentos.db.store import SqliteStore
from omniagentos.goals import collect
from omniagentos.goals.collect import collect_once
from omniagentos.goals.seed import seed
from omniagentos.steward.store import StewardStore

TARGET_DAY = date(2026, 7, 10)

# Golden broker payloads (recorded shapes, deterministic values).
_STRIPE_PAGE_ONE = {
    "ok": True,
    "status": 200,
    "body": {
        "data": [
            {"id": "ch_001", "status": "succeeded", "amount": 5000, "amount_refunded": 500},
            {"id": "ch_002", "status": "failed", "amount": 1200, "amount_refunded": 0},
        ],
        "has_more": True,
    },
}
_STRIPE_PAGE_TWO = {
    "ok": True,
    "status": 200,
    "body": {
        "data": [
            {"id": "ch_003", "status": "succeeded", "amount": 2500, "amount_refunded": 0},
        ],
        "has_more": False,
    },
}
_META_INSIGHTS = {
    "ok": True,
    "status": 200,
    "body": {"data": [{"spend": "42.50", "purchase_roas": [{"value": "2.25"}]}]},
}


@pytest.fixture()
def store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "fh-goals.db"))


def test_golden_stripe_pagination_and_meta_act_prefixed_env(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", "test")
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "test")
    # Env value ALREADY carries the act_ prefix — the live-only double-prefix trap.
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "act_0000000000000001")
    seed(store)
    calls: list[dict[str, Any]] = []

    def fake_call(_: str, __: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs["path"] == "/v1/charges":
            if kwargs["query"].get("starting_after") is None:
                return _STRIPE_PAGE_ONE
            return _STRIPE_PAGE_TWO
        return _META_INSIGHTS

    monkeypatch.setattr(collect.broker, "call", fake_call)
    collect_once(store, target_day=TARGET_DAY)

    # Stripe pagination: exactly two /v1/charges pages, cursor = last id of page 1.
    stripe_calls = [c for c in calls if c["path"] == "/v1/charges"]
    assert len(stripe_calls) == 2
    assert stripe_calls[0]["query"].get("starting_after") is None
    assert stripe_calls[1]["query"]["starting_after"] == "ch_002"

    # Meta path: exactly one act_ prefix, never act_act_.
    meta_paths = [c["path"] for c in calls if c["path"] != "/v1/charges"]
    assert meta_paths == ["/act_0000000000000001/insights"]
    assert "act_act_" not in "".join(meta_paths)

    steward = StewardStore(store)
    revenue = steward.latest_snapshot("stripe", "net_revenue_usd", "increase-revenue")
    failures = steward.latest_snapshot("stripe", "payment_failures", "increase-revenue")
    spend = steward.latest_snapshot("meta", "spend_usd", "improve-ad-roi")
    roas = steward.latest_snapshot("meta", "roas", "improve-ad-roi")

    # Net revenue sums BOTH pages: (5000-500)/100 + 2500/100 = 70.0.
    assert revenue is not None and revenue["value"] == 70.0
    assert revenue["meta"]["charges"] == 3
    assert revenue["meta"].get("truncated") is None
    assert failures is not None and failures["value"] == 1.0
    assert spend is not None and spend["value"] == 42.5
    assert roas is not None and roas["value"] == 2.25
    # The snapshot records the BARE account id (normalized), never a doubled one.
    assert spend["meta"]["ad_account_id"] == "0000000000000001"
    assert spend["meta"]["date"] == TARGET_DAY.isoformat()


def test_bare_account_id_gains_exactly_one_act_prefix(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", raising=False)
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "test")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "999000111")  # bare id
    seed(store)
    paths: list[str] = []

    def fake_call(_: str, __: list[str], **kwargs: Any) -> dict[str, Any]:
        paths.append(kwargs["path"])
        return _META_INSIGHTS

    monkeypatch.setattr(collect.broker, "call", fake_call)
    messages = collect_once(store, target_day=TARGET_DAY)

    assert paths == ["/act_999000111/insights"]
    # Stripe (unconfigured) is skipped with a reason, never a crash.
    stripe_msg = next(m for m in messages if m["collector"] == "stripe")
    assert "skipped" in stripe_msg
    spend = StewardStore(store).latest_snapshot("meta", "spend_usd", "improve-ad-roi")
    assert spend is not None and spend["value"] == 42.5


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"data": [{}]}, "unreadable Meta spend on campaign row"),
        ({}, "invalid Meta response: missing data"),
    ],
)
def test_meta_bad_response_shapes_skip_instead_of_recording_zero(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], expected: str
) -> None:
    monkeypatch.delenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", raising=False)
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "test")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "999000111")
    seed(store)
    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {"ok": True, "status": 200, "body": body},
    )

    messages = collect_once(store, target_day=TARGET_DAY)

    meta_msg = next(message for message in messages if message["collector"] == "meta")
    assert meta_msg["skipped"] == expected
    assert StewardStore(store).latest_snapshot("meta", "spend_usd", "improve-ad-roi") is None


def test_meta_empty_data_still_records_real_zero_spend(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", raising=False)
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "test")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "999000111")
    seed(store)
    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {"ok": True, "status": 200, "body": {"data": []}},
    )

    collect_once(store, target_day=TARGET_DAY)

    spend = StewardStore(store).latest_snapshot("meta", "spend_usd", "improve-ad-roi")
    assert spend is not None and spend["value"] == 0.0
