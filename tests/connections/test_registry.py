"""Registry + vault parser unit tests for the Connections slice (S8).

The vault parser is the security-critical surface here: it MUST extract only
key names (``^[A-Z0-9_]+(?==)``) and never capture values. Every test in this
module verifies that invariant directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.connectors.registry import (
    CATALOG,
    CATEGORY_ORDER,
    build_connections_view,
    parse_vault,
    summary,
)

# ── Vault parser ───────────────────────────────────────────────────────────


def _write_vault(tmp_path: Path, content: str) -> Path:
    target = tmp_path / "connections.env"
    target.write_text(content, encoding="utf-8")
    return target


def test_parse_vault_extracts_key_names_only(tmp_path: Path) -> None:
    """The parser extracts the regex-matched key NAMES and ignores values."""
    vault = _write_vault(
        tmp_path,
        "ANTHROPIC_API_KEY=sk-ant-abc123def\n"
        "OPENAI_API_KEY=sk-prod-xyz\n"
        "STRIP_ME=not-a-match-because-no-equals\n"
        "lowercased_key=this-should-be-skipped\n"
        "EMPTY_NO_EQUALS\n"
        "GMAIL_REFRESH_TOKEN=1//very-long-value\n",
    )
    keys, readable = parse_vault(vault)
    assert readable is True
    # Values are NEVER captured.
    for k in keys:
        assert not k.startswith("sk-")
        assert "//" not in k
    # Assignment lines appear, by NAME.
    assert "ANTHROPIC_API_KEY" in keys
    assert "OPENAI_API_KEY" in keys
    assert "GMAIL_REFRESH_TOKEN" in keys
    # A lowercase name IS a valid POSIX variable name and is now extracted. This
    # assertion previously read ``not in``: the uppercase-only restriction was
    # this reader's alone, and the inventory reader of the SAME file never had
    # it. Unifying the grammar is what removes that divergence; it is inert for
    # the connections view, whose declared key families are all uppercase, so a
    # lowercase name simply matches no family.
    assert "lowercased_key" in keys
    # no-equals line not matched
    assert "EMPTY_NO_EQUALS" not in keys
    # value is NOT in the set (we only capture names)
    assert "sk-ant-abc123def" not in keys


def test_parse_vault_unreadable_returns_error(tmp_path: Path) -> None:
    """A missing or unreadable vault yields (empty set, False) without raising."""
    missing = tmp_path / "does-not-exist.env"
    keys, readable = parse_vault(missing)
    assert readable is False
    assert keys == frozenset()


def test_parse_vault_handles_blank_and_comments(tmp_path: Path) -> None:
    """Blank lines and commented-out entries should not appear in the key set."""
    vault = _write_vault(
        tmp_path,
        "# this is a comment\n"
        "\n"
        "ANTHROPIC_API_KEY=sk-test\n"
        "# DISABLED_KEY=value\n"
        "\n"
        "STRIPE_SECRET_KEY=sk_live_x\n",
    )
    keys, readable = parse_vault(vault)
    assert readable is True
    assert "ANTHROPIC_API_KEY" in keys
    assert "STRIPE_SECRET_KEY" in keys
    # Commented-out keys are NOT matched (the regex requires the match at line start).
    assert "DISABLED_KEY" not in keys


def test_parse_vault_reads_export_prefixed_lines(tmp_path: Path) -> None:
    """``export NAME=`` is a vault assignment, not a comment.

    Regression for the divergence between the two readers of THIS SAME FILE
    (``registry.default_vault_path()``): ``inventory._parse_env_file`` has always
    accepted an optional ``export`` prefix, and ``parse_vault`` did not, so a
    vault written in shell-sourceable form reported its live connectors as
    ``not_configured`` on the dashboard while the credentials were present in the
    environment and brokered calls with them succeeded.
    """
    vault = _write_vault(
        tmp_path,
        "export STRIPE_SECRET_KEY=sk_live_x\n"
        "PIEDPIPER_API_KEY=abc\n"
        "export SLACK_BOT_TOKEN=xoxb-value\n",
    )
    keys, readable = parse_vault(vault)
    assert readable is True
    assert keys == frozenset({"STRIPE_SECRET_KEY", "PIEDPIPER_API_KEY", "SLACK_BOT_TOKEN"})
    # The security invariant this module exists to protect is unchanged: only
    # NAMES are captured, never values.
    assert "sk_live_x" not in keys
    assert "xoxb-value" not in keys


def test_parse_vault_agrees_with_the_inventory_reader(tmp_path: Path) -> None:
    """The two readers of the vault must return the same names for one file.

    Globs the class rather than pinning one example: both production readers are
    driven over the same text, and any future divergence in either grammar fails
    here instead of becoming a second silent disagreement.
    """
    from omniagentos.connectors.inventory import _parse_env_file

    vault = _write_vault(
        tmp_path,
        "# a comment\n"
        "\n"
        "export STRIPE_SECRET_KEY=sk_live_x\n"
        "   export INDENTED_KEY=v\n"
        "PIEDPIPER_API_KEY=abc\n"
        "lowercased_key=v\n"
        "MiXeD_Case=v\n"
        "# DISABLED_KEY=value\n"
        "NO_EQUALS_LINE\n",
    )
    assert parse_vault(vault)[0] == _parse_env_file(vault)


# ── Status rollup ──────────────────────────────────────────────────────────


def test_single_instance_connected_when_all_families_present(tmp_path: Path) -> None:
    vault = _write_vault(
        tmp_path,
        "ANTHROPIC_API_KEY=sk-ant-xyz\n",
    )
    cats, readable = build_connections_view(vault_path=vault)
    assert readable is True
    ai = next(c for c in cats if c.id == "ai_providers")
    anthropic = next(i for i in ai.integrations if i.id == "anthropic")
    assert anthropic.status == "connected"
    assert "1 key configured" in anthropic.detail


def test_single_instance_not_configured_when_no_keys(tmp_path: Path) -> None:
    vault = _write_vault(tmp_path, "")
    cats, readable = build_connections_view(vault_path=vault)
    assert readable is True
    ai = next(c for c in cats if c.id == "ai_providers")
    anthropic = next(i for i in ai.integrations if i.id == "anthropic")
    assert anthropic.status == "not_configured"
    assert "Not configured" in anthropic.detail


def test_single_instance_configured_when_partial_families(tmp_path: Path) -> None:
    """Gmail requires 3 keys; presence of 1 yields 'configured' rather than connected."""
    vault = _write_vault(tmp_path, "GOOGLE_OAUTH_CLIENT_ID=abc123\n")
    cats, _ = build_connections_view(vault_path=vault)
    comms = next(c for c in cats if c.id == "email_comms")
    gmail = next(i for i in comms.integrations if i.id == "gmail")
    assert gmail.status == "configured"
    assert "1 key configured" in gmail.detail


def test_multi_instance_per_instance_rollup(tmp_path: Path) -> None:
    """PiedPiper has 3 sub-accounts; each instance has its own status chip."""
    vault = _write_vault(
        tmp_path,
        # AcmeUni: present
        "PIEDPIPER_ACMEUNI_TOKEN=acmeuni-key\n"
        "PIEDPIPER_ACMEUNI_LOCATION_ID=acmeuni-loc\n"
        # INITECH: absent
        # GLOBEX: present
        "PIEDPIPER_GLOBEX_TOKEN=cf-key\n",
    )
    cats, _ = build_connections_view(vault_path=vault)
    crm = next(c for c in cats if c.id == "crm_marketing")
    piedpiper = next(i for i in crm.integrations if i.id == "piedpiper")
    assert len(piedpiper.instances) == 3
    labels = {inst.label: inst.status for inst in piedpiper.instances}
    assert labels["AcmeUni"] == "connected"
    assert labels["INITECH"] == "not_configured"
    assert labels["GLOBEX"] == "connected"
    # Overall: not all connected → "configured"
    assert piedpiper.status == "configured"
    assert "2/3 instances connected" in piedpiper.detail


def test_multi_instance_all_connected_yields_connected(tmp_path: Path) -> None:
    vault = _write_vault(
        tmp_path,
        "PIEDPIPER_ACMEUNI_TOKEN=k\n"
        "PIEDPIPER_INITECH_TOKEN=k\n"
        "PIEDPIPER_GLOBEX_TOKEN=k\n",
    )
    cats, _ = build_connections_view(vault_path=vault)
    crm = next(c for c in cats if c.id == "crm_marketing")
    piedpiper = next(i for i in crm.integrations if i.id == "piedpiper")
    assert piedpiper.status == "connected"
    assert "3/3 instances connected" in piedpiper.detail


def test_vault_unreadable_marks_every_integration_error(tmp_path: Path) -> None:
    """When the vault is unreadable, every integration reports status=error."""
    missing = tmp_path / "not-there.env"
    cats, readable = build_connections_view(vault_path=missing)
    assert readable is False
    for cat in cats:
        for view in cat.integrations:
            assert view.status == "error"
            assert "Vault unreadable" in view.detail


def test_category_order_matches_spec(tmp_path: Path) -> None:
    vault = _write_vault(tmp_path, "")
    cats, _ = build_connections_view(vault_path=vault)
    ids = [c.id for c in cats]
    expected = [cid for cid, _ in CATEGORY_ORDER]
    assert ids == expected


def test_catalog_preserves_stable_ids() -> None:
    ids = {i.id for i in CATALOG}
    # Spot-check every seeded integration is present and stable.
    for must in (
        "anthropic",
        "openai",
        "xai",
        "google_ai",
        "moonshot",
        "openrouter",
        "together",
        "mistral",
        "replicate",
        "piedpiper",
        "gmail",
        "slack",
        "telegram",
        "meta_ads",
        "stripe",
        "paypal",
        "vandelay",
        "fanbasis",
        "nmi",
        "slash",
        "teller",
        "chargeblast",
        "elevenlabs",
        "pexels",
        "pixabay",
        "jamendo",
        "sync",
        "cloudflare",
        "aws",
        "runpod",
        "huggingface",
        "checkly",
        "livesession",
        "vwo",
        "kowboykit",
    ):
        assert must in ids, f"catalog missing stable id {must!r}"


def test_summary_counts(tmp_path: Path) -> None:
    vault = _write_vault(
        tmp_path,
        "ANTHROPIC_API_KEY=sk-x\nSTRIPE_SECRET_KEY=sk_y\nSTRIPE_PUBLISHABLE_KEY=pk_z\n",
    )
    cats, _ = build_connections_view(vault_path=vault)
    connected, total = summary(cats)
    # Anthropic (1 family, all present) + Stripe (2 families, both present) = 2 connected
    assert connected == 2
    assert total == len(CATALOG)


# ── API contract shape ─────────────────────────────────────────────────────


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_get_connections_returns_pinned_contract_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response matches FINAL-PLAN section B's pinned JSON schema."""
    vault = _write_vault(
        tmp_path,
        "ANTHROPIC_API_KEY=sk-x\nPIEDPIPER_ACMEUNI_TOKEN=k\n",
    )
    monkeypatch.setenv("OMNIAGENTOS_CONNECTIONS_VAULT", str(vault))

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: None
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/connections")
                assert resp.status_code == 200
                body = resp.json()
                # Top-level shape
                assert set(body.keys()) == {
                    "categories",
                    "connected_count",
                    "total_count",
                }
                assert isinstance(body["connected_count"], int)
                assert isinstance(body["total_count"], int)
                assert body["connected_count"] <= body["total_count"]

                # Categories
                assert isinstance(body["categories"], list)
                assert len(body["categories"]) == len(CATEGORY_ORDER)
                for cat in body["categories"]:
                    assert set(cat.keys()) == {"id", "label", "integrations"}
                    assert isinstance(cat["id"], str)
                    assert isinstance(cat["label"], str)
                    assert isinstance(cat["integrations"], list)
                    for integ in cat["integrations"]:
                        assert set(integ.keys()) == {
                            "id",
                            "name",
                            "logo",
                            "status",
                            "instances",
                            "detail",
                            "docs_url",
                        }
                        assert integ["status"] in {
                            "connected",
                            "configured",
                            "not_configured",
                            "error",
                        }
                        assert isinstance(integ["instances"], list)
                        for inst in integ["instances"]:
                            assert set(inst.keys()) == {"label", "status"}

                # Category ordering
                ids = [c["id"] for c in body["categories"]]
                expected = [cid for cid, _ in CATEGORY_ORDER]
                assert ids == expected
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_get_connections_handles_missing_vault_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with no vault file the endpoint returns 200 with status=error for all."""
    missing = tmp_path / "definitely-not-here.env"
    monkeypatch.setenv("OMNIAGENTOS_CONNECTIONS_VAULT", str(missing))

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: None
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/connections")
                assert resp.status_code == 200
                body = resp.json()
                assert body["connected_count"] == 0
                assert body["total_count"] > 0
                for cat in body["categories"]:
                    for integ in cat["integrations"]:
                        assert integ["status"] == "error"
                        assert "Vault unreadable" in integ["detail"]
        finally:
            app.dependency_overrides.clear()

    _run(request())


def test_get_connections_never_returns_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked response must not contain credential values or env-var names."""
    secret_value = "sk-ant-SUPERSECRET-cannot-leak"
    vault = _write_vault(tmp_path, f"ANTHROPIC_API_KEY={secret_value}\n")
    monkeypatch.setenv("OMNIAGENTOS_CONNECTIONS_VAULT", str(vault))

    async def request() -> None:
        app.dependency_overrides[get_store] = lambda: None
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                resp = await client.get("/api/connections")
                assert resp.status_code == 200
                raw = resp.text
                # The secret value must NEVER appear in the response.
                assert secret_value not in raw
                assert "SUPERSECRET" not in raw
        finally:
            app.dependency_overrides.clear()

    _run(request())
