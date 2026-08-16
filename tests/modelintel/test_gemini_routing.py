"""Unit tests for Gemini and LiteLLM integration router (Lane 1).

Verifies fallback behavior, shadow-mode logging, and loopback connectivity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omniagentos.modelintel import router as router_mod
from omniagentos.modelintel.router import route

RANKINGS = {
    "agents": [
        {
            "id": "fast-coder",
            "provider": "codex",
            "model": "fast",
            "role": "coder",
            "capabilityTier": "moderate",
            "maxReasoning": "high",
            "defaultReasoning": "medium",
            "available": True,
            "warmLatencyMs": 1000,
            "codingScore": 0.7,
            "toolUseScore": 0.8,
            "costScore": 0.9,
            "fastLane": True,
        },
        {
            "id": "deep-coder",
            "provider": "codex",
            "model": "deep",
            "role": "coder",
            "capabilityTier": "architectural",
            "maxReasoning": "xhigh",
            "defaultReasoning": "high",
            "available": True,
            "warmLatencyMs": 3000,
            "codingScore": 0.95,
            "toolUseScore": 0.9,
            "costScore": 0.4,
            "fastLane": False,
        },
    ]
}


@pytest.fixture(autouse=True)
def _fake_rankings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rankings = tmp_path / "rankings.json"
    rankings.write_text(json.dumps(RANKINGS), encoding="utf-8")
    digest = tmp_path / "model-intel.json"
    digest.write_text(
        json.dumps(
            {
                "agents": [
                    {"id": "fast-coder", "model": "fast", "lineage": "codex"},
                    {"id": "deep-coder", "model": "deep", "lineage": "codex"},
                ],
                "domains": {},
                "topByDomain": {},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(router_mod, "FUSION_RANKINGS", rankings)
    monkeypatch.setattr(router_mod, "FUSION_DIGEST", digest)

    import omniagentos.modelintel.config as config_mod

    monkeypatch.setattr(config_mod, "var_dir", lambda: tmp_path)


class FakeResponse:
    def __init__(
        self, content: str, status_code: int = 200, raise_error: Exception | None = None
    ) -> None:
        self._content = content
        self.status_code = status_code
        self._raise_error = raise_error

    def raise_for_status(self) -> None:
        if self._raise_error:
            raise self._raise_error

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self._content}}]}


def test_gemini_routing_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """If OMNIAGENTOS_ROUTER_LLM=gemini-flash, we call Gemini and parse the pick."""
    monkeypatch.setenv("OMNIAGENTOS_ROUTER_LLM", "gemini-flash")
    monkeypatch.setattr(router_mod, "gemini_api_key", lambda: "mock-gemini-key")

    mock_resp = FakeResponse(
        content=json.dumps({"pick": "fast-coder", "effort": "low", "why": "super fast"})
    )
    calls = []

    def mock_post(url: str, **kwargs: Any) -> Any:
        calls.append((url, kwargs.get("headers"), kwargs.get("json")))
        return mock_resp

    monkeypatch.setattr(router_mod.requests, "post", mock_post)

    verdict = route("simple python task", mode="superfast")
    assert verdict.router == "gemini-flash"
    assert verdict.pick == "fast-coder"
    assert verdict.effort == "low"
    assert len(calls) == 1
    assert "localhost:4000" in calls[0][0]


def test_gemini_routing_malformed_json_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """If Gemini returns unparseable content, we fall back to mechanical-fallback instead of crashing."""
    monkeypatch.setenv("OMNIAGENTOS_ROUTER_LLM", "gemini-flash")
    monkeypatch.setattr(router_mod, "gemini_api_key", lambda: "mock-gemini-key")

    mock_resp = FakeResponse(content="not-even-json")
    monkeypatch.setattr(router_mod.requests, "post", lambda *a, **k: mock_resp)

    verdict = route("simple python task", mode="superfast")
    assert verdict.router == "mechanical-fallback"
    assert verdict.pick == "fast-coder"  # default fallback winner
    assert "gemini route failed" in (verdict.fallback_reason or "")


def test_gemini_routing_port_4000_down_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """If loopback proxy is down, we must fall back to mechanical-fallback, never dying."""
    monkeypatch.setenv("OMNIAGENTOS_ROUTER_LLM", "gemini-flash")
    monkeypatch.setattr(router_mod, "gemini_api_key", lambda: "mock-gemini-key")

    import requests

    def mock_post_fail(*args: Any, **kwargs: Any) -> Any:
        raise requests.exceptions.ConnectionError("Connection refused to localhost:4000")

    monkeypatch.setattr(router_mod.requests, "post", mock_post_fail)

    verdict = route("simple python task", mode="superfast")
    assert verdict.router == "mechanical-fallback"
    assert verdict.pick == "fast-coder"
    assert "Connection refused" in (verdict.fallback_reason or "")


def test_router_shadow_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """If OMNIAGENTOS_ROUTER_SHADOW=1, we call BOTH Grok and Gemini, write comparison log, and return Grok."""
    monkeypatch.setenv("OMNIAGENTOS_ROUTER_SHADOW", "1")
    monkeypatch.setattr(router_mod, "xai_api_key", lambda: "mock-xai-key")
    monkeypatch.setattr(router_mod, "gemini_api_key", lambda: "mock-gemini-key")

    calls: list[str] = []

    def mock_post(url: str, **kwargs: Any) -> Any:
        if "api.x.ai" in url:
            calls.append("incumbent")
            return FakeResponse(
                json.dumps({"pick": "deep-coder", "effort": "high", "why": "needs quality"})
            )
        elif "localhost:4000" in url:
            calls.append("flash")
            return FakeResponse(
                json.dumps({"pick": "fast-coder", "effort": "low", "why": "simple task"})
            )

        raise ValueError(f"unexpected request {url}")

    monkeypatch.setattr(router_mod.requests, "post", mock_post)

    verdict = route("rename a flag", mode="fusionbuild")

    # Should return the incumbent (Grok) verdict
    assert verdict.router == "grok"
    assert verdict.pick == "deep-coder"
    assert verdict.effort == "high"

    # Both routes must have been called
    assert set(calls) == {"incumbent", "flash"}

    # Must have written a line to var/modelintel/router_shadow.jsonl
    shadow_log = tmp_path / "router_shadow.jsonl"
    assert shadow_log.is_file()
    lines = shadow_log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    log_data = json.loads(lines[0])
    assert log_data["input_hash"] is not None
    assert log_data["incumbent_decision"]["pick"] == "deep-coder"
    assert log_data["flash_decision"]["pick"] == "fast-coder"
    assert "incumbent_latency_ms" in log_data
    assert "flash_latency_ms" in log_data
