"""Company enrichment helper for board task rows.

Best-effort enrichment: tasks with project_id are joined to projects.org_company_id
→ org_companies (slug, id) and stamped atomically (both or neither). Existing
classifier-set values are never overwritten. Cards without project→company chain
stay unstamped (honesty rule).

Used by reconcile_board (live kanban), collab.list_board (API), and org view
data paths (matrix/portfolio) to ensure all consumers agree on company context.
"""

from __future__ import annotations

import logging
from typing import Any

LOG = logging.getLogger(__name__)


def enrich_company_from_projects(
    rows: list[dict[str, Any]], connection: Any, *, chunk_size: int = 500
) -> None:
    """Enrich task rows with company_slug + company_id from project chain.

    Mutates rows in-place. For each row with a project_id:
    - Query projects to find org_company_id
    - Query org_companies to find slug
    - Stamp org.organization_context.company_slug + company_id ONLY if absent
    - Both set or neither (atomic)

    Args:
        rows: List of task dicts from board_tasks (or similar projection).
              Must have "project_id" and "org" (dict or None) keys.
        connection: sqlite3.Connection to the same DB (projects + org_companies tables).
        chunk_size: IN(...) chunk size (default 500).
    """
    if not rows:
        return

    try:
        # Batch-load projects and companies, chunked to avoid SQL size limits
        project_ids = {
            str(t.get("project_id")) for t in rows if t.get("project_id")
        }
        if not project_ids:
            return

        project_company_map = {}  # project_id -> company_id
        ids = list(project_ids)
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            placeholders = ", ".join("?" * len(chunk))
            rows_chunk = connection.execute(
                f"SELECT id, org_company_id FROM projects WHERE id IN ({placeholders})",
                chunk,
            ).fetchall()
            for row in rows_chunk:
                if row["org_company_id"]:
                    project_company_map[str(row["id"])] = str(row["org_company_id"])

        if not project_company_map:
            return

        # Load company info
        company_ids = set(project_company_map.values())
        company_info = {}  # company_id -> (slug, id)
        ids = list(company_ids)
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            comp_placeholders = ", ".join("?" * len(chunk))
            comp_rows = connection.execute(
                f"SELECT id, slug FROM org_companies WHERE id IN ({comp_placeholders})",
                chunk,
            ).fetchall()
            for row in comp_rows:
                company_info[str(row["id"])] = (str(row["slug"]), str(row["id"]))

        # Enrich tasks with company data (atomic: both or neither)
        for task in rows:
            project_id = task.get("project_id")
            if project_id and project_id in project_company_map:
                company_id = project_company_map[project_id]
                if company_id in company_info:
                    slug, cid = company_info[company_id]
                    # Ensure org structure exists
                    if "org" not in task or task["org"] is None:
                        task["org"] = {}
                    if "organization_context" not in task["org"]:
                        task["org"]["organization_context"] = {}

                    # Only set if BOTH are absent (respect classifier values)
                    has_slug = task["org"]["organization_context"].get("company_slug")
                    has_id = task["org"]["organization_context"].get("company_id")
                    if not has_slug and not has_id:
                        task["org"]["organization_context"]["company_slug"] = slug
                        task["org"]["organization_context"]["company_id"] = cid
    except Exception as exc:  # noqa: BLE001
        LOG.debug("company enrichment skipped", exc_info=exc)
