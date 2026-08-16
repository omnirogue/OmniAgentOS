"""Tier3 UI/API surface suite — real API calls on every UI-called path family.

Every class below exercises paths the dashboard actually fetches (see
tier1/test_ui_api_drift.py for the mechanical enumeration) against the ISOLATED
app: `fastapi.testclient.TestClient` over the real `omniagentos.api.main.app`
with a per-test tmp `SqliteStore` injected through `get_store` (the
tests/api / tests/projects idiom). Route families that bypass `get_store`
(collab board, graph runtime, skills registry, /dashboard/today) get a per-test
`OMNIAGENTOS_DB` pin plus a cache reset of their module-level store singleton.

Auth: the root conftest bypasses the app-level session-token gate for every
test not marked `real_auth`; the project-files gate test opts back in and mints
a tmp token exactly like tests/api's `auth_headers` fixture.

Class names are load-bearing: configs/feature-health.yaml attributes ledger
records per feature via `test_ui_api_paths.py::Test<Feature>Paths`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from omniagentos.api.deps import get_knowledge_store, get_store
from omniagentos.api.main import app
from omniagentos.api.routes.sessions import _authorized as _team_router_authorized
from omniagentos.db.store import SqliteStore
from tests.routines.conftest import apply_routines_meta_migration, draft_routine_payload


def _assert_error_envelope(body: Any) -> None:
    """Every failure must carry the {"error": {code, message, detail}} envelope."""
    assert isinstance(body, dict) and "error" in body, f"no error envelope: {body!r}"
    error = body["error"]
    for key in ("code", "message", "detail"):
        assert key in error, f"error envelope missing {key!r}: {error!r}"
    assert error["code"], f"error.code empty: {error!r}"
    assert error["message"], f"error.message empty: {error!r}"


@pytest.fixture()
def sqlite_store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "fh-tier3.db"))


@pytest.fixture()
def client(sqlite_store: SqliteStore) -> Iterator[TestClient]:
    """TestClient over the real app with the tmp store injected.

    No context-manager entry: the lifespan's seeding/resume arms are pinned off
    session-wide anyway (root conftest) and none of these routes need startup.
    """
    app.dependency_overrides[get_store] = lambda: sqlite_store
    try:
        yield TestClient(app, base_url="http://testserver")
    finally:
        app.dependency_overrides.pop(get_store, None)


@pytest.fixture()
def pinned_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    """Per-test OMNIAGENTOS_DB pin for routes that read default_db_path()."""
    db_path = str(tmp_path / "fh-tier3-default.db")
    monkeypatch.setenv("OMNIAGENTOS_DB", db_path)
    return db_path


class TestGoalsPaths:
    """GET /api/goals — the steward goals list + detail the dashboard reads."""

    def test_goals_list_returns_empty_list(self, client: TestClient) -> None:
        response = client.get("/api/goals")
        assert response.status_code == 200, response.text
        assert response.json() == []

    def test_unknown_goal_is_404_with_envelope(self, client: TestClient) -> None:
        response = client.get("/api/goals/goal-does-not-exist")
        assert response.status_code == 404, response.text
        body = response.json()
        _assert_error_envelope(body)
        assert body["error"]["code"] == "not_found"


class TestRoutinesPaths:
    """GET /api/routines + the enable/disable shapes the routines page drives."""

    @pytest.fixture(autouse=True)
    def _routines_schema(self, sqlite_store: SqliteStore) -> None:
        # Migration F (scope/purpose) still lives in migrations-staging; the
        # routines suite applies it in fixtures — same here.
        apply_routines_meta_migration(sqlite_store)

    @pytest.fixture()
    def draft_routine(self, client: TestClient) -> dict[str, Any]:
        created = client.post("/api/routines", json=draft_routine_payload())
        assert created.status_code == 201, created.text
        return created.json()

    def test_list_routines_shape(self, client: TestClient, draft_routine: dict[str, Any]) -> None:
        response = client.get("/api/routines")
        assert response.status_code == 200, response.text
        rows = response.json()
        assert isinstance(rows, list) and rows
        row = next(r for r in rows if r["id"] == draft_routine["id"])
        for key in ("id", "name", "status", "next_run"):
            assert key in row, f"serialized routine missing {key!r}: {sorted(row)}"
        assert row["status"] == "disabled"

    def test_disable_returns_updated_routine(
        self, client: TestClient, draft_routine: dict[str, Any]
    ) -> None:
        response = client.post(f"/api/routines/{draft_routine['id']}/disable")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "disabled"

    def test_enable_draft_fails_activation_gate_with_envelope(
        self, client: TestClient, draft_routine: dict[str, Any]
    ) -> None:
        # A disabled draft has no proof-of-life harness: D5 must refuse the
        # flip with the error envelope, never silently activate.
        response = client.post(f"/api/routines/{draft_routine['id']}/enable")
        assert 400 <= response.status_code < 500, response.text
        _assert_error_envelope(response.json())

    def test_enable_unknown_routine_is_404_with_envelope(self, client: TestClient) -> None:
        response = client.post("/api/routines/rtn_does_not_exist/enable")
        assert response.status_code == 404, response.text
        body = response.json()
        _assert_error_envelope(body)
        assert body["error"]["code"] == "not_found"


class TestBoardPaths:
    """GET /api/board — the kanban list the dashboard reads, and its single-card read.

    (``GET /api/collab/board`` used to be tested here; it was a duplicate,
    unauthenticated listing of the same table and has been removed. The
    dashboard has always read ``/api/board``.)"""


    @pytest.fixture()
    def board_store(self, pinned_db: str) -> Iterator[Any]:
        from omniagentos.api.routes.collab import get_collab_store
        from omniagentos.collab.store import CollabStore

        get_collab_store.cache_clear()
        try:
            yield CollabStore(db_path=pinned_db)
        finally:
            get_collab_store.cache_clear()

    @pytest.fixture()
    def seeded_card(self, board_store: Any) -> dict[str, Any]:
        from omniagentos.collab.contracts import BoardTask

        task = BoardTask(title="fh tier3 board card", description="seeded by feature-health")
        board_store.create_board_task(task)
        row = board_store.get_board_task(task.id)
        assert row is not None
        return row

    def test_board_list_projection_fields(
        self, client: TestClient, seeded_card: dict[str, Any]
    ) -> None:
        response = client.get("/api/board")
        assert response.status_code == 200, response.text
        cards = response.json()
        card = next(c for c in cards if c["id"] == seeded_card["id"])
        # The projection contract the kanban reads: swarm_json is flattened to
        # exactly the two consumed fields, never shipped whole (payload guard).
        for key in ("id", "title", "status", "claim_version", "swarm_phase", "swarm_integration"):
            assert key in card, f"board projection missing {key!r}: {sorted(card)}"
        assert "swarm_json" not in card, "board list leaked the raw swarm_json envelope"
        assert "project_id" in card, "board list must carry project_id for the client filter"

    def test_board_status_filter_narrows_the_list(
        self, client: TestClient, seeded_card: dict[str, Any]
    ) -> None:
        response = client.get("/api/board", params={"status": "open"})
        assert response.status_code == 200, response.text
        cards = {c["id"]: c for c in response.json()}
        assert seeded_card["id"] in cards
        card = cards[seeded_card["id"]]
        assert card["title"] == "fh tier3 board card"
        assert card["status"] == "open"
        assert card["claim_version"] == 0
        assert client.get("/api/board", params={"status": "done"}).json() == []

    def test_board_single_card_route_reads_one_card(
        self, client: TestClient, seeded_card: dict[str, Any]
    ) -> None:
        """The drawer's read: one card, without downloading the whole board."""
        response = client.get(f"/api/board/{seeded_card['id']}")
        assert response.status_code == 200, response.text
        card = response.json()
        assert card["id"] == seeded_card["id"]
        assert card["title"] == "fh tier3 board card"
        assert client.get("/api/board/btk_does_not_exist").status_code == 404


class TestGraphPaths:
    """Graph run list/status routes the graph page reads."""

    @pytest.fixture(autouse=True)
    def _graph_service(self, pinned_db: str, monkeypatch: pytest.MonkeyPatch) -> None:
        from omniagentos.api.routes import graph as graph_routes

        # Reset the module singleton so the service binds the per-test DB pin;
        # monkeypatch restores the previous singleton afterwards.
        monkeypatch.setattr(graph_routes, "_SERVICE", None)

    def test_graph_health(self, client: TestClient) -> None:
        response = client.get("/api/graph/health")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["templates"], "graph health must list builtin templates"

    def test_runs_list_shape(self, client: TestClient) -> None:
        response = client.get("/api/graph/runs")
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body["runs"], list)

    def test_start_diamond_then_read_run_status(self, client: TestClient) -> None:
        started = client.post("/api/graph/runs/diamond", json={"title": "fh tier3 graph run"})
        assert started.status_code == 200, started.text
        run = started.json()["run"]
        run_id = run["id"] if isinstance(run, dict) else run
        detail = client.get(f"/api/graph/runs/{run_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["run"]["id"] == run_id
        listing = client.get("/api/graph/runs")
        assert listing.status_code == 200
        assert any(r.get("id") == run_id for r in listing.json()["runs"])

    def test_unknown_run_is_404_with_envelope(self, client: TestClient) -> None:
        response = client.get("/api/graph/runs/grn_does_not_exist")
        assert response.status_code == 404, response.text
        _assert_error_envelope(response.json())


class _FakeKnowledgeStore:
    """Minimal fake following tests/knowledge/test_api.py — the real store is
    Postgres+Ollama-backed and unavailable in the isolated tier3 lane."""

    def __init__(self) -> None:
        now = datetime.now(UTC)
        from omniagentos.knowledge.contracts import Fact, FactStatus, Provenance

        self.fact = Fact(
            id=1,
            statement="feature-health knowledge probe fact",
            episode_id=1,
            provenance=Provenance.EXTRACTED,
            trust=0.7,
            confidence=0.8,
            status=FactStatus.ACTIVE,
            valid_at=now,
            recorded_at=now,
            importance=0.6,
            access_count=0,
            last_accessed=now,
            helped_count=0,
        )

    def graph_snapshot(self, *, limit_nodes: int) -> dict[str, Any]:
        return {
            "facts": [
                {
                    "id": 1,
                    "statement": self.fact.statement,
                    "status": "active",
                    "importance": 0.6,
                    "trust": 0.7,
                    "discipline": None,
                }
            ],
            "entities": [],
            "edges": [],
        }

    def recall_candidates(self, **_kwargs: Any) -> list[dict[str, Any]]:
        return [self.fact.model_dump()]


class TestKnowledgePaths:
    """Knowledge graph/search routes the knowledge page reads."""

    @pytest.fixture()
    def knowledge_client(self, client: TestClient) -> Iterator[TestClient]:
        app.dependency_overrides[get_knowledge_store] = _FakeKnowledgeStore
        try:
            yield client
        finally:
            app.dependency_overrides.pop(get_knowledge_store, None)

    def test_knowledge_graph_shape(self, knowledge_client: TestClient) -> None:
        response = knowledge_client.get("/api/knowledge/graph", params={"limit": 500})
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"nodes", "edges"}
        assert body["nodes"] and body["nodes"][0]["kind"] == "fact"

    def test_knowledge_search_shape(self, knowledge_client: TestClient) -> None:
        response = knowledge_client.get("/api/knowledge/search", params={"q": "probe", "k": 5})
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert results, "search must surface the seeded active fact"
        first = results[0]
        assert first["fact"]["status"] == "active"
        assert 0.0 <= first["score"] <= 1.0


class TestSkillsPaths:
    """GET /api/skills — the flat registry the skills page and spawner read."""

    def test_flat_registry_non_empty_shape(self, client: TestClient, pinned_db: str) -> None:
        created = client.post(
            "/api/skills",
            json={
                "slug": "fh-tier3-skill",
                "category": "Testing",
                "subcategory": "FeatureHealth",
                "title": "Feature-health tier3 probe skill",
                "summary": "seeded by tests/feature_health/tier3",
            },
        )
        assert created.status_code == 200, created.text
        skill_id = created.json()
        assert isinstance(skill_id, str) and skill_id

        response = client.get("/api/skills")
        assert response.status_code == 200, response.text
        rows = response.json()
        assert isinstance(rows, list) and rows, "flat registry must be non-empty after seeding"
        row = next(r for r in rows if r.get("slug") == "fh-tier3-skill")
        for key in ("id", "slug", "title", "status"):
            assert key in row, f"skill row missing {key!r}: {sorted(row)}"


class TestProjectsPaths:
    """Projects list/detail plus the token gate on project-file reads."""

    @pytest.fixture()
    def project(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> dict[str, Any]:
        workspace = tmp_path / "fh-project"
        workspace.mkdir()
        monkeypatch.setenv("OMNIAGENTOS_PROJECT_BASES", str(tmp_path))
        created = client.post(
            "/api/projects",
            json={"name": "FH Tier3 Project", "root_dirs": [str(workspace)]},
        )
        assert created.status_code == 201, created.text
        return created.json()

    def test_projects_list_and_detail(
        self, client: TestClient, project: dict[str, Any]
    ) -> None:
        listing = client.get("/api/projects")
        assert listing.status_code == 200, listing.text
        rows = listing.json()
        assert any(p["id"] == project["id"] for p in rows)
        detail = client.get(f"/api/projects/{project['id']}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["name"] == "FH Tier3 Project"
        assert "resolved_policy" in body

    @pytest.mark.real_auth
    def test_project_files_requires_session_token(self, client: TestClient) -> None:
        # Project-file GETs expose filesystem capability and are token-gated
        # app-wide; the gate runs before routing so a fabricated id suffices.
        response = client.get("/api/projects/proj_any/files")
        assert response.status_code == 401, response.text
        body = response.json()
        _assert_error_envelope(body)
        assert body["error"]["code"] == "unauthorized"

    @pytest.mark.real_auth
    def test_project_files_with_token_lists_files(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from omniagentos.sessions import token

        monkeypatch.setattr(token, "TOKEN_PATH", tmp_path / "sessions-token")
        headers = {"X-Session-Token": token.load_or_create_token()}

        workspace = tmp_path / "fh-project-auth"
        workspace.mkdir()
        (workspace / "note.md").write_text("feature-health file probe\n")
        monkeypatch.setenv("OMNIAGENTOS_PROJECT_BASES", str(tmp_path))
        created = client.post(
            "/api/projects",
            json={"name": "FH Auth Project", "root_dirs": [str(workspace)]},
            headers=headers,
        )
        assert created.status_code == 201, created.text
        project_id = created.json()["id"]

        response = client.get(f"/api/projects/{project_id}/files", headers=headers)
        assert response.status_code == 200, response.text
        assert isinstance(response.json()["files"], list)


class TestTodayPath:
    """GET /api/dashboard/today — the operator daily summary contract."""

    def test_today_counts_and_unrounded_completion_rate(
        self, client: TestClient, pinned_db: str
    ) -> None:
        # /today reads default_db_path() directly (no store DI) — migrate the
        # pinned DB and seed a 1-of-3 completion so the rate is non-integral.
        store = SqliteStore(pinned_db)
        now = datetime.now(UTC).isoformat()
        rows = [
            ("swa_fh_1", "swr_fh", "btk_fh_1", 1, "grok", "grok-4", now, now, "completed"),
            ("swa_fh_2", "swr_fh", "btk_fh_2", 1, "grok", "grok-4", now, now, "crashed"),
            ("swa_fh_3", "swr_fh", "btk_fh_3", 1, "grok", "grok-4", now, now, "timeout"),
        ]
        with store._lock:
            store._connection.executemany(
                "INSERT INTO swarm_attempts (id, swarm_run_id, board_task_id, seq, "
                "provider, model, started_at, ended_at, end_reason, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '')",
                rows,
            )
            store._connection.commit()

        response = client.get("/api/dashboard/today")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["started_today"] == 3
        assert body["completed_today"] == 1
        providers = {p["provider"]: p for p in body["completion_by_provider"]}
        grok = providers["grok"]
        assert grok["started"] == 3
        assert grok["completed"] == 1
        # The /today contract ships the rate UNROUNDED: 1/3 must arrive as
        # 33.333..., not 33 or 33.3.
        assert grok["completion_rate"] == pytest.approx(100.0 / 3.0, abs=1e-9), (
            f"completion_rate was rounded: {grok['completion_rate']!r}"
        )
        assert isinstance(body["escalations"], list)

    def test_sessions_awaiting_approval_counts_live_sessions_not_backlog(
        self, client: TestClient, pinned_db: str
    ) -> None:
        """``sessions_awaiting_approval`` must count sessions actually parked in
        ``state='awaiting_approval'`` -- NOT the unread-escalation notification
        backlog (which is LIMIT 20 and can outlive the session it refers to).
        Seed more unread escalation notifications than live awaiting sessions
        so the two numbers can never be conflated by accident."""
        store = SqliteStore(pinned_db)
        now = datetime.now(UTC).isoformat()
        session_rows = [
            ("ses_fh_1", "bridge", "/tmp/fh-1", "claude", "awaiting_approval"),
            ("ses_fh_2", "bridge", "/tmp/fh-2", "claude", "awaiting_approval"),
            ("ses_fh_3", "bridge", "/tmp/fh-3", "claude", "running"),
            ("ses_fh_4", "bridge", "/tmp/fh-4", "claude", "completed"),
        ]
        with store._lock:
            store._connection.executemany(
                "INSERT INTO sessions (id, source, project_dir, provider, state, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(*row, now, now) for row in session_rows],
            )
            # A notification backlog bigger than the live awaiting count --
            # if the endpoint ever regresses to counting this array's length
            # (or the raw row count) instead of the sessions table, this
            # assertion catches it immediately.
            store._connection.executemany(
                "INSERT INTO notifications (id, kind, title, body, created_at) "
                "VALUES (?, 'escalation', ?, '', ?)",
                [
                    (f"ntf_fh_{index}", f"stale escalation {index}", now)
                    for index in range(5)
                ],
            )
            store._connection.commit()

        response = client.get("/api/dashboard/today")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["sessions_awaiting_approval"] == 2
        assert len(body["escalations"]) == 5
        assert body["sessions_awaiting_approval"] != len(body["escalations"])


class TestReflectionPaths:
    """GET /api/reflection/proposals + the approve 4xx contract."""

    def _seed_proposal(self, store: SqliteStore, proposal_id: str) -> None:
        now = datetime.now(UTC).isoformat()
        with store._lock:
            store._connection.execute(
                "INSERT INTO reflection_proposals (id, run_id, kind, target, current, "
                "proposed, rationale, evidence_refs_json, predicted_impact, risk_class, "
                "status, created_at, updated_at) "
                "VALUES (?, NULL, 'lesson', ?, 'old', 'new', 'feature-health probe', "
                "'[]', 'low', 'low', 'pending', ?, ?)",
                (proposal_id, json.dumps({"doc": "lessons"}), now, now),
            )
            store._connection.commit()

    def test_proposals_list_shape(self, client: TestClient, sqlite_store: SqliteStore) -> None:
        self._seed_proposal(sqlite_store, "rp_fh_tier3")
        response = client.get("/api/reflection/proposals")
        assert response.status_code == 200, response.text
        rows = response.json()
        row = next(r for r in rows if r["id"] == "rp_fh_tier3")
        for key in ("id", "kind", "status", "risk_class", "rationale", "created_at"):
            assert key in row, f"proposal missing {key!r}: {sorted(row)}"
        assert row["kind"] == "lesson"
        assert row["status"] == "pending"

        filtered = client.get("/api/reflection/proposals", params={"status": "pending"})
        assert filtered.status_code == 200
        assert any(r["id"] == "rp_fh_tier3" for r in filtered.json())

    def test_approve_unknown_proposal_is_404_with_envelope(self, client: TestClient) -> None:
        # apply_proposal mutates real config targets, so the happy path cannot
        # run isolated; the 4xx contract on a fabricated id is the UI-visible
        # failure shape the reflection page must handle.
        response = client.post("/api/reflection/rp_does_not_exist/approve", json={})
        assert response.status_code == 404, response.text
        body = response.json()
        _assert_error_envelope(body)
        assert body["error"]["code"] == "not_found"


class TestTeamPaths:
    """GET /api/team/* — the Team Work OS surface the /team dashboard reads.

    Every route here goes through ``StoreDep`` (``get_store``), same idiom as
    the rest of this file — no ``pinned_db`` singleton reset needed. Auth is
    the real surprise: ``router = APIRouter(..., dependencies=
    [Depends(_authorized)])`` in team.py wires ``sessions._authorized`` /
    ``sessions.token.verify_token`` directly at the ROUTER level -- a
    different dependency object than ``omniagentos.api.main.
    require_session_token``, the only one the root conftest's
    ``_bypass_global_auth`` overrides, so ``/api/team/*`` stays 401 under the
    plain isolated ``client`` fixture. It compounds with a second app-level
    gate: ``/api/team`` is also in ``_GATED_READ_NAMESPACES``
    (``require_session_token``'s ``cross_principal_read`` class), which
    refuses even a real minted token presented as the raw machine principal
    with 403 ``system_principal_forbidden`` -- so ``real_auth`` + a token
    (the ``TestProjectsPaths`` idiom) does not clear this router either.
    Overriding ``sessions._authorized`` directly, like
    ``test_agent_exec_paths.py`` does for the swarm router's own
    ``control._authorized``, is the one bypass that reaches both: it keeps
    the file's default (non-``real_auth``) auth-bypassed ``client`` fixture,
    which already handles ``require_session_token``.

    The scoreboard/overall envelope also carries fleet-ledger fields
    (``fleet_merged`` etc.); those read a REAL ledger file resolved relative to
    the installed package, so only their presence is asserted here, never a
    value — the number is live production data, not this test's business.
    """

    @pytest.fixture(autouse=True)
    def _team_router_auth_bypass(self) -> Iterator[None]:
        app.dependency_overrides[_team_router_authorized] = lambda: None
        try:
            yield
        finally:
            app.dependency_overrides.pop(_team_router_authorized, None)

    def test_board_empty_store_returns_pool_only(self, client: TestClient) -> None:
        response = client.get("/api/team/board")
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"pool"}, f"unexpected top-level keys: {sorted(body)}"
        pool = body["pool"]
        for key in ("cards", "depth", "low", "truncated"):
            assert key in pool, f"pool payload missing {key!r}: {sorted(pool)}"
        assert pool["cards"] == []
        assert pool["depth"] == 0

    def test_board_unknown_owner_is_404_with_envelope(self, client: TestClient) -> None:
        response = client.get("/api/team/board", params={"owner": "emp_does_not_exist"})
        assert response.status_code == 404, response.text
        body = response.json()
        _assert_error_envelope(body)
        assert body["error"]["code"] == "not_found"

    def test_tree_empty_store_returns_empty_companies(self, client: TestClient) -> None:
        response = client.get("/api/team/tree")
        assert response.status_code == 200, response.text
        assert response.json() == {"companies": []}

    def test_scoreboard_empty_store_shape(self, client: TestClient) -> None:
        response = client.get("/api/team/scoreboard")
        assert response.status_code == 200, response.text
        body = response.json()
        for key in ("period", "score_version", "people", "team", "overall"):
            assert key in body, f"scoreboard missing {key!r}: {sorted(body)}"
        assert body["people"] == [], "empty-roster store must yield no people rows"
        assert body["team"]["score"] == 0
        assert body["team"]["baseline_points"] == 0
        for key in (
            "humans_x",
            "fleet_merged",
            "fleet_baseline_merged",
            "fleet_x",
            "production_x",
            "pct_to_10x",
        ):
            assert key in body["overall"], f"overall missing {key!r}: {sorted(body['overall'])}"

    def test_diagnostics_empty_store_shape(self, client: TestClient) -> None:
        response = client.get("/api/team/diagnostics")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["window"] == "7d"
        assert "period" in body and "start" in body["period"] and "end" in body["period"]
        assert body["people"] == {}
