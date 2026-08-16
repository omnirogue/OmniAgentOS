"""Test GET /api/models/formation endpoint."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from omniagentos.api.main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_models_formation_endpoint_returns_200(client: TestClient):
    """Test that GET /api/models/formation returns 200 with expected top-level keys."""
    response = client.get("/api/models/formation")
    assert response.status_code == 200

    data = response.json()
    assert isinstance(data, dict)


def test_models_formation_has_expected_keys(client: TestClient):
    """Test that the response contains the expected formation keys."""
    response = client.get("/api/models/formation")
    assert response.status_code == 200

    data = response.json()

    # At least one key should be present from the configs
    expected_possible_keys = {
        "swarm_planner",
        "model_ladder",
        "lane_floors",
        "integration_roles",
        "loop_models",
        "default_model",
    }
    actual_keys = set(data.keys())
    # At least some keys should be present (graceful degradation on missing files)
    assert len(actual_keys & expected_possible_keys) > 0


def test_models_formation_swarm_planner_structure(client: TestClient):
    """Test swarm_planner structure when present."""
    response = client.get("/api/models/formation")
    assert response.status_code == 200

    data = response.json()
    if "swarm_planner" in data:
        planner = data["swarm_planner"]
        assert isinstance(planner, dict)
        assert "model" in planner
        assert "effort" in planner
        assert isinstance(planner["model"], str)
        assert isinstance(planner["effort"], str)


def test_models_formation_model_ladder_structure(client: TestClient):
    """Test model_ladder structure when present."""
    response = client.get("/api/models/formation")
    assert response.status_code == 200

    data = response.json()
    if "model_ladder" in data:
        ladder = data["model_ladder"]
        assert isinstance(ladder, list)
        assert all(isinstance(m, str) for m in ladder)
        assert len(ladder) > 0


def test_models_formation_lane_floors_structure(client: TestClient):
    """Test lane_floors structure when present."""
    response = client.get("/api/models/formation")
    assert response.status_code == 200

    data = response.json()
    if "lane_floors" in data:
        floors = data["lane_floors"]
        assert isinstance(floors, dict)
        for tier, models in floors.items():
            assert isinstance(tier, str)
            assert isinstance(models, list)
            assert all(isinstance(m, str) for m in models)


def test_models_formation_integration_roles_structure(client: TestClient):
    """Test integration_roles structure when present."""
    response = client.get("/api/models/formation")
    assert response.status_code == 200

    data = response.json()
    if "integration_roles" in data:
        roles = data["integration_roles"]
        assert isinstance(roles, dict)
        for role, config in roles.items():
            assert isinstance(role, str)
            assert isinstance(config, dict)
            # Each role should have model and effort (and optionally harness, can_merge_to_main)
            if "model" in config:
                assert isinstance(config["model"], str)


def test_models_formation_loop_models_structure(client: TestClient):
    """Test loop_models structure when present."""
    response = client.get("/api/models/formation")
    assert response.status_code == 200

    data = response.json()
    if "loop_models" in data:
        loop_models = data["loop_models"]
        assert isinstance(loop_models, dict)
        for role, config in loop_models.items():
            assert isinstance(role, str)
            assert isinstance(config, dict)
            if "model" in config:
                assert isinstance(config["model"], str)


def test_models_formation_default_model_structure(client: TestClient):
    """Test default_model structure when present."""
    response = client.get("/api/models/formation")
    assert response.status_code == 200

    data = response.json()
    if "default_model" in data:
        default_model = data["default_model"]
        assert isinstance(default_model, str)


def test_models_formation_json_serializable(client: TestClient):
    """Test that response is JSON-serializable (no datetime or complex objects)."""
    response = client.get("/api/models/formation")
    assert response.status_code == 200

    data = response.json()
    # If we can serialize it without error, the test passes
    serialized = json.dumps(data)
    assert isinstance(serialized, str)
    # Verify we can deserialize it back
    deserialized = json.loads(serialized)
    assert deserialized == data
