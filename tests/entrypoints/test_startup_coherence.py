"""The startup-coherence predicate, exercised directly.

``assert_startup_coherence`` is pure environment validation with no app
involved, so the unit shape is the right one here. The wiring into the real
ASGI lifespan is covered separately by
``test_api_startup_refuses_incoherent_sim.py``, which boots the production app
and never imports this mechanism.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.simgate import SimGateError, assert_startup_coherence

REPO_REGISTRY = Path(__file__).resolve().parents[2] / "configs" / "connectors.yaml"


def _coherent_environment(
    tmp_path: Path,
    *,
    campaign: str = "campaign-a",
    var_key: str = "OMNIAGENTOS_VAR_DIR",
) -> tuple[dict[str, str], Path, Path]:
    sim_root = tmp_path / "simulations"
    campaign_root = sim_root / campaign
    var_root = campaign_root / "var"
    (var_root / "secrets").mkdir(parents=True)
    (var_root / "connectors.yaml").write_bytes(REPO_REGISTRY.read_bytes())
    environment = {
        "OMNIAGENTOS_SIM_MODE": "1",
        "OMNIAGENTOS_SIM_CAMPAIGN": campaign,
        "OMNIAGENTOS_SIM_CAMPAIGN_ROOT": str(campaign_root),
        "OMNIAGENTOS_SIM_ROOT": str(sim_root),
        var_key: str(var_root),
    }
    return environment, campaign_root, var_root


def test_assert_startup_coherence_accepts_coherent_sim(tmp_path: Path) -> None:
    environment, _campaign_root, _var_root = _coherent_environment(tmp_path)

    assert_startup_coherence(environment)


def test_assert_startup_coherence_prefixes_incoherent_sim(tmp_path: Path) -> None:
    environment, _campaign_root, _var_root = _coherent_environment(tmp_path)
    environment["OMNIAGENTOS_SIM_MODE"] = "true"

    with pytest.raises(SimGateError, match=r"^REFUSING TO START: "):
        assert_startup_coherence(environment)


def test_assert_startup_coherence_accepts_production() -> None:
    assert_startup_coherence({})
