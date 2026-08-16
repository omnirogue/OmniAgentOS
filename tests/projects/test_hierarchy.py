from __future__ import annotations

import pytest

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectError, ProjectStore
from scripts.nest_brands import nest_brands


def _task(store: SqliteStore, project_id: str) -> None:
    now = utc_now_iso()
    store.create_task(
        {
            "id": new_id("tsk"),
            "project_id": project_id,
            "title": "Task",
            "state": "ready",
            "created_at": now,
            "updated_at": now,
        }
    )


def test_set_parent_reparents_project(project_store: ProjectStore) -> None:
    old = project_store.create_project({"name": "Old"})
    new = project_store.create_project({"name": "New"})
    child = project_store.create_subproject(old["id"], {"name": "Child"})
    project_store.set_parent(child["id"], new["id"])
    tree = project_store.project_tree()
    new_node = next((n for n in tree if n["project"]["id"] == new["id"]), None)
    assert new_node is not None
    assert new_node["sub_projects"][0]["project"]["id"] == child["id"]


def test_set_parent_makes_top_level(project_store: ProjectStore) -> None:
    parent = project_store.create_project({"name": "Parent"})
    child = project_store.create_subproject(parent["id"], {"name": "Child"})
    assert project_store.set_parent(child["id"], None)["parent_project_id"] is None


def test_set_parent_nonexistent_project_rejected(project_store: ProjectStore) -> None:
    with pytest.raises(ProjectError):
        project_store.set_parent("proj_missing", None)


def test_set_parent_nonexistent_parent_rejected(project_store: ProjectStore) -> None:
    project = project_store.create_project({"name": "Child"})
    with pytest.raises(ProjectError):
        project_store.set_parent(project["id"], "proj_missing")


def test_set_parent_self_rejected(project_store: ProjectStore) -> None:
    project = project_store.create_project({"name": "Self"})
    with pytest.raises(ProjectError):
        project_store.set_parent(project["id"], project["id"])


def test_set_parent_cycle_rejected(project_store: ProjectStore) -> None:
    root = project_store.create_project({"name": "Root"})
    child = project_store.create_subproject(root["id"], {"name": "Child"})
    with pytest.raises(ProjectError):
        project_store.set_parent(root["id"], child["id"])


def test_set_parent_idempotent(project_store: ProjectStore) -> None:
    parent = project_store.create_project({"name": "Parent"})
    child = project_store.create_subproject(parent["id"], {"name": "Child"})
    assert project_store.set_parent(child["id"], parent["id"])["parent_project_id"] == parent["id"]


def test_project_tree_includes_tasks(project_store: ProjectStore, store: SqliteStore) -> None:
    project = project_store.create_project({"name": "Project"})
    _task(store, project["id"])
    assert len(project_store.project_tree()[0]["tasks"]) == 1


def test_project_tree_builds_hierarchy(project_store: ProjectStore) -> None:
    root = project_store.create_project({"name": "Root"})
    child = project_store.create_subproject(root["id"], {"name": "Child"})
    grandchild = project_store.create_subproject(child["id"], {"name": "Grandchild"})
    tree = project_store.project_tree()
    assert tree[0]["project"]["id"] == root["id"]
    assert tree[0]["sub_projects"][0]["project"]["id"] == child["id"]
    assert tree[0]["sub_projects"][0]["sub_projects"][0]["project"]["id"] == grandchild["id"]


def test_project_tree_excludes_seen_cycles(project_store: ProjectStore, store: SqliteStore) -> None:
    a = project_store.create_project({"name": "A"})
    b = project_store.create_project({"name": "B"})
    store._connection.execute(
        "UPDATE projects SET parent_project_id = ? WHERE id = ?", (b["id"], a["id"])
    )
    store._connection.execute(
        "UPDATE projects SET parent_project_id = ? WHERE id = ?", (a["id"], b["id"])
    )
    store._connection.commit()
    assert project_store.project_tree() == []


def test_nest_brands_creates_brands_project(project_store: ProjectStore) -> None:
    result = nest_brands(project_store, [])
    assert result["reparented_count"] == 0
    brands_proj = project_store.get_project(result["brands_project_id"])
    assert brands_proj is not None
    assert brands_proj["name"] == "Brands"


def test_nest_brands_reparents_given_ids(project_store: ProjectStore) -> None:
    brands = [project_store.create_project({"name": name}) for name in ("A", "B")]
    result = nest_brands(project_store, [p["id"] for p in brands])
    assert result["reparented_count"] == 2
    for b in brands:
        proj = project_store.get_project(b["id"])
        assert proj is not None
        assert proj["parent_project_id"] == result["brands_project_id"]


def test_nest_brands_rejects_nonexistent_brand(project_store: ProjectStore) -> None:
    existing = project_store.create_project({"name": "Existing"})
    with pytest.raises(ProjectError):
        nest_brands(project_store, [existing["id"], "proj_missing"])
    existing_proj = project_store.get_project(existing["id"])
    assert existing_proj is not None
    assert existing_proj["parent_project_id"] is None
    assert not any(p["name"] == "Brands" for p in project_store.list_projects())


def test_nest_brands_idempotent_on_existing_brands(project_store: ProjectStore) -> None:
    brand = project_store.create_project({"name": "Brand"})
    first = nest_brands(project_store, [brand["id"]])
    second = nest_brands(project_store, [brand["id"]])
    assert second == {"brands_project_id": first["brands_project_id"], "reparented_count": 0}
