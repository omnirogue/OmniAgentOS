"""A ledger LINE carrying two complete events must not erase both.

MEASURED, not hypothesised. `var/loopqueue/ledger.jsonl` line 3323 holds two
whole, well-formed JSON objects concatenated on one physical line: an ad-hoc
appender wrote object 1 without its trailing newline and the next O_APPEND
writer landed object 2 behind it. `json.loads` refuses the whole line with
`Extra data: line 1 column 3262`, so the decoder every reader shares returned
UNDECODABLE and BOTH events vanished.

Two live consequences, and the second is the worse one:

  * `integration.read_ledger` counted the line as `discarded`, and
    `spawn_builders._queue_state` fails closed on `view.discarded` — so ONE
    line refused ALL claim selection with
    `ledger state incomplete (torn_tail=False, discarded=1)` and starved the
    Implementer loop's entire build drain.
  * The erased pair included a TERMINAL `rejected` for
    `sha256:truepending-defa2b64a`. Every reader in this repo therefore read
    that id as non-terminal WIP, forever — favourable absence in the
    terminal-state replay. Starvation is loud; this is silent.

`ledger.jsonl` is append-only and is never rewritten, so the repair is in the
READER and nowhere else.

Direction, restated because a partial recovery is where a fix like this goes
wrong: the recovered objects and the status are INDEPENDENT. A line that is
half event and half garbage must hand back the whole event AND a failing
status. Reporting OK because something was recovered is the favourable lie;
returning nothing because something was broken is the erasure being repaired.

Every carrier is pinned individually. The sibling module
(`test_ledger_nondict_event.py`) learned this the hard way: a grouped call
cannot say WHICH reader it exercised, and cross-lineage review reverted two
carriers with all tests still green. Incomplete propagation across a clone
family is this repo's most-refused defect class.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge import file_proposal as FP  # noqa: E402
from bridge import integration as I  # noqa: E402
from bridge import integrity as INT  # noqa: E402
from bridge import janitor as J  # noqa: E402
from bridge import known_traps as KT  # noqa: E402
from bridge import ledger_read as LR  # noqa: E402
from bridge import ledger_write as LW  # noqa: E402
from bridge import pr_reconcile as PR  # noqa: E402
from bridge import spawn_builders as SB  # noqa: E402

FIRST_ID = "sha256:" + "1" * 64
SECOND_ID = "sha256:" + "2" * 64

# The live shape: object 1 is a planner `observed`, object 2 is a TERMINAL
# `rejected`, and there is no separator of any kind between them.
FIRST = {"ts": "2026-08-10T05:00:00Z", "role": "planner", "actor": "planner-loop",
         "event": "observed", "id": FIRST_ID}
SECOND = {"ts": "2026-08-10T05:00:01Z", "role": "reviewer", "actor": "reviewer-loop",
          "event": "rejected", "id": SECOND_ID}
DOUBLE = json.dumps(FIRST) + json.dumps(SECOND)


def _write(root: Path, raw_lines: list[str]) -> Path:
    (root / "state").mkdir(parents=True, exist_ok=True)
    p = root / "ledger.jsonl"
    p.write_text("".join(ln + "\n" for ln in raw_lines), encoding="utf-8")
    return p


# --------------------------------------------------------------- the decoder


def test_blank_line_is_blank_and_carries_nothing() -> None:
    for blank in ("", "   ", "\t "):
        assert LR.parse_events(blank) == ([], LR.BLANK)


def test_one_object_is_ok_and_normalized_exactly_as_before() -> None:
    """The single-object path is the 99.99% case and must not have moved.

    Normalization is asserted here rather than assumed: `parse_events` replaced
    a `json.loads` that fed `normalize_event`, and a rewrite that dropped the
    projection would leave every agent-written event without a top-level `ts`
    again (the defect fixed hours earlier on 2026-08-10).
    """
    agent_shape = {"contract": "v1.1", "at": "2026-08-10T05:00:00Z",
                   "event": "observed", "id": FIRST_ID,
                   "producer": {"role": "planner", "actor": "planner-loop"}}
    events, status = LR.parse_events(json.dumps(agent_shape))
    assert status == LR.OK
    assert len(events) == 1
    assert events[0]["ts"] == "2026-08-10T05:00:00Z"
    assert events[0]["role"] == "planner"
    assert events[0]["actor"] == "planner-loop"


def test_two_complete_objects_on_one_line_are_both_recovered_and_flagged() -> None:
    """The measured line. Both events, in order, come back whole — but the
    splice that produced the line is a real writer-bug signal, so the status
    is RECOVERED, not OK (2026-08-10, cross-lineage review, GPT-5.6-Sol,
    BLOCKER): folding a fully-recovered splice into plain OK made the short
    write that dropped the newline invisible forever on an append-only file.
    """
    events, status = LR.parse_events(DOUBLE)
    assert status == LR.RECOVERED
    assert status != LR.OK
    assert [e["id"] for e in events] == [FIRST_ID, SECOND_ID]
    assert [e["event"] for e in events] == ["observed", "rejected"]


@pytest.mark.parametrize("sep", ["", " ", "\t", "   "])
def test_separators_between_objects_do_not_change_the_result(sep: str) -> None:
    events, status = LR.parse_events(json.dumps(FIRST) + sep + json.dumps(SECOND))
    assert status == LR.RECOVERED
    assert [e["id"] for e in events] == [FIRST_ID, SECOND_ID]


def test_n_objects_on_one_line_all_come_back() -> None:
    """Two interleaves in a row is the same defect twice; nothing caps it at 2."""
    objs = [dict(FIRST, id=f"sha256:{i}" + "0" * 63) for i in range(5)]
    events, status = LR.parse_events("".join(json.dumps(o) for o in objs))
    assert status == LR.RECOVERED
    assert [e["id"] for e in events] == [o["id"] for o in objs]


def test_a_complete_prefix_with_a_torn_remainder_recovers_AND_still_alerts() -> None:
    """The genuine interleave: `{"a":1}{"b":` — a real event, then a half one.

    Both halves of the truth are required. The event is whole and dropping it
    is the erasure this module was repaired to stop; the remainder is unread
    garbage and reporting OK over it would tell every caller the line was fine.
    """
    events, status = LR.parse_events(json.dumps(FIRST) + '{"b":')
    assert status == LR.UNDECODABLE, "a partial recovery must never report clean"
    assert [e["id"] for e in events] == [FIRST_ID], "the whole event was dropped"


@pytest.mark.parametrize("value", ["null", "123", '"a bare string"', "[1, 2]", "true"])
def test_a_non_dict_value_is_still_NOT_AN_OBJECT(value: str) -> None:
    """Preserved direction: nothing without `.get` may reach a caller."""
    events, status = LR.parse_events(value)
    assert status == LR.NOT_AN_OBJECT
    assert events == []


@pytest.mark.parametrize("value", ["null", "123", "true"])
def test_a_non_dict_beside_a_real_event_costs_the_status_not_the_event(value: str) -> None:
    """Scanning continues past a non-dict: the caller alerts, and keeps the event.

    Every returned element is still a dict — the property the whole module
    exists for — so a caller may use the events unconditionally.
    """
    events, status = LR.parse_events(value + json.dumps(SECOND))
    assert status == LR.NOT_AN_OBJECT
    assert [e["id"] for e in events] == [SECOND_ID]
    assert all(isinstance(e, dict) for e in events)


def test_undecodable_outranks_not_an_object_when_a_line_is_wrong_twice() -> None:
    """Scanning STOPPED at the torn byte, so there may be objects nobody reached.

    "There is unread garbage here" is the stronger report, and the one that
    makes integration treat a torn FINAL line as torn rather than as a mere
    producer bug.
    """
    events, status = LR.parse_events("null" + json.dumps(FIRST) + '{"b":')
    assert status == LR.UNDECODABLE
    assert [e["id"] for e in events] == [FIRST_ID]


def test_iter_events_yields_both_events_from_one_line() -> None:
    got = list(LR.iter_events(DOUBLE + "\n" + json.dumps(FIRST)))
    assert [e["id"] for e in got] == [FIRST_ID, SECOND_ID, FIRST_ID]


def test_iter_events_still_yields_the_recovered_half_of_a_torn_line() -> None:
    """Gating the yield on `status == OK` throws a whole event away because
    something ELSE on its line was broken."""
    got = list(LR.iter_events(json.dumps(FIRST) + '{"b":'))
    assert [e["id"] for e in got] == [FIRST_ID]


# ------------------------------------------------------------- the carriers


def test_integration_read_ledger_keeps_both_and_reports_the_line_clean(tmp_path: Path) -> None:
    """The starvation carrier. `discarded` must be 0 — a recovered line is not
    a hole, and spawn_builders refuses ALL selection on a non-zero count."""
    _write(tmp_path, [DOUBLE])
    events, torn = I.read_ledger(tmp_path)
    assert [e["id"] for e in events] == [FIRST_ID, SECOND_ID]
    assert torn is False
    assert getattr(events, "discarded", 0) == 0
    # BLOCKER (2026-08-10, cross-lineage review, GPT-5.6-Sol): the splice must
    # not be silent just because it did not re-block builders — it has to
    # show up SOMEWHERE, in a counter that is not `discarded`.
    assert getattr(events, "recovered", 0) == 1


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
def test_sanctioned_writer_unicode_separator_is_not_a_physical_ledger_line(
    tmp_path: Path, separator: str,
) -> None:
    """Free text round-trips through append_event without fabricating damage."""
    for minute, event in enumerate(("proposed", "admitted")):
        LW.append_event(tmp_path, {
            "ts": f"2026-08-13T18:0{minute}:00Z",
            "role": "implementer",
            "event": event,
            "id": FIRST_ID,
            "actor": "test",
            "detail": {"title": f"gate refuses{separator}on a moved merge base"},
        })

    view = I.LedgerView.build(tmp_path)
    queue = I.rebuild_queue(tmp_path, view, 60)
    assert len(view.events) == 2
    assert view.discarded == 0
    assert view.torn_tail is False
    assert queue["wip_degraded"] is False
    assert queue["wip"] == 1


def test_unicode_separator_survives_every_ledger_clone_reader(tmp_path: Path) -> None:
    """The six sibling read sites agree that only byte line breaks frame events."""
    separator = "\u2028"
    events = (
        ("merged", FIRST_ID),
        ("unparked", SECOND_ID),
        ("parked", "sha256:" + "3" * 64),
    )
    for minute, (event, ident) in enumerate(events):
        LW.append_event(tmp_path, {
            "ts": f"2026-08-13T18:0{minute}:00Z",
            "role": "implementer",
            "event": event,
            "id": ident,
            "actor": "test",
            "detail": {"reason": f"before{separator}after"},
        })

    terminal, merged = J.read_ledger(tmp_path)
    assert FIRST_ID in terminal and FIRST_ID in merged
    assert SECOND_ID in J._unparked_ids(tmp_path)
    assert any(ev["id"] == events[2][1] for ev in J._parked_events(tmp_path))
    assert len(INT.read_ledger(tmp_path)) == 3
    assert "merged events:            1" in KT.stats(tmp_path, since=0)
    window = KT._window_events(tmp_path, start=0, end=4_000_000_000)
    assert window["merged"] == 1


def test_integration_still_counts_a_partially_recovered_middle_line(tmp_path: Path) -> None:
    """Recovery must not buy silence: the event is kept AND the line counts."""
    _write(tmp_path, [json.dumps(FIRST) + '{"b":', json.dumps(SECOND)])
    events, torn = I.read_ledger(tmp_path)
    assert [e["id"] for e in events] == [FIRST_ID, SECOND_ID]
    assert torn is False
    assert getattr(events, "discarded", 0) == 1
    assert getattr(events, "recovered", 0) == 0, "a partial recovery is discarded, not spliced"


def test_integration_still_calls_a_partially_recovered_FINAL_line_torn(tmp_path: Path) -> None:
    """The torn-tail rule is unchanged: last line, undecodable remainder."""
    _write(tmp_path, [json.dumps(SECOND), json.dumps(FIRST) + '{"b":'])
    events, torn = I.read_ledger(tmp_path)
    assert [e["id"] for e in events] == [SECOND_ID, FIRST_ID]
    assert torn is True
    assert getattr(events, "discarded", 0) == 0
    assert getattr(events, "recovered", 0) == 0


def test_the_terminal_event_hidden_behind_another_reaches_the_replay(tmp_path: Path) -> None:
    """The silent half of the incident, in one assertion.

    `sha256:truepending-defa2b64a`'s `rejected` was object 2 on line 3323, so
    the id read as live WIP to every reader of the replay.
    """
    _write(tmp_path, [DOUBLE])
    view = I.LedgerView.build(tmp_path)
    assert SECOND_ID in view.terminal, "a terminal event was invisible to the replay"
    assert view.status[SECOND_ID] == "rejected"
    assert view.discarded == 0
    assert view.recovered == 1


def test_spawn_builders_does_not_refuse_selection_on_a_recovered_line(tmp_path: Path) -> None:
    """The falsifier, in unit form: one line must not starve the build drain."""
    _write(tmp_path, [DOUBLE])
    view, _parked = SB._queue_state(tmp_path)       # must not raise
    assert view.discarded == 0 and view.torn_tail is False
    assert view.recovered == 1, "the splice must still be visible on the view"


def test_spawn_builders_STILL_refuses_on_a_genuinely_torn_line(tmp_path: Path) -> None:
    """The other direction. A guard that never fires is not a guard, and this
    one is the fail-closed protection over an incomplete replay."""
    _write(tmp_path, ['{"torn', json.dumps(SECOND)])
    with pytest.raises(SB._SelectionRefused):
        SB._queue_state(tmp_path)


def test_integrity_read_ledger_keeps_both_and_discards_nothing(tmp_path: Path) -> None:
    _write(tmp_path, [DOUBLE])
    events = INT.read_ledger(tmp_path)
    assert [e["id"] for e in events] == [FIRST_ID, SECOND_ID]
    assert getattr(events, "discarded", 0) == 0
    assert getattr(events, "recovered", 0) == 1


def test_integrity_still_counts_a_partially_recovered_line(tmp_path: Path) -> None:
    """The auditor must skip but COUNT — recovering part of a line is not a
    clean bill on it."""
    _write(tmp_path, [json.dumps(FIRST) + '{"b":'])
    events = INT.read_ledger(tmp_path)
    assert [e["id"] for e in events] == [FIRST_ID]
    assert getattr(events, "discarded", 0) == 1
    assert getattr(events, "recovered", 0) == 0


def test_integrity_line_split_parity_flags_wide_separator(tmp_path: Path) -> None:
    _write(tmp_path, [json.dumps({**FIRST, "detail": {"title": "before\u2028after"}},
                                 ensure_ascii=False)])
    result = INT.Result()
    INT.assert_ledger_line_split_parity(tmp_path, result)
    assert [failure["assertion"] for failure in result.failures] == [
        "ledger.decoded_and_physical_line_counts_match"
    ]


def test_integrity_line_split_parity_accepts_ascii_ledger(tmp_path: Path) -> None:
    _write(tmp_path, [json.dumps(FIRST)])
    result = INT.Result()
    INT.assert_ledger_line_split_parity(tmp_path, result)
    assert result.failures == []


@pytest.mark.parametrize("category", ["liveness", "invariants", "absence"])
def test_the_auditor_does_not_flag_a_fully_recovered_ledger_as_discarded(
    tmp_path: Path, category: str,
) -> None:
    """A recovered line is not a partial read: it must not trip the
    `discarded` finding, and it must not re-block builders. It DOES still get
    its own, separate finding — see
    `test_the_auditor_flags_a_fully_recovered_ledger_as_spliced` below."""
    _write(tmp_path, [DOUBLE])
    now = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
    (tmp_path / "state" / "budget.json").write_text(json.dumps({
        "disk_free_gb_min": 20, "disk_free_gb": 500,
        "load_avg_1m_max": 16, "load_avg_1m": 1.0,
        "wip_cap": 24, "updated_at": now,
        "subscription": {"claude_pool": {"distinct_accounts": 4}},
    }))
    r = INT.Result()
    getattr(INT, f"check_{category}")(tmp_path, r)
    assert "ledger.every_line_is_an_object" not in {f["assertion"] for f in r.failures}


@pytest.mark.parametrize("category", ["liveness", "invariants", "absence"])
def test_the_auditor_flags_a_fully_recovered_ledger_as_spliced(
    tmp_path: Path, category: str,
) -> None:
    """BLOCKER, cross-lineage review (GPT-5.6-Sol, 2026-08-10): recovering
    the events must not make the splice itself invisible. Each of the three
    category processes must independently report the condition (they read
    the ledger separately) so the operator sees it regardless of which one
    they check — the same "all three must converge" property `discarded`
    already has, verified the same way."""
    _write(tmp_path, [DOUBLE])
    now = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
    (tmp_path / "state" / "budget.json").write_text(json.dumps({
        "disk_free_gb_min": 20, "disk_free_gb": 500,
        "load_avg_1m_max": 16, "load_avg_1m": 1.0,
        "wip_cap": 24, "updated_at": now,
        "subscription": {"claude_pool": {"distinct_accounts": 4}},
    }))
    r = INT.Result()
    getattr(INT, f"check_{category}")(tmp_path, r)
    names = {f["assertion"] for f in r.failures}
    assert "ledger.line_carries_spliced_events" in names
    assert "ledger.every_line_is_an_object" not in names, \
        "the splice must not double as a discarded-read finding too"


def test_janitor_read_ledger_sees_the_terminal_event_behind_the_first(tmp_path: Path) -> None:
    """Retention: an unseen `merged` keeps a finished item's artifacts forever
    and makes its receipt look unreferenced."""
    merged = dict(SECOND, event="merged", id=SECOND_ID)
    _write(tmp_path, [json.dumps(FIRST) + json.dumps(merged)])
    terminal, merged_ids = J.read_ledger(tmp_path)
    assert SECOND_ID in terminal
    assert SECOND_ID in merged_ids


def test_janitor_parked_events_sees_a_park_behind_another_event(tmp_path: Path) -> None:
    parked = dict(SECOND, event="parked", id=SECOND_ID)
    _write(tmp_path, [json.dumps(FIRST) + json.dumps(parked)])
    assert [e["id"] for e in J._parked_events(tmp_path)] == [SECOND_ID]


def test_janitor_unparked_ids_sees_an_unpark_behind_another_event(tmp_path: Path) -> None:
    """This scan used to be `json.loads(line)["id"]` behind a decodability
    predicate. On a two-object line the predicate now says "readable" and the
    bare `json.loads` would raise `Extra data` and take the whole sweep down —
    which is why the second decode had to go, not just be guarded."""
    unparked = dict(SECOND, event="unparked", id=SECOND_ID)
    _write(tmp_path, [json.dumps(FIRST) + json.dumps(unparked)])
    assert J._unparked_ids(tmp_path) == {SECOND_ID}


def test_janitor_sweep_does_not_alert_on_a_park_released_behind_another_event(
    tmp_path: Path,
) -> None:
    """End to end through the reader that actually runs on a timer.

    A missed `unparked` makes the janitor announce that a park marker vanished
    with no authenticated release — a false accusation of an unaudited unpark,
    aimed at the one alert whose whole job is to be trusted.
    """
    for d in ("parked", "inquiries", "findings", "proposals", "candidates", "receipts", "claims"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    parked = dict(FIRST, event="parked", id=SECOND_ID)
    unparked = dict(SECOND, event="unparked", id=SECOND_ID)
    _write(tmp_path, [json.dumps(parked), json.dumps(FIRST) + json.dumps(unparked)])
    jan = J.Janitor(tmp_path, apply=False)
    jan.sweep()
    assert not [a for a in jan.alerts if "vanished" in a], jan.alerts


def test_known_traps_stats_counts_both_events(tmp_path: Path) -> None:
    merged = dict(FIRST, event="merged")
    _write(tmp_path, [json.dumps(merged) + json.dumps(SECOND)])
    out = KT.stats(tmp_path, 0.0)
    assert "merged events:            1" in out
    assert "rejected events:          1" in out


def test_known_traps_window_events_counts_both_events(tmp_path: Path) -> None:
    merged = dict(FIRST, event="merged")
    _write(tmp_path, [json.dumps(merged) + json.dumps(SECOND)])
    out = KT._window_events(tmp_path, 0.0, 4e9)
    assert out["merged"] == 1 and out["rejected"] == 1


def test_known_traps_load_rejections_reads_both_estate_records(tmp_path: Path, monkeypatch) -> None:
    """ESTATE_REJECTIONS is a DIFFERENT jsonl and its path is module-level —
    without the monkeypatch this passes vacuously on a box that has no such
    file (the trap the sibling module documents)."""
    rejections = tmp_path / "rejections.jsonl"
    rejections.write_text(json.dumps(FIRST) + json.dumps(SECOND) + "\n")
    monkeypatch.setattr(KT, "ESTATE_REJECTIONS", rejections)
    (tmp_path / "rejected").mkdir(exist_ok=True)
    got = KT.load_rejections(tmp_path, 0.0)
    assert [r["id"] for r in got] == [FIRST_ID, SECOND_ID]


def test_known_traps_queue_depth_delta_reads_both_receipts(tmp_path: Path, monkeypatch) -> None:
    receipts = tmp_path / "offload-receipts.jsonl"
    receipts.write_text(
        '{"kind":"advise","queued":{"candidates":5,"proposals":2},"at":"2026-08-10T05:00:00Z"}'
        '{"kind":"advise","queued":{"candidates":1,"proposals":1},"at":"2026-08-10T06:00:00Z"}\n'
    )
    monkeypatch.setattr(KT, "OFFLOAD_RECEIPTS", receipts)
    # Both samples are on ONE line: the second is the `end` of the delta, so
    # losing it makes a draining queue look static.
    assert KT._queue_depth_delta(0.0, 4e9) == {
        "candidates": {"start": 5, "end": 1}, "proposals": {"start": 2, "end": 1}}


def test_pr_reconcile_terminal_ids_sees_the_hidden_terminal_event(tmp_path: Path) -> None:
    _write(tmp_path, [DOUBLE])
    assert SECOND_ID in PR._ids_with_terminal_ledger_event(tmp_path)


def test_pr_reconcile_sees_a_FOREIGN_closure_hidden_behind_another_event(tmp_path: Path) -> None:
    """The destructive miss: `foreign_ids` is what stops the repair path
    writing a reconciler tombstone over somebody else's closure."""
    foreign = dict(SECOND, event="merged", actor="somebody-else")
    _write(tmp_path, [json.dumps(FIRST) + json.dumps(foreign)])
    foreign_ids, own_orphans = PR._terminal_ledger_state(tmp_path)
    assert SECOND_ID in foreign_ids
    assert SECOND_ID not in own_orphans


def test_file_proposal_read_back_finds_an_event_written_behind_another(tmp_path: Path) -> None:
    """ABSENT here rolls back an artifact whose event is durably on disk
    (R3-FI-01) — and our own append is exactly the one a newline-less writer
    can leave object 2 behind."""
    _write(tmp_path, [DOUBLE])
    assert FP._ledger_event_state(tmp_path, SECOND_ID, "rejected") == FP.LEDGER_EVENT_PRESENT
    assert FP._ledger_event_state(tmp_path, FIRST_ID, "observed") == FP.LEDGER_EVENT_PRESENT
    assert FP._ledger_event_state(tmp_path, FIRST_ID, "merged") == FP.LEDGER_EVENT_ABSENT


# ----------------------------------------------------- the gate daemon itself
#
# Cross-lineage review (gpt-5.6-sol @ xhigh, 2026-08-10) found the propagation
# hole this file's own docstring warns about: three readers inside `GateLoop`
# kept a private `json.loads` and were never exercised here, so the recovered
# terminal stayed invisible to the one process that decides whether to land.
# Each carrier is pinned separately, per the rule at the top of this file.


def _gate_loop(root: Path):
    from bridge.gate_loop import GateLoop
    (root / "state").mkdir(parents=True, exist_ok=True)
    return GateLoop.__new__(GateLoop)


def test_gate_loop_terminal_ids_sees_a_terminal_hidden_behind_another_event(
        tmp_path: Path) -> None:
    """The worst of the three: a terminal id this set cannot see is an id the
    gate is willing to land a SECOND time, and the duplicate terminal history
    that writes can never be repaired on an append-only ledger."""
    _write(tmp_path, [DOUBLE])
    loop = _gate_loop(tmp_path)
    loop.root = tmp_path
    assert SECOND_ID in loop._terminal_ids()


def test_gate_loop_ledger_has_event_sees_an_event_hidden_behind_another(
        tmp_path: Path) -> None:
    """This one guards a HEALING append: False is the favourable direction, so
    an event hidden behind another gets its heal written twice."""
    _write(tmp_path, [DOUBLE])
    loop = _gate_loop(tmp_path)
    loop.root = tmp_path
    assert loop._ledger_has_event(SECOND_ID, "rejected") is True
    assert loop._ledger_has_event(FIRST_ID, "observed") is True
    assert loop._ledger_has_event(FIRST_ID, "merged") is False


def test_gate_loop_regate_count_sees_an_instrument_error_behind_another_event(
        tmp_path: Path) -> None:
    """An under-count here is a re-gate bound that does not bind — the storm
    the bound exists to stop."""
    hidden = {"ts": "2026-08-10T05:00:02Z", "role": "implementer",
              "event": "instrument_error", "id": SECOND_ID,
              "detail": {"train_key": "train/gl-abc@deadbeef"}}
    _write(tmp_path, [json.dumps(FIRST) + json.dumps(hidden)])
    loop = _gate_loop(tmp_path)
    loop.root = tmp_path
    assert loop._instrument_regate_count("train/gl-abc@deadbeef") == 1


# --------------------------------------------------------- non-JSON whitespace
#
# MAJOR, cross-lineage review (GPT-5.6-Sol, 2026-08-10): `str.isspace()` also
# accepts Unicode separators (U+00A0 NBSP among them) that JSON's own
# whitespace grammar (RFC 8259 §2: space, tab, CR, LF only) does not. Using it
# to skip between objects let a genuinely corrupt separator read as a clean
# adjacent pair.


def test_nbsp_between_objects_is_not_json_whitespace_and_reads_as_corrupt() -> None:
    """`'{"event":"observed"}\\xa0{"event":"rejected"}'` must NOT read as two
    clean adjacent objects: U+00A0 is not valid JSON whitespace, so this line
    is genuinely corrupt. The first object is still whole and must still be
    recovered — but the status must say so."""
    line = json.dumps(FIRST) + " " + json.dumps(SECOND)
    events, status = LR.parse_events(line)
    assert status == LR.UNDECODABLE, "NBSP is not JSON whitespace; this is corrupt, not clean"
    assert [e["id"] for e in events] == [FIRST_ID], "the whole first event must still come back"


@pytest.mark.parametrize("ws", [" ", "\t", "\r", "\n", "  \t "])
def test_actual_json_whitespace_between_objects_still_recovers_cleanly(ws: str) -> None:
    """The positive control: real JSON whitespace (space/tab/CR/LF) between
    two objects must still splice cleanly — only NON-JSON whitespace is the
    corruption signal."""
    line = json.dumps(FIRST) + ws + json.dumps(SECOND)
    events, status = LR.parse_events(line)
    assert status == LR.RECOVERED
    assert [e["id"] for e in events] == [FIRST_ID, SECOND_ID]


# ------------------------------------------------------------ streaming iter


def test_iter_events_consuming_one_item_does_not_materialize_the_whole_line() -> None:
    """MINOR, cross-lineage review (GPT-5.6-Sol, 2026-08-10): the nominal path
    must stream, i.e. per-line memory must be bounded, not proportional to
    the object count. `parse_events(line)[0]` (the pre-fix `iter_events`
    body) has to finish decoding and building the WHOLE line's list before
    `yield from` can hand back even the first element — so peak memory while
    consuming only ONE item off a huge line scales with the object count.
    Measured with `tracemalloc`, not wall-clock: a fast machine can decode
    200k tiny objects well within any reasonable timeout either way, so a
    timing assertion would not reliably distinguish the two implementations.
    """
    import tracemalloc

    huge_line = "{}" * 200_000

    tracemalloc.start()
    try:
        it = LR.iter_events(huge_line)
        first = next(it)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert first == {}
    # A single decoded `{}` plus generator/frame bookkeeping costs a few KB.
    # 200,000 materialized dicts (even empty ones, each with per-object
    # dict/list overhead) costs several MB. The ceiling sits well below the
    # eager cost and well above the streaming cost, so it discriminates the
    # two implementations rather than merely being generous.
    assert peak < 2_000_000, f"peak={peak} bytes — looks like the whole line was materialized"


def test_walk_stays_linear_on_a_hundred_thousand_objects() -> None:
    """Non-regression on the documented cost: the walk must stay linear, not
    become quadratic. Generous ceiling (base measured 0.031s) so this does
    not flake on a loaded CI box."""
    import time

    line = "{}" * 100_000
    started = time.monotonic()
    events, status = LR.parse_events(line)
    elapsed = time.monotonic() - started
    assert len(events) == 100_000
    assert status == LR.RECOVERED
    assert elapsed < 5.0, f"took {elapsed:.2f}s for 100k objects — looks quadratic"
