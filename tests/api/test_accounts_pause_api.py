"""Pause/resume/usage HTTP surface, plus a literal-path contract check.

The contract test exists because UI↔API drift only shows at integration: the
dashboard client hard-codes its paths as strings, fixtures bypass real routing,
and a typo'd or renamed route 404s a whole feature while every unit test stays
green. Asserting the exact literal strings resolve is the cheap guard.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from omniagentos.db.store import SqliteStore
from tests.support.db_template import migrated_db


def _account(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[str, str]:
    """A registered account on an isolated DB. Returns (db_path, account_id)."""
    db_path = str(tmp_path / "test.db")
    migrated_db(SqliteStore, db_path)
    monkeypatch.setattr("omniagentos.accounts.service.default_db_path", lambda: db_path)
    monkeypatch.setattr("omniagentos.accounts.service.detect_config_dirs", lambda: [])

    from omniagentos.accounts.service import add_account

    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    account = add_account(label="test", config_dir=str(config_dir), db_path=db_path)
    return db_path, str(account["id"])


def test_pause_then_resume_round_trip(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, account_id = _account(monkeypatch, tmp_path)

    paused = asyncio.run(
        asgi_client.post(
            f"/api/accounts/{account_id}/pause",
            json={"minutes": 30, "reason": "draining"},
            headers=auth_headers,
        )
    )
    assert paused.status_code == 200
    body = paused.json()
    assert body["paused"] is True
    assert body["pause_reason"] == "draining"
    assert body["paused_until"] is not None
    # A pause is NOT a disable — the account stays enabled, just not handed out.
    assert body["enabled"] is True

    resumed = asyncio.run(
        asgi_client.post(f"/api/accounts/{account_id}/resume", headers=auth_headers)
    )
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    assert resumed.json()["pause_reason"] is None


@pytest.mark.parametrize("minutes", [0, -5])
def test_pause_rejects_non_positive_durations(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    minutes: int,
) -> None:
    _, account_id = _account(monkeypatch, tmp_path)

    response = asyncio.run(
        asgi_client.post(
            f"/api/accounts/{account_id}/pause",
            json={"minutes": minutes},
            headers=auth_headers,
        )
    )
    assert response.status_code == 400


def test_pause_is_capped_so_a_typo_cannot_bench_an_account_for_a_month(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _, account_id = _account(monkeypatch, tmp_path)

    response = asyncio.run(
        asgi_client.post(
            f"/api/accounts/{account_id}/pause",
            json={"minutes": 60 * 24 * 30},  # "30 days" — almost certainly a slip
            headers=auth_headers,
        )
    )
    assert response.status_code == 400
    assert "disable" in response.json()["error"]["message"]  # points at the right lever


def test_pause_and_resume_404_on_unknown_account(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _account(monkeypatch, tmp_path)

    for path, payload in (
        ("/api/accounts/acct_nope/pause", {"minutes": 5}),
        ("/api/accounts/acct_nope/resume", None),
    ):
        response = asyncio.run(asgi_client.post(path, json=payload, headers=auth_headers))
        assert response.status_code == 404


def test_usage_endpoint_reports_every_provider(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Telemetry-less providers must appear as an explicit gap, not be omitted."""
    monkeypatch.setattr("omniagentos.accounts.usage.detect_config_dirs", lambda: [])
    monkeypatch.setattr("omniagentos.accounts.usage._codex_home", lambda: tmp_path / "none")

    response = asyncio.run(asgi_client.get("/api/accounts/usage"))
    assert response.status_code == 200
    by_provider = {row["provider"]: row for row in response.json()["usage"]}

    for provider in ("codex", "grok", "gemini", "kimi", "qwen"):
        assert provider in by_provider
        assert by_provider[provider]["available"] is False
        assert by_provider[provider]["reason"]  # always explains itself
        assert by_provider[provider]["windows"] == []


def test_usage_route_is_not_shadowed_by_the_id_routes(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`/accounts/usage` sits in the same namespace as `/accounts/{account_id}`.

    Nothing stops a future GET-by-id route from swallowing it, at which point
    the page would silently start rendering an account named "usage".
    """
    monkeypatch.setattr("omniagentos.accounts.usage.detect_config_dirs", lambda: [])
    monkeypatch.setattr("omniagentos.accounts.usage._codex_home", lambda: tmp_path / "none")

    response = asyncio.run(asgi_client.get("/api/accounts/usage"))

    assert response.status_code == 200
    assert "usage" in response.json()


def test_dashboard_client_paths_resolve(
    asgi_client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Every literal path in features/accounts/client.ts must reach a real route.

    Asserts only that the route EXISTS (no 404/405) — behavior is covered above.
    """
    _, account_id = _account(monkeypatch, tmp_path)
    monkeypatch.setattr("omniagentos.accounts.usage.detect_config_dirs", lambda: [])
    monkeypatch.setattr("omniagentos.accounts.usage._codex_home", lambda: tmp_path / "none")

    calls = [
        ("GET", "/api/accounts", None),
        ("GET", "/api/accounts/usage", None),
        ("POST", "/api/accounts", {"label": "x", "config_dir": str(tmp_path)}),
        ("PATCH", f"/api/accounts/{account_id}", {"enabled": True}),
        ("POST", f"/api/accounts/{account_id}/pause", {"minutes": 5}),
        ("POST", f"/api/accounts/{account_id}/resume", None),
        ("DELETE", f"/api/accounts/{account_id}", None),
    ]

    for method, path, payload in calls:
        response = asyncio.run(
            asgi_client.request(method, path, json=payload, headers=auth_headers)
        )
        assert response.status_code not in (404, 405), f"{method} {path} -> {response.status_code}"
