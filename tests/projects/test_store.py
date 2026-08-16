from __future__ import annotations

import pytest

from omniagentos.projects import ProjectError, ProjectStore


def test_create_get_and_list_roundtrip(project_store: ProjectStore) -> None:
    created = project_store.create_project(
        {
            "name": "Acme",
            "root_dirs": ["/work/acme", "/work/acme-docs"],
            "vault_subfolder": "projects/acme",
            "budget_usd": 42.5,
            "allowed_tools": ["shell", "file_write"],
            "allowed_dirs": ["/work/acme"],
        }
    )
    assert created["id"].startswith("proj_")
    assert created["root_dirs"] == ["/work/acme", "/work/acme-docs"]
    assert created["allowed_tools"] == ["shell", "file_write"]
    assert created["budget_usd"] == 42.5
    assert created["grants"] == []

    fetched = project_store.get_project(created["id"])
    assert fetched is not None
    assert fetched["name"] == "Acme"
    assert fetched["vault_subfolder"] == "projects/acme"

    assert [p["id"] for p in project_store.list_projects()] == [created["id"]]


def test_duplicate_name_rejected(project_store: ProjectStore) -> None:
    project_store.create_project({"name": "Dup"})
    with pytest.raises(ProjectError):
        project_store.create_project({"name": "Dup"})


def test_blank_name_rejected(project_store: ProjectStore) -> None:
    with pytest.raises(ProjectError):
        project_store.create_project({"name": "   "})


def test_grants_are_created_and_upserted(project_store: ProjectStore) -> None:
    project = project_store.create_project(
        {"name": "Grants"},
        grants=[
            {"action_class": "external_reversible", "requires_approval": False},
            {"action_class": "consequential", "requires_approval": True, "always_human": True},
        ],
    )
    grants = {g["action_class"]: g for g in project["grants"]}
    assert grants["external_reversible"]["requires_approval"] is False
    assert grants["consequential"]["always_human"] is True

    # Upsert: same (project, action_class) overwrites rather than duplicating.
    project_store.set_grant(
        project["id"],
        {"action_class": "external_reversible", "requires_approval": True},
    )
    updated = {g["action_class"]: g for g in project_store.list_grants(project["id"])}
    assert updated["external_reversible"]["requires_approval"] is True
    assert len(updated) == 2


def test_grant_rejects_unknown_action_class(project_store: ProjectStore) -> None:
    project = project_store.create_project({"name": "Bad"})
    with pytest.raises(ProjectError):
        project_store.set_grant(project["id"], {"action_class": "not_a_class"})


def test_get_missing_project_returns_none(project_store: ProjectStore) -> None:
    assert project_store.get_project("proj_missing") is None


def test_create_project_rolls_back_on_invalid_grant(project_store: ProjectStore) -> None:
    # F9: a bad grant must abort the whole create -- no project, no partial grant.
    with pytest.raises(ProjectError):
        project_store.create_project(
            {"name": "Atomic"},
            grants=[
                {"action_class": "read_only"},
                {"action_class": "evil"},  # invalid -> rejected before any write
            ],
        )
    assert project_store.list_projects() == []


def test_list_projects_includes_grants(project_store: ProjectStore) -> None:
    # F5: list rows must carry grants so the dashboard's p.grants.length is safe.
    project_store.create_project(
        {"name": "WithGrants"},
        grants=[{"action_class": "read_only", "requires_approval": True}],
    )
    rows = project_store.list_projects()
    assert rows[0]["grants"][0]["action_class"] == "read_only"


def test_create_project_with_parent(project_store: ProjectStore) -> None:
    parent = project_store.create_project({"name": "Parent"})
    child = project_store.create_project({"name": "Child", "parent_project_id": parent["id"]})
    assert child["parent_project_id"] == parent["id"]


def test_create_project_with_nonexistent_parent_rejected(
    project_store: ProjectStore,
) -> None:
    with pytest.raises(ProjectError):
        project_store.create_project({"name": "Orphan", "parent_project_id": "proj_missing"})


def test_create_project_with_self_as_parent(project_store: ProjectStore) -> None:
    with pytest.raises(ProjectError):
        project_store.create_project(
            {"id": "proj_self", "name": "Self", "parent_project_id": "proj_self"}
        )
