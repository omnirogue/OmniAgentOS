"""One-shot repair of the episodic record staged before salience scoring worked.

Why this module exists. The scoring fix is correct *going forward*, but the
contamination it removes was already persisted in every carrier the pipeline
writes: ``memlife_candidates.salience`` in SQLite, the ``candidates/*.json``
filesystem mirror, and ``episodic/archive.jsonl``. The dream cycle cannot repair
any of them on its own: it re-scores only the events in the hot ``events.jsonl``,
that file is consumed and emptied at the end of every cycle, a cycle that reads
nothing returns ``NO_INPUT`` and touches no candidate, and the archive is
append-only and de-duplicated by event **id**, so even a full ``since=None``
re-capture skips every line already in it. So without this, W3 inherits exactly
the flat zeros it would have read before the fix.

Re-measured 2026-08-06T18:36Z on a ``VACUUM INTO`` copy of
``var/runtime/state.sqlite3`` plus the live memlife store: 210 candidate
rows, all active (165 one-offs + 45 recurring), **all 210** at salience 0.0 —
one distinct value, p50 zero — and the five largest clusters, sizes 205, 73, 69,
53, 52, all zero. 210 rows and 210 ``cand_*.json`` files; the 211th file in
``candidates/`` is ``queue.json``, which is not a candidate. No candidate
escaped the collapse.

Repair, not invention. Every candidate stores the attempt ids it was clustered
from, and all 909 evidence ids across those 210 candidates resolve to live
``swarm_attempts`` rows (100%, same measurement). So the score is re-derived
from the original evidence through the same capture and the same aggregator the
live pipeline uses — :func:`salience.candidate_salience` — not guessed, and not
back-filled with a placeholder. A candidate whose evidence has since been pruned
scores ``None``: unknown and kept, never 0.0.

Two things this deliberately does not do:

- **It does not change any candidate's status.** Re-scoring is a data repair,
  not a lifecycle decision, so it writes no ``memlife_decisions`` row (the enum
  has no member for it, and the table is append-only for decisions a human or
  the cycle actually made). Decided candidates are left alone entirely.
- **It does not retire one-off candidates.** A cluster of one is a real
  observation that a human may still want to judge; with a working score it
  *will* sort to the bottom once something ranks by salience, which is the
  correct outcome rather than a deletion nobody authorised. Forward-looking, not
  a present fact: as of 2026-08-06T18:36Z nothing consumes salience at all
  (``store.refresh_queue`` sorts pending by candidate id,
  ``api/routes/memlife.py`` has no ``ORDER BY`` and no salience reference, and
  neither does the dashboard). The contract for whoever becomes the first
  consumer is in :func:`capture._resolve_importance`: **not** a bare
  ``ORDER BY salience DESC``, because SQLite sorts NULL last and unknown would
  render as least salient.

Point-in-time, like every salience: recency decays, so re-running later yields
slightly lower numbers. Re-running is otherwise safe and idempotent — the result
is a pure function of the stored evidence and the clock.
"""

from __future__ import annotations

import argparse
import ast
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omniagentos.contracts import utc_now_iso
from omniagentos.memlife.capture import capture_events
from omniagentos.memlife.contracts import Candidate
from omniagentos.memlife.salience import candidate_salience
from omniagentos.memlife.store import (
    CandidateNotFoundError,
    MemlifeStore,
    StoreUnavailableError,
    atomic_write_text,
)

# Candidates still awaiting human judgement. Decided ones keep the score they
# were decided under — rewriting settled history is not a repair.
ACTIVE_STATUSES: tuple[str, ...] = ("staged", "reopened")


@dataclass
class RescoreReport:
    """What the pass saw and what it changed.

    ``examined`` is asserted non-empty by the CLI before any success is
    reported: a run that found no candidates at all must not look like a run
    that repaired everything.

    ``errors`` is the CLI's exit code. Any entry here means the repair did not
    fully land, and :func:`main` returns non-zero and rolls the SQLite
    transaction back rather than committing half of it — see ``committed``.
    """

    examined: int = 0
    rescored: int = 0
    unchanged: int = 0
    # Evidence that resolved to nothing — the attempt rows were pruned. A real,
    # expected outcome: the candidate scores None (unknown and kept) and is
    # written. Named by id so it is auditable rather than a bare count.
    unresolvable: int = 0
    unresolvable_ids: list[str] = field(default_factory=list)
    # Evidence we could not READ. Not the same fact at all, and not a repair
    # input: these candidates are left untouched and the run fails.
    evidence_corrupt: int = 0
    mirror_written: int = 0
    # Every candidate file actually written, by id. A mid-write mirror failure
    # rolls SQLite back but cannot un-write these, so the operator needs the
    # exact list to reconcile — a count is not enough to fix anything with.
    # Not in as_dict() on the happy path (stdout stays a summary, like
    # ``changes``); on the failure path it is spelled out in ``errors``, and it
    # is always in the receipt.
    mirror_written_ids: list[str] = field(default_factory=list)
    # Of those, how many were put back from the pre-flight copies after a
    # mid-write failure, and which ones could not be — the only candidates that
    # end up genuinely ahead of a rolled-back database.
    mirror_restored: int = 0
    mirror_diverged_ids: list[str] = field(default_factory=list)
    mirror_missing: int = 0
    before_distinct: int = 0
    after_distinct: int = 0
    dry_run: bool = True
    # None means "SQLite only" — recorded so a report can never be read as
    # describing a two-carrier repair when only one carrier was touched.
    store_path: str | None = None
    committed: bool = False
    errors: list[str] = field(default_factory=list)
    # Per-candidate before -> after. Deliberately NOT in as_dict(): stdout stays
    # a summary, the receipt carries the detail. See _write_receipt.
    changes: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "examined": self.examined,
            "rescored": self.rescored,
            "unchanged": self.unchanged,
            "unresolvable": self.unresolvable,
            "unresolvable_ids": self.unresolvable_ids,
            "evidence_corrupt": self.evidence_corrupt,
            "mirror_written": self.mirror_written,
            "mirror_restored": self.mirror_restored,
            "mirror_diverged_ids": self.mirror_diverged_ids,
            "mirror_missing": self.mirror_missing,
            "before_distinct": self.before_distinct,
            "after_distinct": self.after_distinct,
            "dry_run": self.dry_run,
            "store_path": self.store_path,
            "committed": self.committed,
            "errors": self.errors,
        }


@dataclass
class _RescorePlan:
    """Everything pass 3 needs, computed and pre-flighted, nothing written yet.

    Kept separate from :class:`RescoreReport` because the report is the record
    and this is the intent: :func:`main` has to hold the intent for all three
    carriers at once before it writes any of them.
    """

    scores: list[tuple[str, float | None]] = field(default_factory=list)
    loaded: dict[str, Candidate] = field(default_factory=dict)
    store: MemlifeStore | None = None


def rescore_candidates(
    conn: sqlite3.Connection,
    db_path: Path | str,
    *,
    store: MemlifeStore | Path | str | None = None,
    now: datetime | None = None,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
    dry_run: bool = True,
) -> RescoreReport:
    """Plan, pre-flight and (unless ``dry_run``) apply, in one call.

    :func:`main` uses :func:`_plan_rescore` / :func:`_apply_rescore` directly so
    that it can pre-flight every carrier before writing any of them; this
    wrapper is the single-carrier convenience and the tested public entry point.
    """
    report, plan = _plan_rescore(
        conn, db_path, store=store, now=now, statuses=statuses, dry_run=dry_run
    )
    if dry_run or report.errors:
        return report
    _apply_rescore(conn, report, plan)
    return report


def _plan_rescore(
    conn: sqlite3.Connection,
    db_path: Path | str,
    *,
    store: MemlifeStore | Path | str | None = None,
    now: datetime | None = None,
    statuses: tuple[str, ...] = ACTIVE_STATUSES,
    dry_run: bool = True,
) -> tuple[RescoreReport, _RescorePlan]:
    """Re-score active candidates from their stored evidence.

    Parameters
    ----------
    conn:
        Connection to the database holding ``memlife_candidates``.
    db_path:
        Database holding ``swarm_attempts`` — the evidence. Usually the same
        file as ``conn``; kept separate so a repair can be rehearsed against a
        copy without pointing the writer at production.
    store:
        Filesystem mirror to update alongside SQLite. ``None`` updates SQLite
        only, which leaves the two carriers disagreeing — pass it in production.
        The CLI refuses to default to ``None``; see :func:`main`.
    dry_run:
        When true (the default) no repair is written. The report is still
        complete AND the pre-flight still runs, so the change can be inspected
        before it is applied *and* the apply is known to be able to land.

    Write ordering is deliberate. Everything is computed first, then the mirror
    is *pre-flighted* (every candidate file is loaded and the directory is probed
    for writability), and only then is anything written. A repair that cannot
    reach both carriers must fail before it has written either, not after it has
    written one — "both carriers or neither" is not achievable by rolling back
    at the end, because filesystem writes do not roll back.

    **The dry run rehearses the apply; it is not a subset of it.** The pre-flight
    used to sit *after* the ``dry_run`` return, so a rehearsal over a store with
    two candidate files missing and the candidates directory unwritable returned
    ``{"examined": 210, "mirror_missing": 0, "errors": []}`` and exit 0 — a
    perfectly green rehearsal for a run that could not succeed, skipping the one
    check whose failure produces the half-landed state this module exists to
    prevent. The pre-flight now runs in both modes, which is what turns
    ``errors: []`` from a claim into evidence. Its only footprint is
    :func:`_probe_mirror_writable`'s transient dot-file, written and removed;
    that write is the point, because ``os.access`` lies.
    """
    clock = now if now is not None else datetime.now(tz=UTC)
    report = RescoreReport(dry_run=dry_run)
    result = _RescorePlan()

    events_by_id = {e.id: e for e in capture_events(db_path)}

    placeholders = ",".join("?" for _ in statuses)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT id, cluster_size, evidence_ids_json, salience "
        f"FROM memlife_candidates WHERE status IN ({placeholders})",
        statuses,
    ).fetchall()

    report.examined = len(rows)
    report.before_distinct = len({row["salience"] for row in rows})

    memlife_store: MemlifeStore | None = None
    if store is not None:
        memlife_store = store if isinstance(store, MemlifeStore) else MemlifeStore(store)
        report.store_path = str(memlife_store.root)
        result.store = memlife_store

    # --- pass 1: compute, write nothing ------------------------------------
    after: set[float | None] = set()
    plan = result.scores

    for row in rows:
        candidate_id = str(row["id"])
        evidence_ids = _evidence_ids(candidate_id, row["evidence_ids_json"], report)
        if evidence_ids is None:
            # Corrupt evidence. Carry the OLD score forward untouched and fail
            # the run: overwriting it would destroy the only surviving record of
            # what this candidate scored, using input we have just proved we
            # cannot read.
            report.evidence_corrupt += 1
            after.add(row["salience"])
            report.unchanged += 1
            continue

        score = candidate_salience(
            events_by_id,
            [str(e) for e in evidence_ids],
            cluster_size=int(row["cluster_size"]),
            now=clock,
        )
        after.add(score)
        plan.append((candidate_id, score))
        report.changes.append(
            {"id": candidate_id, "before": row["salience"], "after": score}
        )

        if score is None:
            report.unresolvable += 1
            report.unresolvable_ids.append(candidate_id)
        if score == row["salience"]:
            report.unchanged += 1
        else:
            report.rescored += 1

    report.after_distinct = len(after)
    if report.errors:
        # Unreadable input. Write nothing at all — a partial repair over evidence
        # we could not parse is worse than no repair, because it is invisible.
        return report, result

    # --- pass 2: pre-flight the mirror -------------------------------------
    # Runs in DRY-RUN TOO. A rehearsal that skips the only check whose failure
    # produces a half-landed repair is not a rehearsal of anything.
    if memlife_store is not None:
        for candidate_id, _ in plan:
            try:
                result.loaded[candidate_id] = memlife_store.load_candidate(candidate_id)
            except (CandidateNotFoundError, StoreUnavailableError, OSError) as exc:
                report.mirror_missing += 1
                report.errors.append(f"mirror candidate missing for {candidate_id}: {exc}")
        _probe_mirror_writable(memlife_store, report)

    return report, result


def _apply_rescore(
    conn: sqlite3.Connection,
    report: RescoreReport,
    plan: _RescorePlan,
) -> None:
    """Pass 3: write. Only ever called after every carrier has been pre-flighted.

    SQLite first, in one uninterrupted run of ``UPDATE``s: that takes the write
    lock immediately, so a concurrent writer is discovered here — before any
    filesystem write — rather than halfway through the mirror. The mirror is
    written last, closest to the commit, because it is the carrier that cannot
    be rolled back.
    """
    _update_sqlite(conn, plan)
    _write_mirror(report, plan)


def _update_sqlite(conn: sqlite3.Connection, plan: _RescorePlan) -> None:
    """The SQLite half of pass 3, uncommitted. Separate so ``main`` can order it."""
    stamp = utc_now_iso()
    for candidate_id, score in plan.scores:
        conn.execute(
            "UPDATE memlife_candidates SET salience = ?, updated_at = ? WHERE id = ?",
            (score, stamp, candidate_id),
        )


def _write_mirror(report: RescoreReport, plan: _RescorePlan) -> None:
    """The mirror half of pass 3. Separate so ``main`` can order it.

    **Stops at the first failure, and puts back what it already wrote.** It used
    to catch ``OSError`` per candidate and CONTINUE, so one injected failure at
    candidate 50 produced 209 candidate files carrying the new score against 210
    SQLite rows rolled back to the old one — BLK-001 inverted, and the review
    queue reads the mirror. The pre-flight makes that unlikely, not impossible:
    it proves the directory writable with one 6-byte file, so ENOSPC, a
    permission change, or NFS trouble during the 210 real writes all land here.

    Filesystem writes do not roll back, but they can be *compensated*: the
    pre-flight already holds every candidate exactly as it was read, so the
    files written before the failure are rewritten from those originals. That
    restores the state the caller's SQLite rollback is about to claim. A
    compensation that itself fails is reported by id — the one genuinely
    irreducible case, and the operator gets the exact list rather than a count.
    """
    if plan.store is None:
        return
    for candidate_id, score in plan.scores:
        # Both carriers or neither: a queue row and its candidate file that
        # disagree about salience is a split brain that nothing would detect.
        try:
            plan.store.save_candidate(
                plan.loaded[candidate_id].model_copy(update={"salience": score})
            )
            report.mirror_written += 1
            report.mirror_written_ids.append(candidate_id)
        except OSError as exc:
            report.errors.append(f"mirror write failed for {candidate_id}: {exc}")
            _restore_mirror(report, plan)
            return


def _restore_mirror(report: RescoreReport, plan: _RescorePlan) -> None:
    """Put back every candidate file this run wrote, from the pre-flight copies."""
    if plan.store is None or not report.mirror_written_ids:
        return
    for candidate_id in report.mirror_written_ids:
        try:
            plan.store.save_candidate(plan.loaded[candidate_id])
            report.mirror_restored += 1
        except OSError as exc:
            report.mirror_diverged_ids.append(candidate_id)
            report.errors.append(
                f"mirror could not be restored for {candidate_id}: {exc}"
            )
    if report.mirror_diverged_ids:
        report.errors.append(
            f"{len(report.mirror_diverged_ids)} candidate file(s) are AHEAD of "
            "the database, which is being rolled back — reconcile by hand: "
            + ", ".join(report.mirror_diverged_ids)
        )
    else:
        report.errors.append(
            f"{report.mirror_restored} candidate file(s) written before the "
            "failure were restored to their previous values, so the mirror "
            "matches the rolled-back database."
        )


@dataclass
class ArchiveReport:
    """What the archive re-derivation saw and what it changed."""

    lines_in: int = 0
    lines_out: int = 0
    distinct_ids: int = 0
    repaired: int = 0  # rewritten from the DB
    result_changed: int = 0  # ...of which the recorded outcome was wrong
    score_changed: int = 0  # ...of which pain/importance were wrong
    duplicates_dropped: int = 0
    # ...of which the survivor is a line we keep VERBATIM (no attempt row), so
    # which copy survives is the whole answer rather than a cosmetic detail.
    unresolved_duplicates_repicked: int = 0
    unresolved_kept: int = 0  # no attempt row today — kept verbatim, never dropped
    unparseable_kept: int = 0  # kept verbatim and counted, never dropped
    dry_run: bool = True
    backup_path: str | None = None
    # Whether archive.jsonl was ACTUALLY replaced. A failure message that says
    # "nothing repaired" while a permanent record has been rewritten is worse
    # than no message, so the fact is carried rather than inferred.
    written: bool = False
    # Which tree's EventResult this rewrite was checked against, and what that
    # tree can read. Recorded, not just checked: "we verified the reader could
    # read it" is only evidence if it says WHICH reader.
    reader_contracts_path: str | None = None
    reader_results: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lines_in": self.lines_in,
            "lines_out": self.lines_out,
            "distinct_ids": self.distinct_ids,
            "repaired": self.repaired,
            "result_changed": self.result_changed,
            "score_changed": self.score_changed,
            "duplicates_dropped": self.duplicates_dropped,
            "unresolved_duplicates_repicked": self.unresolved_duplicates_repicked,
            "unresolved_kept": self.unresolved_kept,
            "unparseable_kept": self.unparseable_kept,
            "dry_run": self.dry_run,
            "backup_path": self.backup_path,
            "written": self.written,
            "reader_contracts_path": self.reader_contracts_path,
            "reader_results": self.reader_results,
            "errors": self.errors,
        }


@dataclass
class _ArchivePlan:
    """The replacement archive, computed and gated, not yet written."""

    archive_path: Path
    raw: str
    out_lines: list[str]


def rederive_archive(
    store: MemlifeStore | Path | str,
    db_path: Path | str,
    *,
    dry_run: bool = True,
    now: datetime | None = None,
) -> ArchiveReport:
    """Plan, gate and (unless ``dry_run``) rewrite the archive, in one call.

    :func:`main` uses :func:`_plan_archive` / :func:`_commit_archive` directly
    so that the archive is not rewritten until the *other* carriers have also
    been pre-flighted; this wrapper is the tested public entry point.
    """
    memlife_store = store if isinstance(store, MemlifeStore) else MemlifeStore(store)
    report, plan = _plan_archive(memlife_store, db_path, dry_run=dry_run)
    if plan is None or dry_run or report.errors:
        return report
    _commit_archive(plan, report, now=now)
    return report


def _plan_archive(
    memlife_store: MemlifeStore,
    db_path: Path | str,
    *,
    dry_run: bool = True,
) -> tuple[ArchiveReport, _ArchivePlan | None]:
    """Rewrite ``episodic/archive.jsonl`` from the attempt rows it came from.

    The fourth carrier of the same defect, and the one that was **frozen**.
    Measured on the live store (2026-08-06T18:36Z): 910 archived lines, 904
    distinct ids, and every one of those 910 lines carrying
    ``pain: 0.0, importance: 0.0``. Because
    :func:`salience._usable_score` correctly treats a *measured* 0.0 as measured,
    re-scoring an archived event yields exactly ``0.0`` — not ``None``. The
    unknown has been laundered into a confident LOWEST score in the permanent
    episodic record. 15 further lines carry a ``result`` that contradicts what
    their now-terminal attempt row says (11 unknown→success, 2 →killed, 2
    →crashed) and 6 lines duplicate an id already present.

    Nothing repairs this by itself. ``dream._known_event_ids`` de-duplicates on
    id ALONE, so a full ``capture(since=None)`` returns 904 rows and 0 novel
    ones. Comparing ``(id, result, pain, importance)`` instead would let the
    corrected line be appended — but the wrong line would still be there, and
    the id would then genuinely be duplicated. Re-derivation is the repair that
    leaves the file *correct* rather than merely *augmented*.

    **The reader gate.** Before writing anything, every ``result`` value this
    rewrite would INTRODUCE is checked against the ``EventResult`` declared in
    the checkout that will READ the file — located by climbing from the store
    root (see :func:`_reader_contracts_path`) and parsed as source, never
    imported. ``EventResult.MOVED`` exists only on this branch; the live API
    runs ``uvicorn omniagentos.api:app`` out of the serving checkout, whose
    enum is ``['success','failure','denied','unknown']``, and
    ``EpisodicEvent.model_validate_json`` rejects a ``"result":"moved"`` line —
    which ``scheduler/builtin_jobs`` then swallows in a bare
    ``except Exception: continue``. Repairing the archive ahead of the code
    that can read it would therefore make ~30 corrected records **silently
    invisible** to the running system: no error, no counter, no receipt. That
    is a favourable absence manufactured by the repair itself, so the ordering
    (merge, restart, then repair) is enforced here rather than remembered by an
    operator. A vocabulary that cannot be determined is an error, not a pass.

    The gate covers values this rewrite introduces. A ``result`` already in a
    line we keep VERBATIM is left alone deliberately: we cannot re-derive it and
    will not delete it, and it is a line the reader already has today.

    Rules, in the same posture as the dream cycle:

    - An id whose attempt row exists today is rewritten from that row.
    - An id with no attempt row is kept **verbatim**. We cannot re-derive it and
      we do not delete evidence we cannot replace.
    - An unparseable line is kept verbatim and counted — never dropped.
    - A repeated id collapses to ONE line, at the position of its first
      occurrence, so the file ends with exactly one line per distinct id and in
      the order it was already in. *Which* copy supplies that line depends on
      whether we can re-derive it:

      - **Resolvable id** — the survivor is overwritten from the attempt row
        anyway, so the choice cannot affect the output. First occurrence.
      - **Unresolved id** — the survivor is kept VERBATIM, so the choice IS the
        output. Take the LAST occurrence, except never let an ``unknown``
        result displace a known one. Measured on the live archive
        (2026-08-06T18:36Z): of 4 duplicated ids, 2 are byte-identical and 2 are
        not — ``swa_4bab01b7ec604ce4bb97`` is ``attempt|unknown`` then twice
        ``killed|failure``, and ``swa_d49a0aa02cbf43ff8151`` is twice
        ``attempt|unknown`` then ``completed|success``. Those pairs are the
        in-flight capture and the terminal capture of one attempt — the
        round-1 defect, visible in the data — and first-occurrence kept the
        in-flight lie. It is benign on today's corpus ONLY because both ids
        still resolve to an attempt row and are re-derived; the rule above is
        what makes it benign when they no longer do.
    - The pre-image is written to ``archive-pre-backfill-<stamp>.jsonl`` first,
      through the same ``atomic_write_text`` (tmp + fsync + rename) as the
      replacement. This rewrite is not reversible from the DB alone, so it does
      not happen without a **durable** copy of what was there: a buffered
      ``write_text`` returns before the bytes reach the disk, so a crash in the
      window between the two writes could leave the fsync'd replacement durable
      and the only copy of the original truncated — the one file in this whole
      repair whose loss is unrecoverable.
    """
    report = ArchiveReport(dry_run=dry_run)
    archive_path = memlife_store.episodic_dir / "archive.jsonl"
    if not archive_path.is_file():
        return report, None

    try:
        raw = archive_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report.errors.append(f"archive read failed ({archive_path}): {exc}")
        return report, None

    reader_contracts = _reader_contracts_path(memlife_store.root)
    report.reader_contracts_path = str(reader_contracts)
    try:
        readable_results = _event_result_vocabulary(reader_contracts)
    except (OSError, SyntaxError, ValueError) as exc:
        # Not knowing what the reader can parse is not permission to write.
        report.errors.append(
            f"cannot determine the EventResult vocabulary of the tree that will "
            f"read this archive ({reader_contracts}): {exc}"
        )
        return report, None
    report.reader_results = sorted(readable_results)

    fresh = {e.id: e for e in capture_events(db_path)}

    # Position of each id's surviving line, so a later copy can REPLACE it in
    # place rather than append: the file keeps its order, and the duplicate is
    # still dropped. Only unresolved ids need their kept result remembered —
    # everything else is overwritten from the DB regardless.
    position_by_id: dict[str, int] = {}
    kept_result_by_id: dict[str, Any] = {}
    # Every `result` this rewrite would INTRODUCE, counted by value, for the
    # reader check below.
    introduced: dict[str, int] = {}
    out_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        report.lines_in += 1
        try:
            obj = json.loads(stripped)
        except ValueError:
            report.unparseable_kept += 1
            out_lines.append(stripped)
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get("id"), str):
            report.unparseable_kept += 1
            out_lines.append(stripped)
            continue

        event_id = obj["id"]
        event = fresh.get(event_id)
        if event_id in position_by_id:
            report.duplicates_dropped += 1
            if event is None and _supersedes(obj.get("result"), kept_result_by_id[event_id]):
                # Verbatim survivor: the later, more-terminal copy is the one
                # that tells the truth about how the attempt ended.
                out_lines[position_by_id[event_id]] = stripped
                kept_result_by_id[event_id] = obj.get("result")
                report.unresolved_duplicates_repicked += 1
            continue
        position_by_id[event_id] = len(out_lines)

        if event is None:
            report.unresolved_kept += 1
            kept_result_by_id[event_id] = obj.get("result")
            out_lines.append(stripped)
            continue

        rebuilt = event.model_dump_json()
        introduced[event.result.value] = introduced.get(event.result.value, 0) + 1
        if rebuilt != stripped:
            report.repaired += 1
            if obj.get("result") != event.result.value:
                report.result_changed += 1
            if obj.get("pain") != event.pain or obj.get("importance") != event.importance:
                report.score_changed += 1
        out_lines.append(rebuilt)

    report.lines_out = len(out_lines)
    report.distinct_ids = len(position_by_id)

    unreadable = sorted(set(introduced) - readable_results)
    if unreadable:
        offending = ", ".join(
            f"{introduced[value]} line(s) with result={value!r}" for value in unreadable
        )
        report.errors.append(
            f"REFUSED: this repair would write {offending} "
            f"into {archive_path}, and the tree that reads it "
            f"({reader_contracts}) only knows {sorted(readable_results)}. "
            "That tree would reject those lines with a ValidationError, and "
            "scheduler/builtin_jobs swallows the rejection — the repaired "
            "records would become silently invisible to the running system. "
            "Merge the branch that adds the member, restart the reading "
            "process, then re-run."
        )
        return report, None

    # The archive directory has to take a pre-image AND a replacement. Probe it
    # here, with the other pre-flights, rather than discovering it is read-only
    # at the moment we are about to replace a permanent record.
    error = _probe_dir_writable(archive_path.parent, "archive directory")
    if error is not None:
        report.errors.append(error)
        return report, None

    return report, _ArchivePlan(archive_path=archive_path, raw=raw, out_lines=out_lines)


def _commit_archive(
    plan: _ArchivePlan,
    report: ArchiveReport,
    *,
    now: datetime | None = None,
) -> None:
    """Pre-image, then replacement. Only ever called after every pre-flight passed."""
    stamp = (now or datetime.now(tz=UTC)).strftime("%Y%m%dT%H%M%SZ")
    backup = _unclobbered(
        plan.archive_path.with_name(f"archive-pre-backfill-{stamp}.jsonl")
    )
    try:
        atomic_write_text(backup, plan.raw)
        report.backup_path = str(backup)
        atomic_write_text(
            plan.archive_path, "".join(f"{line}\n" for line in plan.out_lines)
        )
        report.written = True
    except OSError as exc:
        report.errors.append(f"archive rewrite failed ({plan.archive_path}): {exc}")


def _reader_contracts_path(store_root: Path) -> Path:
    """The ``contracts.py`` of the checkout that will READ this store's archive.

    A store is served by the checkout it lives under: the live archive is
    ``<repo>/var/memories/memlife/episodic/archive.jsonl`` and the process that
    reads it is ``uvicorn omniagentos.api:app`` running from ``<repo>``. So
    climb from the store root until an ``omniagentos/memlife/contracts.py``
    appears, and that is the reader.

    Nothing here imports that file. It may be a DIFFERENT copy of a module this
    process already has in ``sys.modules``, and importing it would either return
    ours (proving nothing) or load a second, conflicting definition. It is read
    as source, which is the only honest way to ask "what can that tree parse".

    A store with no enclosing checkout — a scratch copy, a test fixture — falls
    back to the tree this module is running from, which is trivially consistent.
    The path is recorded in the report either way so the check names its subject.
    """
    checkout = _enclosing_checkout(store_root)
    if checkout is not None:
        return checkout / "omniagentos" / "memlife" / "contracts.py"
    return Path(__file__).resolve().with_name("contracts.py")


def _enclosing_checkout(store_root: Path) -> Path | None:
    """The nearest ancestor of ``store_root`` that is an omniagentos checkout."""
    for parent in store_root.resolve().parents:
        if (parent / "omniagentos" / "memlife" / "contracts.py").is_file():
            return parent
    return None


def _event_result_vocabulary(contracts_path: Path) -> frozenset[str]:
    """The ``EventResult`` members declared in ``contracts_path``, parsed as source."""
    module = ast.parse(
        contracts_path.read_text(encoding="utf-8"), filename=str(contracts_path)
    )
    for node in ast.walk(module):
        if isinstance(node, ast.ClassDef) and node.name == "EventResult":
            values = {
                statement.value.value
                for statement in node.body
                if isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            }
            if values:
                return frozenset(values)
    raise ValueError("no EventResult members found")


def _supersedes(later: Any, kept: Any) -> bool:
    """Should a later duplicate line replace the one already kept?

    Only ever asked about ids we cannot re-derive, where the surviving line is
    kept verbatim and the choice therefore IS the recorded history.

    Later wins, because the archive is append-ordered and the later capture of
    the same attempt is the more terminal one — with one exception: ``unknown``
    never displaces a recorded outcome. That exception is the point. The live
    duplicates are an in-flight capture (``result: unknown``) followed by the
    terminal capture, so "keep the first" kept the in-flight lie; but a bare
    "keep the last" would let a stray ``unknown`` re-bury a known result, which
    is the same defect class in the other direction. A line with no ``result``
    at all is treated as unknown for this purpose — an absent field is not a
    recorded outcome.
    """
    if later == kept:
        return False
    return isinstance(later, str) and later not in ("", "unknown")


def _unclobbered(path: Path) -> Path:
    """``path``, or the first free ``…-2``/``-3``/… variant of it.

    The stamp these paths are built from is second-resolution, and a repair run
    twice inside one second would otherwise write its pre-image backup over the
    pre-image of the first run — destroying the only copy of the original while
    appearing to have made one. This module already argues at length (see
    ``capture.capture_events``) that second-resolution timestamps collide in
    practice; the same caution applies to filenames derived from them. Nothing
    here ever overwrites an existing evidence file.
    """
    if not path.exists():
        return path
    for n in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"cannot find a free name beside {path}")


RECEIPT_DIRNAME = "backfill-receipts"


def _receipt_dir_for(store_root: Path) -> Path:
    """A receipts directory outside every memory root above ``store_root``.

    "Not inside the store" was the right instinct and one directory short of the
    right destination. The live store is ``var/memories/memlife``, so the old
    default landed receipts in ``var/memories/`` — which is a **memory root**:
    ``var/memories`` is the ``omniagentos-agent-memories`` source enrolled in the
    ``wis`` knowledge catalog, so every receipt would have been indexed as a
    note and returned by knowledge searches as if it were curated memory. An
    operational record of a one-shot repair is not knowledge about the estate.

    So: find the checkout the store is served by — the same
    :func:`_enclosing_checkout` the reader gate uses — and land in that
    checkout's ``var/backfill-receipts/``, which is outside every enrolled tree.
    A store with no enclosing checkout (a test fixture, a scratch copy) gets a
    ``backfill-receipts/`` directory beside it.

    Deliberately NOT "climb to the first ancestor named ``var``": on macOS the
    system temp directory is ``/private/var/folders/...``, so that rule sent a
    scratch copy's receipts to the system ``/var/backfill-receipts`` — caught by
    re-running the round-1 repro, which refused with ``[Errno 13] Permission
    denied: '/var/backfill-receipts'``. A path rule that can escape into the
    root filesystem is worse than the enrolled-tree collision it was fixing.
    """
    checkout = _enclosing_checkout(store_root)
    if checkout is not None:
        return checkout / "var" / RECEIPT_DIRNAME
    return store_root.resolve().parent / RECEIPT_DIRNAME


def _default_receipt_path(db: str, store: str | None, stamp: str) -> Path:
    """Under ``var/backfill-receipts/`` when there is a store, beside the DB when not.

    Not *inside* the store: the store layout is enumerated by
    ``MemlifeStore.list_candidates`` / ``list_lessons`` and a receipt is not a
    memlife artifact. And not in the store's parent either — see
    :func:`_receipt_dir_for`, that parent is an enrolled memory root.
    """
    if store:
        root = Path(store)
        return _receipt_dir_for(root) / f"{root.name}-backfill-receipt-{stamp}.json"
    return Path(f"{db}.backfill-receipt-{stamp}.json")


def _write_receipt(
    path: Path,
    *,
    stamp: str,
    argv: list[str],
    db: str,
    store: str | None,
    report: RescoreReport,
    archive: ArchiveReport | None,
    exit_code: int,
) -> None:
    """Persist what this run did, so "did the repair land" survives the terminal.

    This tool mutates production data once, by hand. Its only evidence was
    stdout, and stdout from a one-shot operator command is not evidence: it is
    not addressable, not timestamped, and gone with the scrollback. The receipt
    is written on FAILURE too — that is the case where it matters most.
    """
    payload = {
        "tool": "omniagentos.memlife.backfill",
        "at": stamp,
        "argv": argv,
        "db_path": str(Path(db).resolve()),
        "store_path": str(Path(store).resolve()) if store else None,
        "exit_code": exit_code,
        "committed": report.committed,
        "summary": report.as_dict(),
        "archive": archive.as_dict() if archive is not None else None,
        "mirror_written_ids": report.mirror_written_ids,
        "changes": report.changes,
    }
    atomic_write_text(path, json.dumps(payload, indent=2) + "\n")


def _evidence_ids(
    candidate_id: str,
    raw: Any,
    report: RescoreReport,
) -> list[str] | None:
    """Parse ``evidence_ids_json``, or return None and NAME the corruption.

    This used to be ``except ValueError: evidence_ids = []``, which made data
    corruption indistinguishable from evidence that was legitimately pruned:
    both landed in the aggregate ``unresolvable`` counter, neither was named by
    id, the candidate's score was overwritten with NULL, and the process exited
    0 with ``errors: []`` having nulled the largest candidate in the store.

    ``dream.run_dream_cycle`` quarantines and COUNTS unparseable input one file
    over. This is that discipline, in the module whose stated job is "repair
    from evidence": unreadable evidence is a reason to stop, not a reason to
    guess.
    """
    try:
        parsed = json.loads(raw or "[]")
    except ValueError as exc:
        report.errors.append(f"evidence_ids_json is unparseable for {candidate_id}: {exc}")
        return None
    if not isinstance(parsed, list):
        report.errors.append(
            f"evidence_ids_json for {candidate_id} is {type(parsed).__name__}, not a list"
        )
        return None
    out: list[str] = []
    for item in parsed:
        if not isinstance(item, str) or not item:
            report.errors.append(
                f"evidence_ids_json for {candidate_id} holds a non-id member: {item!r}"
            )
            return None
        out.append(item)
    return out


def _probe_dir_writable(directory: Path, label: str) -> str | None:
    """Probe ``directory`` with a real write; return the failure, or None.

    ``os.access`` is advisory and lies for root; the only honest test of "can I
    write here" is writing. A carrier discovered unwritable *after* another
    carrier has been updated is the split brain this module refuses to create,
    so every carrier gets probed in the pre-flight and none is taken on trust.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".backfill-write-probe"
        probe.write_text("probe\n", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"{label} is not writable ({directory}): {exc}"
    return None


def _probe_mirror_writable(store: MemlifeStore, report: RescoreReport) -> None:
    """Probe the candidates directory before any repair."""
    error = _probe_dir_writable(store.candidates_dir, "mirror")
    if error is not None:
        report.errors.append(error)


def main(argv: list[str] | None = None) -> int:
    """CLI. Exit codes are the contract; stdout is not evidence.

    - ``0`` — the repair landed completely, in every carrier it was asked to
      touch. Without ``--apply``: it *would* have landed — the same pre-flight
      ran, so this is a rehearsal result and not a guess.
    - ``1`` — something did not land, or (without ``--apply``) would not have.
      The SQLite transaction is **rolled back**, and every reason is named (by
      candidate id where there is one) on stderr and in ``errors``.
    - ``2`` — refused before doing anything: no active candidates, an ``--apply``
      that did not say which carriers it was allowed to write, or a receipt
      destination that cannot be written.

    No path exits by traceback. A ``sqlite3.Error`` — ``database is locked`` from
    a concurrent scheduler tick being the one that actually happens — is caught,
    named, rolled back and given a receipt like any other failure.

    Every ``--apply`` writes a JSON **receipt** — timestamp, argv, resolved DB
    and store paths, per-candidate before→after, the archive summary, the errors
    and the exit code — because this tool mutates production data once, by hand,
    and stdout from a one-shot operator command is not evidence. The receipt is
    written on the failure paths too.

    ``--apply`` used to default ``--store`` to ``None``, so the DEFAULT
    invocation repaired SQLite, wrote zero mirror files, reported ``errors: []``
    and exited 0 — the exact split brain this module's own comment says it
    refuses to create. Naming a carrier is now mandatory.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Re-score memlife candidates staged before salience scoring worked. "
            "Dry-run by default; pass --apply to write."
        )
    )
    parser.add_argument("--db", required=True, help="SQLite DB (candidates + attempts)")
    parser.add_argument(
        "--store",
        default=None,
        help="memlife store root (the filesystem mirror). Required with --apply "
        "unless --sqlite-only is given.",
    )
    parser.add_argument("--apply", action="store_true", help="write the new scores")
    parser.add_argument(
        "--sqlite-only",
        action="store_true",
        help="apply to SQLite ONLY and knowingly leave the OTHER THREE carriers "
        "un-repaired: the filesystem mirror keeps the old scores, and "
        "episodic/archive.jsonl is not re-derived at all (that needs --store). "
        "The carriers will disagree, nothing detects that, and the review queue "
        "reads the mirror. Only ever correct when they are being rebuilt "
        "separately.",
    )
    parser.add_argument(
        "--receipt",
        default=None,
        help=f"where --apply writes its JSON receipt (default: var/"
        f"{RECEIPT_DIRNAME}/ in the checkout the store is served by, which is "
        "outside every enrolled memory root; or beside the DB under "
        "--sqlite-only)",
    )
    args = parser.parse_args(argv)

    if args.apply and not args.store and not args.sqlite_only:
        print(
            "REFUSED: --apply needs --store <memlife store root> so both carriers "
            "are repaired together. Pass --sqlite-only if you really mean to "
            "update SQLite alone and leave the mirror stale.",
            file=sys.stderr,
        )
        return 2
    if args.store and args.sqlite_only:
        print(
            "REFUSED: --store and --sqlite-only contradict each other; pick one.",
            file=sys.stderr,
        )
        return 2

    started = datetime.now(tz=UTC)
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    receipt_path = (
        Path(args.receipt)
        if args.receipt
        else _unclobbered(_default_receipt_path(args.db, args.store, stamp))
    )

    def finish(
        rc: int,
        report: RescoreReport,
        archive: ArchiveReport | None,
    ) -> int:
        """Write the receipt for every --apply outcome, including the failures."""
        if not args.apply:
            return rc
        try:
            _write_receipt(
                receipt_path,
                stamp=started.isoformat().replace("+00:00", "Z"),
                argv=list(argv) if argv is not None else sys.argv[1:],
                db=args.db,
                store=args.store,
                report=report,
                archive=archive,
                exit_code=rc,
            )
            print(f"receipt: {receipt_path}", file=sys.stderr)
        except OSError as exc:
            print(
                f"ERROR: the repair finished (committed={report.committed}) but its "
                f"receipt could not be written to {receipt_path}: {exc}",
                file=sys.stderr,
            )
            return rc or 1
        return rc

    # Probe the receipt destination before anything is mutated: a repair whose
    # only record cannot be written is a repair nobody can audit afterwards.
    if args.apply:
        try:
            receipt_path.parent.mkdir(parents=True, exist_ok=True)
            probe = receipt_path.parent / f".{receipt_path.name}.probe"
            probe.write_text("probe\n", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            print(
                f"REFUSED: cannot write a receipt at {receipt_path}: {exc}",
                file=sys.stderr,
            )
            return 2

    # ---- PRE-FLIGHT: every carrier, before any of them is written ----------
    # --sqlite-only has no store, so it cannot repair the archive — its help
    # text says so, and that is the same knowing trade as the stale mirror.
    archive: ArchiveReport | None = None
    archive_plan: _ArchivePlan | None = None
    memlife_store: MemlifeStore | None = None
    report = RescoreReport(dry_run=not args.apply)
    plan = _RescorePlan()
    conn: sqlite3.Connection | None = None
    try:
        if args.store:
            memlife_store = MemlifeStore(args.store)
            report.store_path = str(memlife_store.root)
            archive, archive_plan = _plan_archive(
                memlife_store, args.db, dry_run=not args.apply
            )

        conn = sqlite3.connect(args.db)
        report, plan = _plan_rescore(
            conn,
            args.db,
            store=memlife_store,
            now=started,
            dry_run=not args.apply,
        )
        blocked = bool(report.errors) or bool(archive is not None and archive.errors)

        # ---- WRITE: only now, and only if nothing said no ------------------
        if args.apply and not blocked:
            # SQLite first and uncommitted: the UPDATEs take the write lock, so
            # a concurrent writer stops us here, before any file has been
            # touched. Then the archive, which is atomic and has a pre-image.
            # The mirror is last, closest to the commit, because it is the one
            # carrier that cannot be rolled back.
            _update_sqlite(conn, plan)
            if archive_plan is not None and archive is not None:
                _commit_archive(archive_plan, archive, now=started)
            if archive is None or not archive.errors:
                _write_mirror(report, plan)
            if report.errors or (archive is not None and archive.errors):
                conn.rollback()
            else:
                conn.commit()
                report.committed = True
        elif args.apply and conn is not None:
            conn.rollback()
    except sqlite3.Error as exc:
        # The live scheduler writes to this database. `database is locked` used
        # to escape main() entirely — a raw traceback, no named reason, and no
        # receipt on the one path where the module's own docstring says the
        # receipt "matters most". It is an outcome of the repair, so it is
        # reported like any other outcome.
        report.errors.append(
            f"the database refused the repair ({type(exc).__name__}: {exc}); "
            "another writer — most likely a scheduler tick — held it. Nothing "
            "was committed; re-run when the database is free."
        )
        if conn is not None:
            try:
                conn.rollback()
            except sqlite3.Error:  # pragma: no cover - rollback of a dead handle
                pass
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - close of a dead handle
                pass

    payload = report.as_dict()
    if archive is not None:
        payload["archive"] = archive.as_dict()
    print(json.dumps(payload, indent=2))

    errors = list(report.errors) + (list(archive.errors) if archive is not None else [])
    if report.examined == 0 and not errors:
        # "Nothing to do" and "nothing was there" are different facts.
        print("REFUSED: no active candidates found — check --db", file=sys.stderr)
        return finish(2, report, archive)
    if errors:
        for line in errors:
            print(f"ERROR: {line}", file=sys.stderr)
        print(
            f"FAILED: {len(errors)} error(s); {_what_landed(report, archive, args.apply)}",
            file=sys.stderr,
        )
        return finish(1, report, archive)
    return finish(0, report, archive)


def _what_landed(
    report: RescoreReport,
    archive: ArchiveReport | None,
    applied: bool,
) -> str:
    """Name every carrier that WAS written, on the path that says we failed.

    "SQLite rolled back, nothing repaired" was printed by runs that had already
    permanently rewritten ``archive.jsonl`` — a failure message that contradicts
    what is on disk is worse than no message, because it is the message the
    operator will act on.
    """
    parts = [f"SQLite {'rolled back' if applied else 'untouched'}"]
    if archive is not None and archive.written:
        parts.append(
            f"archive.jsonl WAS REWRITTEN (pre-image: {archive.backup_path})"
        )
    else:
        parts.append("archive untouched")
    if report.mirror_diverged_ids:
        parts.append(
            f"{len(report.mirror_diverged_ids)} mirror file(s) LEFT AHEAD of the "
            "database"
        )
    elif report.mirror_restored:
        parts.append(f"{report.mirror_restored} mirror file(s) written then restored")
    elif report.mirror_written and not report.committed:
        parts.append(f"{report.mirror_written} mirror file(s) WRITTEN")
    else:
        parts.append("mirror untouched")
    return "; ".join(parts) + "."


__all__ = [
    "ACTIVE_STATUSES",
    "ArchiveReport",
    "RescoreReport",
    "rederive_archive",
    "rescore_candidates",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
