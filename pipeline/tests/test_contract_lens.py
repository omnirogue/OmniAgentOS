"""Pins bridge/contract_lens.py (warn-only lens) and bridge/file_inquiry.py.

Two properties are load-bearing and both are asserted here:

1. WARN MODE IS MECHANICAL, NOT POLICY: an artifact tripping every lens class
   still files with exit 0. If a future edit turns a lens warning into a
   refusal without the contract bump the ruling requires, the warn-mode test
   fails — that is the gate on the gate.

2. THE §4 RE-RAISE TRAP IS CLOSED FOR INQUIRIES: an inquiry whose id carries a
   live tombstone in rejected/ is refused with exit 3 (do-not-retry) and NOTHING is written —
   same discipline as test_file_proposal.py (a refusal that leaves a degraded
   artifact is the failure mode, not a partial success).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG / "bridge"))

import file_inquiry  # noqa: E402
import file_proposal  # noqa: E402
from canonical import content_id  # noqa: E402
from contract_lens import lens_warnings  # noqa: E402

# --------------------------------------------------------------------------
# fixtures (mirror test_file_proposal.py's conventions)
# --------------------------------------------------------------------------

@pytest.fixture
def queue(tmp_path: Path) -> Path:
    q = tmp_path / "loopqueue"
    for sub in ("proposals", "inquiries", "rejected", "parked"):
        (q / sub).mkdir(parents=True)
    (q / "ledger.jsonl").touch()
    return q


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "bridge").mkdir(parents=True)
    (r / "bridge" / "governor.py").write_text("# real\n")
    return r


def proposal_draft(payload_over: dict | None = None, **envelope_over) -> dict:
    payload = {
        "problem": "an observable thing is wrong",
        "implementation_plan": "1. do the thing",
    }
    payload.update(payload_over or {})
    art = {
        "contract": "v1.1",
        "kind": "proposal",
        "title": "a test proposal",
        "created_at": "2026-08-08T12:00:00Z",
        "producer": {"role": "planner", "actor": "test"},
        "paths": ["bridge/governor.py"],
        "payload": payload,
    }
    art.update(envelope_over)
    art["id"] = content_id(art["payload"])
    return art


def inquiry_draft(payload_over: dict | None = None) -> dict:
    payload = {
        "area": "tooling",
        "observation": "a thing was observed twelve times",
        "why_not_a_fix": "the carrier is unknown",
    }
    payload.update(payload_over or {})
    art = {
        "contract": "v1.1",
        "kind": "inquiry",
        "title": "a test inquiry",
        "created_at": "2026-08-08T12:00:00Z",
        "producer": {"role": "external", "actor": "test"},
        "payload": payload,
    }
    art["id"] = content_id(art["payload"])
    return art


def write_draft(tmp_path: Path, art: dict) -> Path:
    p = tmp_path / "draft.json"
    p.write_text(json.dumps(art))
    return p


# --------------------------------------------------------------------------
# the six lens classes, one at a time
# --------------------------------------------------------------------------

def _classes(warnings: list[str]) -> set[str]:
    return {w.split(":", 1)[0] for w in warnings}


def test_no_execution_evidence_warns():
    art = proposal_draft()
    assert "lens.evidence" in _classes(lens_warnings(art))


def test_cwd_relative_execution_command_warns():
    art = proposal_draft(evidence=[
        {"claim": "x", "verified_by": "execution",
         "command": "pytest tests/x.py", "exit_code": 0}])
    warns = [w for w in lens_warnings(art) if w.startswith("lens.evidence")]
    assert warns and "cwd-relative" in warns[0]


def test_absolute_execution_command_is_clean():
    art = proposal_draft(evidence=[
        {"claim": "x", "verified_by": "execution",
         "command": "/usr/bin/true", "exit_code": 0}])
    assert not any(w.startswith("lens.evidence") for w in lens_warnings(art))


def test_tests_without_invocation_warn():
    art = proposal_draft({"tests_required": ["the gate must refuse the label"]})
    assert "lens.tests" in _classes(lens_warnings(art))


def test_tests_with_invocation_clean():
    art = proposal_draft({"tests_required": [
        "pytest tests/test_x.py::test_refusal — MUST FAIL at base"]})
    assert "lens.tests" not in _classes(lens_warnings(art))


def test_missing_considered_and_rejected_warns():
    assert "lens.dead_ends" in _classes(lens_warnings(proposal_draft()))
    art = proposal_draft({"considered_and_rejected": "X: rejected because Y"})
    assert "lens.dead_ends" not in _classes(lens_warnings(art))


def test_missing_observed_at_main_sha_warns():
    assert "lens.deps" in _classes(lens_warnings(proposal_draft()))


def test_plan_delegation_without_authority_pin_warns():
    art = proposal_draft({
        "observed_at_main_sha": "0" * 40,
        "implementation_plan": "1. execute ~/Work/Ops/Plans/Some-Plan.md section 3",
    })
    warns = [w for w in lens_warnings(art) if "plan_authority_sha256" in w]
    assert warns and warns[0].startswith("lens.deps")
    art["payload"]["plan_authority_sha256"] = "a" * 64
    assert not [w for w in lens_warnings(art) if "plan_authority_sha256" in w]


def test_opaque_dependency_warns():
    art = proposal_draft({"dependencies": ["audit-5 (board_tasks detox)"],
                          "observed_at_main_sha": "0" * 40})
    warns = [w for w in lens_warnings(art) if w.startswith("lens.deps")]
    assert warns and "audit-5" in warns[0]
    art = proposal_draft({"dependencies": [
        "sha256:0f3c5b630e396577 in var/loopqueue/proposals/"],
        "observed_at_main_sha": "0" * 40})
    assert "lens.deps" not in _classes(lens_warnings(art))


def test_conversation_reference_warns():
    art = proposal_draft({"urgency": "Morgan-named night focus"})
    assert "lens.context" in _classes(lens_warnings(art))


def test_multistep_plan_without_done_criteria_warns():
    art = proposal_draft({"implementation_plan": "1. build it. 2. wire it. 3. test it."})
    assert "lens.checkpoints" in _classes(lens_warnings(art))
    art = proposal_draft({"implementation_plan":
                          "1. build (DONE when: x exists). 2. wire (DONE when: y passes)."})
    assert "lens.checkpoints" not in _classes(lens_warnings(art))


def test_single_step_plan_is_exempt():
    assert "lens.checkpoints" not in _classes(lens_warnings(proposal_draft()))


# --------------------------------------------------------------------------
# warn mode is mechanical: all six classes tripping still exits 0
# --------------------------------------------------------------------------

def test_warn_mode_never_refuses(tmp_path, queue, root, monkeypatch, capsys):
    art = proposal_draft({
        "tests_required": ["the behaviour must be correct"],
        "dependencies": ["audit-5"],
        "urgency": "Morgan-named night focus",
        "implementation_plan": "1. do. 2. redo. 3. verify.",
    })
    rc = file_proposal.main([
        str(write_draft(tmp_path, art)),
        "--queue", str(queue), "--root", f"repo={root}", "--check",
    ])
    err = capsys.readouterr().err
    assert rc == 0, err
    # every class fired as a warning, none as a refusal
    for cls in ("lens.evidence", "lens.tests", "lens.dead_ends",
                "lens.deps", "lens.context", "lens.checkpoints"):
        assert f"warn: {cls}" in err
    assert "REFUSE" not in err


def test_clean_proposal_warns_nothing(tmp_path, queue, root, capsys):
    art = proposal_draft(
        {
            "tests_required": ["pytest tests/test_x.py — MUST FAIL at base"],
            "considered_and_rejected": "Y: rejected because Z",
            "observed_at_main_sha": "0" * 40,
            "implementation_plan": "1. do the thing (DONE when: pytest passes).",
        },
        evidence=[{"claim": "x", "verified_by": "execution",
                   "command": "/usr/bin/true", "exit_code": 0}],
    )
    rc = file_proposal.main([
        str(write_draft(tmp_path, art)),
        "--queue", str(queue), "--root", f"repo={root}", "--check",
    ])
    err = capsys.readouterr().err
    assert rc == 0
    assert "warn: lens." not in err


# --------------------------------------------------------------------------
# file_inquiry: schema, identity, and the §4 re-raise trap
# --------------------------------------------------------------------------

def test_inquiry_files_cleanly(tmp_path, queue, capsys):
    art = inquiry_draft()
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 0
    stem = art["id"].replace(":", "_")
    assert (queue / "inquiries" / f"{stem}.json").exists()
    events = [json.loads(line) for line in
              (queue / "ledger.jsonl").read_text().splitlines() if line.strip()]
    assert events and events[-1]["event"] == "inquired" and events[-1]["id"] == art["id"]


def test_inquiry_missing_why_not_a_fix_refused(tmp_path, queue):
    art = inquiry_draft()
    del art["payload"]["why_not_a_fix"]
    art["id"] = content_id(art["payload"])
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 1
    assert not list((queue / "inquiries").glob("*.json"))


def test_inquiry_live_tombstone_refused_exit_3(tmp_path, queue):
    art = inquiry_draft()
    stem = art["id"].replace(":", "_")
    (queue / "rejected" / f"{stem}.json").write_text(json.dumps({
        "id": art["id"], "class": "answered", "reason": "already researched",
        "expires_at": "2099-01-01T00:00:00Z",
    }))
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 3
    assert not list((queue / "inquiries").glob("*.json"))
    assert (queue / "ledger.jsonl").read_text() == ""


def test_inquiry_expired_tombstone_allows_refile(tmp_path, queue, capsys):
    art = inquiry_draft()
    stem = art["id"].replace(":", "_")
    (queue / "rejected" / f"{stem}.json").write_text(json.dumps({
        "id": art["id"], "class": "answered", "reason": "long ago",
        "expires_at": "2020-01-01T00:00:00Z",
    }))
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 0
    assert "EXPIRED" in capsys.readouterr().err


def test_inquiry_duplicate_refused(tmp_path, queue):
    art = inquiry_draft()
    assert file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)]) == 0
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 3


def test_inquiry_wrong_kind_refused(tmp_path, queue):
    art = proposal_draft()
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 2
    assert not list((queue / "inquiries").glob("*.json"))


def test_inquiry_bad_id_gets_remedy(tmp_path, queue, capsys):
    art = inquiry_draft()
    art["id"] = "sha256:" + "f" * 64
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 1
    assert "set id to sha256:" in capsys.readouterr().err


# --------------------------------------------------------------------------
# review round 2 (codex-critic findings): races, fail-closed, exit contract
# --------------------------------------------------------------------------

def test_publish_race_loser_gets_exit_3_and_artifact_survives(tmp_path, queue, monkeypatch):
    """FI-01: two producers race; the loser must report a duplicate and the
    first artifact must be byte-identical afterwards (immutability held)."""
    art = inquiry_draft()
    stem = art["id"].replace(":", "_")
    target = queue / "inquiries" / f"{stem}.json"
    target.write_text('{"first": "writer wins"}')
    # bypass the pre-check so the exclusive create is what decides
    monkeypatch.setattr(file_inquiry, "check_inquiry_identity", lambda a, q, r: None)
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 3
    assert target.read_text() == '{"first": "writer wins"}'
    assert (queue / "ledger.jsonl").read_text() == ""  # loser appends nothing


def test_atomic_write_refuses_existing_target(tmp_path):
    target = tmp_path / "a.json"
    target.write_text("{}")
    with pytest.raises(FileExistsError):
        file_proposal.atomic_write(target, {"x": 1})
    assert target.read_text() == "{}"
    assert not list(tmp_path.glob("*.tmp*"))  # tmp cleaned up on the loss


def test_corrupt_tombstone_fails_closed_inquiry(tmp_path, queue):
    """FI-02: an unreadable rejected/ marker must block (exit 2, could-not-run), not vanish."""
    art = inquiry_draft()
    stem = art["id"].replace(":", "_")
    (queue / "rejected" / f"{stem}.json").write_text("{not json")
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 2
    assert not list((queue / "inquiries").glob("*.json"))


def test_corrupt_tombstone_fails_closed_proposal(tmp_path, queue, root):
    art = proposal_draft()
    stem = art["id"].replace(":", "_")
    (queue / "rejected" / f"{stem}.json").write_text("{not json")
    rc = file_proposal.main([str(write_draft(tmp_path, art)),
                             "--queue", str(queue), "--root", f"repo={root}", "--check"])
    assert rc == 2


def test_parked_marker_never_decays_inquiry(tmp_path, queue):
    """FI-03: a park with a PAST expires_at is still parked — only a human
    unparked event ends a park, never a TTL."""
    art = inquiry_draft()
    stem = art["id"].replace(":", "_")
    (queue / "parked" / f"{stem}.json").write_text(json.dumps({
        "id": art["id"], "reason": "operator decision owed",
        "expires_at": "2020-01-01T00:00:00Z", "needs": "human",
    }))
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 3
    assert not list((queue / "inquiries").glob("*.json"))


def test_parked_marker_never_decays_proposal(tmp_path, queue, root):
    art = proposal_draft()
    stem = art["id"].replace(":", "_")
    (queue / "parked" / f"{stem}.json").write_text(json.dumps({
        "id": art["id"], "reason": "operator decision owed",
        "expires_at": "2020-01-01T00:00:00Z", "needs": "human",
    }))
    rc = file_proposal.main([str(write_draft(tmp_path, art)),
                             "--queue", str(queue), "--root", f"repo={root}", "--check"])
    assert rc == 3


def test_write_failure_exits_2(tmp_path, queue, monkeypatch):
    """FI-04a: an OSError during publish is a refusal, not a traceback."""
    art = inquiry_draft()
    monkeypatch.setattr(file_inquiry, "atomic_write",
                        lambda p, o: (_ for _ in ()).throw(OSError("disk full")))
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 2


def test_ledger_failure_rolls_back_artifact(tmp_path, queue, monkeypatch, capsys):
    """FI-04b: ledger-append failure rolls the publish back — status is
    ledger-derived, so an artifact without its event is contract-invalid.
    Exit 2 (could-not-run); a plain re-run recovers."""
    art = inquiry_draft()
    monkeypatch.setattr(file_inquiry, "append_inquired",
                        lambda q, a: (_ for _ in ()).throw(OSError("EROFS")))
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 2
    assert not list((queue / "inquiries").glob("*.json"))
    assert "rolled back" in capsys.readouterr().err


def test_no_ledger_flag_refuses(tmp_path, queue):
    """FI-04c: --no-ledger would mint an invisible artifact; it refuses."""
    art = inquiry_draft()
    rc = file_inquiry.main([str(write_draft(tmp_path, art)),
                            "--queue", str(queue), "--no-ledger"])
    assert rc == 2
    assert not list((queue / "inquiries").glob("*.json"))
    assert (queue / "ledger.jsonl").read_text() == ""


def test_lens_nondict_payload_never_raises():
    """LENS-01: a payload that passed nothing else must not crash the lens."""
    for bad in (["a", "b"], "prose", 7, None):
        assert isinstance(lens_warnings({"payload": bad, "evidence": ["x"]}), list)


def test_lens_crash_does_not_block_filing(tmp_path, queue, root, monkeypatch, capsys):
    import contract_lens
    monkeypatch.setattr(contract_lens, "lens_warnings",
                        lambda a: (_ for _ in ()).throw(RuntimeError("boom")))
    art = proposal_draft()
    rc = file_proposal.main([str(write_draft(tmp_path, art)),
                             "--queue", str(queue), "--root", f"repo={root}", "--check"])
    assert rc == 0
    assert "lens.error" in capsys.readouterr().err


def test_empty_values_do_not_satisfy_lens(capsys):
    """LENS-02: present-but-empty reads as absent."""
    art = proposal_draft(
        {"considered_and_rejected": "  ", "observed_at_main_sha": ""},
        evidence=[{"claim": "x", "verified_by": "execution",
                   "command": "", "exit_code": 0}],
    )
    classes = _classes(lens_warnings(art))
    assert {"lens.evidence", "lens.dead_ends", "lens.deps"} <= classes


def test_prose_evidence_gets_upgrade_wording():
    art = proposal_draft({"evidence": ["re-runnable: wc -c AGENTS.md"]})
    warns = [w for w in lens_warnings(art) if w.startswith("lens.evidence")]
    assert warns and "structured entry" in warns[0]


# --------------------------------------------------------------------------
# review round 3 (codex-critic round-2 findings)
# --------------------------------------------------------------------------

def test_concurrent_same_process_writers_one_winner_no_corruption(tmp_path):
    """R2-FI-01: N threads publish DISTINCT content to ONE target. Exactly one
    file survives, it is a COMPLETE valid JSON (never interleaved/truncated),
    and its bytes match one writer's — pid-named temps could not guarantee this."""
    import threading
    target = tmp_path / "p" / "art.json"
    target.parent.mkdir()
    results: list[object] = []
    barrier = threading.Barrier(8)

    def writer(n: int) -> None:
        barrier.wait()
        try:
            file_proposal.atomic_write(target, {"writer": n, "pad": "x" * 500})
            results.append(("won", n))
        except FileExistsError:
            results.append(("lost", n))

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(1 for r in results if r[0] == "won") == 1
    assert sum(1 for r in results if r[0] == "lost") == 7
    loaded = json.loads(target.read_text())          # complete, parseable
    assert loaded["writer"] in range(8) and loaded["pad"] == "x" * 500
    assert not list(target.parent.glob("*.tmp*"))     # every temp cleaned up


@pytest.mark.parametrize("writer_mod,kind", [(file_inquiry, "inquiry"),
                                             (file_proposal, "proposal")])
@pytest.mark.parametrize("area", ["rejected", "parked"])
def test_valid_nonobject_marker_fails_closed(tmp_path, queue, root, writer_mod, kind, area):
    """R2-FI-02: a marker that is valid JSON but not an object (e.g. []) must
    refuse with exit 2 (could-not-run), not AttributeError, for both writers and both dirs."""
    art = inquiry_draft() if kind == "inquiry" else proposal_draft()
    stem = art["id"].replace(":", "_")
    (queue / area / f"{stem}.json").write_text("[]")
    argv = [str(write_draft(tmp_path, art)), "--queue", str(queue)]
    if kind == "proposal":
        argv += ["--root", f"repo={root}", "--check"]
    assert writer_mod.main(argv) == 2


def test_proposal_no_ledger_refuses(tmp_path, queue, root):
    """R2-FI-04: file_proposal --no-ledger must refuse, matching file_inquiry —
    an artifact without its event is an invisible filing."""
    art = proposal_draft()
    rc = file_proposal.main([str(write_draft(tmp_path, art)),
                             "--queue", str(queue), "--root", f"repo={root}",
                             "--no-ledger"])
    assert rc == 2
    assert not list((queue / "proposals").glob("*.json"))


def test_ledger_event_present_but_append_errored_keeps_artifact(
        tmp_path, queue, monkeypatch, capsys):
    """R2-FI-04: the append wrote its bytes (event on disk) then raised — the
    caller must READ the ledger, see the event, and KEEP the artifact rather
    than roll back and orphan a real ledger line."""
    art = inquiry_draft()

    def wrote_then_raised(q, a):
        # bytes land, THEN an error is reported — the append-then-error race
        with open(q / "ledger.jsonl", "a") as fh:
            fh.write(json.dumps({"event": "inquired", "id": a["id"]}) + "\n")
        raise OSError("close EIO after durable append")

    monkeypatch.setattr(file_inquiry, "append_inquired", wrote_then_raised)
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 0
    assert len(list((queue / "inquiries").glob("*.json"))) == 1
    err = capsys.readouterr().err
    assert "is on disk" in err and "KEPT" in err
    # NO phantom: exactly one event line, artifact present
    assert (queue / "ledger.jsonl").read_text().count('"inquired"') == 1


def test_unreadable_ledger_during_readback_keeps_artifact_no_phantom(
        tmp_path, queue, monkeypatch, capsys):
    """R3-FI-01: the append lands its bytes then raises, AND the readback finds
    the ledger unreadable. UNKNOWN must be treated like PRESENT (keep the
    artifact) — rolling back on an unconfirmable absence is what creates a
    phantom (§5 forbids deleting the durable event to repair it)."""
    art = inquiry_draft()

    def wrote_then_raised(q, a):
        with open(q / "ledger.jsonl", "a") as fh:
            fh.write(json.dumps({"event": "inquired", "id": a["id"]}) + "\n")
        raise OSError("EIO after durable append")

    monkeypatch.setattr(file_inquiry, "append_inquired", wrote_then_raised)
    # the post-append readback cannot read the ledger → UNKNOWN
    import file_proposal
    monkeypatch.setattr(file_inquiry, "_ledger_event_state",
                        lambda q, i, e: file_proposal.LEDGER_EVENT_UNKNOWN)
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 0                                    # not rolled back
    assert len(list((queue / "inquiries").glob("*.json"))) == 1   # artifact kept
    assert (queue / "ledger.jsonl").read_text().count('"inquired"') == 1  # no phantom
    assert "could not be confirmed" in capsys.readouterr().err


def test_ledger_event_state_unreadable_is_unknown_not_absent(tmp_path):
    """R3-FI-01 at the unit level: an unreadable ledger is UNKNOWN, never
    ABSENT — the mapping that let a rollback delete a durable event."""
    import file_proposal
    missing = tmp_path / "does-not-exist"
    (missing).mkdir()
    # a directory where ledger.jsonl should be a file → read raises OSError
    (missing / "ledger.jsonl").mkdir()
    state = file_proposal._ledger_event_state(missing, "sha256:x", "proposed")
    assert state == file_proposal.LEDGER_EVENT_UNKNOWN


def test_ledger_write_fail_rollback_reports_orphan_when_unlink_fails(
        tmp_path, queue, monkeypatch, capsys):
    """R2-FI-04: if the ledger write never lands AND the rollback unlink also
    fails, the message must say ORPHANED, not falsely claim 'rolled back'."""
    art = inquiry_draft()
    monkeypatch.setattr(file_inquiry, "append_inquired",
                        lambda q, a: (_ for _ in ()).throw(OSError("ENOSPC")))
    monkeypatch.setattr(file_inquiry, "_rollback", lambda target: False)
    rc = file_inquiry.main([str(write_draft(tmp_path, art)), "--queue", str(queue)])
    assert rc == 2
    assert "ORPHANED" in capsys.readouterr().err


def test_list_shaped_blank_considered_and_rejected_warns():
    """R2-LENS-02: a list of whitespace strings is blank, not present."""
    art = proposal_draft({"considered_and_rejected": ["   ", "\t"]})
    assert "lens.dead_ends" in _classes(lens_warnings(art))
    art = proposal_draft({"considered_and_rejected": ["X: rejected because Y"]})
    assert "lens.dead_ends" not in _classes(lens_warnings(art))


def test_reference_class_fires_at_most_once():
    """LENS-03: opaque deps + missing sha pin + unpinned plan authority are one
    class (2, lens.deps) and produce exactly ONE warning; an artifact tripping
    everything yields exactly six warnings with six unique prefixes."""
    art = proposal_draft({"implementation_plan":
                          "1. execute ~/Work/Ops/Plans/Some-Plan.md section 3",
                          "dependencies": ["audit-5"]})
    refs = [w for w in lens_warnings(art) if w.startswith("lens.deps")]
    assert len(refs) == 1
    assert "observed_at_main_sha" in refs[0] and "plan_authority_sha256" in refs[0]
    everything_bad = proposal_draft({
        "tests_required": ["the behaviour must be correct"],
        "dependencies": ["audit-5"],
        "urgency": "Morgan-named night focus",
        "implementation_plan": "1. do. 2. redo. 3. verify.",
    })
    warns = lens_warnings(everything_bad)
    assert len(warns) == len(_classes(warns)) == 6
