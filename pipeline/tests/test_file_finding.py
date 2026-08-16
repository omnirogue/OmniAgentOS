"""Pins bridge/file_finding.py — the sanctioned finding writer.

The defect this file exists to keep closed: queue publication is ledger-derived,
so an artifact without a status-producing ledger event is INVISIBLE to every
reader while looking perfectly healthy on disk. Proposals are immune because
file_proposal.py writes the artifact and its event together; findings had no
such writer, and 63 of them accumulated unseen (28 with no ledger event at all).

Every test here asserts the pair that matters: the exit code, AND what is on
disk afterwards. A writer that reports success while leaving either half behind
— artifact without event, or event without artifact — recreates the class.

No test in this file touches the live queue: each builds its own loopqueue
under `tmp_path`, and every ledger append goes through the shared transport.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / "bridge"))

import file_finding  # noqa: E402
import file_proposal  # noqa: E402
from canonical import content_id  # noqa: E402


@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "loopqueue"
    for sub in ("findings", "rejected", "parked"):
        (q / sub).mkdir(parents=True)
    (q / "ledger.jsonl").touch()
    return q


def draft(**over) -> dict:
    payload = {"symptom": "the gate refused an unchanged input 28 times"}
    payload.update(over.pop("payload", {}))
    art = {
        "contract": "v1.1",
        "kind": "finding",
        "title": "a symptom worth someone's time",
        "created_at": "2026-08-11T00:00:00Z",
        "producer": {"role": "external", "actor": "test"},
        "payload": payload,
    }
    art.update(over)
    art.setdefault("id", content_id(art["payload"]))
    return art


def run(art: dict, tmp_path: Path, queue: Path, *extra) -> tuple[int, str]:
    """Run the tool in-process; return (exit code, stderr)."""
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(art))
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        code = file_finding.main([str(path), "--queue", str(queue), *extra])
    return code, err.getvalue()


def written(queue: Path) -> list[Path]:
    return sorted((queue / "findings").glob("*.json"))


def ledger_events(queue: Path) -> list[dict]:
    raw = (queue / "ledger.jsonl").read_text()
    return [json.loads(line) for line in raw.splitlines() if line.strip()]


# --------------------------------------------------------------------------
# the core guarantee: artifact and event land together, or neither does
# --------------------------------------------------------------------------

def test_filing_writes_the_artifact_and_its_found_event(tmp_path, queue):
    art = draft()
    code, err = run(art, tmp_path, queue)
    assert code == 0, err
    assert [p.name for p in written(queue)] == [art["id"].replace(":", "_") + ".json"]
    events = ledger_events(queue)
    assert [e["event"] for e in events] == ["found"]
    assert events[0]["id"] == art["id"]


def test_found_is_inside_the_status_map_that_publication_reads(tmp_path, queue):
    """The event vocabulary is the whole mechanism: `observed`/`published`/
    `finding` are all real events producers emit, and NONE of them puts an
    artifact in queue.json. A writer that picked one of those would report
    success and stay invisible."""
    sys.path.insert(0, str(PKG))
    from bridge.integration import LedgerView

    run(draft(), tmp_path, queue)
    view = LedgerView.build(queue)
    assert view.status[content_id(draft()["payload"])] == "open"
    assert file_finding.FOUND_EVENT == "found"


def test_rollback_on_ledger_failure(tmp_path, queue, monkeypatch):
    """A confirmed-absent ledger append rolls the artifact back.

    This is the exact gap that produced the 28 event-less findings: a
    write-first / append-best-effort writer leaves an artifact no reader can
    see, and reports success."""
    def boom(_queue, _line):
        raise OSError("simulated ledger failure: no bytes written")

    monkeypatch.setattr(file_finding, "_append_ledger_line", boom)
    code, err = run(draft(), tmp_path, queue)
    assert code == file_finding.EXIT_COULD_NOT_RUN
    assert "rolled back" in err
    assert written(queue) == [], "an artifact whose event never landed must not survive"
    assert ledger_events(queue) == []


def test_unconfirmed_append_keeps_the_artifact(tmp_path, queue, monkeypatch):
    """An UNREADABLE ledger is 'unknown', never 'absent'.

    Rolling back on unknown is how a real ledger line ends up pointing at
    nothing (R3-FI-01): an orphan artifact is visible and repairable by
    reconcile_queue.py; a phantom event is neither."""
    monkeypatch.setattr(file_finding, "_ledger_event_state",
                        lambda *a, **k: file_proposal.LEDGER_EVENT_UNKNOWN)

    def boom(_queue, _line):
        raise file_finding.LedgerNotDurable("fsync failed after the bytes landed")

    monkeypatch.setattr(file_finding, "_append_ledger_line", boom)
    code, err = run(draft(), tmp_path, queue)
    assert code == 0, err
    assert len(written(queue)) == 1, "a rollback we cannot justify risks a phantom"
    assert "KEPT" in err


# --------------------------------------------------------------------------
# refusals — each one must leave nothing behind
# --------------------------------------------------------------------------

def test_wrong_kind_is_refused(tmp_path, queue):
    code, err = run(draft(kind="proposal"), tmp_path, queue)
    assert code == file_finding.EXIT_COULD_NOT_RUN
    assert "this tool files findings" in err
    assert written(queue) == []


def test_missing_symptom_is_refused_by_schema(tmp_path, queue):
    art = draft()
    art["payload"] = {"note": "something feels off"}
    art["id"] = content_id(art["payload"])
    code, err = run(art, tmp_path, queue)
    assert code == file_finding.EXIT_REFUSED_FIXABLE
    assert "schema" in err and "symptom" in err
    assert written(queue) == []
    assert ledger_events(queue) == []


def test_id_that_does_not_hash_the_payload_is_refused(tmp_path, queue):
    art = draft(id="sha256:" + "0" * 64)
    code, err = run(art, tmp_path, queue)
    assert code == file_finding.EXIT_REFUSED_FIXABLE
    assert "id.hash" in err
    assert written(queue) == []


def test_duplicate_is_refused_do_not_retry(tmp_path, queue):
    art = draft()
    assert run(art, tmp_path, queue)[0] == 0
    code, err = run(art, tmp_path, queue)
    assert code == file_finding.EXIT_REFUSED_DO_NOT_RETRY
    assert "already filed in findings/" in err
    assert len(written(queue)) == 1
    assert len(ledger_events(queue)) == 1, "a refused re-file must not append a second event"


def test_duplicate_is_refused_across_artifact_kinds(tmp_path, queue):
    """R4/F3 (cross-lineage MAJOR, 2026-08-12): `content_id` deliberately does
    NOT include `kind` in its hash input (out of scope to change -- it would
    re-key every artifact already in the queue), so an identical payload filed
    as a proposal and then re-filed as a finding hashes to the SAME id. The
    duplicate check used to scan only `findings/`, so this re-file used to
    succeed, leaving both artifacts and both ledger events on disk with a
    hybrid identity once publish_queue.py collapsed them into one item. It
    must be refused, and nothing written, regardless of which OTHER kind
    directory already holds the id."""
    (queue / "proposals").mkdir(parents=True, exist_ok=True)
    art = draft()
    stem = art["id"].replace(":", "_")
    proposal = {"kind": "proposal", "id": art["id"], "title": "an existing proposal",
                "created_at": "2026-01-01T00:00:00Z", "producer": {"role": "planner"},
                "paths": ["pipeline/x.py"], "payload": art["payload"]}
    (queue / "proposals" / f"{stem}.json").write_text(json.dumps(proposal))

    code, err = run(art, tmp_path, queue)
    assert code == file_finding.EXIT_REFUSED_DO_NOT_RETRY, err
    assert "already filed in proposals/" in err
    assert written(queue) == [], "a cross-kind id collision must write nothing"
    assert ledger_events(queue) == []


def test_live_rejected_marker_refuses_the_re_raise(tmp_path, queue):
    art = draft()
    stem = art["id"].replace(":", "_")
    (queue / "rejected" / f"{stem}.json").write_text(json.dumps({
        "class": "answered", "reason": "settled by research on 2026-08-08",
        "expires_at": "2099-01-01T00:00:00Z"}))
    code, err = run(art, tmp_path, queue)
    assert code == file_finding.EXIT_REFUSED_DO_NOT_RETRY
    assert "live rejected marker" in err
    assert written(queue) == []


def test_expired_rejected_marker_allows_the_re_raise_with_a_warning(tmp_path, queue):
    art = draft()
    stem = art["id"].replace(":", "_")
    (queue / "rejected" / f"{stem}.json").write_text(json.dumps({
        "class": "answered", "reason": "settled once", "expires_at": "2000-01-01T00:00:00Z"}))
    code, err = run(art, tmp_path, queue)
    assert code == 0, err
    assert "EXPIRED marker" in err
    assert len(written(queue)) == 1


def test_parked_marker_refuses_regardless_of_expiry(tmp_path, queue):
    """Parking never decays by TTL (CONTRACT §9)."""
    art = draft()
    stem = art["id"].replace(":", "_")
    (queue / "parked" / f"{stem}.json").write_text(json.dumps({
        "reason": "awaiting the operator's ruling", "expires_at": "2000-01-01T00:00:00Z"}))
    code, err = run(art, tmp_path, queue)
    assert code == file_finding.EXIT_REFUSED_DO_NOT_RETRY
    assert "parked" in err
    assert written(queue) == []


def test_unreadable_marker_fails_closed(tmp_path, queue):
    """An unreadable ban is an instrument error, not an absence."""
    art = draft()
    stem = art["id"].replace(":", "_")
    (queue / "rejected" / f"{stem}.json").write_text("{not json")
    code, err = run(art, tmp_path, queue)
    assert code == file_finding.EXIT_COULD_NOT_RUN
    assert "cannot be read" in err
    assert written(queue) == []


def test_no_ledger_flag_is_refused(tmp_path, queue):
    """The flag exists only to say NO: an artifact without its event is exactly
    the invisible filing this tool was written to stop."""
    code, err = run(draft(), tmp_path, queue, "--no-ledger")
    assert code == file_finding.EXIT_COULD_NOT_RUN
    assert "ledger-derived" in err
    assert written(queue) == []


def test_check_mode_writes_nothing(tmp_path, queue):
    code, err = run(draft(), tmp_path, queue, "--check")
    assert code == 0, err
    assert written(queue) == []
    assert ledger_events(queue) == []


def test_unsearchable_rejected_directory_refuses_the_filing(tmp_path, queue):
    """R5/F1-REOPENED (cross-lineage BLOCKER, 2026-08-12), sibling carrier of
    the reconcile_queue.py defect: `os.path.lexists()` returns False when the
    stat itself FAILS, not only when the path is absent, so a live ban inside
    an unsearchable `rejected/` directory read as "no marker" and the filing
    went through. Fail CLOSED instead: a ban that cannot be examined is an
    instrument error, not an absence."""
    import os
    if os.geteuid() == 0:
        pytest.skip("root bypasses directory permissions, so the trap cannot be armed")

    art = draft()
    stem = art["id"].replace(":", "_")
    (queue / "rejected" / f"{stem}.json").write_text(json.dumps({
        "class": "answered", "reason": "a live ban the filer must not walk past",
        "expires_at": "2099-01-01T00:00:00Z"}))

    os.chmod(queue / "rejected", 0o000)
    try:
        assert not os.path.lexists(queue / "rejected" / f"{stem}.json"), \
            "the trap is not armed"
        code, err = run(art, tmp_path, queue)
        assert code == file_finding.EXIT_COULD_NOT_RUN, err
        assert written(queue) == [], "a filing must not land past an unexaminable ban"
        assert ledger_events(queue) == []
    finally:
        os.chmod(queue / "rejected", 0o755)
