"""Regression coverage for model-rankings refresh drift."""

from __future__ import annotations

import json
from pathlib import Path

from omniagentos.modelintel import registry as registry_mod
from omniagentos.modelintel.config import DomainSpec, ModelIntelConfig, ModelSpec


def _rankings_config() -> ModelIntelConfig:
    return ModelIntelConfig(
        domains=[
            DomainSpec(key="coding-implementation", title="Coding", description="d"),
            DomainSpec(key="agentic-tool-use", title="Tool use", description="d"),
        ],
        models=[
            ModelSpec(
                key="alpha",
                title="Alpha",
                provider="test",
                lineage="test",
                fusion_agents=["alpha-coder", "beta-coder"],
                priors={"coding-implementation": 0.8, "agentic-tool-use": 0.7},
            )
        ],
    )


def test_k_b3_autoroute_drift_tripwire_preserves_pin_only_guard(tmp_path: Path) -> None:
    """K-B3: refresh must not drop pin-only autoRoute:false (2026-08-06 ruling).

    Dropping this flag would let expensive pin-only tiers re-enter AUTO routing;
    agents without the optional flag must remain unannotated as well.
    """
    cfg = _rankings_config()
    registry = registry_mod.build(cfg, {}, None, None)
    target = tmp_path / "model-rankings.json"
    target.write_text(
        json.dumps(
            {
                "agents": [
                    {
                        "id": "alpha-coder",
                        "codingScore": 0.1,
                        "toolUseScore": 0.1,
                        "autoRoute": False,
                    },
                    {"id": "beta-coder", "codingScore": 0.1, "toolUseScore": 0.1},
                ]
            }
        ),
        encoding="utf-8",
    )

    result = registry_mod.refresh_fusion_rankings(cfg, registry, target)

    assert result.status == "written"
    agents = {
        agent["id"]: agent for agent in json.loads(target.read_text(encoding="utf-8"))["agents"]
    }
    assert agents["alpha-coder"]["autoRoute"] is False
    assert "autoRoute" not in agents["beta-coder"]
