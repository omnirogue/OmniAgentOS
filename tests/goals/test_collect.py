from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from omniagentos.goals import collect
from omniagentos.goals.collect import MetaCollector, collect_once
from omniagentos.goals.seed import seed
from omniagentos.steward.store import StewardStore


def test_collects_stripe_pagination_and_meta_metrics(store: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", "test")
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "test")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "123,456")
    seed(store)
    calls: list[dict[str, Any]] = []

    def fake_call(_: str, __: list[str], **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs["path"] == "/v1/charges":
            if kwargs["query"].get("starting_after") is None:
                return {
                    "ok": True,
                    "status": 200,
                    "body": {
                        "data": [
                            {
                                "id": "ch_1",
                                "status": "succeeded",
                                "amount": 1250,
                                "amount_refunded": 250,
                            },
                            {"id": "ch_2", "status": "failed", "amount": 500, "amount_refunded": 0},
                        ],
                        "has_more": True,
                    },
                }
            return {
                "ok": True,
                "status": 200,
                "body": {
                    "data": [
                        {"id": "ch_3", "status": "succeeded", "amount": 300, "amount_refunded": 0}
                    ],
                    "has_more": False,
                },
            }
        return {
            "ok": True,
            "status": 200,
            "body": {"data": [{"spend": "42.50", "purchase_roas": [{"value": "2.25"}]}]},
        }

    monkeypatch.setattr(collect.broker, "call", fake_call)
    collect_once(store, target_day=date(2026, 7, 10))

    steward = StewardStore(store)
    revenue = steward.latest_snapshot("stripe", "net_revenue_usd", "increase-revenue")
    failures = steward.latest_snapshot("stripe", "payment_failures", "increase-revenue")
    spend = steward.latest_snapshot("meta", "spend_usd", "improve-ad-roi")
    roas = steward.latest_snapshot("meta", "roas", "improve-ad-roi")
    assert revenue is not None and revenue["value"] == 13.0
    assert failures is not None and failures["value"] == 1.0
    assert spend is not None and spend["value"] == 42.5
    assert roas is not None and roas["value"] == 2.25
    assert calls[1]["query"]["starting_after"] == "ch_2"
    assert calls[-1]["path"] == "/act_123/insights"


def test_collector_failures_skip_and_dry_run_does_not_write(
    store: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    monkeypatch.setenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", "test")
    monkeypatch.delenv("ACMEUNI_META_ACCESS_TOKEN", raising=False)

    def denied(*_: Any, **__: Any) -> dict[str, Any]:
        return {"ok": False, "status": 403, "body": {"error": "denied"}}

    monkeypatch.setattr(collect.broker, "call", denied)
    messages = collect_once(store, target_day=date(2026, 7, 10), dry_run=True)
    assert messages[0]["collector"] == "stripe" and "skipped" in messages[0]
    assert messages[1]["collector"] == "meta" and "skipped" in messages[1]
    assert (
        StewardStore(store).latest_snapshot("stripe", "net_revenue_usd", "increase-revenue") is None
    )
    # tmp_path, NOT a fixed /tmp file: a persistent db seeded by an older
    # migration tree goes stale the moment version numbers shift (bit us when
    # 044_swarm was renumbered to 045 — the stale file's bookkeeping said "44
    # applied" meaning the OLD swarm file, so 045 re-applied onto existing tables).
    monkeypatch.setenv("OMNIAGENTOS_DB", str(tmp_path / "goals-collect-cli.db"))
    assert collect.main(["--once", "--dry-run", "--date", "2026-07-10"]) == 0


def test_stripe_pagination_truncation_is_bounded_and_flagged(store: Any, monkeypatch: Any) -> None:
    # H14: an infinite has_more stream must stop at MAX_PAGES (not silently sum a
    # partial), and the snapshot meta must carry truncated=True so the undercount
    # is visible rather than silent.
    monkeypatch.setenv("ACMEUNI_STRIPE_PRIMARY_SECRET_KEY", "test")
    seed(store)
    page_calls = {"n": 0}

    def fake_call(_: str, __: list[str], **kwargs: Any) -> dict[str, Any]:
        if kwargs["path"] == "/v1/charges":
            page_calls["n"] += 1
            # Always has_more -> an infinite stream the bounded loop must cut off.
            return {
                "ok": True,
                "status": 200,
                "body": {
                    "data": [
                        {
                            "id": f"ch_{page_calls['n']}",
                            "status": "succeeded",
                            "amount": 100,
                            "amount_refunded": 0,
                        }
                    ],
                    "has_more": True,
                },
            }
        return {"ok": False, "status": 500, "body": {}}

    monkeypatch.setattr(collect.broker, "call", fake_call)
    collect_once(store, target_day=date(2026, 7, 10))

    assert page_calls["n"] == 100  # bounded at MAX_PAGES, not infinite
    revenue = StewardStore(store).latest_snapshot("stripe", "net_revenue_usd", "increase-revenue")
    assert revenue is not None
    assert revenue["meta"].get("truncated") is True  # undercount is flagged, not silent


def test_meta_account_id_act_prefix_is_normalized(store: Any, monkeypatch: Any) -> None:
    # Live-only bug: ACMEUNI_META_AD_ACCOUNT_IDS may already carry the act_ prefix,
    # and prepending act_ again yields /act_act_.../insights -> Graph 400. The
    # collector must normalize to a single act_ prefix.
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "test")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "act_0000000000000001")  # pre-prefixed
    seed(store)
    paths: list[str] = []

    def fake_call(_: str, __: list[str], **kwargs: Any) -> dict[str, Any]:
        paths.append(kwargs["path"])
        return {
            "ok": True,
            "status": 200,
            "body": {"data": [{"spend": "10.0", "purchase_roas": [{"value": "3.0"}]}]},
        }

    monkeypatch.setattr(collect.broker, "call", fake_call)
    collect_once(store, target_day=date(2026, 7, 10))
    assert paths == ["/act_0000000000000001/insights"]  # exactly one act_ prefix
    assert "/act_act_" not in "".join(paths)


@pytest.mark.parametrize(
    "spend",
    [
        pytest.param(None, id="null"),
        pytest.param("not-a-number", id="non-numeric"),
        pytest.param(float("nan"), id="non-finite"),
    ],
)
def test_meta_collector_skips_unreadable_spend(monkeypatch: Any, spend: Any) -> None:
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "test")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "123")
    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {
            "ok": True,
            "status": 200,
            "body": {"data": [{"spend": spend}]},
        },
    )

    snapshots, reason = MetaCollector().collect(date(2026, 7, 10))

    assert snapshots == []
    assert reason == "unreadable Meta spend on campaign row"


def test_meta_collector_skips_missing_spend_or_data(monkeypatch: Any) -> None:
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "test")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "123")

    for body, expected in (
        ({"data": [{}]}, "unreadable Meta spend on campaign row"),
        ({}, "invalid Meta response: missing data"),
    ):
        monkeypatch.setattr(
            collect.broker,
            "call",
            lambda *_args, body=body, **_kwargs: {"ok": True, "status": 200, "body": body},
        )
        snapshots, reason = MetaCollector().collect(date(2026, 7, 10))
        assert snapshots == []
        assert reason == expected


def test_meta_collector_keeps_genuine_empty_data_as_zero_spend(monkeypatch: Any) -> None:
    monkeypatch.setenv("ACMEUNI_META_ACCESS_TOKEN", "test")
    monkeypatch.setenv("ACMEUNI_META_AD_ACCOUNT_IDS", "123")
    monkeypatch.setattr(
        collect.broker,
        "call",
        lambda *_args, **_kwargs: {"ok": True, "status": 200, "body": {"data": []}},
    )

    snapshots, reason = MetaCollector().collect(date(2026, 7, 10))

    assert reason is None
    assert next(snapshot for snapshot in snapshots if snapshot.metric == "spend_usd").value == 0.0
