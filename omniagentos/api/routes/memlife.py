"""API routes for the memory-lifecycle review queue.

Modelled on ``routes/reflection.py`` (GET list + POST {id}/approve|reject).

The decisive invariant for ``GET /queue``: three states with *distinct* payloads.

- store present, N staged/reopened → ``200 {pending: N}``
- store present, none pending      → ``200 {pending: 0}``
- store root missing / unreadable  → non-200 ``{error: store_unavailable}``

A missing store directory must NEVER return ``200 {pending: 0}`` — that is the
agentic-stack ``review_state`` defect (broken question identical to a true empty
answer) and the same class of lie as ``q.sh`` returning blank on a bad column.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from omniagentos.api.deps import StoreDep
from omniagentos.contracts import new_id, utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.memlife.contracts import (
    CandidateStatus,
    DecisionAction,
    Lesson,
    LessonStatus,
)
from omniagentos.memlife.db import append_decision
from omniagentos.memlife.paths import (
    MEMLIFE_PROJECT,
    memories_root,
)
from omniagentos.memlife.paths import (
    memlife_store_root as _paths_memlife_store_root,
)
from omniagentos.memlife.render import render_lessons
from omniagentos.memlife.store import MemlifeStore, atomic_write_text

router = APIRouter(prefix="/api/memlife", tags=["memlife"])
_LOG = logging.getLogger(__name__)

# Statuses that await human judgement on the review queue.
_PENDING_STATUSES = (
    CandidateStatus.STAGED.value,
    CandidateStatus.REOPENED.value,
)

# Kept for callers/tests that still import the historical name; resolution
# lives in omniagentos.memlife.paths (Lane 0).
_ENV_STORE = "OMNIAGENTOS_MEMLIFE_STORE"


def memlife_store_root() -> Path:
    """Resolve the filesystem store root for the memlife review surface.

    Delegates to ``omniagentos.memlife.paths.memlife_store_root`` so the API
    queue and the render/lessons path share one resolver (Lane 0).
    """
    return _paths_memlife_store_root()


def _clear_store_root_cache() -> None:
    """Hook for tests; resolver is pure today but keep the seam stable."""
    return None


def _store_unavailable_response() -> JSONResponse:
    """Explicit non-result. Must not look like an empty queue."""
    return JSONResponse(
        status_code=503,
        content={"error": "store_unavailable"},
    )


def _require_store_root() -> Path | JSONResponse:
    """Return the store root if present, else the unavailable response.

    ``Path.is_dir()`` is False for missing paths *and* for files. Both are
    unavailable — we refuse to invent an empty answer either way.
    """
    root = memlife_store_root()
    if not root.is_dir():
        _LOG.warning("memlife store unavailable at %s", root)
        return _store_unavailable_response()
    return root


class MemlifeDecisionRequest(BaseModel):
    """Named distinctly from reflection.MemlifeDecisionRequest ON PURPOSE.

    FastAPI keys OpenAPI component schemas by CLASS NAME, so two route modules
    both exporting `MemlifeDecisionRequest` collide: one silently overwrites the other
    and clients receive the wrong shape for whichever endpoint loses. These two
    are not interchangeable — this one carries `reason`, reflection's does not.
    Caught by cross-lineage verification of a different lane.
    """

    decided_by: str = "human"
    reason: str = ""


def _pending_count(db: SqliteStore) -> int:
    placeholders = ",".join("?" for _ in _PENDING_STATUSES)
    row = db._connection.execute(
        f"SELECT COUNT(*) AS n FROM memlife_candidates WHERE status IN ({placeholders})",
        _PENDING_STATUSES,
    ).fetchone()
    return int(row["n"] if row is not None else 0)


def _get_candidate_row(db: SqliteStore, candidate_id: str) -> Any | None:
    return db._connection.execute(
        "SELECT id, key, claim, conditions, evidence_ids_json, status, "
        "rejection_count FROM memlife_candidates WHERE id = ?",
        (candidate_id,),
    ).fetchone()


def _append_decision(
    db: SqliteStore,
    *,
    candidate_id: str,
    action: str,
    actor: str,
    reason: str,
    at: str,
) -> None:
    # F4 single-writer: all memlife_decisions INSERTs go through memlife.db.
    append_decision(
        db._connection,
        candidate_id=candidate_id,
        action=action,
        at=at,
        actor=actor,
        reason=reason,
    )


@router.get("/queue")
def get_queue(store: StoreDep) -> Any:
    """Review-queue summary with three distinct outcomes (see module docstring)."""
    root_or_err = _require_store_root()
    if isinstance(root_or_err, JSONResponse):
        return root_or_err

    db = cast(SqliteStore, store)
    try:
        with db._lock:
            pending = _pending_count(db)
    except sqlite3.OperationalError as exc:
        # Missing table / schema not migrated: also unavailable, not empty.
        _LOG.warning("memlife queue query failed (store schema unavailable): %s", exc)
        return _store_unavailable_response()

    return {"pending": pending}


@router.post("/{candidate_id}/graduate")
def graduate_candidate(
    candidate_id: str,
    store: StoreDep,
    req: MemlifeDecisionRequest | None = None,
) -> dict[str, Any]:
    """Graduate a staged/reopened candidate into an accepted lesson.

    Writes the lesson row AND the decision entry in one locked transaction —
    reporting success without the lesson is the upstream ``mark_graduated`` bug.
    """
    root_or_err = _require_store_root()
    if isinstance(root_or_err, JSONResponse):
        raise HTTPException(status_code=503, detail="store_unavailable")

    body = req or MemlifeDecisionRequest()
    now = utc_now_iso()
    db = cast(SqliteStore, store)
    filesystem_store = MemlifeStore(memlife_store_root())
    rendered_path = memories_root() / MEMLIFE_PROJECT / "LESSONS.md"

    with db._lock:
        row = _get_candidate_row(db, candidate_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Candidate {candidate_id} not found",
            )
        if row["status"] not in _PENDING_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Candidate {candidate_id} is in status '{row['status']}', "
                    f"only {list(_PENDING_STATUSES)} candidates can be graduated"
                ),
            )

        lesson_id = new_id("mlsn")
        previous_render = (
            rendered_path.read_text(encoding="utf-8") if rendered_path.is_file() else None
        )
        lesson_saved = False

        db._begin()
        try:
            db._connection.execute(
                "UPDATE memlife_candidates SET status = ?, updated_at = ? WHERE id = ?",
                (CandidateStatus.GRADUATED.value, now, candidate_id),
            )
            _append_decision(
                db,
                candidate_id=candidate_id,
                action=DecisionAction.GRADUATE.value,
                actor=body.decided_by,
                reason=body.reason,
                at=now,
            )
            db._connection.execute(
                """
                INSERT INTO memlife_lessons (
                    id, candidate_id, claim, conditions, status,
                    graduated_at, graduated_by, evidence_ids_json, provenance_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lesson_id,
                    candidate_id,
                    row["claim"],
                    row["conditions"] or "",
                    LessonStatus.ACCEPTED.value,
                    now,
                    body.decided_by,
                    row["evidence_ids_json"] or "[]",
                    "{}",
                    now,
                ),
            )

            lesson = Lesson(
                id=lesson_id,
                candidate_id=candidate_id,
                claim=row["claim"],
                conditions=row["conditions"] or "",
                status=LessonStatus.ACCEPTED,
                graduated_at=datetime.fromisoformat(now.replace("Z", "+00:00")),
                graduated_by=body.decided_by,
                evidence_ids=json.loads(row["evidence_ids_json"] or "[]"),
            )
            filesystem_store.save_lesson(lesson)
            lesson_saved = True
            render_lessons(
                MEMLIFE_PROJECT,
                filesystem_store.list_lessons(),
                memories_root=memories_root(),
            )
            db._commit()
        except Exception as exc:
            db._rollback()
            if lesson_saved:
                filesystem_store.delete_lesson(lesson_id)
                try:
                    if previous_render is None:
                        rendered_path.unlink(missing_ok=True)
                    else:
                        atomic_write_text(rendered_path, previous_render)
                except OSError:
                    _LOG.exception("failed to restore rendered lessons after graduation rollback")
            raise HTTPException(
                status_code=500,
                detail="lesson_render_failed",
            ) from exc

    return {
        "id": candidate_id,
        "status": CandidateStatus.GRADUATED.value,
        "lesson_id": lesson_id,
    }


@router.post("/{candidate_id}/reject")
def reject_candidate(
    candidate_id: str,
    store: StoreDep,
    req: MemlifeDecisionRequest | None = None,
) -> dict[str, Any]:
    """Reject a staged/reopened candidate; append the decision.

    SQLite is system of record. The filesystem mirror is updated so a later
    dream cycle cannot overwrite a REJECTED candidate back to STAGED (B4).
    """
    root_or_err = _require_store_root()
    if isinstance(root_or_err, JSONResponse):
        raise HTTPException(status_code=503, detail="store_unavailable")

    body = req or MemlifeDecisionRequest()
    now = utc_now_iso()
    db = cast(SqliteStore, store)

    with db._lock:
        row = _get_candidate_row(db, candidate_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Candidate {candidate_id} not found",
            )
        if row["status"] not in _PENDING_STATUSES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Candidate {candidate_id} is in status '{row['status']}', "
                    f"only {list(_PENDING_STATUSES)} candidates can be rejected"
                ),
            )

        rejection_count = int(row["rejection_count"] or 0) + 1
        db._connection.execute(
            "UPDATE memlife_candidates SET status = ?, rejection_count = ?, "
            "updated_at = ? WHERE id = ?",
            (CandidateStatus.REJECTED.value, rejection_count, now, candidate_id),
        )
        _append_decision(
            db,
            candidate_id=candidate_id,
            action=DecisionAction.REJECT.value,
            actor=body.decided_by,
            reason=body.reason,
            at=now,
        )

    # Keep the filesystem mirror in step with SQLite (B4). DB remains SoR;
    # a missing FS candidate is not a reject failure.
    try:
        from omniagentos.memlife.lifecycle import Lifecycle
        from omniagentos.memlife.store import (
            CandidateNotFoundError,
            StoreUnavailableError,
        )

        life = Lifecycle(MemlifeStore(root_or_err))
        life.reject(
            candidate_id,
            actor=body.decided_by,
            reason=body.reason,
        )
    except (CandidateNotFoundError, StoreUnavailableError, OSError) as exc:
        _LOG.warning(
            "memlife reject: filesystem mirror not updated for %s: %s",
            candidate_id,
            exc,
        )

    return {
        "id": candidate_id,
        "status": CandidateStatus.REJECTED.value,
    }


@router.post("/{candidate_id}/reopen")
def reopen_candidate(
    candidate_id: str,
    store: StoreDep,
    req: MemlifeDecisionRequest | None = None,
) -> dict[str, Any]:
    """Reopen a rejected candidate for another review pass."""
    root_or_err = _require_store_root()
    if isinstance(root_or_err, JSONResponse):
        raise HTTPException(status_code=503, detail="store_unavailable")

    body = req or MemlifeDecisionRequest()
    now = utc_now_iso()
    db = cast(SqliteStore, store)

    with db._lock:
        row = _get_candidate_row(db, candidate_id)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=f"Candidate {candidate_id} not found",
            )
        if row["status"] != CandidateStatus.REJECTED.value:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Candidate {candidate_id} is in status '{row['status']}', "
                    "only 'rejected' candidates can be reopened"
                ),
            )

        db._connection.execute(
            "UPDATE memlife_candidates SET status = ?, updated_at = ? WHERE id = ?",
            (CandidateStatus.REOPENED.value, now, candidate_id),
        )
        _append_decision(
            db,
            candidate_id=candidate_id,
            action=DecisionAction.REOPEN.value,
            actor=body.decided_by,
            reason=body.reason,
            at=now,
        )

    return {
        "id": candidate_id,
        "status": CandidateStatus.REOPENED.value,
    }
