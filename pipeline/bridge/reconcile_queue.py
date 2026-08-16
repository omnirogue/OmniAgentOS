#!/usr/bin/env python3
"""Reconcile the artifacts on disk with the ids `state/queue.json` publishes.

WHAT THIS REPAIRS. Publication is ledger-derived: `integration.rebuild_queue()`
iterates `ledger.status`, which `LedgerView.build()` fills from a fixed
event->status map, so an artifact is offered to the Implementer **if and only
if** it carries an event in that map. Measured at 1bb016c3f: 63 non-terminal
findings sat on disk and in no queue — 28 with no ledger event at all, 35 whose
only events (`observed`, `published`, `finding`, `corroborated`, ...) are
outside the map. The work was already filed; nothing ever offered it.

`bridge/file_finding.py` closes the SOURCE. This tool drains the backlog that
source already produced, and then serves as the standing check that the class
has not returned.

THE RULES IT OBEYS, and why each one is not negotiable:

* **The ledger is append-only and IS the history (CONTRACT §5).** This tool
  never rewrites, truncates, or reorders a line. It only appends, and only
  through `bridge/ledger_write.py` — the one transport that locks, checks short
  writes, and fsyncs.
* **No fabricated timestamps.** Each appended event carries the CURRENT time,
  never the artifact's `created_at`. A back-dated event is a false record, and
  anti-starvation aging would silently promote items on the strength of it.
  (Ordering is unharmed: `rebuild_queue` sorts by the artifact's `created_at`
  read from disk, not by the ledger timestamp.)
* **Dry-run by default; `--apply` is required.** An append cannot be undone, so
  an accidental invocation must be a report, not a mutation.
* **Only events inside the status map.** Appending `observed` — or adding
  `observed` to the map — would either leave the artifact invisible while
  reporting success, or publish large volumes of non-claimable bookkeeping.
  The per-kind event is the artifact's own canonical first event.
* **Terminal artifacts stay out.** A `merged`/`completed`/`rejected`/`closed`
  id is correctly absent from the queue; publishing it would be a regression,
  not a repair.
* **Parked artifacts are reported, not touched.** Parking ends only with an
  authenticated `unparked` event (CONTRACT §9); they are listed as `held` so
  the exclusion is visible rather than silent.
* **The disk-side tombstones are read BEFORE the queue, not after.** An id the
  queue OFFERS while a live `rejected/` or `parked/` marker sits on disk is a
  CONTRADICTION between the two surfaces — the queue is offering work the
  rejection archive says is settled — and it is reported as `contradicted`
  with a non-zero exit, never absorbed into `published`. Measured on a copy of
  a prior queue snapshot: 75 published ids carried a live tombstone (35
  rejected-marker ids at status `open`, hence CLAIMABLE; 39 parked-marker ids
  already withheld at status `parked`; 1 parked-marker id at status `open`).
  Checking the marker only AFTER an `ident in queue_ids` short-circuit means
  the exact condition this tool exists to detect is invisible to it.
* **It refuses on an unreadable ledger.** A torn-tail read with zero events
  would make every artifact on disk look like an orphan and trigger a mass
  append. Fail closed instead.
* **It refuses on an unreadable TOMBSTONE, too.** A `rejected/`/`parked/`
  marker that will not parse is an instrument error, not an absence: it might
  be a live tombstone. Exiting 0 on it would be byte-identical to a clean
  queue — the favourable-absence class. `file_proposal.check_identity` and
  `file_finding.check_finding_identity` both refuse with could-not-run on this
  identical condition; so does this tool, and it appends nothing on that run.

It does NOT touch `integration.py`: the publication rule is correct, and making
`rebuild_queue` union the filesystem would create a second source of truth
against CONTRACT §5 and would publish the terminal artifacts that are
deliberately excluded.

Usage:
    reconcile_queue.py --queue /abs/path/to/var/loopqueue            # report only
    reconcile_queue.py --queue /abs/path/to/var/loopqueue --apply    # append events
    reconcile_queue.py --queue /abs/path/to/var/loopqueue --json     # machine-readable

Exit codes: 0 nothing to reconcile (or `--apply` drained it) · 1 orphans remain
(the standing invariant: run it without `--apply` and a non-zero exit means the
publication surface is missing filed work) · 2 could not run (unreadable ledger,
unreadable tombstone, bad arguments) · 3 the two surfaces CONTRADICT each other
(the queue offers an id disk says is settled).

Precedence when several hold at once, most-severe first: 2 (the scan could not
be trusted) > 3 (the queue is offering settled work — a live hazard) > 1 (filed
work is not offered — a starvation, not a hazard). Every class is itemised in
the report regardless of which one supplies the exit code.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bridge import integration as I  # noqa: E402
from bridge import publish_queue as PQ  # noqa: E402
from bridge.file_finding import (  # noqa: E402
    PRESENCE_ABSENT,
    PRESENCE_UNDETERMINED,
    marker_presence,
)
from bridge.file_proposal import _expired  # noqa: E402
from bridge.ledger_write import LedgerAppendError, append_event  # noqa: E402

EXIT_OK = 0
EXIT_ORPHANS_REMAIN = 1
EXIT_COULD_NOT_RUN = 2
EXIT_CONTRADICTED = 3

ACTOR = "reconcile_queue.py"
ROLE = "external"

#: The reconciliation marker. Lane B's events are NOT revertible by a code
#: revert (the ledger is append-only), so every one of them says in its own
#: detail what it is, and a later reader can identify and discount them.
RECONCILE_REASON = (
    "reconciliation: artifact was on disk with no status-producing ledger event, "
    "so queue publication (which is ledger-derived) never offered it. Event "
    "appended at the CURRENT time by bridge/reconcile_queue.py — it is NOT a "
    "record of when the artifact was created."
)

#: The canonical first event per artifact kind. Every value here MUST be inside
#: the event->status map in `integration.LedgerView.build`, and must not be a
#: terminal event — `test_reconcile_queue.py` proves that BY EXECUTION against
#: the real LedgerView rather than by restating the map here, because a second
#: copy of that map is exactly how this defect returns.
STATUS_EVENT_BY_KIND = {
    "finding": "found",
    "inquiry": "inquired",
    "proposal": "proposed",
    "candidate": "submitted",
}

#: Claimable kinds (CONTRACT §6: "candidates are not claimed"). Candidates are
#: reconcilable too, but only when named explicitly with `--kinds`: an absent
#: candidate is a gate-surface question, while the measured defect — and the
#: thing an Implementer is starved of — is claimable work.
DEFAULT_KINDS = ("finding", "inquiry", "proposal")

_TERMINAL = ("merged", "completed", "rejected", "closed")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stem(ident: str) -> str:
    return ident.replace(":", "_")


@dataclass
class Orphan:
    ident: str
    kind: str
    path: Path
    title: str
    prior_events: list[str]
    event: str          # the status-producing event that would be appended

    def as_dict(self) -> dict:
        return {"id": self.ident, "kind": self.kind, "path": str(self.path),
                "title": self.title, "prior_events": self.prior_events,
                "event": self.event}


@dataclass
class Contradiction:
    """The queue OFFERS an id whose disk-side tombstone says it is settled.

    Not an orphan (nothing to append) and emphatically not `published`: it is
    the rejection archive and the publication surface disagreeing, with the
    queue on the permissive side. `queue_status` is carried because it is what
    separates a live hazard (`open`/`admitted`/… — an Implementer can claim
    rejected work) from a disagreement of mechanism only.
    """

    ident: str
    kind: str
    marker: str        # MARKER_REJECTED | MARKER_PARKED
    queue_status: str

    def as_dict(self) -> dict:
        return {"id": self.ident, "kind": self.kind, "marker": self.marker,
                "queue_status": self.queue_status}


@dataclass
class Reconciliation:
    """What the disk says versus what publication would offer."""

    orphans: list[Orphan] = field(default_factory=list)
    published: list[str] = field(default_factory=list)
    terminal: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)      # parked, deliberately withheld
    contradicted: list[Contradiction] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)  # kind with no canonical event
    unreadable: list[str] = field(default_factory=list)  # rejected/parked marker fails to parse
    applied: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> dict:
        return {
            "orphans": len(self.orphans),
            "published": len(self.published),
            "terminal_excluded": len(self.terminal),
            "held_parked": len(self.held),
            "contradicted": len(self.contradicted),
            "unsupported_kind": len(self.unsupported),
            "unreadable_marker": len(self.unreadable),
            "applied": len(self.applied),
            "error": self.error,
        }


def _artifact_title(path: Path) -> str:
    try:
        art = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return art.get("title", "") if isinstance(art, dict) else ""


def _artifact_paths(root: Path, kinds: tuple[str, ...]) -> dict[str, tuple[str, Path]]:
    """id -> (kind, path) for every artifact file on disk in the wanted kinds.

    The directory<->kind mapping is `integration.kinds_from_disk`'s, reused so
    the two readers cannot drift on what "an artifact on disk" means.
    """
    dirs = {"inquiry": "inquiries", "finding": "findings",
            "proposal": "proposals", "candidate": "candidates"}
    out: dict[str, tuple[str, Path]] = {}
    for kind in kinds:
        d = root / dirs[kind]
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.json")):
            out[p.stem.replace("_", ":", 1)] = (kind, p)
    return out


#: Marker classifications returned by `_marker_status`.
MARKER_CLEAR = "clear"
MARKER_PARKED = "parked"
MARKER_REJECTED = "rejected"
MARKER_UNREADABLE = "unreadable"


def _marker_status(root: Path, ident: str) -> str:
    """Classify `ident` against the `rejected/` / `parked/` MARKER FILES.

    These are a disk-side tombstone mechanism, independent of the ledger —
    `file_proposal.check_identity` and `file_finding.check_finding_identity`
    both refuse to (re-)file an id carrying one. `scan()` previously read the
    ledger only (`ledger.terminal` / `ledger.status` / `ledger.parked`), so an
    artifact that was rejected or parked via a MARKER but never picked up a
    matching terminal ledger event read as an ordinary orphan — measured live:
    768 rejected/ markers on disk, 64 with no terminal ledger event, 36 of
    those still sitting in a scanned kind directory. `--apply` against that
    reading republishes definitively-rejected work, defeating the whole
    rejection archive (the "favourable absence" defect class: absence of a
    ledger event read as "eligible" rather than "already settled").

    The TTL rule is the SAME rule `file_proposal.py` / `file_finding.py` use —
    `_expired` is imported, not re-derived, so the policy cannot drift between
    the writer and this reconciler. Parking never decays by TTL (CONTRACT §9):
    only an authenticated `unparked` event ends it, so a parked/ marker is
    always live regardless of any `expires_at` it happens to carry.
    """
    parked_marker = root / "parked" / f"{_stem(ident)}.json"
    rejected_marker = root / "rejected" / f"{_stem(ident)}.json"

    # `marker_presence` (shared with file_finding.py, imported not re-derived,
    # so the two cannot drift): neither `Path.exists()` nor `os.path.lexists()`
    # can express "cannot tell". `exists()` follows symlinks, so a dangling
    # tombstone read as absent; `lexists()` fixed that but swallows OSError, so
    # a marker under an unsearchable directory STILL read as absent. Both are
    # the same favourable-absence shape this function exists to refuse. Now an
    # undeterminable marker is UNREADABLE — a halt — and only a genuine
    # ENOENT is absence.
    parked_presence = marker_presence(parked_marker)
    if parked_presence == PRESENCE_UNDETERMINED:
        return MARKER_UNREADABLE
    if parked_presence != PRESENCE_ABSENT:
        try:
            record = json.loads(parked_marker.read_text())
        except (OSError, json.JSONDecodeError):
            return MARKER_UNREADABLE
        if not isinstance(record, dict):
            return MARKER_UNREADABLE
        return MARKER_PARKED

    rejected_presence = marker_presence(rejected_marker)
    if rejected_presence == PRESENCE_UNDETERMINED:
        return MARKER_UNREADABLE
    if rejected_presence != PRESENCE_ABSENT:
        try:
            record = json.loads(rejected_marker.read_text())
        except (OSError, json.JSONDecodeError):
            return MARKER_UNREADABLE
        if not isinstance(record, dict):
            return MARKER_UNREADABLE
        if _expired(record.get("expires_at")):
            return MARKER_CLEAR  # an EXPIRED rejection is inert, not a halt
        return MARKER_REJECTED

    return MARKER_CLEAR


@contextlib.contextmanager
def _reconcile_lock(root: Path):
    """Serialize scan-decide-append across CONCURRENT `--apply` invocations.

    `bridge/ledger_write.append_event` locks `locks/ledger.lock` only around
    the physical write, so two `--apply` processes can both SCAN, both decide
    the same id is an orphan, and both append their own `found` event — each
    append is individually well-formed and durably written, but the id now
    carries two status-producing events for one on-disk artifact. Holding
    THIS lock around scan+decide+append (not just the write) makes the whole
    sequence one critical section: the second process cannot even start its
    scan until the first has fully landed (or failed) its append, so its scan
    sees the id as already published and skips it. A dedicated lock file
    (`locks/reconcile.lock`), not `locks/ledger.lock` itself, because holding
    `ledger.lock` here would deadlock this process's own later call into
    `append_event` (`flock` locks are per open-file-description, not
    per-process, so a second `open()+flock()` on the same path within one
    process blocks on its own outer hold).
    """
    locks_dir = root / "locks"
    locks_dir.mkdir(mode=0o755, exist_ok=True)
    lock_path = locks_dir / "reconcile.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def scan(root: Path, kinds: tuple[str, ...] = DEFAULT_KINDS) -> Reconciliation:
    """Classify every on-disk artifact against the queue publication WOULD emit.

    The queue is computed through `publish_queue.publish_from(..., write=False)`
    — the one emitter, reused and not re-derived, and with `write=False` so a
    read-only scan never races the 300s publisher for `state/queue.json`.
    """
    rec = Reconciliation()

    unknown = [k for k in kinds if k not in STATUS_EVENT_BY_KIND]
    if unknown:
        rec.error = f"unknown kind(s): {', '.join(sorted(unknown))}"
        return rec

    # Disk snapshot FIRST, ledger view SECOND. The publisher ticks every 300s
    # and the gate loop every 60s on a live host, so a terminal event can land
    # in the window between the two reads. Reading the ledger first would make
    # it stale relative to what we then find on disk: a terminal event that
    # lands after the (stale) ledger read but before the disk walk is missed
    # entirely, while its artifact is still sitting on disk, and `apply()`
    # would append an event over an already-settled disposition. Building the
    # ledger AFTER the disk snapshot guarantees it is at least as fresh.
    artifacts = _artifact_paths(root, kinds)

    ledger = I.LedgerView.build(root)
    ledger.kinds.update(I.kinds_from_disk(root))
    queue, message = PQ.publish_from(root, ledger, write=False)
    if queue is None:
        # An end-to-end unreadable ledger makes EVERY artifact look orphaned.
        # Appending against that reading would be a mass write on a false
        # premise, so refuse rather than "repair".
        rec.error = f"refusing to reconcile: {message}"
        return rec

    # id -> status, not a bare set of ids: `status` is what separates an id the
    # queue is OFFERING from one it is already withholding, and a contradiction
    # can only be graded against that.
    queue_status: dict[str, str] = {
        i.get("id"): str(i.get("status") or "")
        for i in queue.get("items", []) if isinstance(i, dict) and i.get("id")
    }
    events_by_id: dict[str, list[str]] = {}
    for ev in ledger.events:
        ident, event = ev.get("id"), ev.get("event")
        if ident and event:
            events_by_id.setdefault(ident, []).append(event)

    for ident, (kind, path) in artifacts.items():
        # MARKERS FIRST, queue membership second. The reverse order (which this
        # commit repairs) makes the contradiction it exists to detect
        # unreachable: an id that is BOTH in the queue and tombstoned on disk
        # short-circuits into `published` and is reported as healthy.
        marker = _marker_status(root, ident)
        if marker == MARKER_UNREADABLE:
            # Fail closed, same as file_proposal.py / file_finding.py: a
            # marker we cannot read might be a live tombstone, and reconciling
            # past it defeats the CONTRACT §4 re-raise trap invisibly. The
            # caller turns a non-empty `unreadable` into EXIT_COULD_NOT_RUN and
            # appends nothing — an unreadable tombstone is an instrument error,
            # and an instrument error must not be reportable as a clean queue.
            rec.unreadable.append(ident)
            continue
        if marker in (MARKER_PARKED, MARKER_REJECTED):
            status = queue_status.get(ident)
            if status is not None and status != "parked":
                # The queue is offering an id the archive says is settled.
                rec.contradicted.append(Contradiction(
                    ident=ident, kind=kind, marker=marker,
                    queue_status=status or "<no status>"))
                continue
            # Withheld by BOTH surfaces (or absent from the queue entirely):
            # consistent, and still counted so the exclusion is not silent.
            if marker == MARKER_PARKED:
                rec.held.append(ident)
            else:
                rec.terminal.append(ident)
            continue
        if ident in queue_status:
            rec.published.append(ident)
            continue
        if ident in ledger.terminal or ledger.status.get(ident) in _TERMINAL:
            rec.terminal.append(ident)
            continue
        if ident in ledger.parked:
            # Reported, never touched: parking ends only with an authenticated
            # `unparked` event (CONTRACT §9).
            rec.held.append(ident)
            continue
        event = STATUS_EVENT_BY_KIND.get(kind)
        if not event:
            rec.unsupported.append(ident)
            continue
        rec.orphans.append(Orphan(
            ident=ident, kind=kind, path=path, title=_artifact_title(path),
            prior_events=sorted(set(events_by_id.get(ident, []))), event=event,
        ))
    return rec


def apply(root: Path, rec: Reconciliation) -> Reconciliation:
    """Append one status-producing event per orphan. Append-only, current time.

    Stops at the first append failure: the ledger cannot be rolled back, so the
    honest outcome is a partial reconciliation that says exactly how far it got.
    """
    for orphan in rec.orphans:
        event = {
            "ts": _now(),
            "role": ROLE,
            "event": orphan.event,
            "id": orphan.ident,
            "actor": ACTOR,
            "detail": {
                "reason": RECONCILE_REASON,
                "reconciled": True,
                "kind": orphan.kind,
                "title": orphan.title,
                "prior_events": orphan.prior_events,
            },
        }
        try:
            append_event(root, event)
        except (LedgerAppendError, OSError) as exc:
            rec.error = (
                f"ledger append failed on {orphan.ident[:19]}… after "
                f"{len(rec.applied)} event(s) landed: {exc}. The ledger is "
                "append-only, so what landed stays; re-run to continue from here "
                "(already-published ids are skipped)."
            )
            return rec
        rec.applied.append(orphan.ident)
    return rec


def render(rec: Reconciliation, applied: bool) -> str:
    lines: list[str] = []
    if rec.error:
        lines.append(f"ERROR: {rec.error}")
    verb = "APPENDED" if applied else "WOULD APPEND (dry-run — pass --apply to write)"
    lines.append(
        f"{verb}: {len(rec.applied) if applied else len(rec.orphans)} event(s); "
        f"already published {len(rec.published)}, terminal (correctly excluded) "
        f"{len(rec.terminal)}, parked (held, untouched) {len(rec.held)}, "
        f"CONTRADICTED (queue offers a tombstoned id) {len(rec.contradicted)}, "
        f"unreadable marker (refused, untouched) {len(rec.unreadable)}"
    )
    for bad in rec.contradicted:
        lines.append(
            f"  ! {bad.ident[:19]}… {bad.kind:9s} queue_status={bad.queue_status} "
            f"but a live {bad.marker} marker is on disk — the queue is offering "
            f"work the rejection archive settled")
    for ident in rec.unreadable:
        lines.append(
            f"  ? {ident[:19]}… rejected/parked marker exists but will not parse — "
            f"fix or remove it; an unreadable tombstone is an instrument error, "
            f"not an absence")
    for orphan in rec.orphans:
        mark = "+" if orphan.ident in rec.applied else ("." if applied else "-")
        prior = ",".join(orphan.prior_events) or "<no ledger event at all>"
        lines.append(f"  {mark} {orphan.ident[:19]}… {orphan.kind:9s} "
                     f"{orphan.event:9s} prior=[{prior}] {orphan.title[:70]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queue", required=True, type=Path,
                    help="absolute path to the loop queue (var/loopqueue)")
    ap.add_argument("--apply", action="store_true",
                    help="actually append the events (default: report only). Ledger "
                         "appends cannot be undone, which is why this is opt-in.")
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS),
                    help=f"comma-separated artifact kinds (default: {','.join(DEFAULT_KINDS)}; "
                         f"known: {','.join(sorted(STATUS_EVENT_BY_KIND))})")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable report")
    args = ap.parse_args(argv)

    root: Path = args.queue
    if not root.is_absolute():
        print("REFUSE[2] queue: --queue must be an absolute path — a relative path "
              "reconciles whatever queue the current directory happens to sit next "
              "to, and these writes cannot be undone.", file=sys.stderr)
        return EXIT_COULD_NOT_RUN
    if not root.is_dir():
        print(f"REFUSE[2] queue: no such queue directory: {root}", file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    # `--apply` runs scan-decide-append as ONE critical section (`_reconcile_lock`)
    # so two concurrent `--apply` invocations cannot both scan a stale disk state
    # and both append a `found`/`proposed`/... event for the same orphan. A dry
    # run appends nothing, so it is not locked.
    if args.apply:
        with _reconcile_lock(root):
            rec = scan(root, kinds)
            if not (rec.error or rec.unreadable) and rec.orphans:
                rec = apply(root, rec)
    else:
        rec = scan(root, kinds)
    if rec.error or rec.unreadable:
        # An unreadable tombstone refuses the WHOLE run, before any append: the
        # scan cannot say whether that id is settled, so it cannot say the queue
        # is clean either, and exiting 0 here would be byte-identical to a
        # healthy queue. Same fail-closed rule as file_proposal.py /
        # file_finding.py, and the report names the file to fix.
        if args.json:
            print(json.dumps({"summary": rec.summary(), "orphans": [],
                              "contradicted": [c.as_dict() for c in rec.contradicted],
                              "unreadable": list(rec.unreadable)}, indent=2))
        print(render(rec, applied=False), file=sys.stderr)
        return EXIT_COULD_NOT_RUN

    if args.json:
        print(json.dumps({"summary": rec.summary(),
                          "orphans": [o.as_dict() for o in rec.orphans],
                          "contradicted": [c.as_dict() for c in rec.contradicted],
                          "unreadable": list(rec.unreadable)}, indent=2))
    else:
        print(render(rec, applied=args.apply))

    # Most severe first: an untrustworthy scan, then the queue offering settled
    # work (a live hazard), then filed work nobody is offered.
    if rec.error or rec.unreadable:
        return EXIT_COULD_NOT_RUN
    if rec.contradicted:
        return EXIT_CONTRADICTED
    if not rec.orphans:
        return EXIT_OK
    if args.apply and len(rec.applied) == len(rec.orphans):
        return EXIT_OK
    return EXIT_ORPHANS_REMAIN


if __name__ == "__main__":
    raise SystemExit(main())
