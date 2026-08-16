"""Project every project's pending events into its per-project human log, once.

``GET /api/projects/{id}/activity`` (omniagentos.api.routes.projects) already
calls :func:`omniagentos.projects.activity.project_pending_activity`
best-effort on every read, so opening the dashboard always shows a fresh
on-disk log -- but that only writes while someone is actually looking. Run
this periodically (cron/launchd; an operator's choice installed by hand after
review, same posture as ``scripts/scheduler/com.omniagentos.routines.plist
.template``) if the log under ``var/projects/<id>/logs/`` should stay current
even when nobody has the dashboard open.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Any

from omniagentos.contracts import default_db_path
from omniagentos.db.store import SqliteStore
from omniagentos.projects.activity import project_pending_activity

logger = logging.getLogger(__name__)


def tick(store: SqliteStore, *, project_ids: list[str] | None = None) -> dict[str, Any]:
    """Project pending activity for every project (or just `project_ids`).

    Returns a JSON-serializable summary: lines appended per project id.
    ``project_pending_activity`` already never raises, but a defensive
    per-project try/except keeps an unexpected fault from aborting the tick
    partway through the sweep.
    """
    ids = list(project_ids) if project_ids is not None else store.list_project_ids()
    appended: dict[str, int] = {}
    for project_id in ids:
        try:
            appended[project_id] = project_pending_activity(store, project_id)
        except Exception:  # noqa: BLE001 -- one project's fault must not abort the tick.
            logger.exception("project activity projection failed for %s", project_id)
            appended[project_id] = 0
    return {"projects": len(ids), "lines_appended": appended}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--project",
        action="append",
        dest="project_ids",
        help="project id to project (repeatable); default: every project",
    )
    args = parser.parse_args(argv)
    store = SqliteStore(default_db_path())
    result = tick(store, project_ids=args.project_ids)
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "tick"]
