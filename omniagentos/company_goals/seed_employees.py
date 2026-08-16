"""Idempotent seed of the four people whose transcripts drive the pipeline.

Ids are DETERMINISTIC (``emp_owner``, ``emp_frank``, ``emp_bob``, ``emp_alice``)
rather than ``new_id('emp')`` randoms. The roster is a fixed four-person set
that other lanes (transcripts, recommendations) and the operator's Jira-account
mapping must reference across databases, so a stable id is worth more here than
the random-id convention; the ``emp_`` prefix keeps the house id shape.

``role`` and ``jira_account_id`` are left NULL on purpose — this lane does not
invent titles, and the operator maps Jira accounts later (JG2-E13).

Idempotency is keyed on BOTH the deterministic id and the UNIQUE ``name``, so a
row another lane created first (with a random id) is adopted, never duplicated.
"""

from __future__ import annotations

import logging
from typing import Any

from omniagentos.company_goals.store import CompanyGoalsStore

logger = logging.getLogger(__name__)

SEED_EMPLOYEES: tuple[dict[str, Any], ...] = (
    {"id": "emp_owner", "name": "the operator"},
    {"id": "emp_frank", "name": "Frank"},
    {"id": "emp_bob", "name": "Bob"},
    {"id": "emp_alice", "name": "Alice"},
)


def seed_employees(store: CompanyGoalsStore) -> dict[str, int]:
    """Ensure the four seed employees exist. Returns created/existing counts.

    Each row is an ATOMIC upsert (``ON CONFLICT DO NOTHING`` on either the
    deterministic id or the UNIQUE name), never check-then-insert: two API
    processes booting simultaneously must both finish silently, and the loser of
    a check-then-insert would instead raise IntegrityError inside the startup
    path. The adopt-by-name semantics are unchanged — a "the operator" another lane
    created with a random id conflicts on the name and is left exactly as it is.
    """
    counts = {"created": 0, "existing": 0}
    for spec in SEED_EMPLOYEES:
        inserted = store.ensure_employee(
            employee_id=str(spec["id"]),
            name=str(spec["name"]),
            role=spec.get("role"),
            jira_account_id=None,
            status="active",
        )
        counts["created" if inserted else "existing"] += 1
    logger.info(
        "company_goals seed_employees: created=%d existing=%d",
        counts["created"],
        counts["existing"],
    )
    return counts
