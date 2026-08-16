"""Tier2 live probes: ShortCallClient via LiteLLM :4000 + OpenRouterAdapter.

Every test skip-gates on its prerequisite with a precise reason (the reason
lands in the ledger — silence is not success) and records spend against the
session ``fh_budget``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parents[3]

_PROXY_BASE = "http://localhost:4000/v1"
# The proxy's own auth mode decides whether this placeholder matters — same
# idiom as omniagentos/adapters/api_base.py LiteLLMAdapter.PLACEHOLDER_KEY.
_PROXY_KEY = "sk-local-litellm-proxy-secure"

# Preference order for the ShortCallClient probe: the client's pinned default
# first, then the served gemini flash-family aliases this proxy actually
# exposes (`/v1/models` on this host serves aliases like gemini25-flash-lite /
# gemini36, not the canonical ids).
_GEMINI_FLASH_PREFERENCE = ("gemini-3.6-flash", "gemini25-flash-lite", "gemini36")


def _proxy_models(timeout: float = 4.0) -> list[str] | None:
    """Model ids served by the local LiteLLM proxy, or None when unreachable."""
    request = urllib.request.Request(
        f"{_PROXY_BASE}/models",
        headers={"Authorization": f"Bearer {_PROXY_KEY}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None
    return [str(entry.get("id") or "") for entry in payload.get("data", [])]


def _pick_served_gemini_flash(models: list[str]) -> str | None:
    for candidate in _GEMINI_FLASH_PREFERENCE:
        if candidate in models:
            return candidate
    return None


def test_shortcall_client_litellm_probe(fh_budget) -> None:
    """One tiny real completion through ShortCallClient against :4000.

    Asserts non-empty text, one shortcalls ledger line appended under the
    fh-isolated var root, and that the budget guard's accounting sees the
    spend (fields honored)."""
    fh_budget.require_headroom()
    models = _proxy_models()
    if models is None:
        pytest.skip(f"litellm proxy unreachable at {_PROXY_BASE} — cannot probe ShortCallClient")
    model = _pick_served_gemini_flash(models)
    if model is None:
        pytest.skip(
            f"litellm proxy at {_PROXY_BASE} serves none of {_GEMINI_FLASH_PREFERENCE} "
            f"(served: {sorted(models)[:8]}...)"
        )

    from omniagentos.llm import budget as budget_mod
    from omniagentos.llm.client import ShortCallClient

    # The shortcalls ledger path derives from OMNIAGENTOS_VAR_DIR/OMNIAGENTOS_VAR
    # at call time — under fh isolation it must land inside the pytest-pinned tmp
    # var, never the product var/. Assert THERE, before spending anything.
    var_pin = os.environ.get("OMNIAGENTOS_VAR_DIR") or os.environ.get("OMNIAGENTOS_VAR")
    assert var_pin, "OMNIAGENTOS_VAR_DIR/OMNIAGENTOS_VAR pin missing — root conftest did not apply"
    ledger = Path(budget_mod._ledger_path())
    assert ledger.is_relative_to(Path(var_pin)), (
        f"shortcalls ledger {ledger} escapes the isolated var root {var_pin}"
    )
    assert not ledger.is_relative_to(REPO / "var"), (
        f"shortcalls ledger {ledger} points into the product var/ — refusing to spend"
    )

    lines_before = (
        len(ledger.read_text(encoding="utf-8").splitlines()) if ledger.exists() else 0
    )

    client = ShortCallClient(timeout=30.0)
    # Budget guard fields honored: the guard is live, configured, and consulted.
    assert float(client.budget_guard.config["daily_usd_cap"]) > 0
    assert client.budget_guard.config["proxy_base_url"].startswith("http")

    text = client.complete(
        [{"role": "user", "content": "Reply with the single word OK."}],
        model=model,
        max_tokens=64,
        temperature=0.0,
        purpose="fh-tier2-probe",
    )
    assert text.strip(), f"empty completion from {model} via {_PROXY_BASE}"

    assert ledger.exists(), f"no shortcalls ledger appeared at {ledger}"
    lines = ledger.read_text(encoding="utf-8").splitlines()
    assert len(lines) == lines_before + 1, (
        f"expected exactly one appended ledger line, had {lines_before}, now {len(lines)}"
    )
    entry = json.loads(lines[-1])
    assert entry["model"] == model
    assert entry["purpose"] == "fh-tier2-probe"
    cost = float(entry["estimated_usd_cost"])
    assert cost >= 0.0
    # The guard reads the SAME ledger for its daily cap — spend must be visible.
    assert client.budget_guard.get_today_spend() >= cost
    fh_budget.record_cost(cost)


def test_openrouter_adapter_probe(fh_budget, monkeypatch: pytest.MonkeyPatch) -> None:
    """16-token-scale probe through OpenRouterAdapter on an allow-listed cheap model.

    Captures the REAL requests.Response (status_code) and asserts exact cost
    provenance from the provider's usage.cost (cost_quality 'exact', nonzero
    cost_usd_nanos) via the adapter's receipts."""
    fh_budget.require_headroom()

    from omniagentos.connectors.secrets_env import load_secrets_env

    load_secrets_env(REPO / "var" / "secrets")
    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        pytest.skip(
            "OPENROUTER_API_KEY unset after loading var/secrets/openrouter.env — "
            "OpenRouter rung unconfigured on this host"
        )

    from omniagentos.routing.api_policy import openrouter_models

    allowed = list(openrouter_models())
    if not allowed:
        pytest.skip("configs/swarm.yaml api_fallback.openrouter_models is empty")
    # Prefer a gemini flash-lite entry when the allow-list carries one; the
    # current list does not, so fall back to the cheapest registered coder
    # ($0.20/M in per the swarm.yaml registration note), then the first entry.
    model = next(
        (m for m in allowed if "gemini" in m and "flash" in m),
        "qwen/qwen3-coder-flash" if "qwen/qwen3-coder-flash" in allowed else allowed[0],
    )

    import requests

    from omniagentos.adapters.openrouter import OpenRouterAdapter
    from omniagentos.contracts import AgentInput, BudgetSpec, ResultStatus, new_id

    captured: dict[str, object] = {}
    real_post = requests.post

    def _capturing_post(url: str, **kwargs: object) -> object:
        payload = kwargs.get("json")
        if isinstance(payload, dict):
            # Cap the probe and ask OpenRouter for exact billed cost in usage.
            payload.setdefault("max_tokens", 64)
            payload.setdefault("usage", {"include": True})
        response = real_post(url, **kwargs)  # type: ignore[arg-type]
        captured["response"] = response
        return response

    monkeypatch.setattr(requests, "post", _capturing_post)

    adapter = OpenRouterAdapter()
    health = adapter.health()
    assert health.healthy, f"openrouter adapter unhealthy despite key: {health.detail}"

    result = adapter.run(
        AgentInput(
            run_id=new_id("run"),
            task_id=new_id("tsk"),
            prompt="Reply with the single word OK.",
            model=model,
            budget=BudgetSpec(wall_ms_max=60_000),
        )
    )

    response = captured.get("response")
    assert response is not None, "adapter never issued a real HTTP request"
    status_code = response.status_code  # real requests.Response attribute
    assert isinstance(status_code, int)
    assert status_code == 200, f"OpenRouter HTTP {status_code} for {model}: {result.error}"
    assert result.status is ResultStatus.OK, f"probe failed: {result.error}"
    assert result.output_text.strip()

    # Exact cost provenance through the api_base receipt seam.
    obs_receipts = [r for r in result.receipts if r.key == "cost_observation_json"]
    assert obs_receipts, "no cost_observation_json receipt on the result"
    observation = json.loads(obs_receipts[0].target)
    assert observation["cost_quality"] == "exact", f"cost not exact: {observation}"
    assert isinstance(observation["cost_usd_nanos"], int)
    assert observation["cost_usd_nanos"] > 0, f"zero-cost observation: {observation}"
    assert result.usage.cost_usd is not None and result.usage.cost_usd > 0

    usage = response.json().get("usage", {})
    actual_cost = float(usage.get("cost", result.usage.cost_usd))
    fh_budget.record_cost(actual_cost)
