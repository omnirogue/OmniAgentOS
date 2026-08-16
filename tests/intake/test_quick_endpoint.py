"""Coverage for the no-dialog cockpit intake endpoint."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import intake as intake_routes
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.store import CollabStore
from omniagentos.intake.orchestrations import OrchestrationsDal
from omniagentos.intake.planner import PlannedTask, ProjectPlan


@pytest.fixture
def collab() -> CollabStore:
    return CollabStore(":memory:")


@pytest.fixture
def client(collab: CollabStore) -> httpx.AsyncClient:
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def planned() -> ProjectPlan:
    return ProjectPlan(
        project_name="Ship cockpit",
        description="Make the cockpit dispatch work immediately.",
        tasks=[
            PlannedTask(
                title="Implement quick intake",
                acceptance_criteria=["endpoint works", "board card is visible"],
                suggested_discipline="frontend",
            )
        ],
    )


@pytest.mark.parametrize(
    ("executor", "pins"),
    [
        ("auto", None),
        ("sol", {"executor_model_or_lineage": "gpt-5.6-sol"}),
        ("terra", {"executor_model_or_lineage": "gpt-5.6-terra"}),
        ("luna", {"executor_model_or_lineage": "luna"}),
        ("opus", {"executor_model_or_lineage": "opus"}),
    ],
)
def test_quick_dispatch_plans_then_orchestrates(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
    executor: str,
    pins: dict[str, str] | None,
) -> None:
    plan_calls: list[str] = []
    dispatch_calls: list[dict[str, Any]] = []

    def fake_plan(goal: str, **_kwargs: Any) -> ProjectPlan:
        plan_calls.append(goal)
        return planned

    def fake_dispatch(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        dispatch_calls.append(kwargs)
        return {"board_task": {"id": "board_quick"}, "run_id": "orch_quick"}

    monkeypatch.setattr(intake_routes, "plan_goal", fake_plan)
    monkeypatch.setattr(intake_routes, "dispatch_spec", fake_dispatch)

    # A genuinely complex goal takes the PLANNED lane (Fable plans, then orchestrates).
    planned_goal = "Research and refactor the entire billing system across multiple services"
    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": planned_goal, "executor": executor})
    )

    assert response.status_code == 201
    body = response.json()
    assert body["board_task_id"].startswith("btk")
    assert body["run_id"].startswith("orch")
    assert body["status"] == "queued"
    assert body["lane"] == "planned"
    assert body["message"] == "On it — planning, then tracking on the board ↓"
    assert plan_calls == [planned_goal]
    assert dispatch_calls[0]["execute"] == "orchestrate"
    assert dispatch_calls[0]["pins"] == pins
    assert dispatch_calls[0]["priority"] == "balanced"


def test_quick_dispatch_threads_fast_priority(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake_routes, "plan_goal", lambda *_args, **_kwargs: planned)
    monkeypatch.setattr(
        intake_routes,
        "dispatch_spec",
        lambda *_args, **kwargs: (
            calls.append(kwargs) or {"board_task": {"id": "board_fast"}, "run_id": "orch_fast"}
        ),
    )

    # priority="fast" is the executor priority (balanced/fast/quality), NOT the fast
    # LANE -- a complex goal still orchestrates, threading the priority through.
    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={
                "goal": "Analyze and migrate the whole data pipeline end to end",
                "priority": "fast",
            },
        )
    )

    assert response.status_code == 201
    assert calls[0]["priority"] == "fast"


def test_quick_dispatch_fast_lane_spawns_session_without_planning(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A small, bounded goal takes the SUPERFAST lane: a live session is spawned
    straight from the goal (execute="session", fast=True) with NO planning pass, and
    a named location is granted as the session's write target."""
    plan_calls: list[str] = []
    dispatch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        intake_routes, "plan_goal", lambda goal, **_kw: plan_calls.append(goal) or None
    )
    monkeypatch.setattr(
        intake_routes,
        "dispatch_spec",
        lambda *_a, **kw: (
            dispatch_calls.append(kw) or {"board_task": {"id": "board_x"}, "session_id": "ses_x"}
        ),
    )

    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={"goal": "make a folder on my desktop which is called tiger"},
        )
    )

    assert response.status_code == 201
    body = response.json()
    assert body["lane"] == "fast"
    assert body["status"] == "queued"
    # The heavy planner was NEVER called on the fast lane.
    assert plan_calls == []
    assert dispatch_calls[0]["execute"] == "session"
    assert dispatch_calls[0]["fast"] is True
    assert str(dispatch_calls[0]["target_write_root"]).endswith("/Desktop")


def test_quick_plan_creates_pending_card_without_dispatch(
    client: httpx.AsyncClient,
    collab: CollabStore,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_calls: list[str] = []
    monkeypatch.setattr(
        intake_routes,
        "plan_goal",
        lambda goal, **_kwargs: plan_calls.append(goal) or planned,
    )
    monkeypatch.setattr(
        intake_routes,
        "dispatch_spec",
        lambda *_args, **_kwargs: pytest.fail("pending plans must not dispatch"),
    )

    response = asyncio.run(client.post("/api/intake/quick", json={"goal": "Ship it", "plan": True}))

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["run_id"] is None
    card = collab.get_board_task(body["board_task_id"])
    assert card is not None
    assert card["status"] == "pending"
    assert plan_calls == ["Ship it"]


def test_quick_planning_crash_blocks_card_and_records_lifecycle_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = str(tmp_path / "planning-crash.db")
    collab = CollabStore(db)
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab

    def fail_plan(*_args: Any, **_kwargs: Any) -> ProjectPlan:
        raise RuntimeError("planner crashed")

    monkeypatch.setattr(intake_routes, "plan_goal", fail_plan)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        response = asyncio.run(
            client.post(
                "/api/intake/quick",
                json={"goal": "Research and refactor every billing service end to end"},
            )
        )
        assert response.status_code == 201
        body = response.json()
        row = OrchestrationsDal(db)
        try:
            lifecycle = row.get(body["run_id"])
        finally:
            row.close()
        assert lifecycle is not None
        assert lifecycle["status"] == "failed"
        assert lifecycle["stage"] == "planning"
        assert lifecycle["error"] == "planner crashed"
        card = collab.get_board_task(body["board_task_id"])
        assert card is not None
        assert card["status"] == "blocked"
        assert "planner crashed" in card["description"]
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "payload",
    [{}, {"goal": "Ship it", "executor": "unknown"}],
)
def test_quick_dispatch_validates_input(client: httpx.AsyncClient, payload: dict[str, str]) -> None:
    response = asyncio.run(client.post("/api/intake/quick", json=payload))
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# D10 Mode dial: speed → priority mapping + execute="single" suppression
# ---------------------------------------------------------------------------

_COMPLEX_GOAL = "Research and refactor the entire billing system across multiple services"


def _capture_dispatch(
    monkeypatch: pytest.MonkeyPatch, planned: ProjectPlan
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake_routes, "plan_goal", lambda *_a, **_kw: planned)
    monkeypatch.setattr(
        intake_routes,
        "dispatch_spec",
        lambda *_a, **kw: (
            calls.append(kw) or {"board_task": {"id": "board_speed"}, "run_id": "orch_speed"}
        ),
    )
    return calls


@pytest.mark.parametrize(
    ("speed", "priority"),
    [("fast", "fast"), ("auto", "balanced"), ("ultra", "quality")],
)
def test_quick_speed_maps_to_priority_and_threads_speed(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
    speed: str,
    priority: str,
) -> None:
    """D10: fast→'fast', auto→'balanced', ultra→'quality'; speed rides along."""
    calls = _capture_dispatch(monkeypatch, planned)

    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": _COMPLEX_GOAL, "speed": speed})
    )

    assert response.status_code == 201
    assert calls[0]["priority"] == priority
    assert calls[0]["speed"] == speed
    assert calls[0]["execute"] == "orchestrate"


def test_quick_speed_fast_pins_the_gemini_planner(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fast Mode pins the orchestrator's replan onto gemini-3.6-flash (explicit
    model id — no modelintel registry lookup involved)."""
    calls = _capture_dispatch(monkeypatch, planned)

    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": _COMPLEX_GOAL, "speed": "fast"})
    )

    assert response.status_code == 201
    assert calls[0]["pins"] == {"planner_model": "gemini-3.6-flash"}


def test_quick_explicit_priority_survives_default_speed(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Automation backcompat: an old client sending only priority (no speed →
    default 'auto') keeps its explicit priority exactly as today."""
    calls = _capture_dispatch(monkeypatch, planned)

    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": _COMPLEX_GOAL, "priority": "quality"})
    )

    assert response.status_code == 201
    assert calls[0]["priority"] == "quality"


def test_quick_nondefault_speed_wins_over_legacy_priority(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Mode dial is the master knob: speed='ultra' beats priority='fast'."""
    calls = _capture_dispatch(monkeypatch, planned)

    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={"goal": _COMPLEX_GOAL, "speed": "ultra", "priority": "fast"},
        )
    )

    assert response.status_code == 201
    assert calls[0]["priority"] == "quality"


def test_quick_slimmed_payload_still_dispatches(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The simplified composer sends only {goal, execute, speed} — the tolerated-
    optional executor/priority/category fields (D11) default server-side."""
    calls = _capture_dispatch(monkeypatch, planned)

    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={"goal": _COMPLEX_GOAL, "execute": "single", "speed": "auto"},
        )
    )

    assert response.status_code == 201
    assert calls[0]["priority"] == "balanced"
    assert calls[0]["pins"] is None
    assert calls[0]["category"] is None


def test_quick_execute_single_threads_the_hard_suppress(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute='single' reaches dispatch_spec verbatim — the service-level HARD
    suppress of the auto solo-vs-swarm upgrade (D10)."""
    calls = _capture_dispatch(monkeypatch, planned)

    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": _COMPLEX_GOAL, "execute": "single"})
    )

    assert response.status_code == 201
    assert calls[0]["execute"] == "single"


def test_quick_invalid_speed_is_rejected(client: httpx.AsyncClient) -> None:
    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": "Ship it", "speed": "warp"})
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# D12 force hatch: a leading "solo:" / "single:" / "swarm:" forces topology
# ---------------------------------------------------------------------------


def _capture_plan_and_dispatch(
    monkeypatch: pytest.MonkeyPatch, planned: ProjectPlan
) -> tuple[list[str], list[dict[str, Any]]]:
    plan_goals: list[str] = []
    calls: list[dict[str, Any]] = []

    def fake_plan(goal: str, **_kw: Any) -> ProjectPlan:
        plan_goals.append(goal)
        return planned

    monkeypatch.setattr(intake_routes, "plan_goal", fake_plan)
    monkeypatch.setattr(
        intake_routes,
        "dispatch_spec",
        lambda *_a, **kw: (
            calls.append(kw) or {"board_task": {"id": "board_prefix"}, "run_id": "orch_prefix"}
        ),
    )
    return plan_goals, calls


@pytest.mark.parametrize("prefix", ["solo:", "single:", "SOLO:"])
def test_quick_solo_prefix_forces_single_and_strips_goal(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
) -> None:
    """D12: 'solo:' is the cockpit's no-auto-swarm guarantee now that the
    composer sends no execute — dispatch_spec gets execute='single' and
    planning sees the goal WITHOUT the literal prefix."""
    plan_goals, calls = _capture_plan_and_dispatch(monkeypatch, planned)

    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": f"{prefix} {_COMPLEX_GOAL}"})
    )

    assert response.status_code == 201
    assert calls[0]["execute"] == "single"
    assert plan_goals == [_COMPLEX_GOAL]


def test_quick_unknown_colon_word_stays_untouched(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrecognized leading word ('deploy:') is NOT a hatch — the goal rides
    the default router path verbatim, prefix included."""
    plan_goals, calls = _capture_plan_and_dispatch(monkeypatch, planned)
    goal = f"deploy: {_COMPLEX_GOAL}"

    response = asyncio.run(client.post("/api/intake/quick", json={"goal": goal}))

    assert response.status_code == 201
    assert calls[0]["execute"] == "orchestrate"
    assert plan_goals == [goal]


def test_quick_bare_prefix_is_a_normal_goal(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brief that is ONLY 'swarm:' has no task text — treated as a plain
    (tiny) goal: the fastlane catches it and the swarm branch never fires."""
    _, calls = _capture_plan_and_dispatch(monkeypatch, planned)

    response = asyncio.run(client.post("/api/intake/quick", json={"goal": "swarm:"}))

    assert response.status_code == 201
    assert response.json()["lane"] == "fast"
    assert calls[0]["execute"] == "session"


# ---------------------------------------------------------------------------
# F3: omitted speed (legacy payload) vs the dial's EXPLICIT "auto"
# ---------------------------------------------------------------------------


def _capture_dispatch_and_plan(
    monkeypatch: pytest.MonkeyPatch, planned: ProjectPlan
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    plan_calls: list[dict[str, Any]] = []
    dispatch_calls: list[dict[str, Any]] = []

    def fake_plan(goal: str, **kwargs: Any) -> ProjectPlan:
        plan_calls.append({"goal": goal, **kwargs})
        return planned

    monkeypatch.setattr(intake_routes, "plan_goal", fake_plan)
    monkeypatch.setattr(
        intake_routes,
        "dispatch_spec",
        lambda *_a, **kw: (
            dispatch_calls.append(kw) or {"board_task": {"id": "board_f3"}, "run_id": "orch_f3"}
        ),
    )
    return plan_calls, dispatch_calls


def test_quick_legacy_payload_keeps_complexity_derived_planning(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3 regression: a legacy payload (NO speed field) is byte-identical to
    today — planning stays on the default (complexity-derived effort) planner
    seam, the legacy priority wins, and no speed/pins are invented."""
    plan_calls, dispatch_calls = _capture_dispatch_and_plan(monkeypatch, planned)

    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": _COMPLEX_GOAL, "priority": "quality"})
    )

    assert response.status_code == 201
    # The planner seam is the DEFAULT one — NOT the speed chain (whose 'auto'
    # would force Fable xhigh instead of the complexity-derived effort).
    assert plan_calls[0]["llm"] is intake_routes.default_planner_llm
    assert dispatch_calls[0]["priority"] == "quality"
    assert dispatch_calls[0]["speed"] is None
    assert dispatch_calls[0]["pins"] is None


def test_quick_explicit_auto_speed_plans_via_the_speed_seam(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F3: the dial's EXPLICIT 'auto' is distinguishable from an omitted field —
    it plans through the speed seam (Fable at xhigh per D10), not the
    complexity-derived default."""
    plan_calls, dispatch_calls = _capture_dispatch_and_plan(monkeypatch, planned)

    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": _COMPLEX_GOAL, "speed": "auto"})
    )

    assert response.status_code == 201
    assert plan_calls[0]["llm"] is not intake_routes.default_planner_llm
    assert dispatch_calls[0]["speed"] == "auto"
    assert dispatch_calls[0]["priority"] == "balanced"


# ---------------------------------------------------------------------------
# F2: the superfast-lane heuristic vs the dial
# ---------------------------------------------------------------------------

_FASTLANE_GOAL = "make a folder on my desktop which is called tiger"


def test_quick_ultra_speed_skips_the_fastlane_heuristic(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: an EXPLICIT speed='ultra' skips the fastlane heuristic entirely —
    even a fastlane-shaped goal takes the planned lane with ultra semantics."""
    plan_calls, dispatch_calls = _capture_dispatch_and_plan(monkeypatch, planned)

    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": _FASTLANE_GOAL, "speed": "ultra"})
    )

    assert response.status_code == 201
    body = response.json()
    assert body["lane"] == "planned"
    # The heavy planner DID run (the fastlane would have skipped it)…
    assert plan_calls and plan_calls[0]["goal"] == _FASTLANE_GOAL
    # …and dispatch is the planned orchestrate with ultra semantics.
    assert dispatch_calls[0]["execute"] == "orchestrate"
    assert dispatch_calls[0]["priority"] == "quality"
    assert dispatch_calls[0]["speed"] == "ultra"


def test_quick_fast_speed_keeps_fastlane_and_threads_speed(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2: speed='fast' keeps the fastlane, and the speed rides into the
    session dispatch so the spawned session records it."""
    plan_calls, dispatch_calls = _capture_dispatch_and_plan(monkeypatch, planned)

    response = asyncio.run(
        client.post("/api/intake/quick", json={"goal": _FASTLANE_GOAL, "speed": "fast"})
    )

    assert response.status_code == 201
    assert response.json()["lane"] == "fast"
    assert plan_calls == []
    assert dispatch_calls[0]["execute"] == "session"
    assert dispatch_calls[0]["speed"] == "fast"


def test_quick_omitted_speed_keeps_fastlane(
    client: httpx.AsyncClient,
    planned: ProjectPlan,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F2/F3 regression: a legacy payload keeps the fastlane heuristic exactly,
    with no speed invented on the session dispatch."""
    plan_calls, dispatch_calls = _capture_dispatch_and_plan(monkeypatch, planned)

    response = asyncio.run(client.post("/api/intake/quick", json={"goal": _FASTLANE_GOAL}))

    assert response.status_code == 201
    assert response.json()["lane"] == "fast"
    assert plan_calls == []
    assert dispatch_calls[0]["execute"] == "session"
    assert dispatch_calls[0]["speed"] is None


# ---------------------------------------------------------------------------
# F1: Plan-first persists the dial on the pending card
# ---------------------------------------------------------------------------


def _plan_first_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    planned: ProjectPlan,
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """POST a plan-first payload against a FILE-backed store and return the
    pending card's id + its persisted longhaul_json."""
    from omniagentos.longhaul.store import LonghaulStore

    db = str(tmp_path / "plan-first.db")
    collab = CollabStore(db)
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    monkeypatch.setattr(intake_routes, "plan_goal", lambda *_a, **_kw: planned)
    monkeypatch.setattr(
        intake_routes,
        "dispatch_spec",
        lambda *_a, **_kw: pytest.fail("pending plans must not dispatch"),
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    try:
        response = asyncio.run(client.post("/api/intake/quick", json=payload))
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending"
        longhaul = LonghaulStore(db)
        try:
            state = longhaul.get_longhaul_json(body["board_task_id"])
        finally:
            longhaul.close()
        return str(body["board_task_id"]), state
    finally:
        asyncio.run(client.aclose())
        app.dependency_overrides.clear()


def test_quick_plan_first_persists_dial_directives(
    tmp_path: Path, planned: ProjectPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: Plan-first + Single Task persists execute+speed on the pending card
    (longhaul_json['intake']) so approval-time dispatch honors both — the card
    must never auto-swarm at approval."""
    _, state = _plan_first_card(
        tmp_path,
        monkeypatch,
        planned,
        {"goal": "Ship it", "plan": True, "execute": "single", "speed": "ultra"},
    )
    assert state is not None
    assert state["intake"] == {"execute": "single", "speed": "ultra"}


def test_quick_plan_first_legacy_payload_persists_nothing(
    tmp_path: Path, planned: ProjectPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1/F3 regression: a legacy plan-first payload writes NO dial directives."""
    _, state = _plan_first_card(tmp_path, monkeypatch, planned, {"goal": "Ship it", "plan": True})
    assert not (state or {}).get("intake")


def test_quick_plan_first_persists_prefix_forced_execute(
    tmp_path: Path, planned: ProjectPlan, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D12: a 'solo:' plan-first brief persists the FORCED execute in the
    pending card's intake directives — the hatch must not silently no-op on
    plan-first."""
    _, state = _plan_first_card(
        tmp_path,
        monkeypatch,
        planned,
        {"goal": "solo: Ship it", "plan": True, "speed": "auto"},
    )
    assert state is not None
    assert state["intake"] == {"execute": "single", "speed": "auto"}
