"""`unrejected` must clear a `rejected` terminal — and nothing else may resurrect.

Models a corruption class of the file every loop reads.
In the modeled incident, five proposals whose last ledger event was `admitted` were ABSENT
from `state/queue.json`, because `LedgerView.build` treats the FIRST
merged/completed/rejected/closed as absorbing: it `setdefault`s the event into
`terminal`, writes `status`, and then `if ident in v.terminal: continue` drops
every LATER event for that id. Six operator-reasoned `unrejected` events
(2024-01-05T10:00:00Z, actor `implementer-loop@claude-account-5`) and the
`admitted`s that followed them were discarded in silence, so the published `wip`
under-read by ~5 and live, re-admitted work was unclaimable from the queue.

Direction matters, and it is not symmetric:

  * `merged`, `completed`, `closed` are HARD terminals. Landed work, verified
    out-of-repo work, and a gate-closed finding must NEVER come back — a later
    event on such an id is a bug somewhere else, and honouring it would let
    narration re-open something that already shipped.
  * `rejected` is the one REVERSIBLE terminal, and `unrejected` is its one
    reversal verb. It is reversible because a human overturns refusals, and a
    ledger must be able to record that.

So the correct rule is NOT "last event wins" (dead end #4 in the research file):
that is precisely what `test_merged_is_never_resurrected_by_unrejected` and its
two siblings exist to catch. Nor is it "add `unrejected` to the `order` map" —
that line is unreachable for an id already in `terminal` (the documented no-op
trap). The clear must happen ABOVE the terminal guard.

Research: internal ledger-replay note.
Proposal: sha256:0000000000000000000000000000000000000000000000000000000000000000
"""
from __future__ import annotations

import json
import sys
from itertools import product
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))

from bridge import integration as I  # noqa: E402

IDENT = "sha256:" + "a" * 64
WIP_CAP = 8


def _ev(minute: int, event: str, *, ident: str = IDENT, detail: dict | None = None) -> dict:
    return {
        "ts": f"2026-08-10T00:{minute:02d}:00Z",
        "role": "implementer",
        "event": event,
        "id": ident,
        "actor": "test",
        "detail": detail if detail is not None else {},
    }


def _write(root: Path, events: list[dict]) -> None:
    (root / "state").mkdir(parents=True, exist_ok=True)
    with (root / "ledger.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _replay(root: Path, events: list[dict]) -> tuple[I.LedgerView, dict]:
    """Build the view and the queue exactly as publish_queue does."""
    _write(root, events)
    view = I.LedgerView.build(root)
    return view, I.rebuild_queue(root, view, WIP_CAP)


def _item(queue: dict, ident: str = IDENT) -> dict | None:
    return next((it for it in queue["items"] if it["id"] == ident), None)


# --- WIP occupancy -----------------------------------------------------------


def test_unadmitted_instrument_error_is_visible_but_not_wip(tmp_path: Path) -> None:
    """An instrument diagnostic before admission is not an occupied slot."""
    _, queue = _replay(tmp_path, [_ev(0, "instrument_error")])

    assert _item(queue)["status"] == "blocked"
    assert queue["wip"] == 0


def test_admitted_then_instrument_error_remains_wip(tmp_path: Path) -> None:
    """A real admitted candidate remains WIP when a later instrument blocks it."""
    _, queue = _replay(tmp_path, [_ev(0, "admitted"), _ev(1, "instrument_error")])

    assert _item(queue)["status"] == "blocked"
    assert queue["wip"] == 1


def test_gated_then_instrument_error_remains_wip_without_admitted_event(
    tmp_path: Path,
) -> None:
    """A gate run is independent proof that the id entered WIP."""
    view, queue = _replay(tmp_path, [_ev(0, "gated"), _ev(1, "instrument_error")])

    assert IDENT not in view.admitted
    assert IDENT in view.wip_entered
    assert queue["wip"] == 1


def test_unparked_then_instrument_error_remains_wip(tmp_path: Path) -> None:
    """Unparking transitions status to admitted and therefore enters WIP."""
    view, queue = _replay(tmp_path, [_ev(0, "parked"), _ev(1, "unparked"),
                                     _ev(2, "instrument_error")])

    assert IDENT not in view.admitted
    assert IDENT in view.wip_entered
    assert queue["wip"] == 1


def test_discarded_entry_line_degrades_read_without_inventing_provenance(
    tmp_path: Path,
) -> None:
    """Damage is surfaced for refusal without guessing per-id provenance."""
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ledger.jsonl").write_text(
        '{"ts":"2026-08-10T00:00:00Z","event":"admi\n'
        + json.dumps(_ev(1, "instrument_error")) + "\n"
    )
    view = I.LedgerView.build(tmp_path)
    queue = I.rebuild_queue(tmp_path, view, WIP_CAP)

    assert view.discarded == 1
    assert IDENT not in view.wip_entered
    assert "wip" not in queue
    assert queue["wip_degraded"] is True
    assert queue["wip_degraded_detail"].startswith("line 1, byte 0: ")


def test_admitted_and_gated_statuses_still_count_as_wip(tmp_path: Path) -> None:
    """The recount changes only unadmitted blocked diagnostics."""
    gated_ident = "sha256:" + "b" * 64
    _, queue = _replay(tmp_path, [
        _ev(0, "admitted"),
        _ev(1, "gated", ident=gated_ident),
    ])

    assert queue["wip"] == 2


def test_blocked_on_human_refusal_occupies_wip(tmp_path: Path) -> None:
    """Waiting for an operator ruling is live WIP, not instrument debris."""
    _, queue = _replay(tmp_path, [
        _ev(0, "instrument_error", detail={"class": "blocked-on-human"}),
    ])

    assert _item(queue)["status"] == "blocked"
    assert queue["wip"] == 1


def test_terminal_event_clears_wip_entry_provenance(tmp_path: Path) -> None:
    """Terminal work cannot retain occupancy provenance or reappear as WIP."""
    view, queue = _replay(tmp_path, [_ev(0, "gated"), _ev(1, "merged")])

    assert IDENT not in view.wip_entered
    assert queue["wip"] == 0


def test_instrument_error_never_decreases_wip_for_short_event_prefixes(
    tmp_path: Path,
) -> None:
    """Exhaust every handled transition prefix through length four (7,381 prefixes)."""
    alphabet = (
        "admitted", "gated", "parked", "unparked", "instrument_error",
        "blocked-on-human", "rejected", "unrejected", "released",
    )

    def events(tokens: tuple[str, ...]) -> list[dict]:
        return [
            _ev(index, "instrument_error", detail={"class": "blocked-on-human"})
            if token == "blocked-on-human" else _ev(index, token)
            for index, token in enumerate(tokens)
        ]

    for length in range(5):
        for prefix in product(alphabet, repeat=length):
            _, before = _replay(tmp_path, events(prefix))
            _, after = _replay(tmp_path, events(prefix + ("instrument_error",)))
            assert after["wip"] >= before["wip"], (
                f"instrument_error decreased WIP for prefix {prefix!r}: "
                f"{before['wip']} -> {after['wip']}"
            )


def test_terminal_event_always_reduces_id_wip_to_zero(tmp_path: Path) -> None:
    """Every short handled prefix followed by any terminal has zero occupancy."""
    alphabet = (
        "admitted", "gated", "parked", "unparked", "instrument_error",
        "blocked-on-human", "rejected", "unrejected", "released",
    )
    terminals = ("merged", "completed", "rejected", "closed")

    def events(tokens: tuple[str, ...]) -> list[dict]:
        return [
            _ev(index, "instrument_error", detail={"class": "blocked-on-human"})
            if token == "blocked-on-human" else _ev(index, token)
            for index, token in enumerate(tokens)
        ]

    for length in range(4):
        for prefix in product(alphabet, repeat=length):
            for terminal in terminals:
                _, queue = _replay(tmp_path, events(prefix + (terminal,)))
                assert queue["wip"] == 0, (
                    f"terminal {terminal!r} retained WIP after prefix {prefix!r}"
                )


# --- the reversal itself ------------------------------------------------------


def test_unrejected_after_rejected_allows_later_admitted(tmp_path: Path) -> None:
    """The modeled shape of all five stranded proposals. RED before the fix.

    Fails on unfixed `LedgerView.build` with `status == 'rejected'` and the id
    still in `terminal`, hence omitted from `rebuild_queue` items entirely.
    """
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed", detail={"title": "reversible"}),
        _ev(1, "rejected", detail={"reason": "out-of-repo", "class": "policy"}),
        _ev(2, "unrejected", detail={"retracts": "rejected", "why_wrong": "operator ruling"}),
        _ev(3, "admitted"),
    ])

    assert IDENT not in view.terminal, "unrejected must clear the rejected terminal"
    assert view.status.get(IDENT) == "admitted"
    assert IDENT in view.admitted

    item = _item(queue)
    assert item is not None, "a re-admitted proposal must be claimable from queue.json"
    assert item["status"] == "admitted"
    assert item["title"] == "reversible", "post-terminal detail must be replayed too"
    assert queue["wip"] >= 1, "restored admitted work occupies a WIP slot"


def test_unrejected_alone_reopens_without_inventing_admission(tmp_path: Path) -> None:
    """An unreversed rejection reopens as `open` — not as `admitted`.

    The reversal restores eligibility, not state. Reading it as admitted would
    invent a WIP slot for something no admitter ever admitted.
    """
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "rejected", detail={"reason": "x"}),
        _ev(2, "unrejected", detail={"retracts": "rejected"}),
    ])

    assert IDENT not in view.terminal
    assert view.status.get(IDENT) == "open"
    item = _item(queue)
    assert item is not None and item["status"] == "open"
    assert queue["wip"] == 0, "reopened != admitted; it must not consume the cap"


def test_events_dropped_between_reject_and_unreject_do_not_resurrect(tmp_path: Path) -> None:
    """Only events AFTER the reversal decide status; the swallowed ones stay swallowed.

    `admitted` arriving while the id was rejected was correctly ignored at the
    time. The unreject must not retroactively promote it — that would read as
    admitted with no admitter after the reversal, which is the favourable
    direction (invented headroom consumption) but still a fabrication.
    """
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "rejected", detail={"reason": "x"}),
        _ev(2, "admitted"),
        _ev(3, "unrejected", detail={"retracts": "rejected"}),
    ])

    assert IDENT not in view.terminal
    assert view.status.get(IDENT) == "open"
    assert _item(queue)["status"] == "open"


def test_full_live_stream_with_park_cycle_after_the_reversal(tmp_path: Path) -> None:
    """Full-cycle shape: reject -> unreject -> park -> unpark -> admit.

    Everything downstream of the reversal must reach its normal handler again,
    not just the status map: the park set is one of the handlers sitting BELOW
    the terminal guard, so a fix that clears `terminal` but leaves the guard
    short-circuiting would strand this id as permanently parked.
    """
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "rejected", detail={"reason": "x"}),
        _ev(2, "unrejected", detail={"retracts": "rejected"}),
        _ev(3, "parked", detail={"reason": "backpressure"}),
        _ev(4, "unparked"),
        _ev(5, "admitted"),
    ])

    assert IDENT not in view.terminal
    assert IDENT not in view.parked, "the unpark after the reversal must land"
    assert view.status.get(IDENT) == "admitted"
    assert _item(queue)["status"] == "admitted"
    assert queue["wip"] >= 1


def test_gate_runs_after_the_reversal_are_recorded(tmp_path: Path) -> None:
    """`gate_runs` feeds anti-storm prior-gate lookups and sits below the guard.

    A reversal that restores the item to the queue but keeps its gate history
    invisible would let the same input be gated again as if it were fresh.
    """
    view, _queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "rejected", detail={"reason": "x"}),
        _ev(2, "unrejected", detail={"retracts": "rejected"}),
        _ev(3, "admitted"),
        _ev(4, "gated", detail={"verdict": "pass"}),
    ])

    assert view.status.get(IDENT) == "gated"
    assert len(view.gate_runs.get(IDENT, [])) == 1


# --- hard terminals must never resurrect (mutation catchers) -------------------


def test_merged_is_never_resurrected_by_unrejected(tmp_path: Path) -> None:
    """THE mutation catcher for a "last event wins" rewrite of the reducer.

    `merged` is landed work. An `unrejected` on a merged id is a bug in whoever
    wrote it; honouring it would put shipped work back in the queue and back
    into the WIP count. A fix that reopens on any `unrejected`, or that clears
    `terminal` without checking WHICH terminal, fails exactly here.
    """
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "merged", detail={"merge_sha": "d" * 40}),
        _ev(2, "unrejected", detail={"retracts": "rejected"}),
        _ev(3, "admitted"),
    ])

    assert view.terminal.get(IDENT, {}).get("event") == "merged"
    assert view.status.get(IDENT) == "merged"
    assert _item(queue) is None, "merged work must stay out of the queue"
    assert queue["wip"] == 0
    assert IDENT not in view.admitted, "the post-merge admitted must stay dropped"


def test_completed_is_never_resurrected_by_unrejected(tmp_path: Path) -> None:
    """`completed` is terminal exactly like merged (ruling D13a)."""
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "completed", detail={"applied": True}),
        _ev(2, "unrejected", detail={"retracts": "rejected"}),
        _ev(3, "admitted"),
    ])

    assert view.terminal.get(IDENT, {}).get("event") == "completed"
    assert view.status.get(IDENT) == "completed"
    assert _item(queue) is None
    assert queue["wip"] == 0


def test_closed_is_never_resurrected_by_unrejected(tmp_path: Path) -> None:
    """`closed` is the finding-side terminal and is NEWER than this defect.

    It joined the terminal tuple on 2026-08-10, after the research that
    specified this fix was written against a three-element set. Pinned here so
    the reversal rule is enumerated against the terminal set as it is TODAY,
    not as it was described.
    """
    view, queue = _replay(tmp_path, [
        _ev(0, "found"),
        _ev(1, "closed", detail={"closed_by": "sha256:" + "c" * 64,
                                 "merge_sha": "d" * 40,
                                 "bound_test": "tests/x/test_y.py::test_z"}),
        _ev(2, "unrejected", detail={"retracts": "rejected"}),
        _ev(3, "admitted"),
    ])

    assert view.terminal.get(IDENT, {}).get("event") == "closed"
    assert view.status.get(IDENT) == "closed"
    assert _item(queue) is None
    assert queue["wip"] == 0


# --- rejection that must stick ------------------------------------------------


def test_rejected_only_stays_rejected(tmp_path: Path) -> None:
    """No reversal, no reopening. The absorbing rule is right in this case."""
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "rejected", detail={"reason": "x"}),
        _ev(2, "admitted"),
    ])

    assert view.terminal.get(IDENT, {}).get("event") == "rejected"
    assert view.status.get(IDENT) == "rejected"
    assert _item(queue) is None


def test_reject_unreject_reject_stays_rejected(tmp_path: Path) -> None:
    """Control case: a second refusal after the reversal sticks.

    First-terminal-wins and last-event-wins AGREE on this id, which is why it
    was correctly absent from the queue while the other five were not. The
    reversal must re-arm the terminal slot, not permanently immunise the id
    against being refused again.
    """
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "rejected", detail={"reason": "first"}),
        _ev(2, "unrejected", detail={"retracts": "rejected"}),
        _ev(3, "admitted"),
        _ev(4, "corrected", detail={"note": "narration"}),
        _ev(5, "rejected", detail={"reason": "second"}),
    ])

    assert view.terminal.get(IDENT, {}).get("event") == "rejected"
    assert view.terminal[IDENT]["detail"]["reason"] == "second", \
        "the re-armed terminal slot records the refusal that actually stuck"
    assert view.status.get(IDENT) == "rejected"
    assert _item(queue) is None
    assert queue["wip"] == 0


def test_unrejected_without_a_prior_terminal_is_inert(tmp_path: Path) -> None:
    """A stray reversal on a live id must not rewrite its status to `open`.

    `unrejected` is off-enum in ledger-event.schema.json, so nothing at the
    write boundary stops one from being appended to an id that was never
    refused. Reading it as a state verb would demote admitted work out of WIP —
    the favourable-absence direction.
    """
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "admitted"),
        _ev(2, "unrejected", detail={"retracts": "rejected"}),
    ])

    assert IDENT not in view.terminal
    assert view.status.get(IDENT) == "admitted"
    assert _item(queue)["status"] == "admitted"
    assert queue["wip"] == 1


def test_park_before_the_rejection_is_not_restored_by_the_reversal(tmp_path: Path) -> None:
    """Rejection clears the park; the reversal returns an UNPARKED open item.

    A park is a suspension of live work. Once the id was refused there was
    nothing suspended, so re-deriving a park from an event the rejection
    already consumed would strand the reopened item as unclaimable.
    """
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "parked", detail={"reason": "backpressure"}),
        _ev(2, "rejected", detail={"reason": "x"}),
        _ev(3, "unrejected", detail={"retracts": "rejected"}),
    ])

    assert IDENT not in view.terminal
    assert IDENT not in view.parked
    assert _item(queue)["status"] == "open"


# --- blast radius: other ids are untouched ------------------------------------


def test_reversal_is_scoped_to_its_own_id(tmp_path: Path) -> None:
    """An `unrejected` clears one id's terminal slot, not the terminal map."""
    other = "sha256:" + "b" * 64
    view, queue = _replay(tmp_path, [
        _ev(0, "proposed"),
        _ev(1, "rejected", detail={"reason": "x"}),
        _ev(2, "proposed", ident=other),
        _ev(3, "rejected", ident=other, detail={"reason": "y"}),
        _ev(4, "unrejected", detail={"retracts": "rejected"}),
        _ev(5, "admitted"),
    ])

    assert view.status.get(IDENT) == "admitted"
    assert view.status.get(other) == "rejected"
    assert other in view.terminal
    assert {it["id"] for it in queue["items"]} == {IDENT}
