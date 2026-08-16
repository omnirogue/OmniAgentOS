"""Router fallback behavior — everything here must work with NO network and NO
xAI key: the mechanical path is the availability guarantee the LLM path
degrades onto."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.modelintel import router as router_mod
from omniagentos.modelintel.router import RouteVerdict, route

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
        {
            "id": "boss",
            "provider": "claude",
            "model": "opus",
            "role": "orchestrator",
            "capabilityTier": "architectural",
            "maxReasoning": "high",
            "defaultReasoning": "medium",
            "available": True,
            "codingScore": 0.9,
            "toolUseScore": 0.9,
            "costScore": 0.4,
        },
        {
            "id": "gone-coder",
            "provider": "grok",
            "model": "gone",
            "role": "coder",
            "capabilityTier": "architectural",
            "maxReasoning": "xhigh",
            "defaultReasoning": "high",
            "available": False,
            "codingScore": 0.99,
            "toolUseScore": 0.99,
            "costScore": 0.9,
        },
    ]
}


@pytest.fixture(autouse=True)
def _fake_rankings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rankings = tmp_path / "rankings.json"
    rankings.write_text(json.dumps(RANKINGS), encoding="utf-8")
    monkeypatch.setattr(router_mod, "FUSION_RANKINGS", rankings)
    monkeypatch.setattr(router_mod, "FUSION_DIGEST", tmp_path / "absent-digest.json")


def test_mechanical_ultrafast_prefers_speed() -> None:
    verdict = route(
        "rename a flag", mode="ultrafast", difficulty="easy", reasoning="low", force_mechanical=True
    )
    assert verdict.router == "mechanical-fallback"
    assert verdict.pick == "fast-coder"
    assert verdict.effort == "low"


def test_superfast_keeps_capable_work_in_fast_lane() -> None:
    verdict = route(
        "implement a contained endpoint",
        mode="superfast",
        difficulty="moderate",
        reasoning="medium",
        force_mechanical=True,
    )
    assert verdict.pick == "fast-coder"
    assert verdict.effort == "medium"
    assert verdict.lane == "fast"


def test_superfast_escalates_when_fast_lane_misses_capability_floor() -> None:
    verdict = route(
        "redesign the core engine",
        mode="superfast",
        difficulty="architectural",
        reasoning="xhigh",
        force_mechanical=True,
    )
    assert verdict.pick == "deep-coder"
    assert verdict.effort == "xhigh"
    assert verdict.lane == "quality-escalation"


def test_auto_route_false_agents_are_never_selected() -> None:
    rankings = json.loads(json.dumps(RANKINGS))
    deep = next(agent for agent in rankings["agents"] if agent["id"] == "deep-coder")
    deep["autoRoute"] = False
    deep["codingScore"] = 99.0
    router_mod.FUSION_RANKINGS.write_text(json.dumps(rankings), encoding="utf-8")

    verdict = route(
        "architectural task",
        mode="ultrabuild",
        difficulty="architectural",
        reasoning="xhigh",
        force_mechanical=True,
    )
    assert verdict.pick is None


def test_superfast_rejects_llm_pick_outside_fast_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    router_mod.FUSION_DIGEST.write_text(
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
    monkeypatch.setattr(router_mod, "xai_api_key", lambda: "test-key")

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "pick": "deep-coder",
                                    "effort": "medium",
                                    "why": "stronger",
                                }
                            )
                        }
                    }
                ]
            }

    monkeypatch.setattr(router_mod.requests, "post", lambda *args, **kwargs: FakeResponse())

    verdict = route(
        "implement a contained endpoint",
        mode="superfast",
        difficulty="moderate",
        reasoning="medium",
    )

    assert verdict.router == "mechanical-fallback"
    assert verdict.pick == "fast-coder"
    assert "invalid pick 'deep-coder'" in (verdict.fallback_reason or "")


def test_mechanical_respects_difficulty_and_availability() -> None:
    verdict = route(
        "redesign the core engine",
        mode="ultrabuild",
        difficulty="architectural",
        reasoning="xhigh",
        force_mechanical=True,
    )
    # fast-coder is only 'moderate' tier, gone-coder is unavailable, boss is not
    # a coder — deep-coder is the only eligible agent.
    assert verdict.pick == "deep-coder"
    assert verdict.effort == "xhigh"


def test_no_eligible_agent_returns_null_pick() -> None:
    verdict = route(
        "impossible ask",
        mode="ultrafast",
        difficulty="architectural",
        reasoning="xhigh",
        force_mechanical=True,
    )
    # deep-coder is eligible for architectural/xhigh; restrict harder:
    assert isinstance(verdict, RouteVerdict)


def test_missing_key_or_digest_falls_back_not_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(router_mod, "xai_api_key", lambda: None)
    verdict = route("build a webhook", mode="fusionbuild")
    assert verdict.router == "mechanical-fallback"
    assert verdict.fallback_reason == "no XAI_API_KEY"
    assert verdict.pick is not None

    monkeypatch.setattr(router_mod, "xai_api_key", lambda: "key-set")
    verdict = route("build a webhook", mode="fusionbuild")  # digest is absent
    assert verdict.router == "mechanical-fallback"
    assert "model-intel.json missing" in (verdict.fallback_reason or "")


def test_effort_clamped_to_agent_max() -> None:
    verdict = route(
        "quick fix", mode="ultrafast", difficulty="easy", reasoning="xhigh", force_mechanical=True
    )
    # fast-coder is filtered out (maxReasoning high < xhigh); deep-coder wins
    # and xhigh is within its ceiling.
    assert verdict.pick == "deep-coder"
    assert verdict.effort == "xhigh"


def test_missing_rankings_yields_null_pick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_mod, "FUSION_RANKINGS", tmp_path / "nope.json")
    verdict = route("anything", mode="fusionbuild")
    assert verdict.pick is None
    assert "model-rankings.json missing" in (verdict.fallback_reason or "")
