"""Ruling D13(a): a `completed` TERMINAL event for out-of-repo / host-ops work.

Some verified-and-applied work legitimately has no `merge_sha` — it never
touched this repo (a host-ops change, an external system reconfigured, a
manual operator action confirmed done). Before this event existed the queue
vocabulary forced a bad choice:

  * `merged` REQUIRES `detail.merge_sha` (schema allOf) — a sha that cannot
    exist for work that never merged here, so the event cannot even validate.
  * `rejected` is a refusal — the wrong tombstone for work that was VERIFIED
    and APPLIED, and it carries a TTL/`class` that reads as "try again later".

`completed` closes that gap. It is terminal exactly like `merged`/`rejected`:
its id must reach terminal status, must NOT occupy a WIP slot, and is swept by
the same 7-day terminal retention. It carries no `merge_sha`.

These tests are the executable definition of "terminal" for `completed`:
schema-valid, terminal status, excluded from WIP, and swept by retention.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import integration as I  # noqa: E402
from bridge import janitor  # noqa: E402


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_ledger(root: Path, events: list[dict]) -> None:
    (root / "state").mkdir(parents=True, exist_ok=True)
    with (root / "ledger.jsonl").open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


# --------------------------------------------------------------- schema valid


def test_completed_validates_against_the_ledger_schema() -> None:
    """A `completed` event with no `merge_sha` must pass the schema.

    `merged` requires `detail.merge_sha`; `completed` must NOT — that absence is
    the entire reason the event exists. It still carries an `id` (required for
    every event except `instrument_error`).
    """
    import jsonschema

    schema = json.loads(
        (PKG / "schema" / "ledger-event.schema.json").read_text(encoding="utf-8"))
    event = {
        "ts": "2026-08-09T12:00:00Z",
        "role": "implementer",
        "event": "completed",
        "id": "sha256:" + "a" * 64,
        "actor": "operator",
        "detail": {"reason": "host-ops change verified and applied; no repo merge"},
    }
    jsonschema.validate(event, schema)  # must not raise


def test_completed_still_requires_an_id() -> None:
    """`completed` is not `instrument_error`, so it must name an artifact."""
    import jsonschema

    schema = json.loads(
        (PKG / "schema" / "ledger-event.schema.json").read_text(encoding="utf-8"))
    event = {"ts": "2026-08-09T12:00:00Z", "role": "implementer", "event": "completed"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(event, schema)


def test_completed_requires_detail_reason() -> None:
    """A `completed` event with no `detail` (or `detail.reason`) at all must be
    REFUSED — 'terminal' cannot mean 'no evidence of what was applied or how it
    was verified'. CONTRACT.md §5 documents `detail.reason` as naming what was
    applied and how it was verified; the schema must enforce that, exactly as
    it enforces `detail.merge_sha` on `merged` and `detail.reason`/`class`/
    `expires_at` on `rejected`."""
    import jsonschema

    schema = json.loads(
        (PKG / "schema" / "ledger-event.schema.json").read_text(encoding="utf-8"))

    no_detail = {"ts": "2026-08-09T12:00:00Z", "role": "implementer",
                 "event": "completed", "id": "sha256:" + "a" * 64}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(no_detail, schema)

    empty_detail = {**no_detail, "detail": {}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(empty_detail, schema)

    with_reason = {**no_detail, "detail": {
        "reason": "host-ops config applied on prod-3; verified via `systemctl status`"}}
    jsonschema.validate(with_reason, schema)  # must not raise


# --------------------------------------------------------------- terminal


def test_completed_id_is_terminal(tmp_path: Path) -> None:
    ident = "sha256:" + "b" * 64
    _write_ledger(tmp_path, [
        {"ts": "2026-08-09T10:00:00Z", "role": "implementer", "event": "admitted",
         "id": ident, "actor": "x", "detail": {"branch": "ops/x", "title": "t"}},
        {"ts": "2026-08-09T11:00:00Z", "role": "implementer", "event": "completed",
         "id": ident, "actor": "operator", "detail": {"reason": "applied out of repo"}},
    ])
    view = I.LedgerView.build(tmp_path)
    assert ident in view.terminal
    assert view.terminal[ident].get("event") == "completed"
    assert view.status.get(ident) == "completed"


def test_completed_after_admitted_does_not_occupy_a_wip_slot(tmp_path: Path) -> None:
    done = "sha256:" + "c" * 64
    live = "sha256:" + "d" * 64
    _write_ledger(tmp_path, [
        {"ts": "t", "role": "implementer", "event": "admitted", "id": done,
         "actor": "x", "detail": {"branch": "ops/done"}},
        {"ts": "t", "role": "implementer", "event": "completed", "id": done,
         "actor": "operator", "detail": {"reason": "done out of repo"}},
        {"ts": "t", "role": "implementer", "event": "admitted", "id": live,
         "actor": "x", "detail": {"branch": "feat/live"}},
    ])
    ledger = I.LedgerView.build(tmp_path)
    q = I.rebuild_queue(tmp_path, ledger, 8)
    ids = {i["id"] for i in q["items"]}
    assert done not in ids           # terminal: dropped from the queue entirely
    assert live in ids               # the still-open one remains
    assert q["wip"] == 1             # only the admitted-not-terminal id counts


def test_completed_wins_over_a_later_non_terminal_event(tmp_path: Path) -> None:
    """Exactly one terminal event: once completed, a stray later event on the
    same id must not resurrect it into a WIP slot."""
    ident = "sha256:" + "e" * 64
    _write_ledger(tmp_path, [
        {"ts": "t", "role": "implementer", "event": "completed", "id": ident,
         "actor": "operator", "detail": {"reason": "applied"}},
        {"ts": "t", "role": "implementer", "event": "admitted", "id": ident,
         "actor": "x", "detail": {"branch": "ops/x"}},
    ])
    view = I.LedgerView.build(tmp_path)
    assert view.status.get(ident) == "completed"
    q = I.rebuild_queue(tmp_path, view, 8)
    assert ident not in {i["id"] for i in q["items"]}


# --------------------------------------------------------------- retention


def test_janitor_read_ledger_treats_completed_as_terminal(tmp_path: Path) -> None:
    root = tmp_path / "lq"
    (root / "state").mkdir(parents=True)
    ident = "sha256:" + "f" * 64
    (root / "ledger.jsonl").write_text(json.dumps(
        {"ts": "2026-08-01T00:00:00Z", "role": "implementer", "event": "completed",
         "id": ident, "actor": "operator", "detail": {"reason": "applied"}}) + "\n")
    terminal, merged = janitor.read_ledger(root)
    assert ident in terminal
    assert terminal[ident].get("event") == "completed"
    assert ident not in merged       # no merge_sha, never a merged reference


def test_janitor_sweeps_a_completed_artifact_after_7_days(tmp_path: Path) -> None:
    root = tmp_path / "lq"
    for sub in ("candidates", "inquiries", "findings", "proposals", "rejected",
                "parked", "receipts", "claims", "state"):
        (root / sub).mkdir(parents=True)
    ident = "sha256:" + "1" * 64
    stem = ident.replace(":", "_", 1)
    art = root / "candidates" / f"{stem}.json"
    art.write_text(json.dumps({"id": ident, "kind": "candidate"}))
    old = _iso(datetime.now(UTC) - timedelta(days=8))
    (root / "ledger.jsonl").write_text(json.dumps(
        {"ts": old, "role": "implementer", "event": "completed",
         "id": ident, "actor": "operator", "detail": {"reason": "applied"}}) + "\n")

    j = janitor.Janitor(root, apply=False)
    j.sweep()
    assert any("delete" in a and stem in a for a in j.actions), j.actions


# ------------------------------------------- sibling terminal-event readers
# The clone family: every module that derives "is this id terminal?" must agree
# that `completed` is terminal, or the CONTRACT §11 guarantee is only enforced
# in some readers. One fix here is structurally several across the family.


def test_pr_reconcile_counts_completed_as_terminal(tmp_path: Path) -> None:
    """The reconciler must treat an already-`completed` id as terminal so it
    never stamps a second (`rejected`) terminal event over it."""
    from bridge import pr_reconcile

    root = tmp_path / "lq"
    root.mkdir(parents=True)
    ident = "sha256:" + "2" * 64
    (root / "ledger.jsonl").write_text(json.dumps(
        {"ts": "2026-08-09T00:00:00Z", "role": "implementer", "event": "completed",
         "id": ident, "actor": "operator", "detail": {"reason": "applied"}}) + "\n")
    assert ident in pr_reconcile._ids_with_terminal_ledger_event(root)


def test_integrity_flags_completed_plus_merged_as_double_terminal(tmp_path: Path) -> None:
    """`completed` + `merged` on one id breaks exactly-one-terminal; the auditor
    must catch it (before this change it counted only merged/rejected)."""
    import subprocess

    root = tmp_path / "lq"
    (root / "state").mkdir(parents=True)
    ident = "sha256:" + "3" * 64
    (root / "ledger.jsonl").write_text(
        json.dumps({"ts": "2026-08-09T00:00:00Z", "role": "implementer",
                    "event": "completed", "id": ident, "actor": "operator",
                    "detail": {"reason": "applied"}}) + "\n"
        + json.dumps({"ts": "2026-08-09T01:00:00Z", "role": "implementer",
                      "event": "merged", "id": ident, "actor": "x",
                      "detail": {"merge_sha": "a" * 40}}) + "\n")
    out = subprocess.run(
        [sys.executable, str(PKG / "bridge" / "integrity.py"),
         "--loops-root", str(root), "--category", "invariants"],
        capture_output=True, text=True)
    combined = out.stdout + out.stderr
    assert "ledger.exactly_one_terminal_event" in combined, combined


def test_integrity_completed_alone_is_not_a_double_terminal(tmp_path: Path) -> None:
    """A single `completed` event is terminal but not a VIOLATION — the auditor
    must not false-flag it."""
    import subprocess

    root = tmp_path / "lq"
    (root / "state").mkdir(parents=True)
    ident = "sha256:" + "4" * 64
    (root / "ledger.jsonl").write_text(json.dumps(
        {"ts": "2026-08-09T00:00:00Z", "role": "implementer", "event": "completed",
         "id": ident, "actor": "operator", "detail": {"reason": "applied"}}) + "\n")
    out = subprocess.run(
        [sys.executable, str(PKG / "bridge" / "integrity.py"),
         "--loops-root", str(root), "--category", "invariants"],
        capture_output=True, text=True)
    combined = out.stdout + out.stderr
    assert "ledger.exactly_one_terminal_event" not in combined, combined
