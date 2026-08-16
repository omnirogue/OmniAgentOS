"""Closure contract Phase 1: the `closed` FINDING-side terminal event.

A finding whose bound test ran GREEN in the gate run that landed its resolving
candidate is CLOSED — by the gate loop, never by an agent's self-report. Before
this event existed, "fixed" was a claim: `merged` terminalises the candidate,
but nothing terminalised the finding it existed to close, so findings
accumulated forever (518 measured on 2026-08-10) and a fix that silently
regressed was indistinguishable from one that held.

These tests are the executable definition of `closed`: schema-valid only with
its full provenance (closed_by + merge_sha + bound_test), terminal in the
derived status map, never occupying WIP, and inert for every pre-existing
event flow.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import integration as I  # noqa: E402


def _write_ledger(root: Path, events: list[dict]) -> None:
    (root / "state").mkdir(parents=True, exist_ok=True)
    with (root / "ledger.jsonl").open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _schema() -> dict:
    return json.loads(
        (PKG / "schema" / "ledger-event.schema.json").read_text(encoding="utf-8"))


def _closed(fid: str = "sha256:" + "f" * 64) -> dict:
    return {
        "ts": "2026-08-10T15:00:00Z",
        "role": "implementer",
        "event": "closed",
        "id": fid,
        "actor": "gate-loop-daemon",
        "detail": {
            "closed_by": "sha256:" + "c" * 64,
            "merge_sha": "d" * 40,
            "bound_test": "tests/x/test_y.py::test_z",
        },
    }


# --------------------------------------------------------------- schema


def test_closed_validates_with_full_provenance() -> None:
    import jsonschema

    jsonschema.validate(_closed(), _schema())  # must not raise


def test_closed_is_in_the_event_enum_with_18_members() -> None:
    enum = _schema()["properties"]["event"]["enum"]
    assert enum == [
        "found",
        "inquired",
        "answered",
        "proposed",
        "claimed",
        "released",
        "claim_expired",
        "submitted",
        "admitted",
        "gated",
        "merged",
        "completed",
        "rejected",
        "parked",
        "unparked",
        "isolated",
        "instrument_error",
        "closed",
    ]


@pytest.mark.parametrize("missing", ["closed_by", "merge_sha", "bound_test"])
def test_closed_refuses_without_each_provenance_field(missing: str) -> None:
    """A `closed` minted on an unmeasured test is the worst outcome of the
    whole closure plan — every provenance field is load-bearing."""
    import jsonschema

    ev = _closed()
    del ev["detail"][missing]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(ev, _schema())


def test_closed_refuses_without_id_or_detail() -> None:
    import jsonschema

    for strip in ("id", "detail"):
        ev = _closed()
        del ev[strip]
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(ev, _schema())


def test_closed_refuses_malformed_provenance_shapes() -> None:
    import jsonschema

    bad = [("closed_by", "c" * 64),            # missing sha256: prefix
           ("merge_sha", "d" * 12),            # abbreviated sha
           ("bound_test", "")]                 # empty binding
    for key, val in bad:
        ev = _closed()
        ev["detail"][key] = val
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(ev, _schema())


# --------------------------------------------------------------- status map


def test_closed_id_is_terminal(tmp_path: Path) -> None:
    fid = "sha256:" + "f" * 64
    _write_ledger(tmp_path, [
        {"ts": "2026-08-10T10:00:00Z", "role": "reviewer", "event": "found",
         "id": fid, "actor": "reviewer-loop"},
        _closed(fid),
    ])
    v = I.LedgerView.build(tmp_path)
    assert v.status[fid] == "closed"
    assert fid in v.terminal


def test_closed_wins_over_a_later_non_terminal_event(tmp_path: Path) -> None:
    """First terminal wins; a late stray `found` must not reopen it."""
    fid = "sha256:" + "f" * 64
    _write_ledger(tmp_path, [
        _closed(fid),
        {"ts": "2026-08-10T16:00:00Z", "role": "reviewer", "event": "found",
         "id": fid, "actor": "reviewer-loop"},
    ])
    v = I.LedgerView.build(tmp_path)
    assert v.status[fid] == "closed"
    assert fid in v.terminal


def test_closed_finding_never_occupies_wip(tmp_path: Path) -> None:
    """A closed finding is neither current WIP nor WIP-entry provenance."""
    fid = "sha256:" + "f" * 64
    _write_ledger(tmp_path, [
        {"ts": "2026-08-10T10:00:00Z", "role": "reviewer", "event": "found",
         "id": fid, "actor": "reviewer-loop"},
        _closed(fid),
    ])
    v = I.LedgerView.build(tmp_path)
    queue = I.rebuild_queue(tmp_path, v, wip_cap=24)
    assert fid not in v.wip_entered
    assert queue["wip"] == 0


def test_closed_clears_a_park(tmp_path: Path) -> None:
    """Terminal events discard a live park, exactly like merged/rejected."""
    fid = "sha256:" + "f" * 64
    _write_ledger(tmp_path, [
        {"ts": "2026-08-10T10:00:00Z", "role": "reviewer", "event": "parked",
         "id": fid, "actor": "operator"},
        _closed(fid),
    ])
    v = I.LedgerView.build(tmp_path)
    assert fid not in v.parked and v.status[fid] == "closed"


def test_closed_and_merged_terminalise_two_DIFFERENT_axes(tmp_path: Path) -> None:
    """The landing that closes a finding writes BOTH events, on two ids: `merged`
    on the candidate (the code landed) and `closed` on the finding (the defect is
    gone, proven by its bound test). Collapsing them onto one id is what made
    "fixed" a claim rather than a measurement — the whole reason this event
    exists — so the derived map must keep them apart."""
    cand = "sha256:" + "c" * 64
    fid = "sha256:" + "f" * 64
    merge_sha = "d" * 40
    ev = _closed(fid)
    ev["detail"]["closed_by"] = cand
    ev["detail"]["merge_sha"] = merge_sha
    _write_ledger(tmp_path, [
        {"ts": "2026-08-10T10:00:00Z", "role": "reviewer", "event": "found",
         "id": fid, "actor": "reviewer-loop"},
        {"ts": "2026-08-10T14:59:59Z", "role": "implementer", "event": "merged",
         "id": cand, "actor": "gate-loop-daemon",
         "detail": {"result": "pass", "merge_sha": merge_sha}},
        ev,
    ])
    v = I.LedgerView.build(tmp_path)
    assert v.status[cand] == "merged"
    assert v.status[fid] == "closed"
    assert {cand, fid} <= set(v.terminal)


def test_pre_existing_flows_unaffected(tmp_path: Path) -> None:
    """The map edit is additive: merged/rejected/completed derivation is
    byte-identical for a ledger with no closed events."""
    ids = {k: "sha256:" + k * 64 for k in ("1", "2", "3")}
    _write_ledger(tmp_path, [
        {"ts": "t", "role": "implementer", "event": "merged", "id": ids["1"],
         "detail": {"merge_sha": "a" * 40}},
        {"ts": "t", "role": "implementer", "event": "rejected", "id": ids["2"],
         "detail": {"reason": "r", "class": "candidate-defect"}},
        {"ts": "t", "role": "implementer", "event": "completed", "id": ids["3"],
         "detail": {"reason": "host-ops"}},
    ])
    v = I.LedgerView.build(tmp_path)
    assert v.status[ids["1"]] == "merged"
    assert v.status[ids["2"]] == "rejected"
    assert v.status[ids["3"]] == "completed"
    assert set(v.terminal) == set(ids.values())


# ------------------------------------------- sibling terminal-event readers
# The clone family (Sol integration-review blocker, 2026-08-10): every module
# that derives "is this id terminal?" must agree `closed` is terminal, or the
# CONTRACT guarantee is enforced in only some readers — a closed finding would
# never be retention-swept, and the reconciler could stamp a second terminal
# over it. One fix here is structurally several across the family.


def _closed_line(ident: str, ts: str = "2026-08-10T15:00:00Z") -> str:
    ev = _closed(ident)
    ev["ts"] = ts
    return json.dumps(ev)


def test_janitor_read_ledger_treats_closed_as_terminal(tmp_path: Path) -> None:
    from bridge import janitor

    root = tmp_path / "lq"
    (root / "state").mkdir(parents=True)
    ident = "sha256:" + "f" * 64
    (root / "ledger.jsonl").write_text(_closed_line(ident) + "\n")
    terminal, merged = janitor.read_ledger(root)
    assert ident in terminal
    assert terminal[ident].get("event") == "closed"
    assert "closed" in janitor.TERMINAL_EVENTS


def test_janitor_sweeps_a_closed_finding_after_7_days(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from bridge import janitor

    root = tmp_path / "lq"
    for sub in ("candidates", "inquiries", "findings", "proposals", "rejected",
                "parked", "receipts", "claims", "state"):
        (root / sub).mkdir(parents=True)
    ident = "sha256:" + "1" * 64
    stem = ident.replace(":", "_", 1)
    (root / "findings" / f"{stem}.json").write_text(
        json.dumps({"id": ident, "kind": "finding"}))
    old = (datetime.now(UTC) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (root / "ledger.jsonl").write_text(_closed_line(ident, ts=old) + "\n")

    j = janitor.Janitor(root, apply=False)
    j.sweep()
    assert any("delete" in a and stem in a for a in j.actions), j.actions


def test_pr_reconcile_counts_closed_as_terminal(tmp_path: Path) -> None:
    """The reconciler must never stamp a `rejected` over a closed finding."""
    from bridge import pr_reconcile

    root = tmp_path / "lq"
    root.mkdir(parents=True)
    ident = "sha256:" + "2" * 64
    (root / "ledger.jsonl").write_text(_closed_line(ident) + "\n")
    assert ident in pr_reconcile._ids_with_terminal_ledger_event(root)
    assert "closed" in pr_reconcile.TERMINAL_LEDGER_EVENTS


def test_integrity_flags_closed_plus_rejected_as_double_terminal(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "lq"
    (root / "state").mkdir(parents=True)
    ident = "sha256:" + "3" * 64
    (root / "ledger.jsonl").write_text(
        _closed_line(ident) + "\n"
        + json.dumps({"ts": "2026-08-10T16:00:00Z", "role": "implementer",
                      "event": "rejected", "id": ident, "actor": "x",
                      "detail": {"reason": "r", "class": "candidate-defect"}}) + "\n")
    out = subprocess.run(
        [sys.executable, str(PKG / "bridge" / "integrity.py"),
         "--loops-root", str(root), "--category", "invariants"],
        capture_output=True, text=True)
    combined = out.stdout + out.stderr
    assert "ledger.exactly_one_terminal_event" in combined, combined


def test_gate_loop_terminal_ids_include_closed(tmp_path: Path) -> None:
    """The daemon's own terminal scan must skip closed ids — otherwise the
    queue (LedgerView) and the lander disagree by one WIP slot."""
    from bridge.gate_loop import GateLoop

    root = tmp_path / "lq"
    (root / "state").mkdir(parents=True)
    ident = "sha256:" + "4" * 64
    (root / "ledger.jsonl").write_text(_closed_line(ident) + "\n")
    loop = GateLoop(root, tmp_path / "repo", remote=None, push=False)
    assert ident in loop._terminal_ids()


def test_gap_filer_terminal_set_includes_closed() -> None:
    text = (PKG.parent / "scripts" / "northstar_cert"
            / "file_gap_findings.py").read_text(encoding="utf-8")
    assert '"closed"' in text.split("TERMINAL_EVENTS", 1)[1][:200]


def test_rebuild_queue_excludes_closed_ids_from_items(tmp_path: Path) -> None:
    """queue.json's sole writer must retire a closed id like every other
    terminal — the ledger is append-only and never rewritten, so a closed
    finding left in `items` would reappear in every 300s rebuild forever,
    listed as in-flight in the very selection list implementers claim from."""
    fid = "sha256:" + "f" * 64
    live = "sha256:" + "a" * 64
    _write_ledger(tmp_path, [
        {"ts": "2026-08-10T10:00:00Z", "role": "reviewer", "event": "found",
         "id": fid, "actor": "reviewer-loop"},
        {"ts": "2026-08-10T10:00:01Z", "role": "reviewer", "event": "found",
         "id": live, "actor": "reviewer-loop"},
        _closed(fid),
    ])
    v = I.LedgerView.build(tmp_path)
    q = I.rebuild_queue(tmp_path, v, wip_cap=24)
    ids = {item["id"] for item in q["items"]}
    assert fid not in ids, "closed id must leave the queue like merged/rejected"
    assert live in ids
    assert q["wip"] == 0
