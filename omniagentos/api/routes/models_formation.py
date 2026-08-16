"""Model formation and escalation ladders — swarm planner, integration roles, and lane floors.

Read-only GET endpoint that returns the system's model formation configuration
by parsing YAML config files. No secrets are exposed — only the routing and
policy keys that the dashboard needs to visualize the model assignments.

Graceful degradation: missing files or keys return the available data and
omit missing keys (never 500).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter

LOG = logging.getLogger(__name__)

router = APIRouter(prefix="/api/models", tags=["models"])


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning an empty dict on any error."""
    if not path.exists():
        LOG.debug("config file not found: %s", path)
        return {}
    try:
        import yaml

        with open(path) as f:
            result = yaml.safe_load(f)
            return result if isinstance(result, dict) else {}
    except Exception as e:  # noqa: BLE001
        LOG.warning("failed to parse %s: %s", path, e)
        return {}


def _get_config_dir() -> Path:
    """Resolve configs/ directory relative to the repo root."""
    # omniagentos/api/routes/models_formation.py -> omniagentos -> repo root
    return Path(__file__).parent.parent.parent.parent / "configs"


@router.get("/formation")
def get_formation() -> dict[str, Any]:
    """Retrieve the model formation: swarm planner, escalation ladder, roles, and lane floors.

    Returns:
        A JSON object with keys:
        - swarm_planner: {model, effort} from swarm.yaml.planner
        - model_ladder: ordered list of models from swarm.yaml.router.model_ladder
        - lane_floors: tier -> [models] mapping from swarm.yaml.router.lane_floors
        - integration_roles: role -> {harness, model, effort} from integration.yaml.integration.roles
        - loop_models: role -> {harness, model, effort} from loop_models.yaml roles
        - default_model: default model from llm.yaml.default_model

        Missing files or keys are omitted; the response is never empty (it always
        includes at least swarm_planner if any config is readable).
    """
    config_dir = _get_config_dir()
    result: dict[str, Any] = {}

    # Load swarm.yaml for planner, model_ladder, lane_floors
    swarm = _load_yaml(config_dir / "swarm.yaml")
    if swarm:
        if "planner" in swarm:
            result["swarm_planner"] = swarm["planner"]
        # model_ladder and lane_floors are under swarm.router
        router_config = swarm.get("router", {})
        if isinstance(router_config, dict):
            if "model_ladder" in router_config:
                result["model_ladder"] = router_config["model_ladder"]
            if "lane_floors" in router_config:
                result["lane_floors"] = router_config["lane_floors"]

    # Load integration.yaml for roles
    integration = _load_yaml(config_dir / "integration.yaml")
    if integration and "roles" in integration.get("integration", {}):
        result["integration_roles"] = integration["integration"]["roles"]

    # Load loop_models.yaml for loop models
    loop_models = _load_yaml(config_dir / "loop_models.yaml")
    if loop_models:
        # Extract the top-level role entries (each is a role with harness/model/effort)
        loop_roles = {}
        for key, value in loop_models.items():
            if isinstance(value, dict) and ("harness" in value or "model" in value):
                loop_roles[key] = value
        if loop_roles:
            result["loop_models"] = loop_roles

    # Load llm.yaml for default_model
    llm = _load_yaml(config_dir / "llm.yaml")
    if llm and "default_model" in llm:
        result["default_model"] = llm["default_model"]

    return result
