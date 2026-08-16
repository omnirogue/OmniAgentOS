"""Tests for the /api/access/tool-search route and ambiguous capability examples.

Covers:
1. no X-Session-Token header -> 401 (error.code == "unauthorized")
2. a wrong token -> 401
3. a valid token (monkeypatch verify_token to return True) -> 200, body has query, results, count
4. empty query -> 200, count == 0
5. a real query -> count <= 5, all result ids are in default_catalog()
6. limit is clamped (limit=999 -> <=25 results, limit=0 -> does not raise, returns >= 0 results)
7. degraded path: search_tools raising, 200 with degraded: True
8. gate is checked before catalog work: no token + raising search_tools -> 401, not degraded 200
9. load_registry loads real configs/connectors.yaml
10. exactly 5 capabilities have non-empty input_examples
11. every example string is non-empty, is a str, and is under 160 characters
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.main import app
from omniagentos.connectors import load_registry
from omniagentos.toolplane.catalog import default_catalog


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_tool_search_no_token(client: TestClient) -> None:
    """No X-Session-Token header -> 401."""
    resp = client.get("/api/access/tool-search", params={"q": "stripe"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_tool_search_wrong_token(client: TestClient) -> None:
    """A wrong token -> 401."""
    resp = client.get(
        "/api/access/tool-search",
        params={"q": "stripe"},
        headers={"X-Session-Token": "bad_token_value"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_tool_search_valid_token(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid token (monkeypatched) -> 200 and standard payload keys."""
    monkeypatch.setattr("omniagentos.sessions.token.verify_token", lambda token: True)
    resp = client.get(
        "/api/access/tool-search",
        params={"q": "stripe"},
        headers={"X-Session-Token": "good_token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "query" in body
    assert "results" in body
    assert "count" in body
    assert body["query"] == "stripe"


def test_tool_search_empty_query(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty q -> 200 with count == 0 and results == [] immediately."""
    monkeypatch.setattr("omniagentos.sessions.token.verify_token", lambda token: True)
    resp = client.get(
        "/api/access/tool-search",
        params={"q": "   ", "limit": 10},
        headers={"X-Session-Token": "good_token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 0
    assert body["results"] == []


def test_tool_search_real_query(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A real query ("refund a stripe charge") -> count <= 5, ids in default_catalog()."""
    monkeypatch.setattr("omniagentos.sessions.token.verify_token", lambda token: True)
    resp = client.get(
        "/api/access/tool-search",
        params={"q": "refund a stripe charge", "limit": 5},
        headers={"X-Session-Token": "good_token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] <= 5
    assert len(body["results"]) == body["count"]

    catalog = default_catalog()
    for result in body["results"]:
        assert result["id"] in catalog
        # Verify schema
        assert "id" in result
        assert "namespace" in result
        assert "label" in result
        assert "compact_hint" in result
        assert "description" in result
        assert "action_class" in result
        assert "risk" in result
        assert "read_only" in result
        assert "callable_now" in result
        assert "parameter_names" in result
        assert "input_examples" in result
        assert "score" in result


def test_tool_search_limit_clamped(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """limit is clamped: limit=999 returns at most 25; limit=0 does not raise and returns >=0."""
    monkeypatch.setattr("omniagentos.sessions.token.verify_token", lambda token: True)

    # limit = 999
    resp = client.get(
        "/api/access/tool-search",
        params={"q": "read file", "limit": 999},
        headers={"X-Session-Token": "good_token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] <= 25

    # limit = 0 -> clamped to 1. Should return some results for a broad query and not raise
    resp_zero = client.get(
        "/api/access/tool-search",
        params={"q": "read file", "limit": 0},
        headers={"X-Session-Token": "good_token"},
    )
    assert resp_zero.status_code == 200
    body_zero = resp_zero.json()
    assert body_zero["count"] <= 1


def test_tool_search_degraded_path(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Degraded path: search_tools raises, 200 with degraded: True is returned."""
    monkeypatch.setattr("omniagentos.sessions.token.verify_token", lambda token: True)

    def mock_search_tools(*args, **kwargs):
        raise RuntimeError("Outage simulation")

    monkeypatch.setattr("omniagentos.api.routes.access.search_tools", mock_search_tools)

    resp = client.get(
        "/api/access/tool-search",
        params={"q": "stripe"},
        headers={"X-Session-Token": "good_token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["degraded"] is True
    assert body["count"] == 0
    assert body["results"] == []


def test_tool_search_gate_precedence(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate is checked BEFORE any catalog work. No token -> 401 even if search_tools raises."""

    def mock_search_tools(*args, **kwargs):
        raise RuntimeError("Should never be reached")

    monkeypatch.setattr("omniagentos.api.routes.access.search_tools", mock_search_tools)

    resp = client.get("/api/access/tool-search", params={"q": "stripe"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


# --- Part B configuration tests -----------------------------------------------


def test_part_b_load_registry_real() -> None:
    """load_registry() still loads the real configs/connectors.yaml and it is valid."""
    registry = load_registry()
    assert registry is not None
    assert len(registry.connectors) > 0


def test_part_b_ambiguous_capabilities() -> None:
    """Exactly 5 capabilities have a non-empty input_examples, matching Part B list."""
    registry = load_registry()

    expected_ids = {
        "zapier.trigger",
        "crm_internal.db_read",
        "google_sheets.write",
        "meta_acmeuni.read",
        "gmail.search",
    }

    found_ids = set()
    for _connector_id, connector in registry.connectors.items():
        for cap_id, cap in connector.capabilities.items():
            if cap.input_examples:
                found_ids.add(cap_id)

    assert found_ids == expected_ids, f"Expected {expected_ids}, but found {found_ids}"


def test_part_b_example_properties() -> None:
    """Every example string is non-empty, is a str, and is under 160 characters."""
    registry = load_registry()

    for _connector_id, connector in registry.connectors.items():
        for cap_id, cap in connector.capabilities.items():
            for example in cap.input_examples:
                assert isinstance(example, str)
                assert example.strip() != ""
                assert len(example) < 160, (
                    f"Example for {cap_id} is too long (> 160 chars): {example!r}"
                )
