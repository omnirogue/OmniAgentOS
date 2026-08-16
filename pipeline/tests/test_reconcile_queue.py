"""Pins bridge/reconcile_queue.py — the drain for artifacts publication skipped.

RED-FIRST, and with the two shapes that were actually missing. Measured at
1bb016c3f against a temp queue built by this file's own fixture:

    (a) NO-EVENT finding      in queue.json = False   <- 28 real instances
    (b) OBSERVED-only finding in queue.json = False   <- 35 real instances
    (control) FOUND-event finding in queue.json = True   <- already worked
    (control) TERMINAL finding    in queue.json = False  <- must STAY false

The control lines are the reason the first two matter: a test written with a
`found` event passes on unpatched code and pins nothing, which is exactly how
63 artifacts accumulated unnoticed.

SAFETY: every test builds its own loopqueue under `tmp_path`. Nothing here
reads or writes the live `var/loopqueue`, and every append goes through
`bridge/ledger_write.py`.
"""

from __future__ import annotations

import calendar
import contextlib
import io
import json
import os
import sys
import time
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(PKG / "bridge"))

from bridge import publish_queue as PQ  # noqa: E402
from bridge import reconcile_queue as RQ  # noqa: E402
from bridge.canonical import content_id  # noqa: E402
from bridge.integration import LedgerView  # noqa: E402

KIND_DIR = {"finding": "findings", "inquiry": "inquiries",
            "proposal": "proposals", "candidate": "candidates"}


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "loopqueue"
    for sub in ("findings", "inquiries", "proposals", "candidates", "state", "locks"):
        (q / sub).mkdir(parents=True)
    (q / "ledger.jsonl").write_text("")
    (q / "state" / "budget.json").write_text(json.dumps({"wip_cap": 4}))
    return q


def write_artifact(queue: Path, symptom: str, kind: str = "finding",
                   created_at: str = "2026-01-01T00:00:00Z") -> str:
    """An artifact on disk, with NO ledger event — the shape that goes missing."""
    payload = {"symptom": symptom} if kind == "finding" else {"note": symptom}
    ident = content_id(payload)
    art = {"contract": "v1.1", "kind": kind, "id": ident, "title": symptom[:80],
           "created_at": created_at, "producer": {"role": "external", "actor": "test"},
           "payload": payload}
    (queue / KIND_DIR[kind] / f"{ident.replace(':', '_')}.json").write_text(
        json.dumps(art, indent=2))
    return ident


def append(queue: Path, ident: str, event: str, ts: str = "2026-01-01T00:00:01Z") -> None:
    """A raw pre-existing ledger line, as a hand-filing producer would leave it."""
    line = json.dumps({"ts": ts, "role": "external", "event": event, "id": ident,
                       "actor": "test", "detail": {}}, separators=(",", ":")) + "\n"
    with open(queue / "ledger.jsonl", "a", encoding="utf-8") as fh:
        fh.write(line)


def run(queue: Path, *extra) -> tuple[int, str]:
    out = io.StringIO()
    err = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = RQ.main(["--queue", str(queue), *extra])
    return code, out.getvalue() + err.getvalue()


def published_ids(queue: Path) -> set[str]:
    """Ids the REAL publisher would put in state/queue.json."""
    PQ.publish(queue)
    data = json.loads((queue / "state" / "queue.json").read_text())
    return {i["id"] for i in data["items"] if isinstance(i, dict)}


def ledger_lines(queue: Path) -> list[str]:
    return [ln for ln in (queue / "ledger.jsonl").read_text().splitlines() if ln.strip()]


def write_marker(queue: Path, area: str, ident: str, expires_at: str | None = None,
                 reason: str = "test marker") -> None:
    """A `rejected/` or `parked/` tombstone marker, the same shape
    `file_proposal.py` / `file_finding.py` write and check at filing time."""
    d = queue / area
    d.mkdir(parents=True, exist_ok=True)
    record: dict = {"reason": reason, "class": "candidate-defect", "remedy": "replan"}
    if expires_at is not None:
        record["expires_at"] = expires_at
    (d / f"{ident.replace(':', '_')}.json").write_text(json.dumps(record))


# --------------------------------------------------------------------------
# the falsifier — both shapes, plus the two controls that make them meaningful
# --------------------------------------------------------------------------

def test_orphan_with_no_event_is_published(queue):
    ident = write_artifact(queue, "an orphan finding with no ledger event at all")
    assert ident not in published_ids(queue), \
        "PRE-CONDITION: publication is ledger-derived, so this starts invisible"

    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_OK, out
    assert ident in published_ids(queue)


def test_orphan_with_non_status_event_is_published(queue):
    ident = write_artifact(queue, "an orphan whose only event is observed")
    append(queue, ident, "observed")
    assert ident not in published_ids(queue), \
        "PRE-CONDITION: `observed` is outside the event->status map"

    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_OK, out
    assert ident in published_ids(queue)


def test_control_found_event_already_publishes_without_this_tool(queue):
    """The reason the two tests above are written the way they are: a finding
    with a `found` event passes on unpatched code and pins nothing."""
    ident = write_artifact(queue, "a finding that already carries its found event")
    append(queue, ident, "found")
    assert ident in published_ids(queue)
    code, out = run(queue)
    assert code == RQ.EXIT_OK, out
    assert "WOULD APPEND (dry-run" in out and ident[:19] not in out


def test_terminal_stays_excluded(queue):
    """35 of the absent findings are terminal and correctly excluded. A naive
    publish-everything fix regresses exactly there."""
    ident = write_artifact(queue, "a finding that was already closed out")
    append(queue, ident, "found")
    append(queue, ident, "rejected")

    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_OK, out
    assert ident not in published_ids(queue)
    assert not any(json.loads(ln).get("actor") == RQ.ACTOR for ln in ledger_lines(queue)), \
        "a terminal artifact must not collect a reconciliation event"


def test_terminal_without_an_artifact_side_event_is_still_excluded(queue):
    """Terminal by `closed` alone — the finding-side terminal event."""
    ident = write_artifact(queue, "a finding closed by a landed candidate")
    append(queue, ident, "closed")
    run(queue, "--apply")
    assert ident not in published_ids(queue)


# --------------------------------------------------------------------------
# the append-only contract (CONTRACT §5)
# --------------------------------------------------------------------------

def test_dry_run_default(queue):
    """Ledger appends are unrevertible, so an accidental invocation must be a
    report, not a mutation."""
    ident = write_artifact(queue, "an orphan nobody asked this tool to fix yet")
    before = (queue / "ledger.jsonl").read_text()

    code, out = run(queue)
    assert code == RQ.EXIT_ORPHANS_REMAIN, out
    assert (queue / "ledger.jsonl").read_text() == before
    assert ident not in published_ids(queue)
    assert "dry-run" in out and ident[:19] in out


def test_scanning_never_writes_state_queue_json(queue):
    """The scan computes the queue with `write=False`: a 300s publisher already
    owns `state/queue.json`, and a second writer racing it is the bug
    publish_queue.py exists to retire."""
    write_artifact(queue, "an orphan counted without publishing anything")
    for argv in ([], ["--apply"]):
        run(queue, *argv)
        assert not (queue / "state" / "queue.json").exists()


def test_apply_only_appends_and_never_rewrites(queue):
    ident = write_artifact(queue, "an orphan that must not disturb existing history")
    append(queue, ident, "observed")
    before = (queue / "ledger.jsonl").read_text()

    run(queue, "--apply")
    after = (queue / "ledger.jsonl").read_text()
    assert after.startswith(before), "existing ledger lines must be untouched"
    assert len(ledger_lines(queue)) == len(before.splitlines()) + 1


def test_no_fabricated_historical_timestamps(queue):
    """A back-dated event is a false record, and aging would silently promote
    items on the strength of it."""
    ident = write_artifact(queue, "an orphan filed long ago", created_at="2026-01-01T00:00:00Z")
    run(queue, "--apply")

    event = [json.loads(ln) for ln in ledger_lines(queue) if json.loads(ln)["actor"] == RQ.ACTOR][0]
    assert event["id"] == ident
    assert event["ts"] != "2026-01-01T00:00:00Z"
    # timegm, not mktime: mktime reads a UTC struct as local time and its DST
    # guess differs between a parsed stamp (isdst=-1) and gmtime(), which is an
    # hour of false failure that says nothing about the code under test.
    then = calendar.timegm(time.strptime(event["ts"], "%Y-%m-%dT%H:%M:%SZ"))
    assert abs(then - calendar.timegm(time.gmtime())) < 300, \
        "the event must carry the CURRENT time"


def test_reconciliation_events_are_self_identifying(queue):
    """Lane B is not revertible by a code revert; the events must say what they
    are so a later reader can discount them."""
    write_artifact(queue, "an orphan whose repair must be legible later")
    run(queue, "--apply")
    event = [json.loads(ln) for ln in ledger_lines(queue) if json.loads(ln)["actor"] == RQ.ACTOR][0]
    assert event["detail"]["reconciled"] is True
    assert "reconciliation" in event["detail"]["reason"]


def test_second_run_is_a_no_op(queue):
    ident = write_artifact(queue, "an orphan reconciled exactly once")
    run(queue, "--apply")
    lines = ledger_lines(queue)

    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_OK, out
    assert ledger_lines(queue) == lines, "a reconciled artifact must not collect a second event"
    assert ident in published_ids(queue)


def test_concurrent_applies_append_at_most_once_per_id(queue):
    """R4/F2 (cross-lineage MAJOR, 2026-08-12): the scan->decide->append
    sequence must be ONE critical section across separate `--apply` PROCESSES,
    not just the physical write inside `ledger_write.append_event`. Proved
    with two REAL, SEPARATE OS processes (not threads/mocks): both are forced
    to block on the ledger lock file before either can write, released
    together, and at most one `found` event for the id must survive.
    Sequential idempotency (`test_second_run_is_a_no_op`) does not cover this
    -- that is one process running twice, never two processes racing."""
    import fcntl
    import subprocess

    ident = write_artifact(queue, "an orphan raced by two concurrent --apply runs")
    lock_path = queue / "locks" / "ledger.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fh = open(lock_path, "a+")
    fcntl.flock(lock_fh, fcntl.LOCK_EX)
    try:
        cmd = [sys.executable, str(PKG / "bridge" / "reconcile_queue.py"),
               "--queue", str(queue), "--apply"]
        procs = [subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                 for _ in range(2)]
        time.sleep(1)
        assert all(p.poll() is None for p in procs), \
            "both processes must still be blocked on the ledger lock before release"
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()
    rcs = [p.wait(timeout=10) for p in procs]
    assert all(rc == RQ.EXIT_OK for rc in rcs), rcs

    matching = [json.loads(ln) for ln in ledger_lines(queue)
               if json.loads(ln).get("id") == ident and json.loads(ln).get("event") == "found"]
    assert len(matching) == 1, \
        f"concurrent --apply runs must append at most once per id, got {len(matching)}"


# --------------------------------------------------------------------------
# the event vocabulary — proven against the real status map, not restated
# --------------------------------------------------------------------------

def test_only_status_producing_events(queue):
    """Every event this tool can emit must produce a NON-TERMINAL status in the
    real `LedgerView`. An implementation appending `observed` or `published`
    leaves the artifact invisible while reporting success — the defect with
    extra steps."""
    for kind, event in RQ.STATUS_EVENT_BY_KIND.items():
        ident = "sha256:" + f"{abs(hash(kind)):064d}"[:64]
        probe = queue.parent / f"probe-{kind}"
        (probe / "state").mkdir(parents=True, exist_ok=True)
        (probe / "ledger.jsonl").write_text(json.dumps(
            {"ts": "2026-01-01T00:00:00Z", "role": "external", "event": event,
             "id": ident, "actor": "test"}) + "\n")
        status = LedgerView.build(probe).status.get(ident)
        assert status is not None, f"{event!r} produces no status — {kind} would stay invisible"
        assert status not in ("merged", "completed", "rejected", "closed"), \
            f"{event!r} is terminal; reconciling with it would bury the artifact"


def test_observed_is_the_control_that_produces_no_status(queue):
    """The mechanism itself, pinned: this is why 35 artifacts were invisible."""
    ident = "sha256:" + "a" * 64
    append(queue, ident, "observed")
    assert LedgerView.build(queue).status.get(ident) is None


def test_emitted_events_come_only_from_the_sanctioned_map(queue):
    for kind in ("finding", "inquiry", "proposal"):
        write_artifact(queue, f"an orphan of kind {kind}", kind=kind)
    run(queue, "--apply")
    emitted = {json.loads(ln)["event"] for ln in ledger_lines(queue)
               if json.loads(ln)["actor"] == RQ.ACTOR}
    assert emitted <= set(RQ.STATUS_EVENT_BY_KIND.values())
    assert emitted == {"found", "inquired", "proposed"}


def test_candidates_are_opt_in(queue):
    """CONTRACT §6: candidates are not claimed. Reconciling one changes the gate
    surface, so it takes an explicit --kinds."""
    ident = write_artifact(queue, "an orphan candidate nobody selected", kind="candidate")
    run(queue, "--apply")
    assert ident not in published_ids(queue)

    code, out = run(queue, "--apply", "--kinds", "candidate")
    assert code == RQ.EXIT_OK, out
    assert ident in published_ids(queue)


def test_unknown_kind_refuses(queue):
    code, out = run(queue, "--kinds", "receipt")
    assert code == RQ.EXIT_COULD_NOT_RUN
    assert "unknown kind" in out


# --------------------------------------------------------------------------
# rejected/parked MARKER FILES (disk-side tombstone, independent of the
# ledger) -- cross-lineage REQUEST-CHANGES blocker 1, confirmed live: 768
# rejected/ markers exist on disk, 64 with no terminal ledger event, 36 of
# those still sitting in a scanned kind directory. scan() previously read
# ledger.terminal / ledger.status / ledger.parked only, so a marker-only
# tombstone read as an ordinary orphan and --apply republished it.
# --------------------------------------------------------------------------

def test_rejected_marker_blocks_republication_with_no_ledger_event(queue):
    """The exact shape measured live: on disk, a live rejected/ marker, and NO
    terminal ledger event at all. Must NOT be classified as an orphan, and
    --apply must not append an event that would republish it."""
    ident = write_artifact(queue, "a finding rejected via a disk marker only")
    write_marker(queue, "rejected", ident)  # no expires_at -> never expires (live)

    rec = RQ.scan(queue)
    assert ident in rec.terminal
    assert ident not in [o.ident for o in rec.orphans]

    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_OK, out
    assert ident not in published_ids(queue)
    assert not any(json.loads(ln).get("actor") == RQ.ACTOR for ln in ledger_lines(queue)), \
        "a marker-rejected artifact must not collect a reconciliation event " \
        "that would republish it into the queue"


def test_expired_rejected_marker_is_still_eligible(queue):
    """A rejection TTL is honoured, same as file_proposal.py / file_finding.py
    (imported `_expired`, not re-derived): an EXPIRED marker is inert and must
    not permanently suppress the artifact."""
    ident = write_artifact(queue, "a finding whose rejection marker has expired")
    write_marker(queue, "rejected", ident, expires_at="2020-01-01T00:00:00Z")

    rec = RQ.scan(queue)
    assert ident in [o.ident for o in rec.orphans], \
        "an EXPIRED rejection is inert and this artifact must remain claimable"

    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_OK, out
    assert ident in published_ids(queue)


def test_parked_marker_is_held_like_a_parked_ledger_event(queue):
    """A parked/ marker with no ledger `parked` event must be held, exactly
    like the ledger-event path already is -- parking never decays by TTL
    (CONTRACT §9), so an expires_at on the marker is ignored."""
    ident = write_artifact(queue, "a finding parked via a disk marker only")
    write_marker(queue, "parked", ident, expires_at="2020-01-01T00:00:00Z")

    rec = RQ.scan(queue)
    assert rec.held == [ident]
    assert ident not in [o.ident for o in rec.orphans]

    run(queue, "--apply")
    assert not any(json.loads(ln).get("actor") == RQ.ACTOR for ln in ledger_lines(queue))


def test_unreadable_rejected_marker_fails_closed(queue):
    """A marker that cannot be parsed might be a live tombstone -- reconciling
    past it defeats the CONTRACT §4 trap invisibly, the same fail-closed rule
    file_proposal.py / file_finding.py apply at filing time.

    FAIL CLOSED MEANS THE EXIT CODE TOO. An earlier revision of this test
    asserted EXIT_OK here, which made an instrument error byte-identical to a
    clean queue for every caller of the standing check -- the favourable-absence
    class, pinned by a test. Both siblings refuse this exact condition with
    could-not-run (`file_proposal.py` check_identity, `file_finding.py`
    check_finding_identity), and the module docstring reserves exit 2 for it."""
    ident = write_artifact(queue, "a finding whose rejected marker will not parse")
    d = queue / "rejected"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ident.replace(':', '_')}.json").write_text("{not json")

    rec = RQ.scan(queue)
    assert ident in rec.unreadable
    assert ident not in [o.ident for o in rec.orphans]

    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_COULD_NOT_RUN, out
    assert "will not parse" in out
    assert ident not in published_ids(queue)
    assert not any(json.loads(ln).get("actor") == RQ.ACTOR for ln in ledger_lines(queue))


def test_broken_symlink_rejected_marker_fails_closed(queue):
    """R4/F1 (cross-lineage BLOCKER, 2026-08-12): `Path.exists()` FOLLOWS
    symlinks and returns False for a broken/dangling one, so a rejection
    tombstone that is (or has become) an unreadable symlink used to read as
    MARKER_CLEAR -- absence -- rather than MARKER_UNREADABLE, and `--apply`
    would append a `found` event for an id that actually carries an unreadable
    rejection tombstone. `_marker_status` must use `os.path.lexists`, which
    reports the marker PRESENT regardless of what it points at, so a broken
    symlink is graded on whether it can be READ, never on whether the target
    happens to resolve."""
    ident = write_artifact(queue, "an orphan whose rejected marker is a broken symlink")
    d = queue / "rejected"
    d.mkdir(parents=True, exist_ok=True)
    marker = d / f"{ident.replace(':', '_')}.json"
    marker.symlink_to(d / "does-not-exist.json")
    assert os.path.lexists(marker) and not marker.exists()

    rec = RQ.scan(queue)
    assert ident in rec.unreadable
    assert ident not in [o.ident for o in rec.orphans]

    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_COULD_NOT_RUN, out
    assert ident not in published_ids(queue)
    assert not any(json.loads(ln).get("actor") == RQ.ACTOR for ln in ledger_lines(queue)), \
        "a broken-symlink tombstone must refuse the run, not fail-open into an append"


def test_unreadable_marker_refuses_before_appending_anything(queue):
    """The refusal is the WHOLE run, not just that id: a scan that cannot say
    whether one artifact is settled cannot certify the queue, and appending the
    rest on the strength of it reports a repair the instrument did not measure."""
    orphan = write_artifact(queue, "an ordinary orphan sharing the run with a bad marker")
    broken = write_artifact(queue, "a finding whose parked marker is a JSON array")
    d = queue / "parked"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{broken.replace(':', '_')}.json").write_text("[\"not an object\"]")

    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_COULD_NOT_RUN, out
    assert not any(json.loads(ln).get("actor") == RQ.ACTOR for ln in ledger_lines(queue)), \
        "no append may happen on a run whose scan is not trustworthy"
    assert orphan not in published_ids(queue)


def test_unreadable_marker_json_report_names_the_id(queue):
    """A machine-readable refusal must carry the id, or the operator is sent to
    grep for which of 800 markers will not parse."""
    ident = write_artifact(queue, "a finding whose marker is unparseable and must be named")
    d = queue / "rejected"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ident.replace(':', '_')}.json").write_text("{not json")

    code, out = run(queue, "--json")
    assert code == RQ.EXIT_COULD_NOT_RUN
    report = json.loads(out[:out.index("\n}") + 2])
    assert report["summary"]["unreadable_marker"] == 1
    assert report["unreadable"] == [ident]


# --------------------------------------------------------------------------
# CONTRADICTION: the queue OFFERS an id disk says is settled. The marker check
# used to sit AFTER an `ident in queue_ids` short-circuit, so this -- the exact
# condition the marker reading exists to detect -- was absorbed into
# rec.published, with no counter and no exit code. Measured on a copy of the
# live queue at 363561bfc: 75 published ids carried a live tombstone (35
# rejected-marker at status open and therefore CLAIMABLE, 39 parked-marker at
# status parked, 1 parked-marker at status open).
# --------------------------------------------------------------------------

def test_published_id_with_a_live_rejected_marker_is_contradicted(queue):
    """The real-shaped case, 35 live instances: the ledger says `found` so the
    queue offers it at status `open` -- claimable -- while a live rejected/
    marker sits on disk. It must NOT read as `published`, and the run must be
    red."""
    ident = write_artifact(queue, "a finding the queue offers though disk rejected it")
    append(queue, ident, "found")
    write_marker(queue, "rejected", ident)  # no expires_at -> live
    assert ident in published_ids(queue), \
        "PRE-CONDITION: this id really is offered by the publisher"

    rec = RQ.scan(queue)
    assert [c.ident for c in rec.contradicted] == [ident]
    assert ident not in rec.published, \
        "absorbing it into `published` is the defect: reported GREEN while the " \
        "queue offers rejected work"
    assert rec.contradicted[0].queue_status == "open"
    assert rec.contradicted[0].marker == RQ.MARKER_REJECTED

    code, out = run(queue)
    assert code == RQ.EXIT_CONTRADICTED, out
    assert code != RQ.EXIT_OK
    assert ident[:19] in out and "CONTRADICTED" in out


def test_contradiction_is_red_even_with_no_orphans_at_all(queue):
    """The exit code cannot be borrowed from the orphan count: a queue with zero
    orphans and one contradiction is still not clean."""
    ident = write_artifact(queue, "the only artifact in a queue with no orphans")
    append(queue, ident, "found")
    write_marker(queue, "rejected", ident)

    rec = RQ.scan(queue)
    assert rec.orphans == []
    code, out = run(queue)
    assert code == RQ.EXIT_CONTRADICTED, out


def test_published_id_with_a_live_parked_marker_at_open_is_contradicted(queue):
    """The 1 live instance of the parked-marker-at-open shape: a marker-parked
    id the queue still offers as claimable. Parking never decays by TTL
    (CONTRACT §9), so an expires_at does not soften it."""
    ident = write_artifact(queue, "a finding parked on disk but offered by the queue")
    append(queue, ident, "found")
    write_marker(queue, "parked", ident, expires_at="2020-01-01T00:00:00Z")

    rec = RQ.scan(queue)
    assert [c.ident for c in rec.contradicted] == [ident]
    assert rec.contradicted[0].marker == RQ.MARKER_PARKED
    assert run(queue)[0] == RQ.EXIT_CONTRADICTED


def test_parked_marker_at_parked_status_is_held_not_contradicted(queue):
    """The 39 live instances of the consistent shape: BOTH surfaces withhold it
    (marker on disk, status `parked` in the queue). It is counted as `held` --
    visible, not silent -- but it is not a hazard and must not go red, or the
    standing check cries wolf on correctly-withheld work."""
    ident = write_artifact(queue, "a finding parked in the ledger and on disk")
    append(queue, ident, "found")
    append(queue, ident, "parked")
    write_marker(queue, "parked", ident)
    assert ident in published_ids(queue), \
        "PRE-CONDITION: parked ids are still ITEMS in the queue, at status parked"

    rec = RQ.scan(queue)
    assert rec.contradicted == []
    assert rec.held == [ident]
    assert run(queue)[0] == RQ.EXIT_OK


def test_expired_rejected_marker_on_a_published_id_is_not_a_contradiction(queue):
    """An EXPIRED rejection is inert (the TTL rule imported from
    file_proposal._expired). A published id carrying one is simply published."""
    ident = write_artifact(queue, "a published finding whose rejection expired long ago")
    append(queue, ident, "found")
    write_marker(queue, "rejected", ident, expires_at="2020-01-01T00:00:00Z")

    rec = RQ.scan(queue)
    assert rec.contradicted == [] and rec.published == [ident]
    assert run(queue)[0] == RQ.EXIT_OK


def test_contradictions_are_itemised_in_the_json_report(queue):
    ident = write_artifact(queue, "a contradicted finding a machine should be able to read")
    append(queue, ident, "found")
    write_marker(queue, "rejected", ident)

    code, out = run(queue, "--json")
    assert code == RQ.EXIT_CONTRADICTED
    report = json.loads(out[:out.rindex("}") + 1])
    assert report["summary"]["contradicted"] == 1
    assert report["contradicted"][0]["id"] == ident
    assert report["contradicted"][0]["queue_status"] == "open"
    assert report["contradicted"][0]["marker"] == RQ.MARKER_REJECTED


# --------------------------------------------------------------------------
# TOCTOU -- cross-lineage REQUEST-CHANGES blocker 2: the ledger must be
# read AFTER the disk snapshot, so it is guaranteed at least as fresh. The
# publisher ticks every 300s and gate_loop every 60s on a live host, so a
# terminal event landing in the read window is not theoretical.
# --------------------------------------------------------------------------

def test_terminal_event_landing_during_scan_is_not_missed(queue, monkeypatch):
    """Simulates a terminal event landing in the window between the disk
    snapshot and the ledger read. If `scan()` reads the ledger BEFORE walking
    disk, this event is invisible to it and the artifact misclassifies as an
    orphan even though it was rejected moments ago. Reading disk first (and
    the ledger after) guarantees the ledger sees it."""
    ident = write_artifact(queue, "a finding rejected in the window between the two reads")

    real_artifact_paths = RQ._artifact_paths

    def racy(root, kinds):
        result = real_artifact_paths(root, kinds)
        # The side effect models an event landing on the live ledger AFTER
        # the disk snapshot but (if scan() is correctly ordered) BEFORE the
        # ledger is read.
        append(queue, ident, "rejected")
        return result

    monkeypatch.setattr(RQ, "_artifact_paths", racy)

    rec = RQ.scan(queue)
    assert ident not in [o.ident for o in rec.orphans], \
        "a terminal event landing after the disk snapshot must still be seen " \
        "-- the ledger must be built AFTER the disk snapshot, not before it"
    assert ident in rec.terminal


# --------------------------------------------------------------------------
# fail-closed
# --------------------------------------------------------------------------

def test_parked_artifacts_are_held_and_reported_not_touched(queue):
    """Parking ends only with an authenticated `unparked` event (CONTRACT §9)."""
    ident = write_artifact(queue, "an orphan parked awaiting the operator")
    append(queue, ident, "parked")

    rec = RQ.scan(queue)
    assert rec.held == [ident] and rec.orphans == []
    run(queue, "--apply")
    assert not any(json.loads(ln)["actor"] == RQ.ACTOR for ln in ledger_lines(queue))


def test_unreadable_ledger_refuses_instead_of_mass_appending(queue):
    """A torn-tail read with zero events makes EVERY artifact look orphaned."""
    write_artifact(queue, "an artifact that must not be mass-reconciled")
    (queue / "ledger.jsonl").write_text("{not json and no newline")

    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_COULD_NOT_RUN
    assert "refusing to reconcile" in out
    assert not any(RQ.ACTOR in ln for ln in ledger_lines(queue))


def test_relative_queue_path_is_refused(queue):
    out = io.StringIO()
    with contextlib.redirect_stderr(out):
        code = RQ.main(["--queue", "var/loopqueue", "--apply"])
    assert code == RQ.EXIT_COULD_NOT_RUN
    assert "absolute path" in out.getvalue()


def test_missing_queue_directory_is_could_not_run(tmp_path):
    out = io.StringIO()
    with contextlib.redirect_stderr(out):
        code = RQ.main(["--queue", str(tmp_path / "nope")])
    assert code == RQ.EXIT_COULD_NOT_RUN


def test_append_failure_reports_how_far_it_got(queue, monkeypatch):
    """The ledger cannot be rolled back; a partial reconciliation must say so."""
    for n in range(3):
        write_artifact(queue, f"orphan number {n} of a partial reconciliation")

    calls = {"n": 0}
    real = RQ.append_event

    def flaky(root, event):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RQ.LedgerAppendError("simulated disk failure", phase="write")
        return real(root, event)

    monkeypatch.setattr(RQ, "append_event", flaky)
    code, out = run(queue, "--apply")
    assert code == RQ.EXIT_COULD_NOT_RUN
    assert "after 1 event(s) landed" in out
    assert len([ln for ln in ledger_lines(queue) if RQ.ACTOR in ln]) == 1


def test_json_report_is_machine_readable(queue):
    ident = write_artifact(queue, "an orphan a machine should be able to count")
    code, out = run(queue, "--json")
    assert code == RQ.EXIT_ORPHANS_REMAIN
    report = json.loads(out)
    assert report["summary"]["orphans"] == 1
    assert report["orphans"][0]["id"] == ident
    assert report["orphans"][0]["event"] == "found"


def test_unsearchable_marker_directory_fails_closed(queue):
    """R5/F1-REOPENED (cross-lineage BLOCKER, 2026-08-12): the R4 fix swapped
    `Path.exists()` for `os.path.lexists()`, which closed the dangling-symlink
    half and left the other half open -- `lexists()` swallows `OSError`
    internally and returns False for it too, so a live rejection tombstone
    inside a directory this process cannot SEARCH (EACCES/EPERM) still read as
    ABSENT and `--apply` fail-opened into appending a `found` event for a
    definitively-rejected id. Verified by execution: with the parent chmod 000,
    `os.path.lexists(marker)` is False while `os.lstat(marker)` raises
    PermissionError(13). A presence check that cannot answer must say so;
    `marker_presence` returns UNDETERMINED and this run halts."""
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions, so the trap cannot be armed")

    ident = write_artifact(queue, "an orphan whose rejected/ dir cannot be searched")
    write_marker(queue, "rejected", ident, expires_at="2099-01-01T00:00:00Z")
    marker = queue / "rejected" / f"{ident.replace(':', '_')}.json"

    os.chmod(queue / "rejected", 0o000)
    try:
        # the OLD check's answer, pinned so a revert to lexists() re-reds this
        assert not os.path.lexists(marker), "the trap is not armed"

        rec = RQ.scan(queue)
        assert ident in rec.unreadable, \
            "a marker that cannot be stat'd must be UNREADABLE, never absent"
        assert ident not in [o.ident for o in rec.orphans]

        code, out = run(queue, "--apply")
        assert code == RQ.EXIT_COULD_NOT_RUN, out
        assert ident not in published_ids(queue)
        assert not any(json.loads(ln).get("actor") == RQ.ACTOR
                       for ln in ledger_lines(queue)), \
            "an undeterminable tombstone must refuse the run, not fail-open into an append"
    finally:
        os.chmod(queue / "rejected", 0o755)
