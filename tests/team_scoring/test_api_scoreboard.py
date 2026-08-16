"""HTTP contract for the scoring surface.

The dashboard and the 07:00 report must never be able to disagree, so these
routes call the SAME functions the report does rather than re-deriving anything.
The tests below check that literally: the API's numbers are asserted against the
report's own ``gather`` output on the same board.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.store import CollabStore
from omniagentos.db.store import SqliteStore
from omniagentos.sessions import token
from omniagentos.team.report import gather
from omniagentos.team.store import TeamStore

from .conftest import DAY, IN_WINDOW


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)


@pytest.fixture
def client(
    collab_store: CollabStore, store: SqliteStore, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[httpx.AsyncClient]:
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_collab_store] = lambda: collab_store
    monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
    api = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Session-Token": token.load_or_create_token()},
    )
    try:
        yield api
    finally:
        app.dependency_overrides.clear()
        _run(api.aclose())


def test_scoreboard_returns_thin_contract_and_pct_to_10x_by_default(
    client: httpx.AsyncClient,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    from omniagentos.team.scoring import BASELINE_SOURCE

    alice = employees["alice"]
    make_task(
        owner=alice,
        size="L",
        ref="BASE-ALICE",
        title="Baseline week",
        source=BASELINE_SOURCE,
        acceptance="",
        evidence=[("doc", "baseline-alice", "pass")],
        verified_at=IN_WINDOW,
    )
    make_task(
        owner=alice,
        size="M",
        ref="API-1",
        title="This week",
        evidence=[("test_run", "tr-api", "pass")],
        verified_at=IN_WINDOW,
    )

    response = _run(client.get(f"/api/team/scoreboard?window=7d&day={DAY}"))
    assert response.status_code == 200, response.text
    payload = response.json()

    assert set(payload) == {"people", "team", "period", "score_version", "overall"}
    assert payload["score_version"] == "v1"
    assert payload["period"] == {"start": "2026-08-08", "end": DAY}
    leader = payload["people"][0]
    assert set(leader) == {
        "employee_id",
        "score",
        "baseline_points",
        "production_x",
        "pct_to_10x",
    }
    assert leader["employee_id"] == alice
    assert leader["score"] == 3
    assert leader["baseline_points"] == 8
    assert leader["production_x"] == 3 / 8
    assert leader["pct_to_10x"] == 4
    assert payload["team"] == {
        "score": 3,
        "baseline_points": 8,
        "production_x": 3 / 8,
        "pct_to_10x": 4,
    }
    # Everyone appears, including the people with nothing.
    assert len(payload["people"]) == 3


def test_scoreboard_detail_one_includes_counted_and_excluded_breakdown(
    client: httpx.AsyncClient,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    alice = employees["alice"]
    make_task(
        owner=alice,
        size="M",
        ref="API-DETAIL",
        title="Detailed work",
        evidence=[("test_run", "tr-api-detail", "pass")],
        verified_at=IN_WINDOW,
    )

    response = _run(client.get(f"/api/team/scoreboard?window=7d&day={DAY}&detail=1"))
    assert response.status_code == 200, response.text
    leader = response.json()["people"][0]
    assert [entry["ref"] for entry in leader["counted"]] == ["API-DETAIL"]
    assert leader["excluded"] == []


def test_scoreboard_rejects_a_window_it_cannot_parse(client: httpx.AsyncClient) -> None:
    response = _run(client.get("/api/team/scoreboard?window=last-tuesday"))
    assert response.status_code == 400, response.text
    assert "7d or 48h" in response.text


def test_scoreboard_rejects_invalid_day_with_400(client: httpx.AsyncClient) -> None:
    response = _run(client.get("/api/team/scoreboard?day=2026-02-30"))
    assert response.status_code == 400, response.text
    assert "YYYY-MM-DD" in response.text


@pytest.mark.parametrize("window", ["3651d", "87601h"])
def test_scoreboard_rejects_window_over_3650_days(
    client: httpx.AsyncClient, window: str
) -> None:
    response = _run(client.get(f"/api/team/scoreboard?window={window}&day={DAY}"))
    assert response.status_code == 400, response.text
    assert "3650 days" in response.text


def test_diagnostics_can_be_scoped_to_one_person(
    client: httpx.AsyncClient,
    employees: dict[str, str],
    make_task: Callable[..., str],
    add_session: Callable[..., str],
) -> None:
    bob = employees["bob"]
    task = make_task(
        owner=bob,
        size="M",
        ref="API-2",
        title="Work",
        evidence=[("test_run", "tr-api2", "pass")],
        verified_at=IN_WINDOW,
    )
    add_session(task_id=task, started_at="2026-08-12T09:00:00Z", ended_at="2026-08-12T10:00:00Z")

    everyone = _run(client.get(f"/api/team/diagnostics?window=7d&day={DAY}")).json()
    assert set(everyone["people"]) == set(employees.values())

    scoped = _run(client.get(f"/api/team/diagnostics?owner={bob}&window=7d&day={DAY}")).json()
    assert list(scoped["people"]) == [bob]
    measured = scoped["people"][bob]
    assert measured["session_count"] == 1
    assert measured["verified_outcomes"] == 1
    assert measured["outcomes_per_session"] == 1.0
    # Unmeasured stays null across the JSON boundary too — never a helpful 0.
    assert measured["first_pass_success"] is None


def test_report_preview_matches_the_report_and_writes_nothing(
    client: httpx.AsyncClient,
    team_store: TeamStore,
    employees: dict[str, str],
    make_task: Callable[..., str],
) -> None:
    make_task(
        owner=employees["alice"],
        size="M",
        ref="API-3",
        title="Work",
        evidence=[("test_run", "tr-api3", "pass")],
        verified_at=IN_WINDOW,
    )
    response = _run(client.get(f"/api/team/report/preview?day={DAY}"))
    assert response.status_code == 200, response.text
    payload = response.json()

    from omniagentos.team.report import render

    assert payload["day"] == DAY
    assert payload["text"] == render(gather(team_store, DAY))
    # A preview is a READ. No snapshot row may exist afterwards.
    assert team_store.list_snapshots(day=DAY) == []


def test_report_preview_rejects_invalid_day_with_400(client: httpx.AsyncClient) -> None:
    response = _run(client.get("/api/team/report/preview?day=not-a-day"))
    assert response.status_code == 400, response.text
    assert "YYYY-MM-DD" in response.text
