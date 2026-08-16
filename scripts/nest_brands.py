"""Post-deploy helper for nesting brand projects under a Brands project."""

from __future__ import annotations

from typing import Any

from omniagentos.projects import ProjectError, ProjectStore


def nest_brands(store: ProjectStore, brand_ids: list[str]) -> dict[str, Any]:
    """Ensure a top-level Brands project and nest the requested projects under it.

    All requested project ids are checked before creating or changing anything,
    so an unknown brand cannot leave a partial reorganization behind.
    """
    projects = {str(project["id"]): project for project in store.list_projects()}
    missing = [project_id for project_id in brand_ids if project_id not in projects]
    if missing:
        raise ProjectError(f"unknown brand project(s): {', '.join(missing)}")

    brands_project = next(
        (project for project in projects.values() if project.get("name") == "Brands"),
        None,
    )
    if brands_project is None:
        brands_project = store.create_project({"name": "Brands"})
    brands_project_id = str(brands_project["id"])

    reparented_count = 0
    for project_id in dict.fromkeys(brand_ids):
        if project_id == brands_project_id:
            raise ProjectError("the Brands project cannot be nested under itself")
        if projects[project_id].get("parent_project_id") == brands_project_id:
            continue
        store.set_parent(project_id, brands_project_id)
        reparented_count += 1

    return {
        "brands_project_id": brands_project_id,
        "reparented_count": reparented_count,
    }


__all__ = ["nest_brands"]
