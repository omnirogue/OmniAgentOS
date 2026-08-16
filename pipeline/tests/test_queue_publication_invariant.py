"""The standing invariant: every non-terminal artifact on disk is PUBLISHED.

This is what stops the class returning silently once the backlog is drained.
The class: `state/queue.json` is rebuilt from the ledger, so an artifact whose
ledger event is missing (or sits outside the event->status map) is on disk,
healthy-looking, and offered to nobody. It failed silently for 63 findings
because nothing anywhere compared the two surfaces.

WHAT IS PINNED HERE, and what is deliberately not:

* The invariant itself — on-disk non-terminal artifacts versus ids in the
  published queue, per kind — and the requirement that a violation is reported
  with an ITEMISED difference. A count alone sends the next reader to grep.
* Its two exclusions, each with a test: terminal artifacts (correctly absent)
  and parked ones (deliberately withheld, CONTRACT §9), so neither exclusion is
  silent.
* That the invariant is FALSIFIABLE: it is proven to go red on a queue with an
  orphan, not merely green on a healthy one. A check that cannot fail is not a
  check.

These tests are HERMETIC — each builds its own loopqueue under `tmp_path`. They
deliberately do NOT assert on the live `var/loopqueue`: a suite that fails on
the ambient state of another process's queue reports the operator's backlog as
a candidate defect, and the builder cannot drain that backlog from a worktree
(the ledger is append-only and live). The live surface is checked by the same
mechanism, run as a command, which exits 1 while any orphan remains:

    pipeline/bridge/reconcile_queue.py --queue /abs/.../var/loopqueue

`test_the_standing_command_is_red_while_an_orphan_remains` below pins that exit
code, so the ops check and this suite cannot drift apart.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "bridge"))

from bridge import reconcile_queue as RQ  # noqa: E402
from bridge.canonical import content_id  # noqa: E402

KIND_DIR = {"finding": "findings", "inquiry": "inquiries",
            "proposal": "proposals", "candidate": "candidates"}


class QueuePublicationViolation(AssertionError):
    """Raised with the difference itemised — id, kind, and prior events."""


def assert_every_artifact_is_published(root: Path,
                                       kinds: tuple[str, ...] = RQ.DEFAULT_KINDS) -> None:
    """THE INVARIANT. Raises with an itemised difference, never a bare count.

    It reads EVERY class `scan()` can report that means "the two surfaces do not
    agree", not just `orphans`. Gating on the orphan list alone inherits the
    favourable value from every other bucket: an unreadable tombstone (the
    instrument could not measure that id) and a contradiction (the queue OFFERS
    an id disk says is settled) would both return green here — silently, and
    with the more dangerous of the two directions being the silent one.
    """
    rec = RQ.scan(root, kinds)
    if rec.error:
        raise QueuePublicationViolation(
            f"the invariant could not be measured: {rec.error} — an unmeasured "
            "invariant must not report green")
    if rec.unreadable:
        raise QueuePublicationViolation(
            f"the invariant could not be measured for {len(rec.unreadable)} "
            f"artifact(s): a rejected/ or parked/ marker exists but will not "
            f"parse, so whether these are settled is UNKNOWN: "
            + ", ".join(sorted(rec.unreadable)) +
            " — REMEDY: fix or remove the marker file; an unreadable tombstone "
            "is an instrument error, not an absence.")
    if rec.contradicted:
        lines = [
            f"{len(rec.contradicted)} id(s) are OFFERED by the published queue "
            f"while a live tombstone sits on disk — the rejection archive and "
            f"the publication surface disagree, with the queue on the "
            f"permissive side:",
        ]
        for bad in rec.contradicted:
            lines.append(f"  - {bad.ident} [{bad.kind}] queue_status="
                         f"{bad.queue_status} live_marker={bad.marker}")
        lines.append("REMEDY: settle the disagreement at its source — record the "
                     "terminal/parked ledger event the marker implies, or remove "
                     "an obsolete marker. Publishing rejected work is a hazard, "
                     "not a backlog.")
        raise QueuePublicationViolation("\n".join(lines))
    if not rec.orphans:
        return
    by_kind: dict[str, int] = {}
    for orphan in rec.orphans:
        by_kind[orphan.kind] = by_kind.get(orphan.kind, 0) + 1
    lines = [
        f"{len(rec.orphans)} non-terminal artifact(s) on disk are absent from the "
        f"published queue: " + ", ".join(f"{k}={n}" for k, n in sorted(by_kind.items())),
        "publication is ledger-derived, so these are filed work nobody is offered:",
    ]
    for orphan in rec.orphans:
        prior = ",".join(orphan.prior_events) or "<no ledger event at all>"
        lines.append(f"  - {orphan.ident} [{orphan.kind}] prior=[{prior}] "
                     f"{orphan.title[:70]}")
    lines.append("REMEDY: bridge/reconcile_queue.py --queue <abs queue> --apply "
                 "(dry-run first), and file new findings with bridge/file_finding.py "
                 "so the artifact and its event land together.")
    raise QueuePublicationViolation("\n".join(lines))


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "loopqueue"
    for sub in ("findings", "inquiries", "proposals", "candidates", "state", "locks"):
        (q / sub).mkdir(parents=True)
    (q / "ledger.jsonl").write_text("")
    (q / "state" / "budget.json").write_text(json.dumps({"wip_cap": 4}))
    return q


def write_artifact(queue: Path, symptom: str, kind: str = "finding") -> str:
    payload = {"symptom": symptom} if kind == "finding" else {"note": symptom}
    ident = content_id(payload)
    art = {"contract": "v1.1", "kind": kind, "id": ident, "title": symptom[:80],
           "created_at": "2026-01-01T00:00:00Z",
           "producer": {"role": "external", "actor": "test"}, "payload": payload}
    (queue / KIND_DIR[kind] / f"{ident.replace(':', '_')}.json").write_text(
        json.dumps(art, indent=2))
    return ident


def append(queue: Path, ident: str, event: str) -> None:
    line = json.dumps({"ts": "2026-01-01T00:00:01Z", "role": "external", "event": event,
                       "id": ident, "actor": "test", "detail": {}},
                      separators=(",", ":")) + "\n"
    with open(queue / "ledger.jsonl", "a", encoding="utf-8") as fh:
        fh.write(line)


# --------------------------------------------------------------------------
# the invariant holds on a healthy queue
# --------------------------------------------------------------------------

def test_empty_queue_satisfies_the_invariant(queue):
    assert_every_artifact_is_published(queue)


def test_properly_filed_artifacts_satisfy_the_invariant(queue):
    for kind, event in (("finding", "found"), ("inquiry", "inquired"),
                        ("proposal", "proposed")):
        append(queue, write_artifact(queue, f"a properly filed {kind}", kind=kind), event)
    assert_every_artifact_is_published(queue)


def test_reconciled_backlog_satisfies_the_invariant(queue):
    """Lane C is green only after lane B drains the backlog — pinned end to end."""
    write_artifact(queue, "an orphan with no ledger event at all")
    append(queue, write_artifact(queue, "an orphan whose only event is observed"), "observed")
    with pytest.raises(QueuePublicationViolation):
        assert_every_artifact_is_published(queue)

    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        assert RQ.main(["--queue", str(queue), "--apply"]) == RQ.EXIT_OK
    assert_every_artifact_is_published(queue)


# --------------------------------------------------------------------------
# ... and FAILS, itemised, when it is broken
# --------------------------------------------------------------------------

def test_violation_is_itemised_not_a_bare_count(queue):
    ident = write_artifact(queue, "a finding that never got its found event")
    with pytest.raises(QueuePublicationViolation) as exc:
        assert_every_artifact_is_published(queue)
    message = str(exc.value)
    assert ident in message, "an id-less report sends the next reader to grep"
    assert "finding=1" in message
    assert "<no ledger event at all>" in message
    assert "REMEDY:" in message


def test_violation_names_the_non_status_event_it_found(queue):
    ident = write_artifact(queue, "a finding whose only event is observed")
    append(queue, ident, "observed")
    with pytest.raises(QueuePublicationViolation) as exc:
        assert_every_artifact_is_published(queue)
    assert "prior=[observed]" in str(exc.value)


def test_removing_a_published_id_from_the_queue_makes_it_fail(queue):
    """The plan's own success metric: the invariant fails with an itemised
    difference when an artifact is removed from the queue."""
    ident = write_artifact(queue, "a finding that is published until its event is not read")
    append(queue, ident, "found")
    assert_every_artifact_is_published(queue)

    (queue / "ledger.jsonl").write_text("")  # the queue no longer carries it
    with pytest.raises(QueuePublicationViolation) as exc:
        assert_every_artifact_is_published(queue)
    assert ident in str(exc.value)


def test_counts_are_reported_per_kind(queue):
    write_artifact(queue, "an orphan finding of the first kind")
    write_artifact(queue, "an orphan finding of the second kind")
    write_artifact(queue, "an orphan inquiry", kind="inquiry")
    with pytest.raises(QueuePublicationViolation) as exc:
        assert_every_artifact_is_published(queue)
    assert "finding=2" in str(exc.value) and "inquiry=1" in str(exc.value)


# --------------------------------------------------------------------------
# the exclusions are deliberate, and neither of them is silent
# --------------------------------------------------------------------------

def test_terminal_artifacts_do_not_violate_the_invariant(queue):
    for event in ("merged", "completed", "rejected", "closed"):
        ident = write_artifact(queue, f"a finding terminalised by {event}")
        append(queue, ident, "found")
        append(queue, ident, event)
    assert_every_artifact_is_published(queue)


def test_parked_artifacts_do_not_violate_the_invariant_and_are_counted(queue):
    ident = write_artifact(queue, "a finding parked awaiting a human decision")
    append(queue, ident, "parked")
    assert_every_artifact_is_published(queue)
    assert RQ.scan(queue).held == [ident], "the exclusion must be visible, not silent"


def write_marker(queue: Path, area: str, ident: str, expires_at: str | None = None) -> None:
    d = queue / area
    d.mkdir(parents=True, exist_ok=True)
    record: dict = {"reason": "test marker", "class": "candidate-defect"}
    if expires_at is not None:
        record["expires_at"] = expires_at
    (d / f"{ident.replace(':', '_')}.json").write_text(json.dumps(record))


def test_a_published_id_with_a_live_rejected_marker_violates_the_invariant(queue):
    """The sibling carrier of the same favourable-absence pattern: this suite
    used to gate on `rec.orphans` alone, so a queue OFFERING a tombstoned id —
    zero orphans — passed. The dangerous direction must be the loud one."""
    ident = write_artifact(queue, "a finding offered by the queue and rejected on disk")
    append(queue, ident, "found")
    write_marker(queue, "rejected", ident)

    with pytest.raises(QueuePublicationViolation) as exc:
        assert_every_artifact_is_published(queue)
    message = str(exc.value)
    assert ident in message and "queue_status=open" in message
    assert "REMEDY:" in message


def test_an_unreadable_marker_is_a_violation_not_a_pass(queue):
    """Whether this id is settled is UNKNOWN, and unknown is not green."""
    ident = write_artifact(queue, "a finding whose rejected marker will not parse")
    append(queue, ident, "found")
    (queue / "rejected").mkdir(parents=True, exist_ok=True)
    (queue / "rejected" / f"{ident.replace(':', '_')}.json").write_text("{not json")

    with pytest.raises(QueuePublicationViolation) as exc:
        assert_every_artifact_is_published(queue)
    assert "could not be measured" in str(exc.value) and ident in str(exc.value)


def test_the_standing_command_is_red_on_a_contradiction(queue):
    """The ops check and this suite must agree on the contradiction too, or the
    command reports green on the state the suite calls a violation."""
    ident = write_artifact(queue, "a finding the ops check must report as contradicted")
    append(queue, ident, "found")
    write_marker(queue, "rejected", ident)
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = RQ.main(["--queue", str(queue)])
    assert code == RQ.EXIT_CONTRADICTED and code != RQ.EXIT_OK
    assert ident[:19] in out.getvalue()


def test_the_standing_command_is_red_on_an_unreadable_marker(queue):
    write_artifact(queue, "an artifact whose neighbour's marker will not parse")
    ident = write_artifact(queue, "a finding whose marker will not parse")
    (queue / "rejected").mkdir(parents=True, exist_ok=True)
    (queue / "rejected" / f"{ident.replace(':', '_')}.json").write_text("{not json")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = RQ.main(["--queue", str(queue)])
    assert code == RQ.EXIT_COULD_NOT_RUN, \
        "an instrument error must not be reportable as a clean queue"


def test_an_unmeasurable_invariant_is_a_violation_not_a_pass(queue):
    """A torn ledger means the invariant could not be measured. Reporting green
    for an unrun check is the shape of defect this whole plan closes."""
    write_artifact(queue, "an artifact whose queue cannot be computed")
    (queue / "ledger.jsonl").write_text("{not json and no newline")
    with pytest.raises(QueuePublicationViolation) as exc:
        assert_every_artifact_is_published(queue)
    assert "could not be measured" in str(exc.value)


# --------------------------------------------------------------------------
# the ops check and this suite must not drift apart
# --------------------------------------------------------------------------

def test_the_standing_command_is_red_while_an_orphan_remains(queue):
    write_artifact(queue, "an orphan the standing ops check must report")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = RQ.main(["--queue", str(queue)])
    assert code == RQ.EXIT_ORPHANS_REMAIN, \
        "the live-surface check is this exit code; a green here would be a lie"
    assert "WOULD APPEND" in out.getvalue()


def test_the_standing_command_is_green_on_a_clean_queue(queue):
    append(queue, write_artifact(queue, "a properly filed finding"), "found")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        assert RQ.main(["--queue", str(queue)]) == RQ.EXIT_OK
