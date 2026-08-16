"""Phase 1 (A1): registry-backed project routing for quick intake.

Covers OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE (off default / shadow / enforce):

* off is byte-for-byte legacy quick-dispatch behavior — the registry is never
  consulted, no clarify gate, no build_preflight.
* shadow computes and LOGS the registry routing decision before dispatch and
  applies NOTHING — no build_preflight, no preflight_json write, no change to
  what dispatch is handed. Stored state and delivered worker inputs stay
  byte-identical to off, which is the property that makes defaulting the flag
  to shadow on the launch path safe at all.
* enforce applies it: a registry hit dispatches through the matched project's
  root_dirs[0] (via project_id, BEFORE dispatch_spec resolves a workspace); a
  genuine miss leaves project_id untouched so dispatch_spec's existing
  scratch-project fallback creates one (with root_dirs assigned); a low-
  confidence hit spends exactly ONE clarify round before proceeding on best
  judgment.

All Fable/router/preflight seams are stubbed — no CLI or real formation
selection is touched.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omniagentos.api.deps import get_store
from omniagentos.api.main import app
from omniagentos.api.routes import intake as intake_routes
from omniagentos.api.routes.collab import get_collab_store
from omniagentos.collab.contracts import BoardTask, BoardTaskStatus
from omniagentos.collab.store import CollabStore
from omniagentos.contracts import HarnessType
from omniagentos.intake.contracts import ClarifyResult, ClarifyTurn
from omniagentos.intake.planner import (
    PlannedTask,
    ProjectPlan,
    ProjectStoreHierarchyDAL,
    RouteDecision,
    provision_plan,
    route_project,
)
from omniagentos.intake.service import (
    QUICK_PROJECT_ROUTING_ENV,
    clarify_intake,
    quick_project_routing_mode,
)
from omniagentos.policy import load_policy


def _stores(tmp_path: Path) -> tuple[Any, CollabStore, Any]:
    collab = CollabStore(str(tmp_path / "phase1.db"))
    return collab._store, collab, load_policy()


@pytest.fixture
def collab() -> CollabStore:
    return CollabStore(":memory:")


@pytest.fixture
def client(collab: CollabStore) -> Generator[httpx.AsyncClient, None, None]:
    app.dependency_overrides[get_store] = lambda: collab._store
    app.dependency_overrides[get_collab_store] = lambda: collab
    test_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    )
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# quick_project_routing_mode() — tri-state gate, DEFAULT off
# ---------------------------------------------------------------------------


def test_quick_project_routing_mode_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(QUICK_PROJECT_ROUTING_ENV, raising=False)
    assert quick_project_routing_mode() == "off"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("off", "off"), ("shadow", "shadow"), ("ENFORCE", "enforce"), ("bogus", "off"), ("", "off")],
)
def test_quick_project_routing_mode_parses_tristate(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv(QUICK_PROJECT_ROUTING_ENV, raw)
    assert quick_project_routing_mode() == expected


# ---------------------------------------------------------------------------
# route_project() — the planner validates the registry workspace before an
# API caller can apply the match (Phase 1 item 1).
# ---------------------------------------------------------------------------


def test_route_project_registry_hit_carries_matched_root_dirs() -> None:
    project = {
        "id": "proj_auth",
        "name": "Authentication",
        "root_dirs": ["/srv/auth", "/srv/auth-docs"],
    }

    decision = route_project(
        ProjectPlan(project_name="Add SSO"),
        [project],
        llm=lambda *_a: {
            "decision": "existing",
            "parent_project_id": "proj_auth",
            "confidence": 0.94,
        },
        require_root_dir=True,
    )

    assert decision.decision == "existing"
    assert decision.parent_project_id == "proj_auth"
    assert decision.root_dirs == ["/srv/auth", "/srv/auth-docs"]
    assert decision.root_dirs[0] == "/srv/auth"


def test_route_project_treats_registry_entry_without_root_as_genuine_miss() -> None:
    llm_calls: list[str] = []

    def unexpected_llm(*_args: Any) -> dict[str, str]:
        llm_calls.append("called")
        return {"decision": "existing"}

    decision = route_project(
        ProjectPlan(project_name="Add SSO"),
        [{"id": "proj_auth", "name": "Authentication", "root_dirs": []}],
        llm=unexpected_llm,
        require_root_dir=True,
    )

    assert decision.decision == "new"
    assert decision.root_dirs == []
    assert "usable root directory" in decision.reason
    assert llm_calls == []


# ---------------------------------------------------------------------------
# ProjectStoreHierarchyDAL — root_dirs assignment (Phase 1 item 4)
# ---------------------------------------------------------------------------


def test_hierarchy_dal_new_top_level_project_gets_root_dirs(tmp_path: Path) -> None:
    store, _collab, _policy = _stores(tmp_path)
    dal = ProjectStoreHierarchyDAL(store)

    project_id = dal.create_project("Standalone")

    project = next(p for p in dal.list_projects() if p["id"] == project_id)
    assert project.get("root_dirs")
    assert len(project["root_dirs"]) == 1


def test_hierarchy_dal_nested_project_reuses_parent_root_dirs(tmp_path: Path) -> None:
    store, _collab, _policy = _stores(tmp_path)
    dal = ProjectStoreHierarchyDAL(store)

    parent_id = dal.create_project("Parent")
    parent = next(p for p in dal.list_projects() if p["id"] == parent_id)
    assert parent["root_dirs"]

    child_id = dal.create_project("Child", parent_project_id=parent_id)
    child = next(p for p in dal.list_projects() if p["id"] == child_id)

    assert child["parent_project_id"] == parent_id
    # The registry match's own root_dirs are reused, not a fresh scratch dir.
    assert child["root_dirs"] == parent["root_dirs"]


# ---------------------------------------------------------------------------
# provision_plan() — decision "existing" reuses matched roots; "new" projects
# get their own root_dirs (Phase 1 item 4).
# ---------------------------------------------------------------------------


def test_provision_plan_existing_route_reuses_matched_root_dirs(tmp_path: Path) -> None:
    store, collab, policy = _stores(tmp_path)
    dal = ProjectStoreHierarchyDAL(store)
    parent_id = dal.create_project("Parent Project")
    parent = next(p for p in dal.list_projects() if p["id"] == parent_id)

    plan = ProjectPlan(project_name="Feature", tasks=[PlannedTask(title="Do it")])
    route = RouteDecision(decision="existing", parent_project_id=parent_id)

    result = provision_plan(store, collab, policy, plan, route, dal, harness=HarnessType.MOCK.value)

    root = next(p for p in dal.list_projects() if p["id"] == result.root_project_id)
    assert root["parent_project_id"] == parent_id
    assert root["root_dirs"] == parent["root_dirs"]


def test_provision_plan_new_route_assigns_its_own_root_dirs(tmp_path: Path) -> None:
    store, collab, policy = _stores(tmp_path)
    dal = ProjectStoreHierarchyDAL(store)

    plan = ProjectPlan(project_name="Greenfield", tasks=[PlannedTask(title="Do it")])
    route = RouteDecision(decision="new", project_name="Greenfield")

    result = provision_plan(store, collab, policy, plan, route, dal, harness=HarnessType.MOCK.value)

    root = next(p for p in dal.list_projects() if p["id"] == result.root_project_id)
    assert root.get("root_dirs")


# ---------------------------------------------------------------------------
# clarify_intake() — exactly one clarify round when routing is gated on
# (Phase 1 item 2, wired via omniagentos.intake.service).
# ---------------------------------------------------------------------------


def test_clarify_forces_a_spec_after_one_round_when_routing_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(QUICK_PROJECT_ROUTING_ENV, "enforce")
    history = [ClarifyTurn(q="What outcome?", a="Ship the button")]

    def llm(prompt: str, _schema: dict[str, Any]) -> dict[str, Any]:
        assert "FINAL round" in prompt
        return {"mode": "questions", "questions": ["ignored — final forces a spec"]}

    result = clarify_intake("Add a button", history, llm=llm)

    assert result.mode == "spec"
    assert result.forced is True


def test_clarify_off_mode_keeps_the_full_round_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(QUICK_PROJECT_ROUTING_ENV, raising=False)
    history = [ClarifyTurn(q="What outcome?", a="Ship the button")]

    def llm(prompt: str, _schema: dict[str, Any]) -> dict[str, Any]:
        assert "FINAL round" not in prompt  # MAX_CLARIFY_ROUNDS is 2; only 1 answered
        return {"mode": "questions", "questions": ["more?"]}

    result = clarify_intake("Add a button", history, llm=llm)

    assert result.mode == "questions"


def test_clarify_shadow_mode_does_not_apply_the_shorter_round_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(QUICK_PROJECT_ROUTING_ENV, "shadow")
    history = [ClarifyTurn(q="What outcome?", a="Ship the button")]

    def llm(prompt: str, _schema: dict[str, Any]) -> dict[str, Any]:
        assert "FINAL round" not in prompt
        return {"mode": "questions", "questions": ["more?"]}

    result = clarify_intake("Add a button", history, llm=llm)

    assert result.mode == "questions"


# ---------------------------------------------------------------------------
# _run_quick_dispatch() — the background dispatch path: registry routing +
# build_preflight must both run BEFORE dispatch_spec (Phase 1 items 1 and 3).
# ---------------------------------------------------------------------------


def _fake_preflight(calls: list[dict[str, Any]]) -> Any:
    def _build(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"formation_id": "coding", "confidence": 0.9, "owned_paths": [], "parallelism": 1}

    return _build


def _fake_dispatch(calls: list[dict[str, Any]]) -> Any:
    def _dispatch(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"board_task": {"id": "btk_test"}, "run_id": "orch_test"}

    return _dispatch


def _fake_plan(calls: list[str], plan: ProjectPlan | None) -> Any:
    def _plan_goal(goal: str, **_kwargs: Any) -> ProjectPlan | None:
        calls.append(goal)
        return plan

    return _plan_goal


def _fake_route(calls: list[Any]) -> Any:
    def _route(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    return _route


def test_run_quick_dispatch_off_mode_never_touches_registry_or_preflight(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(QUICK_PROJECT_ROUTING_ENV, raising=False)
    store, collab, policy = _stores(tmp_path)

    plan = ProjectPlan(project_name="X", description="Y", tasks=[PlannedTask(title="t")])
    monkeypatch.setattr(intake_routes, "plan_goal", lambda *_a, **_k: plan)

    route_calls: list[Any] = []
    monkeypatch.setattr(intake_routes, "route_project", _fake_route(route_calls))

    preflight_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omniagentos.intake.preflight.build_preflight", _fake_preflight(preflight_calls)
    )

    dispatch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake_routes, "dispatch_spec", _fake_dispatch(dispatch_calls))

    intake_routes._run_quick_dispatch(
        "Build a login page",
        executor="auto",
        priority="balanced",
        plan_first=False,
        project_id=None,
        board_task_id="btk_test",
        run_id="orch_test",
        store=store,
        collab_store=collab,
        policy_cfg=policy,
        planner_llm=intake_routes.default_planner_llm,
    )

    assert route_calls == []
    assert preflight_calls == []
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0]["project_id"] is None


def _run_one_quick_dispatch(
    mode: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """One quick dispatch under *mode*; returns (preflight, dispatch, board rows).

    Everything else is held identical — same goal, same stubbed plan, same
    stubbed route decision, same board card — so the ONLY independent variable
    is the flag. The board row is read back in FULL (``SELECT *``, columns
    discovered from the schema) rather than by named column: "shadow writes
    nothing" is a claim about the whole row, and a named-column assertion would
    keep passing if the write moved to another column. Only the two provably
    wall-clock columns are dropped.
    """
    if mode is None:
        monkeypatch.delenv(QUICK_PROJECT_ROUTING_ENV, raising=False)
    else:
        monkeypatch.setenv(QUICK_PROJECT_ROUTING_ENV, mode)
    store, collab, policy = _stores(tmp_path)

    # A FIXED id, so the two runs' rows are comparable at all.
    card = BoardTask(id="btk_fixed", title="Planning the work…", status=BoardTaskStatus.OPEN)
    collab.create_board_task(card)

    plan = ProjectPlan(project_name="Add SSO", description="...", tasks=[PlannedTask(title="t")])
    monkeypatch.setattr(intake_routes, "plan_goal", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        intake_routes,
        "route_project",
        lambda *_a, **_k: RouteDecision(
            decision="existing",
            parent_project_id="proj_auth",
            root_dirs=["/srv/auth"],
            confidence=0.95,
        ),
    )
    preflight_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omniagentos.intake.preflight.build_preflight", _fake_preflight(preflight_calls)
    )
    dispatch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake_routes, "dispatch_spec", _fake_dispatch(dispatch_calls))

    intake_routes._run_quick_dispatch(
        "Add SSO to the auth project",
        executor="auto",
        priority="balanced",
        plan_first=False,
        project_id=None,
        board_task_id=card.id,
        run_id="orch_test",
        store=store,
        collab_store=collab,
        policy_cfg=policy,
        planner_llm=intake_routes.default_planner_llm,
    )

    columns = [
        str(row[1])
        for row in collab._store._connection.execute("PRAGMA table_info(board_tasks)").fetchall()
    ]
    volatile = {"created_at", "updated_at"}
    rows = [
        {name: value for name, value in zip(columns, row, strict=True) if name not in volatile}
        for row in collab._store._connection.execute(
            "SELECT * FROM board_tasks ORDER BY id"
        ).fetchall()
    ]
    return preflight_calls, dispatch_calls, rows


def test_shadow_is_byte_identical_to_off_in_stored_state_and_delivered_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ratified meaning of ``shadow``, asserted against ``off`` directly.

    FEATURE-FLAGS.md: "``off`` means the feature is inert and behavior is
    byte-identical to the flag being absent; ``shadow`` computes and records the
    decision but applies nothing". A flag that is DEFAULTED to shadow on the
    launch path (which is what this lane does) is therefore only safe if shadow
    is observationally inert, so the comparison is made rather than asserted:
    both persisted rows and the inputs handed to ``dispatch_spec`` must match.

    The regression this pins is concrete: shadow used to run ``build_preflight``
    (persisting a classification via ``orgdims/classify.py`` and writing
    ``preflight_json``), and ``swarm/spawn.py`` reads that row to reorder the
    skill hits in the brief the worker receives. Shadow changed the delivered
    prompt.
    """
    off_preflight, off_dispatch, off_rows = _run_one_quick_dispatch(
        None, monkeypatch, tmp_path / "off"
    )
    shadow_preflight, shadow_dispatch, shadow_rows = _run_one_quick_dispatch(
        "shadow", monkeypatch, tmp_path / "shadow"
    )

    assert off_preflight == shadow_preflight == []
    assert shadow_rows == off_rows, "shadow persisted something off did not"
    assert shadow_dispatch == off_dispatch, "shadow changed what dispatch was handed"


def test_enforce_is_the_only_mode_that_diverges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other half of the contract: gating shadow must not disarm enforce.

    A "shadow == off" fix that also stopped enforce from doing the work would
    be green on the test above and would have deleted the feature.
    """
    _, off_dispatch, _ = _run_one_quick_dispatch(None, monkeypatch, tmp_path / "off")
    enforce_preflight, enforce_dispatch, enforce_rows = _run_one_quick_dispatch(
        "enforce", monkeypatch, tmp_path / "enforce"
    )

    assert len(enforce_preflight) == 1, "enforce must still build preflight"
    assert enforce_dispatch[0]["project_id"] == "proj_auth"
    assert off_dispatch[0]["project_id"] is None
    assert enforce_rows[0]["preflight_json"], "enforce must still persist preflight_json"
    assert "formation_id" in enforce_rows[0]["preflight_json"]


def test_run_quick_dispatch_enforce_hit_routes_via_matched_project_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(QUICK_PROJECT_ROUTING_ENV, "enforce")
    store, collab, policy = _stores(tmp_path)

    plan = ProjectPlan(project_name="Add SSO", description="...", tasks=[PlannedTask(title="t")])
    monkeypatch.setattr(intake_routes, "plan_goal", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        intake_routes,
        "route_project",
        lambda *_a, **_k: RouteDecision(
            decision="existing",
            parent_project_id="proj_auth",
            root_dirs=["/srv/auth"],
            confidence=0.9,
        ),
    )

    call_order: list[str] = []
    preflight_calls: list[dict[str, Any]] = []

    def fake_preflight(**kwargs: Any) -> dict[str, Any]:
        call_order.append("preflight")
        preflight_calls.append(kwargs)
        return {"formation_id": "coding", "confidence": 0.9, "owned_paths": [], "parallelism": 1}

    monkeypatch.setattr("omniagentos.intake.preflight.build_preflight", fake_preflight)

    dispatch_calls: list[dict[str, Any]] = []

    def fake_dispatch(*_a: Any, **kw: Any) -> dict[str, Any]:
        call_order.append("dispatch")
        dispatch_calls.append(kw)
        return {"board_task": {"id": "btk_test"}, "run_id": "orch_test"}

    monkeypatch.setattr(intake_routes, "dispatch_spec", fake_dispatch)

    intake_routes._run_quick_dispatch(
        "Add SSO to the auth project",
        executor="auto",
        priority="balanced",
        plan_first=False,
        project_id=None,
        board_task_id="btk_test",
        run_id="orch_test",
        store=store,
        collab_store=collab,
        policy_cfg=policy,
        planner_llm=intake_routes.default_planner_llm,
    )

    # Registry hit -> dispatch through the matched project's root_dirs[0]
    # (dispatch_spec resolves the working dir FROM project_id).
    assert dispatch_calls[0]["project_id"] == "proj_auth"
    # build_preflight ran strictly BEFORE dispatch_spec.
    assert call_order == ["preflight", "dispatch"]
    assert preflight_calls[0]["task_id"] == "btk_test"


def test_run_quick_dispatch_enforce_miss_leaves_scratch_creation_to_dispatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(QUICK_PROJECT_ROUTING_ENV, "enforce")
    store, collab, policy = _stores(tmp_path)

    plan = ProjectPlan(project_name="Greenfield", tasks=[PlannedTask(title="t")])
    monkeypatch.setattr(intake_routes, "plan_goal", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        intake_routes,
        "route_project",
        lambda *_a, **_k: RouteDecision(
            decision="new", project_name="Greenfield", reason="no match", confidence=0.9
        ),
    )
    monkeypatch.setattr("omniagentos.intake.preflight.build_preflight", _fake_preflight([]))

    dispatch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake_routes, "dispatch_spec", _fake_dispatch(dispatch_calls))

    intake_routes._run_quick_dispatch(
        "Do something brand new",
        executor="auto",
        priority="balanced",
        plan_first=False,
        project_id=None,
        board_task_id="btk_test",
        run_id="orch_test",
        store=store,
        collab_store=collab,
        policy_cfg=policy,
        planner_llm=intake_routes.default_planner_llm,
    )

    # A genuine miss must NOT be overridden — dispatch_spec's own existing
    # fallback (untouched) is what creates the scratch project.
    assert dispatch_calls[0]["project_id"] is None


def test_run_quick_dispatch_enforce_never_overrides_an_explicit_project_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(QUICK_PROJECT_ROUTING_ENV, "enforce")
    store, collab, policy = _stores(tmp_path)

    plan = ProjectPlan(project_name="Add SSO", tasks=[PlannedTask(title="t")])
    monkeypatch.setattr(intake_routes, "plan_goal", lambda *_a, **_k: plan)
    monkeypatch.setattr(
        intake_routes,
        "route_project",
        lambda *_a, **_k: RouteDecision(
            decision="existing",
            parent_project_id="proj_registry_match",
            root_dirs=["/srv/registry-match"],
            confidence=0.95,
        ),
    )
    monkeypatch.setattr("omniagentos.intake.preflight.build_preflight", _fake_preflight([]))

    dispatch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake_routes, "dispatch_spec", _fake_dispatch(dispatch_calls))

    intake_routes._run_quick_dispatch(
        "Add SSO to the auth project",
        executor="auto",
        priority="balanced",
        plan_first=False,
        project_id="proj_explicit",
        board_task_id="btk_test",
        run_id="orch_test",
        store=store,
        collab_store=collab,
        policy_cfg=policy,
        planner_llm=intake_routes.default_planner_llm,
    )

    assert dispatch_calls[0]["project_id"] == "proj_explicit"


def test_run_quick_dispatch_shadow_computes_and_logs_without_applying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(QUICK_PROJECT_ROUTING_ENV, "shadow")
    store, collab, policy = _stores(tmp_path)

    plan = ProjectPlan(project_name="Add SSO", tasks=[PlannedTask(title="t")])
    monkeypatch.setattr(intake_routes, "plan_goal", lambda *_a, **_k: plan)
    route_calls: list[Any] = []

    def fake_route(*args: Any, **kwargs: Any) -> RouteDecision:
        route_calls.append((args, kwargs))
        return RouteDecision(
            decision="existing",
            parent_project_id="proj_auth",
            root_dirs=["/srv/auth"],
            confidence=0.95,
        )

    monkeypatch.setattr(intake_routes, "route_project", fake_route)

    preflight_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omniagentos.intake.preflight.build_preflight", _fake_preflight(preflight_calls)
    )

    dispatch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake_routes, "dispatch_spec", _fake_dispatch(dispatch_calls))

    with caplog.at_level(logging.INFO, logger=intake_routes.LOG.name):
        intake_routes._run_quick_dispatch(
            "Add SSO to the auth project",
            executor="auto",
            priority="balanced",
            plan_first=False,
            project_id=None,
            board_task_id="btk_test",
            run_id="orch_test",
            store=store,
            collab_store=collab,
            policy_cfg=policy,
            planner_llm=intake_routes.default_planner_llm,
        )

    # Computed...
    assert len(route_calls) == 1
    assert "decision=existing" in caplog.text
    assert "proj_auth" in caplog.text
    # ...and APPLIED TO NOTHING. `build_preflight` is not additive telemetry:
    # it classifies with apply=True (persisted by orgdims/classify.py) and its
    # `preflight_json` is read by swarm/spawn.py to reorder the skill hits in
    # the brief the worker is given — so running it in shadow changed stored
    # state AND delivered prompts. Shadow computes and records the ROUTING
    # DECISION; that is the whole of what it may do.
    assert preflight_calls == [], (
        "shadow must apply nothing: build_preflight persists a classification "
        "and writes preflight_json, which spawn reads into the worker's brief"
    )
    assert dispatch_calls[0]["project_id"] is None


# ---------------------------------------------------------------------------
# quick_dispatch() HTTP endpoint — exactly one clarify turn on low confidence
# (Phase 1 item 2), then proceed on best judgment (Phase 1 items 1 and 3
# still apply on the resulting background dispatch).
# ---------------------------------------------------------------------------


def test_quick_enforce_low_confidence_triggers_one_clarify_turn(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(QUICK_PROJECT_ROUTING_ENV, "enforce")
    monkeypatch.setattr(
        intake_routes,
        "route_project",
        lambda *_a, **_k: RouteDecision(decision="new", project_name="X", confidence=0.2),
    )
    clarify_calls: list[Any] = []

    def fake_clarify_intake(goal: str, history: Any, **_kw: Any) -> ClarifyResult:
        clarify_calls.append((goal, history))
        return ClarifyResult(mode="questions", questions=["Which project is this for?"])

    monkeypatch.setattr(intake_routes, "clarify_intake", fake_clarify_intake)
    plan_calls: list[str] = []
    monkeypatch.setattr(intake_routes, "plan_goal", _fake_plan(plan_calls, None))
    dispatch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake_routes, "dispatch_spec", _fake_dispatch(dispatch_calls))

    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={"goal": "Research and refactor the entire billing system across services"},
        )
    )

    assert response.status_code == 201
    body = response.json()
    assert body["mode"] == "questions"
    assert body["questions"] == ["Which project is this for?"]
    # Exactly one clarify call, on the FIRST (history-empty) turn.
    assert len(clarify_calls) == 1
    assert clarify_calls[0][1] == []
    # Nothing was planned or dispatched — the operator must answer first.
    assert plan_calls == []
    assert dispatch_calls == []


def test_quick_second_turn_after_clarify_proceeds_on_best_judgment(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(QUICK_PROJECT_ROUTING_ENV, "enforce")
    clarify_calls: list[Any] = []
    monkeypatch.setattr(
        intake_routes, "clarify_intake", lambda *a, **k: clarify_calls.append((a, k))
    )

    plan = ProjectPlan(project_name="Billing fix", tasks=[PlannedTask(title="t")])
    plan_goals: list[str] = []
    monkeypatch.setattr(intake_routes, "plan_goal", _fake_plan(plan_goals, plan))
    monkeypatch.setattr(
        intake_routes,
        "route_project",
        lambda *_a, **_k: RouteDecision(decision="new", project_name="Billing fix", confidence=0.9),
    )
    monkeypatch.setattr("omniagentos.intake.preflight.build_preflight", _fake_preflight([]))
    dispatch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake_routes, "dispatch_spec", _fake_dispatch(dispatch_calls))

    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={
                "goal": "Research and refactor the entire billing system across services",
                "history": [{"q": "Which project is this for?", "a": "The billing project"}],
            },
        )
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "queued"
    # The clarify gate must NOT re-trigger a second round.
    assert clarify_calls == []
    # The one answered round's context reaches planning ("best judgment").
    assert plan_goals
    assert "The billing project" in plan_goals[0]
    assert dispatch_calls[0]["project_id"] is None


def test_quick_off_mode_never_consults_registry_via_http(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(QUICK_PROJECT_ROUTING_ENV, raising=False)
    plan = ProjectPlan(project_name="Ship it", tasks=[PlannedTask(title="t")])
    monkeypatch.setattr(intake_routes, "plan_goal", lambda *_a, **_k: plan)
    route_calls: list[Any] = []
    monkeypatch.setattr(intake_routes, "route_project", _fake_route(route_calls))
    preflight_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "omniagentos.intake.preflight.build_preflight", _fake_preflight(preflight_calls)
    )
    dispatch_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(intake_routes, "dispatch_spec", _fake_dispatch(dispatch_calls))

    response = asyncio.run(
        client.post(
            "/api/intake/quick",
            json={"goal": "Research and refactor the entire billing system across services"},
        )
    )

    assert response.status_code == 201
    assert response.json()["lane"] == "planned"
    assert route_calls == []
    assert preflight_calls == []
    assert dispatch_calls[0]["project_id"] is None
