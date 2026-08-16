"""Dream cycle — nightly consolidation of episodic memory.

Pipeline: optional capture → parse JSONL (quarantine corrupt) → salience →
cluster → prefilter → stage candidates. Returns :class:`CycleReport`.

**Byte conservation** is the decisive property::

    kept_bytes + archived_bytes + quarantined_bytes == input_bytes

Upstream ``auto_dream`` silently deletes unparseable JSONL lines on rewrite
and reports a clean run. Here a corrupt line lands in ``quarantine/`` and is
counted — never dropped.

A cycle that reads nothing returns :attr:`CycleStatus.NO_INPUT`, never the
success path. "Nothing to do" and "nothing was there" are different facts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.memlife.capture import capture_events
from omniagentos.memlife.cluster import cluster
from omniagentos.memlife.contracts import (
    Candidate,
    CandidateStatus,
    CycleReport,
    CycleStatus,
    EpisodicEvent,
)
from omniagentos.memlife.db import delete_staged_candidate, stage_candidate
from omniagentos.memlife.lifecycle import Lifecycle
from omniagentos.memlife.prefilter import prefilter
from omniagentos.memlife.salience import candidate_salience
from omniagentos.memlife.store import (
    CandidateNotFoundError,
    MemlifeStore,
    StoreUnavailableError,
    atomic_write_text,
)
from omniagentos.scheduler.routines import (
    DREAM_CYCLE_CRON,
    DREAM_CYCLE_ROUTINE_NAME,
    dream_cycle_routine,
)

LOG = logging.getLogger(__name__)

_STAGE_ACTOR = "dream-cycle"


def _as_store(root: MemlifeStore | Path | str) -> MemlifeStore:
    if isinstance(root, MemlifeStore):
        return root
    return MemlifeStore(root)


def _split_line_bytes(raw: bytes) -> list[bytes]:
    """Split raw file bytes into lines, preserving each line's exact bytes.

    A trailing newline produces a final empty chunk that is discarded (the
    newline already belongs to the previous line). A file with no trailing
    newline keeps the last partial line intact.
    """
    if not raw:
        return []
    lines: list[bytes] = []
    start = 0
    for i, byte in enumerate(raw):
        if byte == 0x0A:  # \n
            lines.append(raw[start : i + 1])
            start = i + 1
    if start < len(raw):
        lines.append(raw[start:])
    return lines


def _parse_event_line(line_bytes: bytes) -> EpisodicEvent | None:
    """Return an EpisodicEvent, or None when the line is unparseable/empty."""
    try:
        text = line_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return EpisodicEvent.model_validate_json(stripped)
    except Exception:
        return None


def _known_event_ids(store: MemlifeStore) -> set[str]:
    """Ids already captured, across BOTH episodic carriers.

    An event lives in ``events.jsonl`` until a cycle consumes it, then in
    ``archive.jsonl`` forever. Checking only the hot log would re-capture every
    already-archived attempt on the next tick, so both are read.

    Ids are pulled with a plain JSON load rather than full model validation:
    this runs per tick over the whole archive, and a line we cannot parse is
    not a line whose id can collide.

    **Streamed, line by line.** ``archive.jsonl`` is append-only and never
    rotated, and this used to be ``read_text()`` + ``splitlines()`` — two full
    copies of the whole file resident at once, inside the scheduler's store lock
    (``builtin_jobs.run_memlife_dream_cycle`` wraps the cycle in ``with lock:``).
    Measured on a copy of the live archive: at 400x its current size (220 MB,
    roughly a year at 1k attempts/day) that peaked at **1657 MB RSS**. Streaming
    holds one line at a time, so memory is O(distinct ids), not O(file).

    Time is still O(archive) and honestly cannot be otherwise here: the only way
    to make repeat scans cheap is to trust a persisted id index, and a sidecar
    that can silently disagree with the archive is precisely the extra-carrier
    drift this module has spent the lane removing. The scan is instead **not
    performed at all** when capture returned nothing (see
    :func:`run_dream_cycle`), which is the steady state on almost every tick.
    """
    ids: set[str] = set()
    for path in (store.events_path, store.episodic_dir / "archive.jsonl"):
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for line in handle:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    event_id = obj.get("id")
                    if isinstance(event_id, str) and event_id:
                        ids.add(event_id)
    return ids


def _attach_salience(
    candidates: list[Candidate],
    events: list[EpisodicEvent],
    now: datetime,
) -> list[Candidate]:
    """Attach per-candidate salience from member events.

    Unknown (None) member scores mean the candidate salience is None — kept,
    never treated as 0.0.

    ``cluster_size`` is the candidate's recurrence and is passed through to
    :func:`salience_score`. It used to be dropped, which left the scorer's
    ``recurrence`` parameter dead on the only production path that calls it —
    so the 205-member recurring failure in the live store ranked identically to
    a one-off blurb (measured 2026-08-06T18:36Z: the five largest clusters were
    205, 73, 69, 53, 52 members, all at salience 0.0).
    """
    by_id = {e.id: e for e in events}
    out: list[Candidate] = []
    for cand in candidates:
        # Shared with backfill.rescore_candidates so the two cannot drift.
        sal = candidate_salience(
            by_id,
            cand.evidence_ids,
            cluster_size=cand.cluster_size,
            now=now,
        )
        out.append(cand.model_copy(update={"salience": sal}))
    return out


def _append_archive(store: MemlifeStore, lines: list[bytes]) -> None:
    if not lines:
        return
    archive_path = store.episodic_dir / "archive.jsonl"
    store.episodic_dir.mkdir(parents=True, exist_ok=True)
    with archive_path.open("ab") as fh:
        for line in lines:
            fh.write(line)
            if not line.endswith(b"\n"):
                fh.write(b"\n")
        fh.flush()


def run_dream_cycle(
    root: MemlifeStore | Path | str,
    *,
    now: datetime | None = None,
    db_path: Path | str | None = None,
    since: datetime | str | None = None,
    actor: str = _STAGE_ACTOR,
    db_conn: sqlite3.Connection | None = None,
) -> CycleReport:
    """Run one dream cycle over the project's episodic JSONL.

    Parameters
    ----------
    root:
        Memlife store root (or :class:`MemlifeStore`).
    now:
        Clock for salience (injected for determinism). Defaults to UTC now.
    db_path:
        Optional SQLite path; when set, :func:`capture_events` appends new
        episodic rows into ``events.jsonl`` before the consolidation pass.
    since:
        Optional lower bound for capture.
    actor:
        Actor string written on staged candidates.
    db_conn:
        Optional SQLite connection for the review-queue write path (Lane B).
        When ``None``, behaviour is byte-identical to the filesystem-only
        cycle (existing tests stay green). When set, each staged candidate is
        committed to ``memlife_candidates`` **before** the filesystem store so
        the operator-visible queue always has a backing candidate file.
    """
    store = _as_store(root)
    clock = now if now is not None else datetime.now(tz=UTC)
    errors: list[str] = []

    # --- store availability -------------------------------------------------
    try:
        if not store.root.is_dir():
            return CycleReport(
                status=CycleStatus.NO_INPUT,
                input_bytes=0,
                kept_bytes=0,
                archived_bytes=0,
                quarantined_bytes=0,
                errors=["memlife store directory missing"],
            )
    except OSError as exc:
        return CycleReport(
            status=CycleStatus.FAILED,
            input_bytes=0,
            kept_bytes=0,
            archived_bytes=0,
            quarantined_bytes=0,
            errors=[f"store access failed: {exc}"],
        )

    # Ensure subdirs exist so quarantine/archive writes cannot fail for layout.
    try:
        store.ensure_layout()
    except OSError as exc:
        return CycleReport(
            status=CycleStatus.FAILED,
            input_bytes=0,
            kept_bytes=0,
            archived_bytes=0,
            quarantined_bytes=0,
            errors=[f"ensure_layout failed: {exc}"],
        )

    # --- optional capture from swarm DB ------------------------------------
    if db_path is not None:
        try:
            captured = capture_events(db_path, since=since)
            # Idempotent by event id. `since` is an inclusive lower bound, so
            # the watermark row is always re-read; and a second-resolution
            # watermark cannot be made exclusive without dropping co-timed
            # attempts (capture.capture_events explains the measurement).
            # De-duplicating on the id keeps every distinct attempt while
            # archiving each of them exactly once.
            #
            # Nothing captured means nothing to de-duplicate against, so the
            # whole-archive scan is skipped. That is the steady state: the
            # watermark means most ticks capture zero rows, and this scan is
            # O(archive) inside the scheduler's store lock.
            if captured:
                already = _known_event_ids(store)
                for event in captured:
                    if event.id in already:
                        continue
                    store.append_event_json(event.model_dump_json())
                    already.add(event.id)
        except Exception as exc:  # noqa: BLE001 — capture failure is reported, not swallowed as success
            errors.append(f"capture failed: {exc}")
            return CycleReport(
                status=CycleStatus.FAILED,
                input_bytes=0,
                kept_bytes=0,
                archived_bytes=0,
                quarantined_bytes=0,
                errors=errors,
            )

    # --- read episodic source ----------------------------------------------
    events_path = store.events_path
    if not events_path.is_file():
        return CycleReport(
            status=CycleStatus.NO_INPUT,
            input_bytes=0,
            kept_bytes=0,
            archived_bytes=0,
            quarantined_bytes=0,
            errors=errors or ["episodic events file missing"],
        )

    try:
        raw = events_path.read_bytes()
    except OSError as exc:
        return CycleReport(
            status=CycleStatus.FAILED,
            input_bytes=0,
            kept_bytes=0,
            archived_bytes=0,
            quarantined_bytes=0,
            errors=[f"read events failed: {exc}"],
        )

    input_bytes = len(raw)
    if input_bytes == 0:
        return CycleReport(
            status=CycleStatus.NO_INPUT,
            input_bytes=0,
            kept_bytes=0,
            archived_bytes=0,
            quarantined_bytes=0,
            errors=errors or ["episodic events file empty"],
        )

    # --- classify every input byte -----------------------------------------
    kept_lines: list[bytes] = []
    archived_lines: list[bytes] = []
    kept_bytes = 0
    archived_bytes = 0
    quarantined_bytes = 0
    valid_events: list[EpisodicEvent] = []

    for index, line_bytes in enumerate(_split_line_bytes(raw)):
        # Distinct name from the earlier `for event in captured` binding so the
        # Optional parse result is not assigned into a non-optional variable.
        parsed_event = _parse_event_line(line_bytes)
        if parsed_event is None:
            # Unparseable / empty / undecodable — quarantine and COUNT.
            try:
                content = line_bytes.decode("utf-8", errors="replace")
            except Exception:
                content = repr(line_bytes)
            try:
                store.write_quarantine_blob(
                    f"events_line_{index}",
                    content,
                    reason="unparseable episodic JSONL line",
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"quarantine write failed for line {index}: {exc}")
                # Still count the bytes as quarantined so conservation holds
                # even if the blob write failed — they did not go kept/archived.
            quarantined_bytes += len(line_bytes)
            errors.append(f"quarantined unparseable line {index}")
            continue

        # Valid events are consumed this cycle (archived after processing).
        # Salience unknown does not drop them — they still enter the pipeline.
        valid_events.append(parsed_event)
        archived_lines.append(line_bytes)
        archived_bytes += len(line_bytes)

    # --- pipeline: salience → cluster → prefilter → stage ------------------
    staged_count = 0
    try:
        candidates = cluster(valid_events)
        candidates = _attach_salience(candidates, valid_events, clock)

        # Existing lessons: empty list is valid (source present, nothing yet).
        # A missing lessons directory under a laid-out store should not happen;
        # if list_lessons fails, treat as FAILED rather than "no duplicates".
        try:
            existing_lessons = store.list_lessons()
        except StoreUnavailableError:
            existing_lessons = []

        surviving = prefilter(candidates, existing_lessons)

        life = Lifecycle(store)
        now_iso = utc_now_iso()
        for cand in surviving:
            # Guard the filesystem mirror: Lifecycle.stage overwrites any
            # existing candidate file back to STAGED with a fresh single-entry
            # decision list. Skip non-STAGED ids already on disk (Lane B).
            if _fs_candidate_is_decided(store, cand.id):
                continue

            if db_conn is not None:
                # DB first, then filesystem — a queue row the operator can see
                # must always have a backing filesystem candidate.
                with db_conn:
                    inserted = stage_candidate(db_conn, cand, now=now_iso)
                if not inserted:
                    continue  # decided by key, or already present by id
                try:
                    life.stage(
                        id=cand.id,
                        key=cand.key,
                        claim=cand.claim,
                        cluster_size=cand.cluster_size,
                        actor=actor,
                        reason=(cand.decisions[-1].reason if cand.decisions else "dream cycle"),
                        conditions=cand.conditions,
                        evidence_ids=list(cand.evidence_ids),
                        salience=cand.salience,
                    )
                except Exception:
                    # Partial success is failure: drop the DB row just inserted.
                    with db_conn:
                        delete_staged_candidate(db_conn, cand.id)
                    raise
            else:
                # Prefer Lifecycle.stage so queue refresh is part of the commit.
                # Cluster already assigned deterministic id/key/claim; re-stage
                # with the dream actor while preserving evidence and cluster size.
                life.stage(
                    id=cand.id,
                    key=cand.key,
                    claim=cand.claim,
                    cluster_size=cand.cluster_size,
                    actor=actor,
                    reason=(cand.decisions[-1].reason if cand.decisions else "dream cycle"),
                    conditions=cand.conditions,
                    evidence_ids=list(cand.evidence_ids),
                    salience=cand.salience,
                )
            staged_count += 1
    except Exception as exc:  # noqa: BLE001 — pipeline failure is FAILED, not silent success
        errors.append(f"pipeline failed: {exc}")
        # Still rewrite/archive what we classified so bytes do not vanish.
        try:
            _append_archive(store, archived_lines)
            atomic_write_text(
                events_path,
                b"".join(kept_lines).decode("utf-8", errors="replace"),
            )
        except Exception as write_exc:  # noqa: BLE001
            errors.append(f"rewrite after pipeline failure: {write_exc}")
        return CycleReport(
            status=CycleStatus.FAILED,
            input_bytes=input_bytes,
            kept_bytes=kept_bytes,
            archived_bytes=archived_bytes,
            quarantined_bytes=quarantined_bytes,
            staged=staged_count,
            errors=errors,
        )

    # --- rewrite: archive processed, keep remainder, quarantine already written
    try:
        _append_archive(store, archived_lines)
        # kept_lines is empty when every valid line was archived; write empty
        # (or the residual kept set) so the hot log reflects post-cycle state.
        atomic_write_text(
            events_path,
            b"".join(kept_lines).decode("utf-8", errors="replace"),
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"rewrite failed: {exc}")
        return CycleReport(
            status=CycleStatus.FAILED,
            input_bytes=input_bytes,
            kept_bytes=kept_bytes,
            archived_bytes=archived_bytes,
            quarantined_bytes=quarantined_bytes,
            staged=staged_count,
            errors=errors,
        )

    report = CycleReport(
        status=CycleStatus.COMPLETED,
        input_bytes=input_bytes,
        kept_bytes=kept_bytes,
        archived_bytes=archived_bytes,
        quarantined_bytes=quarantined_bytes,
        staged=staged_count,
        graduated=0,
        errors=errors,
    )
    if not report.bytes_conserved:
        # Defensive: should be unreachable if accounting above is correct.
        LOG.error(
            "dream cycle byte conservation broken: kept=%s archived=%s quarantined=%s input=%s",
            report.kept_bytes,
            report.archived_bytes,
            report.quarantined_bytes,
            report.input_bytes,
        )
    return report


def _fs_candidate_is_decided(store: MemlifeStore, candidate_id: str) -> bool:
    """True when the on-disk candidate exists with a non-STAGED status."""
    try:
        existing = store.load_candidate(candidate_id)
    except (CandidateNotFoundError, StoreUnavailableError, OSError):
        return False
    return existing.status is not CandidateStatus.STAGED


def ensure_dream_cycle_routine(store: Any) -> dict[str, Any]:
    """Register the dream-cycle tick once, idempotently.

    Mirrors :func:`omniagentos.improve.dispatcher.ensure_improve_dispatcher_routine`.
    """
    from omniagentos.scheduler.routines import DREAM_CYCLE_ROUTINE_NAME as _NAME
    from omniagentos.scheduler.routines import dream_cycle_routine as _payload
    from omniagentos.scheduler.store import RoutinesStore

    r_store = RoutinesStore(store)
    for existing in r_store.list_routines():
        if existing.get("name") == _NAME:
            return existing
    return r_store.create_routine(_payload())


__all__ = [
    "DREAM_CYCLE_CRON",
    "DREAM_CYCLE_ROUTINE_NAME",
    "dream_cycle_routine",
    "ensure_dream_cycle_routine",
    "run_dream_cycle",
]
