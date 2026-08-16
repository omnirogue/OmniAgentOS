"""A ledger event whose `detail` is not a dict must never crash a reader.

Promoted from a live estate-wide outage on 2026-08-09, not from principle. The
Reviewer loop appended 11 events carrying `detail` as a STRING instead of an
object. `LedgerView.build` does `detail = ev.get("detail") or {}` and then
`detail.get("title")` — a non-empty string survives the `or {}` and has no
`.get`, so the sole writer of state/queue.json died with AttributeError and
com.threeloops.publish-queue went to exit 1. queue.json froze, and every loop
reads its staleness as a halt condition.

`ledger.jsonl` is append-only and is never rewritten, so those events are
PERMANENT. Tolerating them is not a nicety, it is the only available repair.

Direction matters: a malformed detail must cost the event its *detail-derived*
fields only. The event must still count for status and WIP. Dropping the event
would under-count WIP, which invents headroom — the dangerous direction.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import integration as I  # noqa: E402


def _write(root: Path, events: list[dict]) -> None:
    (root / "state").mkdir(parents=True, exist_ok=True)
    with (root / "ledger.jsonl").open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def test_build_survives_string_detail_and_keeps_the_id(tmp_path: Path) -> None:
    """The exact shape that killed the publisher."""
    _write(tmp_path, [
        {"ts": "2026-08-09T05:55:17Z", "role": "reviewer", "event": "found",
         "id": "sha256:" + "a" * 64, "actor": "reviewer-loop",
         "detail": "codex-critic scratch root collides on base head"},
    ])
    view = I.LedgerView.build(tmp_path)          # must not raise
    ident = "sha256:" + "a" * 64
    assert view.status.get(ident) == "open"      # still counted, not dropped
    assert ident not in view.titles              # no title derivable, and that is fine


def test_admitted_with_string_detail_still_occupies_a_wip_slot(tmp_path: Path) -> None:
    """Under-counting WIP is the dangerous direction; prove we do not."""
    ident = "sha256:" + "b" * 64
    _write(tmp_path, [
        {"ts": "2026-08-09T05:00:00Z", "role": "implementer", "event": "admitted",
         "id": ident, "actor": "x", "detail": "a bare string, not an object"},
    ])
    view = I.LedgerView.build(tmp_path)
    assert ident in view.admitted
    assert ident not in view.terminal


def test_every_scalar_detail_shape_is_tolerated(tmp_path: Path) -> None:
    """JSON allows more non-objects than just strings."""
    for n, bad in enumerate([[], 42, "s", True, [1, 2]]):
        ident = "sha256:" + f"{n:064d}"
        _write(tmp_path, [{"ts": "t", "role": "r", "event": "found",
                           "id": ident, "actor": "a", "detail": bad}])
        view = I.LedgerView.build(tmp_path)
        assert view.status.get(ident) == "open", bad


def test_gate_and_instrument_lookups_survive_string_detail(tmp_path: Path) -> None:
    """The same idiom guards short-circuit decisions; a crash there stalls gating."""
    ident = "sha256:" + "c" * 64
    _write(tmp_path, [
        {"ts": "t", "role": "implementer", "event": "gated", "id": ident,
         "actor": "a", "detail": "string where an object belongs"},
        {"ts": "t", "role": "implementer", "event": "instrument_error", "id": ident,
         "actor": "a", "detail": "string where an object belongs"},
    ])
    view = I.LedgerView.build(tmp_path)
    # A malformed record must read as "no prior run matches", never as a crash
    # and never as a false match that would short-circuit a real gate.
    assert view.prior_gate(ident, "deadbeef", None) is None
    assert view.prior_instrument_error(ident, "deadbeef", "fp") is None


def test_rebuild_queue_publishes_with_a_string_detail_present(tmp_path: Path) -> None:
    """End-to-end: the publisher path itself must survive."""
    ident = "sha256:" + "d" * 64
    _write(tmp_path, [
        {"ts": "t", "role": "implementer", "event": "admitted", "id": ident,
         "actor": "a", "detail": {"title": "real object"}},
        {"ts": "t", "role": "reviewer", "event": "found",
         "id": "sha256:" + "e" * 64, "actor": "a", "detail": "bare string"},
    ])
    ledger = I.LedgerView.build(tmp_path)
    q = I.rebuild_queue(tmp_path, ledger, 8)
    assert q["wip"] >= 1
    assert isinstance(q.get("items"), list)


def test_malformed_record_never_matches_even_when_the_query_is_none(tmp_path: Path) -> None:
    """The one direction that must never happen: a false short-circuit.

    `_event_detail` returns `{}` for a malformed record, and `{}.get(k)` is
    None. If a caller ever queries with None, an empty dict would compare EQUAL
    and the lookup would report a prior run that does not exist — skipping a
    real gate. A false "no prior run" only costs one gate run; a false "prior
    run matches" silently lands ungated work. So malformed records are skipped
    outright, not merely emptied.
    """
    ident = "sha256:" + "f" * 64
    _write(tmp_path, [
        {"ts": "t", "role": "implementer", "event": "gated", "id": ident,
         "actor": "a", "detail": "malformed"},
        {"ts": "t", "role": "implementer", "event": "instrument_error", "id": ident,
         "actor": "a", "detail": "malformed"},
    ])
    view = I.LedgerView.build(tmp_path)
    assert view.prior_gate(ident, None, None) is None
    assert view.prior_instrument_error(ident, None, None) is None


def test_integrity_flags_a_malformed_detail_rather_than_reporting_clean(tmp_path: Path) -> None:
    """Tolerance without DETECTION just delays the next outage.

    integration.py must survive a malformed `detail` so the queue can be built.
    integrity.py is the ledger-health auditor, so it must SAY SO — otherwise it
    reports clean health while a producer writes records no reader can use, and
    the next occurrence is found the way this one was: by an outage.

    Pinned on a `found` event deliberately. A malformed `rejected` event already
    trips `rejection.has_expires_at` incidentally, which is what made this look
    covered when it is not.
    """
    import subprocess

    root = tmp_path / "lq"
    (root / "state").mkdir(parents=True)
    (root / "ledger.jsonl").write_text(json.dumps(
        {"ts": "t", "role": "reviewer", "event": "found", "id": "sha256:" + "a" * 64,
         "actor": "reviewer-loop", "detail": "a bare string, not an object"}) + "\n")

    repo = Path(__file__).resolve().parents[1]
    out = subprocess.run(
        [sys.executable, str(repo / "bridge" / "integrity.py"),
         "--loops-root", str(root), "--category", "invariants"],
        capture_output=True, text=True)
    combined = out.stdout + out.stderr
    assert "ledger.detail_is_an_object" in combined, combined
