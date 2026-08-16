from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.sessions import token


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_create_list_and_get_project(
    asgi_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Directory grants must exist under a user-approved allow-root. A real temp
    # directory also verifies that creation persists its canonical path.
    workspace = tmp_path / "acme"
    workspace.mkdir()
    monkeypatch.setenv("OMNIAGENTOS_PROJECT_BASES", str(tmp_path))
    created = _run(
        asgi_client.post(
            "/api/projects",
            json={
                "name": "Acme",
                "root_dirs": [str(workspace)],
                "vault_subfolder": "projects/acme",
                "budget_usd": 100.0,
                "allowed_tools": ["shell"],
                # A *tightening* grant is honored; relaxation is rejected (see below).
                "grants": [{"action_class": "read_only", "requires_approval": True}],
            },
        )
    )
    assert created.status_code == 201
    body = created.json()
    assert body["id"].startswith("proj_")
    assert body["root_dirs"] == [str(workspace.resolve())]
    # resolved_policy overlays grants monotonically: read_only is tightened...
    assert body["resolved_policy"]["read_only"]["requires_approval"] is True
    # ...and the hard-human floor is preserved.
    assert body["resolved_policy"]["consequential"]["always_human"] is True

    listing = _run(asgi_client.get("/api/projects"))
    assert listing.status_code == 200
    rows = listing.json()
    assert [p["id"] for p in rows] == [body["id"]]
    # F5: the list response must carry grants + resolved_policy or the dashboard
    # crashes evaluating p.grants.length.
    assert rows[0]["grants"][0]["action_class"] == "read_only"
    assert rows[0]["resolved_policy"]["consequential"]["always_human"] is True

    detail = _run(asgi_client.get(f"/api/projects/{body['id']}"))
    assert detail.status_code == 200
    assert detail.json()["name"] == "Acme"


def test_relaxing_grant_is_rejected_with_422(asgi_client: httpx.AsyncClient) -> None:
    # F1: a project may not downgrade a globally approval-gated action.
    resp = _run(
        asgi_client.post(
            "/api/projects",
            json={
                "name": "Relaxer",
                "grants": [{"action_class": "external_reversible", "requires_approval": False}],
            },
        )
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation"
    # The rejected project must not have been persisted (F9 atomicity).
    listing = _run(asgi_client.get("/api/projects"))
    assert listing.json() == []


def test_invalid_config_is_rejected(asgi_client: httpx.AsyncClient) -> None:
    # F7: traversal paths, negative budgets, unknown tools, unknown fields.
    cases = [
        {"name": "T1", "root_dirs": ["../../etc"]},
        {"name": "T2", "vault_subfolder": "../../outside"},
        {"name": "T3", "budget_usd": -1},
        {"name": "T4", "allowed_tools": ["not-real"]},
        {"name": "T5", "surprise": "field"},
        {"name": "T6", "root_dirs": ["relative/path"]},
    ]
    for payload in cases:
        resp = _run(asgi_client.post("/api/projects", json=payload))
        assert resp.status_code == 422, payload
    # None of the rejected payloads left a project behind.
    assert _run(asgi_client.get("/api/projects")).json() == []


def test_invalid_grant_is_422_not_409(asgi_client: httpx.AsyncClient) -> None:
    # F9: an unknown action class in a grant is a validation error, distinct from
    # the name-conflict 409 path -- and it must not persist the project.
    resp = _run(
        asgi_client.post(
            "/api/projects",
            json={"name": "BadGrant", "grants": [{"action_class": "made_up"}]},
        )
    )
    assert resp.status_code == 422
    assert _run(asgi_client.get("/api/projects")).json() == []


def test_duplicate_project_name_conflicts(asgi_client: httpx.AsyncClient) -> None:
    first = _run(asgi_client.post("/api/projects", json={"name": "Dup"}))
    assert first.status_code == 201
    second = _run(asgi_client.post("/api/projects", json={"name": "Dup"}))
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


def test_missing_project_is_404(asgi_client: httpx.AsyncClient) -> None:
    resp = _run(asgi_client.get("/api/projects/proj_missing"))
    assert resp.status_code == 404


def test_create_project_with_parent_via_api(asgi_client: httpx.AsyncClient) -> None:
    parent = _run(asgi_client.post("/api/projects", json={"name": "Parent"})).json()
    child = _run(
        asgi_client.post(
            "/api/projects",
            json={"name": "Child", "parent_project_id": parent["id"]},
        )
    )
    assert child.status_code == 201
    assert child.json()["parent_project_id"] == parent["id"]


def test_reparent_project_via_api(asgi_client: httpx.AsyncClient) -> None:
    first = _run(asgi_client.post("/api/projects", json={"name": "First"})).json()
    second = _run(asgi_client.post("/api/projects", json={"name": "Second"})).json()
    child = _run(asgi_client.post("/api/projects", json={"name": "Child"})).json()
    response = _run(
        asgi_client.patch(
            f"/api/projects/{child['id']}",
            json={"parent_project_id": first["id"]},
        )
    )
    assert response.status_code == 200
    assert response.json()["parent_project_id"] == first["id"]
    assert response.json()["resolved_policy"]
    assert second["id"] != first["id"]


def test_reparent_to_null_makes_top_level(asgi_client: httpx.AsyncClient) -> None:
    parent = _run(asgi_client.post("/api/projects", json={"name": "Parent"})).json()
    child = _run(
        asgi_client.post("/api/projects", json={"name": "Child", "parent_project_id": parent["id"]})
    ).json()
    response = _run(
        asgi_client.patch(f"/api/projects/{child['id']}", json={"parent_project_id": None})
    )
    assert response.status_code == 200
    assert response.json()["parent_project_id"] is None


def test_reparent_nonexistent_project_returns_404(asgi_client: httpx.AsyncClient) -> None:
    response = _run(
        asgi_client.patch("/api/projects/proj_missing", json={"parent_project_id": None})
    )
    assert response.status_code == 404


def test_reparent_nonexistent_parent_returns_409(asgi_client: httpx.AsyncClient) -> None:
    project = _run(asgi_client.post("/api/projects", json={"name": "Child"})).json()
    response = _run(
        asgi_client.patch(
            f"/api/projects/{project['id']}",
            json={"parent_project_id": "proj_missing"},
        )
    )
    assert response.status_code == 409


def test_reparent_cycle_returns_409(asgi_client: httpx.AsyncClient) -> None:
    root = _run(asgi_client.post("/api/projects", json={"name": "Root"})).json()
    child = _run(
        asgi_client.post("/api/projects", json={"name": "Child", "parent_project_id": root["id"]})
    ).json()
    response = _run(
        asgi_client.patch(f"/api/projects/{root['id']}", json={"parent_project_id": child["id"]})
    )
    assert response.status_code == 409


def test_jira_project_key_unknown_returns_400_with_allowed_set(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    """DEFECT 4: live-key invariant from jira_fields.yaml; unknown → 400 + named set."""
    project = _run(asgi_client.post("/api/projects", json={"name": "Map Me"})).json()
    response = _run(
        asgi_client.patch(
            f"/api/projects/{project['id']}",
            json={"jira_project_key": "NOT-A-LIVE-PROJECT"},
        )
    )
    assert response.status_code == 400
    body = response.json()
    msg = json.dumps(body)
    for key in ("ACM", "CA", "INI", "HOO", "OAOS"):
        assert key in msg
    # Nothing persisted under the unknown key.
    row = store._connection.execute(  # noqa: SLF001
        "SELECT jira_project_key FROM projects WHERE id = ?", (project["id"],)
    ).fetchone()
    assert row is not None
    assert row["jira_project_key"] is None


def test_combined_reparent_and_jira_key_409_leaves_parent_unchanged(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    """DEFECT 3: atomic PATCH — duplicate jira key 409 must not commit parent change."""
    parent_a = _run(asgi_client.post("/api/projects", json={"name": "Parent A"})).json()
    parent_b = _run(asgi_client.post("/api/projects", json={"name": "Parent B"})).json()
    holder = _run(asgi_client.post("/api/projects", json={"name": "Key Holder"})).json()
    child = _run(
        asgi_client.post(
            "/api/projects",
            json={"name": "Child Map", "parent_project_id": parent_a["id"]},
        )
    ).json()
    # First mapping succeeds.
    ok = _run(
        asgi_client.patch(
            f"/api/projects/{holder['id']}",
            json={"jira_project_key": "ACM"},
        )
    )
    assert ok.status_code == 200
    # Combined reparent + conflicting key must 409 and leave parent_a in place.
    conflict = _run(
        asgi_client.patch(
            f"/api/projects/{child['id']}",
            json={"parent_project_id": parent_b["id"], "jira_project_key": "ACM"},
        )
    )
    assert conflict.status_code == 409
    row = store._connection.execute(  # noqa: SLF001
        "SELECT parent_project_id, jira_project_key FROM projects WHERE id = ?",
        (child["id"],),
    ).fetchone()
    assert row is not None
    assert row["parent_project_id"] == parent_a["id"]  # unchanged
    assert row["jira_project_key"] is None


def test_jira_project_key_live_key_maps_ok(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    project = _run(asgi_client.post("/api/projects", json={"name": "HOO Map"})).json()
    response = _run(
        asgi_client.patch(
            f"/api/projects/{project['id']}",
            json={"jira_project_key": "hoo"},
        )
    )
    assert response.status_code == 200
    assert response.json().get("jira_project_key") == "HOO"


def test_get_project_tree_returns_hierarchy(asgi_client: httpx.AsyncClient) -> None:
    parent = _run(asgi_client.post("/api/projects", json={"name": "Parent"})).json()
    child = _run(
        asgi_client.post("/api/projects", json={"name": "Child", "parent_project_id": parent["id"]})
    ).json()
    response = _run(asgi_client.get("/api/projects/tree"))
    assert response.status_code == 200
    assert response.json()[0]["project"]["id"] == parent["id"]
    assert response.json()[0]["sub_projects"][0]["project"]["id"] == child["id"]


def test_tasks_runs_approvals_filtered_by_project(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    now = utc_now_iso()
    # Real projects (task.project_id carries a FK), one task each; runs +
    # approvals inherit the project transitively via task_id.
    ids = {}
    for suffix in ("a", "b"):
        resp = _run(asgi_client.post("/api/projects", json={"name": f"Proj {suffix}"}))
        ids[suffix] = resp.json()["id"]
    for suffix, project in (("a", ids["a"]), ("b", ids["b"])):
        store.create_task(
            {
                "id": f"tsk_{suffix}",
                "title": f"Task {suffix}",
                "state": "ready",
                "project_id": project,
                "created_at": now,
                "updated_at": now,
            }
        )
        store.enqueue_run(
            {
                "id": f"run_{suffix}",
                "task_id": f"tsk_{suffix}",
                "harness": "mock",
                "trace_id": f"tr_{suffix}",
                "queued_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
        store.create_approval(
            {
                "id": f"apr_{suffix}",
                "task_id": f"tsk_{suffix}",
                "action_class": "read_only",
                "proposed_action": "look",
                "created_at": now,
            }
        )

    tasks = _run(asgi_client.get("/api/tasks", params={"project_id": ids["a"]}))
    assert [t["id"] for t in tasks.json()] == ["tsk_a"]

    runs = _run(asgi_client.get("/api/runs", params={"project_id": ids["a"]}))
    assert [r["id"] for r in runs.json()] == ["run_a"]

    approvals = _run(
        asgi_client.get("/api/approvals", params={"state": "pending", "project_id": ids["b"]})
    )
    assert [a["id"] for a in approvals.json()] == ["apr_b"]

    # No filter still returns everything.
    all_runs = _run(asgi_client.get("/api/runs"))
    assert {r["id"] for r in all_runs.json()} == {"run_a", "run_b"}


def _seed_two_projects(asgi_client: httpx.AsyncClient, store: SqliteStore) -> dict[str, str]:
    now = utc_now_iso()
    ids = {}
    for suffix in ("a", "b"):
        resp = _run(asgi_client.post("/api/projects", json={"name": f"Proj {suffix}"}))
        ids[suffix] = resp.json()["id"]
        store.create_task(
            {
                "id": f"tsk_{suffix}",
                "title": f"Task {suffix}",
                "state": "ready",
                "project_id": ids[suffix],
                "created_at": now,
                "updated_at": now,
            }
        )
        store.enqueue_run(
            {
                "id": f"run_{suffix}",
                "task_id": f"tsk_{suffix}",
                "harness": "mock",
                "trace_id": f"tr_{suffix}",
                "queued_at": now,
                "created_at": now,
                "updated_at": now,
            }
        )
        store.create_approval(
            {
                "id": f"apr_{suffix}",
                "task_id": f"tsk_{suffix}",
                "run_id": f"run_{suffix}",
                "action_class": "read_only",
                "proposed_action": "look",
                "created_at": now,
            }
        )
    return ids


def test_cross_project_reads_and_mutations_are_forbidden(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    # F3: supplying Project A must not expose or mutate Project B's resources.
    ids = _seed_two_projects(asgi_client, store)

    # Reads: B's task/run under A's scope -> 403; own scope -> 200.
    assert (
        _run(asgi_client.get("/api/tasks/tsk_b", params={"project_id": ids["a"]})).status_code
        == 403
    )
    assert (
        _run(asgi_client.get("/api/tasks/tsk_b", params={"project_id": ids["b"]})).status_code
        == 200
    )
    assert (
        _run(asgi_client.get("/api/runs/run_b", params={"project_id": ids["a"]})).status_code == 403
    )
    assert (
        _run(asgi_client.get("/api/runs/run_b", params={"project_id": ids["b"]})).status_code == 200
    )

    # Cancel: cross-project must be blocked before any state change. The cancel
    # route is now session-token gated (fix6), so a valid local token is supplied
    # to exercise the project-scope check rather than 401 auth.
    assert (
        _run(
            asgi_client.post(
                "/api/runs/run_b/cancel",
                params={"project_id": ids["a"]},
                headers={"X-Session-Token": token.load_or_create_token()},
            )
        ).status_code
        == 403
    )

    # Approval decision: cross-project must be blocked. The decision route is
    # session-token gated (SEC-005, W1 session-bridge merge), so a valid local
    # token is supplied to exercise the project-scope check rather than 401 auth.
    decided = _run(
        asgi_client.post(
            "/api/approvals/apr_b/decision",
            params={"project_id": ids["a"]},
            headers={"X-Session-Token": token.load_or_create_token()},
            json={"decision": "approved"},
        )
    )
    assert decided.status_code == 403
    # The approval remains pending (nothing decided cross-project).
    pending = _run(
        asgi_client.get("/api/approvals", params={"state": "pending", "project_id": ids["b"]})
    )
    assert [a["id"] for a in pending.json()] == ["apr_b"]


def test_project_filter_survives_global_limit(
    asgi_client: httpx.AsyncClient, store: SqliteStore
) -> None:
    # F6: one OLD Project A run + many NEWER Project B runs. Filtering must be in
    # SQL before LIMIT, or A's run falls outside the global window and vanishes.
    project_a = _run(asgi_client.post("/api/projects", json={"name": "Old A"})).json()["id"]
    project_b = _run(asgi_client.post("/api/projects", json={"name": "New B"})).json()["id"]
    store.create_task(
        {
            "id": "tsk_old",
            "title": "old",
            "state": "ready",
            "project_id": project_a,
            "created_at": "2000-01-01T00:00:00Z",
            "updated_at": "2000-01-01T00:00:00Z",
        }
    )
    store.enqueue_run(
        {
            "id": "run_old",
            "task_id": "tsk_old",
            "harness": "mock",
            "trace_id": "tr_old",
            "queued_at": "2000-01-01T00:00:00Z",
            "created_at": "2000-01-01T00:00:00Z",
            "updated_at": "2000-01-01T00:00:00Z",
        }
    )
    store.create_approval(
        {
            "id": "apr_old",
            "task_id": "tsk_old",
            "action_class": "read_only",
            "proposed_action": "look",
            "created_at": "2000-01-01T00:00:00Z",
        }
    )
    store.create_task(
        {
            "id": "tsk_new",
            "title": "new",
            "state": "ready",
            "project_id": project_b,
            "created_at": "2030-01-01T00:00:00Z",
            "updated_at": "2030-01-01T00:00:00Z",
        }
    )
    for index in range(150):
        stamp = f"2030-01-01T00:{index // 60:02d}:{index % 60:02d}Z"
        store.enqueue_run(
            {
                "id": f"run_new_{index}",
                "task_id": "tsk_new",
                "harness": "mock",
                "trace_id": f"tr_new_{index}",
                "queued_at": stamp,
                "created_at": stamp,
                "updated_at": stamp,
            }
        )
        store.create_approval(
            {
                "id": f"apr_new_{index}",
                "task_id": "tsk_new",
                "action_class": "read_only",
                "proposed_action": "look",
                "created_at": stamp,
            }
        )

    runs = _run(asgi_client.get("/api/runs", params={"project_id": project_a, "limit": 100}))
    assert [r["id"] for r in runs.json()] == ["run_old"]
    approvals = _run(
        asgi_client.get(
            "/api/approvals", params={"project_id": project_a, "state": "pending", "limit": 100}
        )
    )
    assert [a["id"] for a in approvals.json()] == ["apr_old"]


def test_task_with_unknown_project_is_404_not_500(asgi_client: httpx.AsyncClient) -> None:
    # F8: an unknown project_id trips the FK constraint; translate to a stable 404.
    resp = _run(
        asgi_client.post(
            "/api/tasks",
            headers={"X-Session-Token": token.load_or_create_token()},
            json={"title": "orphan", "project_id": "proj_missing"},
        )
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
