from __future__ import annotations

import pytest

from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.conversations import ConversationError, ConversationStore
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectError, ProjectStore


def _make_task(store: SqliteStore, project_id: str, title: str, state: str) -> str:
    task_id = new_id("tsk")
    now = utc_now_iso()
    store.create_task(
        {
            "id": task_id,
            "project_id": project_id,
            "title": title,
            "state": state,
            "created_at": now,
            "updated_at": now,
        }
    )
    return task_id


def test_create_subproject_requires_existing_parent(project_store: ProjectStore) -> None:
    with pytest.raises(ProjectError):
        project_store.create_subproject("proj_missing", {"name": "orphan"})


def test_create_subproject_links_to_parent(project_store: ProjectStore) -> None:
    parent = project_store.create_project({"name": "Parent"})
    child = project_store.create_subproject(parent["id"], {"name": "Child"})
    assert child["parent_project_id"] == parent["id"]


def test_set_parent_rejects_self_and_cycles(project_store: ProjectStore) -> None:
    a = project_store.create_project({"name": "A"})
    b = project_store.create_subproject(a["id"], {"name": "B"})

    with pytest.raises(ProjectError):
        project_store.set_parent(a["id"], a["id"])  # self-parent
    with pytest.raises(ProjectError):
        project_store.set_parent(a["id"], b["id"])  # would close a cycle

    # Detaching to top-level is allowed and clears the parent.
    detached = project_store.set_parent(b["id"], None)
    assert detached["parent_project_id"] is None


def test_project_tree_nests_subprojects_and_tasks(
    project_store: ProjectStore, store: SqliteStore
) -> None:
    root = project_store.create_project({"name": "Root"})
    sub_a = project_store.create_subproject(root["id"], {"name": "SubA"})
    sub_b = project_store.create_subproject(root["id"], {"name": "SubB"})
    _make_task(store, root["id"], "root-task", "running")
    _make_task(store, sub_a["id"], "a-task", "completed")

    tree = project_store.project_tree()
    assert [node["project"]["id"] for node in tree] == [root["id"]]
    root_node = tree[0]
    assert root_node["status"] == "active"  # running task
    assert [t["title"] for t in root_node["tasks"]] == ["root-task"]

    sub_ids = {node["project"]["id"] for node in root_node["sub_projects"]}
    assert sub_ids == {sub_a["id"], sub_b["id"]}
    a_node = next(n for n in root_node["sub_projects"] if n["project"]["id"] == sub_a["id"])
    assert a_node["status"] == "done"  # its only task is completed
    b_node = next(n for n in root_node["sub_projects"] if n["project"]["id"] == sub_b["id"])
    assert b_node["status"] == "empty"  # no tasks


def test_conversation_append_and_read(conversation_store: ConversationStore) -> None:
    first = conversation_store.append("project", "proj_1", "user", "hello")
    second = conversation_store.append("project", "proj_1", "agent", "hi there", model="fable")
    assert (first["seq"], second["seq"]) == (1, 2)
    assert first["id"].startswith("cnv_")

    messages = conversation_store.read("project", "proj_1")
    assert [m["content"] for m in messages] == ["hello", "hi there"]
    assert messages[1]["model"] == "fable"
    assert messages[0]["meta"] == {}

    # A different scope keeps its own independent sequence.
    other = conversation_store.append("task", "tsk_1", "system", "started")
    assert other["seq"] == 1


def test_conversation_rejects_bad_scope_and_role(
    conversation_store: ConversationStore,
) -> None:
    with pytest.raises(ConversationError):
        conversation_store.append("nope", "x", "user", "hi")
    with pytest.raises(ConversationError):
        conversation_store.append("project", "x", "robot", "hi")
