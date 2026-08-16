"""HTTP surface: the three gated /api/testobs reads, against a tmp var/ root.

Same ASGI-against-httpx pattern the other route tests use. No store override is
needed — these handlers read files, never the control-plane database. They ARE
session-token gated (SEC-005), so every request here carries the header;
``real_auth`` opts out of the suite-wide app-level bypass, which would otherwise
say nothing about a gate that lives in the handler's own dependency.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.main import app
from tests.testobs.conftest import receipt, seed_northstar, ts_ago, write_ledger, write_receipt

pytestmark = pytest.mark.real_auth

PATHS = ("/api/testobs/overview", "/api/testobs/series", "/api/testobs/weakspots")


def _get(path: str, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    async def request() -> tuple[int, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(path, headers=headers or {})
            return response.status_code, response.json()

    return asyncio.run(request())


@pytest.mark.parametrize("path", PATHS)
def test_every_testobs_read_requires_the_session_token(var_root: Path, path: str) -> None:
    """401 BEFORE any query-arity complaint: the gate is the first thing met."""
    status, body = _get(path)
    assert status == 401
    assert body["error"]["code"] == "unauthorized"


def test_series_unknown_category_is_422(var_root: Path, auth_headers: dict[str, str]) -> None:
    status, body = _get("/api/testobs/series?category=nope", auth_headers)
    assert status == 422
    assert "unknown category" in body["error"]["message"]
    assert body["error"]["detail"]["known"] == ["northstar", "memory", "diagnostics", "suite"]


def test_series_without_a_category_is_422_not_500(
    var_root: Path, auth_headers: dict[str, str]
) -> None:
    assert _get("/api/testobs/series", auth_headers)[0] == 422


def test_series_refuses_days_outside_the_declared_range(
    var_root: Path, auth_headers: dict[str, str]
) -> None:
    assert _get("/api/testobs/series?category=suite&days=0", auth_headers)[0] == 422
    assert _get("/api/testobs/series?category=suite&days=366", auth_headers)[0] == 422


def test_overview_renders_absence_as_absence(
    var_root: Path, auth_headers: dict[str, str]
) -> None:
    status, body = _get("/api/testobs/overview", auth_headers)
    assert status == 200
    assert body["generated_at"].endswith("Z")
    for category in ("northstar", "memory", "diagnostics", "suite"):
        assert body[category]["available"] is False
        assert body[category]["reason"]
        # The honesty rule: no zero-shaped stats standing in for "unknown".
        assert set(body[category]) == {"available", "reason"}


def test_overview_and_series_report_measured_numbers(
    var_root: Path, auth_headers: dict[str, str]
) -> None:
    seed_northstar(var_root)
    write_receipt(var_root, "aaa.json", receipt(9, 1))
    write_ledger(
        var_root,
        [{"ts": ts_ago(1), "tier": "tier1", "feature": "api_ui", "passed": 9, "failed": 1,
          "errors": 0, "status": "ok", "failures": [{"nodeid": "tests/api/test_x.py::test_a"}]}],
    )

    status, body = _get("/api/testobs/overview", auth_headers)
    assert status == 200
    assert body["northstar"]["distance"] == 23.7
    assert body["northstar"]["checks_fail"] == 1
    assert body["diagnostics"]["features_failing"] == 1
    assert body["suite"]["runs_7d"] == 1
    assert body["memory"] == {
        "available": False,
        "reason": "no durable memcert runs recorded yet",
    }

    status, series = _get("/api/testobs/series?category=northstar&days=90", auth_headers)
    assert status == 200
    assert series["category"] == "northstar" and series["days"] == 90
    assert series["available"] is True
    distance = next(s for s in series["series"] if s["metric"] == "nsc.distance")
    assert [p["value"] for p in distance["points"]] == [25.1, 23.7]

    status, empty = _get("/api/testobs/series?category=memory&days=30", auth_headers)
    assert status == 200
    assert empty["available"] is False and empty["series"] == []


def test_weakspots_ranks_gate_failures_first(
    var_root: Path, auth_headers: dict[str, str]
) -> None:
    seed_northstar(var_root)
    write_ledger(
        var_root,
        [{"ts": ts_ago(1), "tier": "tier1", "feature": "api_ui", "passed": 9, "failed": 1,
          "errors": 0, "status": "ok", "failures": [{"nodeid": "tests/api/test_x.py::test_a"}]}],
    )
    status, body = _get("/api/testobs/weakspots", auth_headers)
    assert status == 200
    assert body["generated_at"].endswith("Z")
    assert [item["id"] for item in body["items"]] == [
        "NSC-C11-01",  # gate FAIL
        "api_ui/tier1",  # diagnostics failing cell
        "NSC-C01-01",  # mechanics: NOT_EVALUABLE
    ]
    assert body["items"][0]["gate"] is True
    assert len(body["items"]) <= 100
    assert body["sources"]["northstar"] == {"available": True, "reason": None}
    assert body["sources"]["memory"]["available"] is False


def test_weakspots_distinguishes_clean_from_unmeasured(
    var_root: Path, auth_headers: dict[str, str]
) -> None:
    """An empty items list on an empty var means "nothing measured", not "clean"."""
    status, body = _get("/api/testobs/weakspots", auth_headers)
    assert status == 200
    assert body["items"] == []
    assert all(source["available"] is False for source in body["sources"].values())
    assert all(source["reason"] for source in body["sources"].values())
