"""Models API — catalog aggregator + route tests.

Covers full cascade config, empty config, partial/malformed config, provider
health (healthy, all-unhealthy, no-accounts), the auto-entry invariant, and
the HTTP route via FastAPI TestClient.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omniagentos.api.routes.models import router
from omniagentos.modelintel.catalog import (
    build_model_catalog,
    build_provider_health,
    load_cascade_ladder,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FULL_CASCADE: dict[str, Any] = {
    "ladder": [
        {"name": "tier0-gemini-coder", "adapter": "cli-gemini", "model": "gemini-3.1", "effort": "low", "cost_rank": 1.0},
        {"name": "tier1-gemini-retry", "adapter": "cli-gemini", "model": "gemini-3.1", "effort": "high", "cost_rank": 2.0},
        {"name": "tier2-grok", "adapter": "cli-grok", "model": "grok-4.5", "effort": "high", "cost_rank": 5.0},
        {"name": "tier3-sol", "adapter": "cli-codex", "model": "gpt-5.6-sol", "effort": "high", "cost_rank": 8.0},
        {"name": "tier4-fable", "adapter": "cli-claude", "model": "fable", "effort": "high", "cost_rank": 12.0},
    ],
}

PARTIAL_CASCADE: dict[str, Any] = {
    "ladder": [
        {"name": "tier0-gemini-coder", "adapter": "cli-gemini", "model": "gemini-3.1"},
        # missing effort, cost_rank — should still parse
    ],
}

MALFORMED_CASCADE = "this is definitely not valid yaml: [[["

EMPTY_LADDER_CASCADE: dict[str, Any] = {"ladder": []}

NO_LADDER_KEY: dict[str, Any] = {"models": [{"id": "foo"}]}


def _write_cascade(tmp_path: Path, content: Any) -> Path:
    """Write a cascade.yaml to a temp dir and return its path."""
    target = tmp_path / "cascade.yaml"
    if isinstance(content, str):
        target.write_text(content, encoding="utf-8")
    else:
        target.write_text(yaml.dump(content), encoding="utf-8")
    return target


# Healthy accounts — provider has at least one enabled, ok entry.
HEALTHY_ACCOUNTS: list[dict[str, Any]] = [
    {"id": "acct_1", "provider": "gemini", "enabled": True, "status": "ok", "paused": False},
    {"id": "acct_2", "provider": "grok", "enabled": True, "status": "ok", "paused": False},
    {"id": "acct_3", "provider": "codex", "enabled": True, "status": "ok", "paused": False},
    {"id": "acct_4", "provider": "claude", "enabled": True, "status": "ok", "paused": False},
]

# All accounts unhealthy — every provider is either disabled, errored, or paused.
ALL_UNHEALTHY_ACCOUNTS: list[dict[str, Any]] = [
    {"id": "acct_1", "provider": "gemini", "enabled": True, "status": "rate_limited", "paused": False},
    {"id": "acct_2", "provider": "grok", "enabled": False, "status": "ok", "paused": False},
    {"id": "acct_3", "provider": "codex", "enabled": True, "status": "error", "paused": False},
    {"id": "acct_4", "provider": "claude", "enabled": True, "status": "ok", "paused": True},
]

MIXED_HEALTH_ACCOUNTS: list[dict[str, Any]] = [
    {"id": "acct_1", "provider": "gemini", "enabled": True, "status": "ok", "paused": False},
    {"id": "acct_2", "provider": "gemini", "enabled": True, "status": "error", "paused": False},
    {"id": "acct_3", "provider": "grok", "enabled": True, "status": "rate_limited", "paused": False},
    {"id": "acct_4", "provider": "grok", "enabled": True, "status": "rate_limited", "paused": False},
]


def _mock_list_accounts(accounts: list[dict[str, Any]]):
    """Return a patch that returns fixed accounts for list_accounts."""
    def _fake(path: str | None = None) -> list[dict[str, Any]]:
        return accounts
    return _fake


# ---------------------------------------------------------------------------
# load_cascade_ladder
# ---------------------------------------------------------------------------


def test_load_cascade_ladder_full(tmp_path: Path) -> None:
    path = _write_cascade(tmp_path, FULL_CASCADE)
    ladder = load_cascade_ladder(path)
    assert len(ladder) == 5
    assert ladder[0]["name"] == "tier0-gemini-coder"
    assert ladder[0]["adapter"] == "cli-gemini"
    assert ladder[-1]["name"] == "tier4-fable"


def test_load_cascade_ladder_empty(tmp_path: Path) -> None:
    path = _write_cascade(tmp_path, EMPTY_LADDER_CASCADE)
    ladder = load_cascade_ladder(path)
    assert ladder == []


def test_load_cascade_ladder_missing(tmp_path: Path) -> None:
    ladder = load_cascade_ladder(tmp_path / "nonexistent.yaml")
    assert ladder == []


def test_load_cascade_ladder_malformed(tmp_path: Path) -> None:
    path = _write_cascade(tmp_path, MALFORMED_CASCADE)
    ladder = load_cascade_ladder(path)
    assert ladder == []


def test_load_cascade_ladder_no_ladder_key(tmp_path: Path) -> None:
    path = _write_cascade(tmp_path, NO_LADDER_KEY)
    ladder = load_cascade_ladder(path)
    assert ladder == []


def test_load_cascade_ladder_partial(tmp_path: Path) -> None:
    path = _write_cascade(tmp_path, PARTIAL_CASCADE)
    ladder = load_cascade_ladder(path)
    assert len(ladder) == 1
    assert ladder[0]["model"] == "gemini-3.1"


def test_load_cascade_ladder_skips_non_dict_entries(tmp_path: Path) -> None:
    content = {"ladder": [FULL_CASCADE["ladder"][0], "not-a-dict", 42, None]}
    path = _write_cascade(tmp_path, content)
    ladder = load_cascade_ladder(path)
    assert len(ladder) == 1


# ---------------------------------------------------------------------------
# build_provider_health
# ---------------------------------------------------------------------------


def test_provider_health_all_healthy(tmp_path: Path) -> None:
    with patch(
        "omniagentos.modelintel.catalog.list_accounts",
        side_effect=_mock_list_accounts(HEALTHY_ACCOUNTS),
        create=True,
    ):
        # Patch the import inside the function directly
        with patch(
            "omniagentos.accounts.service.list_accounts",
            side_effect=_mock_list_accounts(HEALTHY_ACCOUNTS),
        ):
            health = build_provider_health(str(tmp_path / "test.db"))
    assert health == {"gemini": True, "grok": True, "codex": True, "claude": True}


def test_provider_health_all_unhealthy(tmp_path: Path) -> None:
    with patch(
        "omniagentos.accounts.service.list_accounts",
        side_effect=_mock_list_accounts(ALL_UNHEALTHY_ACCOUNTS),
    ):
        health = build_provider_health(str(tmp_path / "test.db"))
    assert health == {"gemini": False, "grok": False, "codex": False, "claude": False}


def test_provider_health_mixed(tmp_path: Path) -> None:
    """gemini has one healthy + one unhealthy → available; grok all rate-limited → unavailable."""
    with patch(
        "omniagentos.accounts.service.list_accounts",
        side_effect=_mock_list_accounts(MIXED_HEALTH_ACCOUNTS),
    ):
        health = build_provider_health(str(tmp_path / "test.db"))
    assert health["gemini"] is True
    assert health["grok"] is False


def test_provider_health_empty_accounts(tmp_path: Path) -> None:
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        health = build_provider_health(str(tmp_path / "test.db"))
    assert health == {}


def test_provider_health_no_default_provider_uses_claude(tmp_path: Path) -> None:
    """Accounts without a provider field default to 'claude'."""
    accounts = [{"id": "acct_1", "provider": None, "enabled": True, "status": "ok", "paused": False}]
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=accounts,
    ):
        health = build_provider_health(str(tmp_path / "test.db"))
    assert health.get("claude") is True


def test_provider_health_query_failure_is_observable(tmp_path: Path) -> None:
    """A failed health query must not masquerade as a clean empty result."""
    with patch(
        "omniagentos.accounts.service.list_accounts",
        side_effect=sqlite3.OperationalError("health store unavailable"),
    ):
        with pytest.raises(sqlite3.OperationalError, match="health store unavailable"):
            build_provider_health(str(tmp_path / "test.db"))


def test_provider_health_active_cooldown_makes_provider_unavailable(tmp_path: Path) -> None:
    """An enabled, status='ok' account under an ACTIVE cooldown must not report healthy.

    This is the demonstration from the research file promoted to a permanent
    pin: set_account_cooldown(..., status=None) is a documented path that
    leaves status untouched while writing a future cooldown_until, and the
    reporter must agree with AVAILABLE_PREDICATE that such an account is not
    selectable. MUST FAIL against the pre-fix predicate, which never reads
    cooldown_until and reports {"claude": True}.
    """
    accounts = [
        {
            "id": "acct_1",
            "provider": "claude",
            "enabled": True,
            "status": "ok",
            "paused": False,
            "cooldown_until": "2099-01-01T00:00:00Z",
        }
    ]
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=accounts,
    ):
        health = build_provider_health(str(tmp_path / "test.db"))
    assert health == {"claude": False}


def test_provider_health_expired_cooldown_still_healthy(tmp_path: Path) -> None:
    """An EXPIRED cooldown_until (in the past) must not make the account unhealthy.

    MUST FAIL against a naive fix that treats the mere presence of a
    cooldown_until value as unhealthy rather than comparing it to now.
    """
    accounts = [
        {
            "id": "acct_1",
            "provider": "claude",
            "enabled": True,
            "status": "ok",
            "paused": False,
            "cooldown_until": "2000-01-01T00:00:00Z",
        }
    ]
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=accounts,
    ):
        health = build_provider_health(str(tmp_path / "test.db"))
    assert health == {"claude": True}


def test_provider_health_null_cooldown_still_healthy(tmp_path: Path) -> None:
    """A NULL cooldown_until must not make the account unhealthy.

    MUST FAIL against a fix that mishandles the null case (e.g. comparing
    None to a string), which would take the whole picker dark.
    """
    accounts = [
        {
            "id": "acct_1",
            "provider": "claude",
            "enabled": True,
            "status": "ok",
            "paused": False,
            "cooldown_until": None,
        }
    ]
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=accounts,
    ):
        health = build_provider_health(str(tmp_path / "test.db"))
    assert health == {"claude": True}


# ---------------------------------------------------------------------------
# build_model_catalog — full config
# ---------------------------------------------------------------------------


def test_catalog_full_config_healthy_providers(tmp_path: Path) -> None:
    cascade_path = _write_cascade(tmp_path, FULL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=HEALTHY_ACCOUNTS,
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))

    models = catalog["models"]
    assert "updated_at" in catalog

    # Auto entry is always first
    assert models[0]["id"] == "auto"
    assert models[0]["provider"] == "router"
    assert models[0]["available"] is True
    assert models[0]["tier"] is None
    assert models[0]["lineage"] is None

    # Cascade entries follow
    assert len(models) == 1 + 5  # auto + 5 ladder entries

    # tier0-gemini
    gemini = models[1]
    assert gemini["id"] == "tier0-gemini-coder"
    assert gemini["provider"] == "gemini"
    assert gemini["tier"] == 0
    assert gemini["available"] is True
    assert gemini["label"] == "gemini-3.1 (low)"
    assert gemini["lineage"] == "gemini"

    # tier1-gemini-retry (same model, different effort)
    gemini_retry = models[2]
    assert gemini_retry["id"] == "tier1-gemini-retry"
    assert gemini_retry["label"] == "gemini-3.1 (high)"
    assert gemini_retry["tier"] == 1

    # tier2-grok
    grok = models[3]
    assert grok["id"] == "tier2-grok"
    assert grok["provider"] == "grok"
    assert grok["tier"] == 2
    assert grok["lineage"] == "grok"

    # tier3-sol (codex provider, gpt lineage)
    sol = models[4]
    assert sol["id"] == "tier3-sol"
    assert sol["provider"] == "codex"
    assert sol["tier"] == 3
    assert sol["lineage"] == "gpt"

    # tier4-fable (claude provider)
    fable = models[5]
    assert fable["id"] == "tier4-fable"
    assert fable["provider"] == "claude"
    assert fable["tier"] == 4
    assert fable["lineage"] == "fable"


# ---------------------------------------------------------------------------
# build_model_catalog — empty config
# ---------------------------------------------------------------------------


def test_catalog_empty_config(tmp_path: Path) -> None:
    cascade_path = _write_cascade(tmp_path, EMPTY_LADDER_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))

    models = catalog["models"]
    assert len(models) == 1
    assert models[0]["id"] == "auto"
    assert models[0]["available"] is True


# ---------------------------------------------------------------------------
# build_model_catalog — missing config
# ---------------------------------------------------------------------------


def test_catalog_missing_config(tmp_path: Path) -> None:
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        catalog = build_model_catalog(
            cascade_path=tmp_path / "nonexistent.yaml",
            db_path=str(tmp_path / "t.db"),
        )
    models = catalog["models"]
    assert len(models) == 1
    assert models[0]["id"] == "auto"


# ---------------------------------------------------------------------------
# build_model_catalog — partial config
# ---------------------------------------------------------------------------


def test_catalog_partial_config(tmp_path: Path) -> None:
    """Partial entries (missing effort/cost_rank) still parse correctly."""
    cascade_path = _write_cascade(tmp_path, PARTIAL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=HEALTHY_ACCOUNTS,
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))

    models = catalog["models"]
    assert len(models) == 2  # auto + 1 entry
    entry = models[1]
    assert entry["id"] == "tier0-gemini-coder"
    assert entry["label"] == "gemini-3.1"  # no effort suffix
    assert entry["available"] is True


def test_catalog_malformed_config(tmp_path: Path) -> None:
    cascade_path = _write_cascade(tmp_path, MALFORMED_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))
    models = catalog["models"]
    assert len(models) == 1
    assert models[0]["id"] == "auto"


def test_catalog_no_ladder_key(tmp_path: Path) -> None:
    cascade_path = _write_cascade(tmp_path, NO_LADDER_KEY)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))
    models = catalog["models"]
    assert len(models) == 1
    assert models[0]["id"] == "auto"


# ---------------------------------------------------------------------------
# build_model_catalog — provider availability
# ---------------------------------------------------------------------------


def test_catalog_unhealthy_provider(tmp_path: Path) -> None:
    """A provider with all accounts unhealthy → model marked unavailable."""
    cascade_path = _write_cascade(tmp_path, FULL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=ALL_UNHEALTHY_ACCOUNTS,
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))

    models = catalog["models"]
    # Every non-auto provider has accounts and ALL are unhealthy
    for model in models[1:]:
        assert model["available"] is False, f"{model['id']} should be unavailable"


def test_catalog_mixed_providers(tmp_path: Path) -> None:
    """Known healthy is available; unhealthy and missing health fail closed."""
    cascade_path = _write_cascade(tmp_path, FULL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=MIXED_HEALTH_ACCOUNTS,
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))

    models = catalog["models"]
    by_id = {m["id"]: m for m in models}

    assert by_id["tier0-gemini-coder"]["available"] is True
    assert by_id["tier1-gemini-retry"]["available"] is True
    assert by_id["tier2-grok"]["available"] is False
    assert by_id["tier3-sol"]["available"] is False
    assert by_id["tier4-fable"]["available"] is False


def test_catalog_no_accounts_means_unknown_and_unavailable(tmp_path: Path) -> None:
    """An empty health store cannot make a concrete provider selectable."""
    cascade_path = _write_cascade(tmp_path, FULL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))

    models = catalog["models"]
    assert models[0]["available"] is True
    for model in models[1:]:
        assert model["available"] is False


def test_catalog_query_failure_is_observable(tmp_path: Path) -> None:
    """The production catalog path must expose a provider-health DB outage."""
    cascade_path = _write_cascade(tmp_path, PARTIAL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        side_effect=sqlite3.OperationalError("provider health query failed"),
    ):
        with pytest.raises(sqlite3.OperationalError, match="provider health query failed"):
            build_model_catalog(
                cascade_path=cascade_path,
                db_path=str(tmp_path / "t.db"),
            )


def test_catalog_known_healthy_provider_is_selectable(tmp_path: Path) -> None:
    """Healthy evidence keeps fail-closed behavior from becoming hardcoded false."""
    cascade_path = _write_cascade(tmp_path, PARTIAL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[HEALTHY_ACCOUNTS[0]],
    ):
        catalog = build_model_catalog(
            cascade_path=cascade_path,
            db_path=str(tmp_path / "t.db"),
        )

    assert catalog["models"][1]["provider"] == "gemini"
    assert catalog["models"][1]["available"] is True


# ---------------------------------------------------------------------------
# build_model_catalog — auto entry invariant
# ---------------------------------------------------------------------------


def test_auto_entry_is_always_first(tmp_path: Path) -> None:
    """Regardless of config state, auto is always the first entry."""
    for content in [FULL_CASCADE, EMPTY_LADDER_CASCADE, PARTIAL_CASCADE]:
        cascade_path = _write_cascade(tmp_path, content)
        with patch(
            "omniagentos.accounts.service.list_accounts",
            return_value=[],
        ):
            catalog = build_model_catalog(
                cascade_path=cascade_path, db_path=str(tmp_path / "t.db")
            )
        assert catalog["models"][0]["id"] == "auto"
        assert catalog["models"][0]["provider"] == "router"


def test_auto_entry_matches_pinned_contract(tmp_path: Path) -> None:
    """The auto entry matches the exact shape from FINAL-PLAN.md section B."""
    cascade_path = _write_cascade(tmp_path, EMPTY_LADDER_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        catalog = build_model_catalog(
            cascade_path=cascade_path, db_path=str(tmp_path / "t.db")
        )
    auto = catalog["models"][0]
    assert auto == {
        "id": "auto",
        "label": "Auto — router decides",
        "provider": "router",
        "tier": None,
        "available": True,
        "lineage": None,
    }


# ---------------------------------------------------------------------------
# build_model_catalog — entry shape contract
# ---------------------------------------------------------------------------


def test_every_entry_has_required_keys(tmp_path: Path) -> None:
    cascade_path = _write_cascade(tmp_path, FULL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=HEALTHY_ACCOUNTS,
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))

    required_keys = {"id", "label", "provider", "tier", "available", "lineage"}
    for model in catalog["models"]:
        assert required_keys.issubset(model.keys()), f"missing keys in {model}"
        assert isinstance(model["id"], str)
        assert isinstance(model["label"], str)
        assert isinstance(model["provider"], str)
        assert model["tier"] is None or isinstance(model["tier"], int)
        assert isinstance(model["available"], bool)
        assert model["lineage"] is None or isinstance(model["lineage"], str)


# ---------------------------------------------------------------------------
# build_model_catalog — tier parsing
# ---------------------------------------------------------------------------


def test_tier_parsing_from_name(tmp_path: Path) -> None:
    cascade_path = _write_cascade(tmp_path, FULL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))

    tiers = [m["tier"] for m in catalog["models"][1:]]
    assert tiers == [0, 1, 2, 3, 4]


def test_tier_fallback_to_index(tmp_path: Path) -> None:
    """Entries without tier-prefixed names fall back to positional index."""
    content = {"ladder": [{"name": "fast-model", "adapter": "cli-gemini", "model": "gemini-3.1"}]}
    cascade_path = _write_cascade(tmp_path, content)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))
    assert catalog["models"][1]["tier"] == 0


def test_tier_no_name_falls_back_to_index(tmp_path: Path) -> None:
    """Entries with no name field fall back to positional index."""
    content = {"ladder": [{"adapter": "cli-gemini", "model": "gemini-3.1"}]}
    cascade_path = _write_cascade(tmp_path, content)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))
    model = catalog["models"][1]
    assert model["tier"] == 0
    assert model["id"] == "model-0"


# ---------------------------------------------------------------------------
# build_model_catalog — lineage extraction
# ---------------------------------------------------------------------------


def test_lineage_extraction(tmp_path: Path) -> None:
    cascade_path = _write_cascade(tmp_path, FULL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        catalog = build_model_catalog(cascade_path=cascade_path, db_path=str(tmp_path / "t.db"))

    by_id = {m["id"]: m for m in catalog["models"]}
    assert by_id["tier0-gemini-coder"]["lineage"] == "gemini"
    assert by_id["tier2-grok"]["lineage"] == "grok"
    assert by_id["tier3-sol"]["lineage"] == "gpt"
    assert by_id["tier4-fable"]["lineage"] == "fable"


# ---------------------------------------------------------------------------
# HTTP route — FastAPI TestClient
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_route_full_config(tmp_path: Path) -> None:
    cascade_path = _write_cascade(tmp_path, FULL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=HEALTHY_ACCOUNTS,
    ):
        client = TestClient(_make_app())
        resp = client.get(
            "/api/models",
            params={"cascade_path": str(cascade_path), "db_path": str(tmp_path / "t.db")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "models" in body
    assert len(body["models"]) == 6  # auto + 5 cascade entries
    assert body["models"][0]["id"] == "auto"
    assert body["models"][1]["provider"] == "gemini"


def test_route_empty_config(tmp_path: Path) -> None:
    cascade_path = _write_cascade(tmp_path, EMPTY_LADDER_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        client = TestClient(_make_app())
        resp = client.get(
            "/api/models",
            params={"cascade_path": str(cascade_path), "db_path": str(tmp_path / "t.db")},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["models"]) == 1
    assert body["models"][0]["id"] == "auto"


def test_route_missing_config(tmp_path: Path) -> None:
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=[],
    ):
        client = TestClient(_make_app())
        resp = client.get(
            "/api/models",
            params={
                "cascade_path": str(tmp_path / "nonexistent.yaml"),
                "db_path": str(tmp_path / "t.db"),
            },
        )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["models"]) == 1
    assert body["models"][0]["id"] == "auto"


def test_route_response_shape_contract(tmp_path: Path) -> None:
    """Response matches the pinned contract from FINAL-PLAN.md section B exactly."""
    cascade_path = _write_cascade(tmp_path, FULL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=HEALTHY_ACCOUNTS,
    ):
        client = TestClient(_make_app())
        resp = client.get(
            "/api/models",
            params={"cascade_path": str(cascade_path), "db_path": str(tmp_path / "t.db")},
        )

    assert resp.status_code == 200
    body = resp.json()
    models = body["models"]

    # First entry contract
    assert models[0] == {
        "id": "auto",
        "label": "Auto — router decides",
        "provider": "router",
        "tier": None,
        "available": True,
        "lineage": None,
    }

    # Every entry has the required shape
    required_keys = {"id", "label", "provider", "tier", "available", "lineage"}
    for model in models:
        assert required_keys == set(model.keys())


def test_route_unavailable_providers(tmp_path: Path) -> None:
    cascade_path = _write_cascade(tmp_path, FULL_CASCADE)
    with patch(
        "omniagentos.accounts.service.list_accounts",
        return_value=ALL_UNHEALTHY_ACCOUNTS,
    ):
        client = TestClient(_make_app())
        resp = client.get(
            "/api/models",
            params={"cascade_path": str(cascade_path), "db_path": str(tmp_path / "t.db")},
        )

    assert resp.status_code == 200
    models = resp.json()["models"]
    # Auto is always available
    assert models[0]["available"] is True
    # All cascade entries are unavailable
    for model in models[1:]:
        assert model["available"] is False


def test_route_catastrophic_failure_returns_auto(tmp_path: Path) -> None:
    """Even if the catalog builder explodes, the route returns the auto entry."""
    client = TestClient(_make_app())
    with patch(
        "omniagentos.api.routes.models.build_model_catalog",
        side_effect=RuntimeError("catastrophic"),
    ):
        resp = client.get("/api/models")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["models"]) == 1
    assert body["models"][0]["id"] == "auto"
