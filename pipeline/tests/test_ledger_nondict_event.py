"""A ledger LINE that is valid JSON but not an object must never crash a reader.

Sibling of `test_ledger_nondict_detail.py`, one level up. That one covers an
event whose `detail` is a string; this one covers the whole EVENT being a
non-object — `null`, `123`, `"a string"`, `[...]`, `true`.

Why this is reachable, empirically rather than in principle: on 2026-08-09 the
Reviewer loop appended 11 events carrying `detail` as a STRING and killed
com.threeloops.publish-queue (see the sibling module's docstring). The loops
append to `ledger.jsonl` with ad-hoc one-liners and there is no schema check at
the write site, so the shape one level up is the same unvalidated write path.

`ledger.jsonl` is append-only and is never rewritten, so one such line is
PERMANENT: every subsequent run of every reader below hits it forever. Four of
these readers are launchd daemons (com.threeloops.janitor, .claim-reaper, and
the three integrity-* timers), so the failure is silent and recurring.

Direction matters, and it differs by reader:

  * The SKIPPING readers (janitor, pr_reconcile, known_traps, file_proposal)
    must drop the line and keep going. A non-object line carries no `id` and no
    `event`, so it can never have contributed to WIP or to a terminal state —
    dropping it cannot under-count anything. Aborting the read is the harmful
    direction: it stops retention and claim-reaping entirely.
  * The AUDITOR (integrity.read_ledger) must not hand non-dicts to its checks,
    but it must not silently swallow them either — a health checker that
    quietly discards corruption is the failure mode its own zero-byte guard
    exists to prevent. It surfaces the count instead.

`integration.read_ledger` already has the `isinstance(ev, dict)` guard. It is
the one correct sibling, and it is pinned here so the convergence is not lost.
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
from bridge import pr_reconcile as PR  # noqa: E402

# Every JSON value that is valid but is not an object. `.get` exists on none.
NON_OBJECTS = ['null', '123', '"a bare string"', '[1, 2]', 'true']

GOOD = {
    "ts": "2026-08-09T05:00:00Z",
    "role": "reviewer",
    "event": "merged",
    "id": "sha256:" + "a" * 64,
    "actor": "reviewer-loop",
}


def _write(root: Path, raw_lines: list[str]) -> Path:
    """Write ledger.jsonl verbatim — these lines are not all dict-encodable."""
    (root / "state").mkdir(parents=True, exist_ok=True)
    p = root / "ledger.jsonl"
    p.write_text("".join(ln + "\n" for ln in raw_lines), encoding="utf-8")
    return p


def _ledger_with(root: Path, poison: str) -> None:
    """A real event, then the poison line, then another real event.

    The trailing good event is load-bearing: it proves the reader RESUMED
    rather than merely surviving by stopping early at the poison.
    """
    tail = dict(GOOD, id="sha256:" + "b" * 64, event="rejected")
    _write(root, [json.dumps(GOOD), poison, json.dumps(tail)])


@pytest.mark.parametrize("poison", NON_OBJECTS)
def test_janitor_read_ledger_survives(tmp_path: Path, poison: str) -> None:
    """com.threeloops.janitor — retention. Crash here stops it entirely."""
    _ledger_with(tmp_path, poison)
    terminal, merged = J.read_ledger(tmp_path)          # must not raise
    assert "sha256:" + "a" * 64 in merged               # before the poison
    assert "sha256:" + "b" * 64 in terminal             # AFTER it — resumed


# Same trap as PARKED_POISON below: `_unparked_ids` prefilters on the SUBSTRING
# '"unparked"', so a generic non-object line never reaches the decode and would
# pin nothing. These two are valid JSON, non-dict, and match the prefilter.
UNPARKED_POISON = ['"unparked"', '["unparked"]']


@pytest.mark.parametrize("poison", UNPARKED_POISON)
def test_janitor_unparked_ids_survives(tmp_path: Path, poison: str) -> None:
    """The unparked scan decides whether a vanished park marker gets an alert.

    Replaces the old `_safe_id` unit test: that predicate existed only to prove
    a line was decodable so an unguarded `json.loads(line)["id"]` could decode
    it a second time, and it is gone with the second decode. Same coverage,
    exercised on the reader that actually runs.
    """
    unparked = dict(GOOD, event="unparked", id="sha256:" + "d" * 64)
    _write(tmp_path, [poison, json.dumps(unparked)])
    assert J._unparked_ids(tmp_path) == {"sha256:" + "d" * 64}   # must not raise


# `_parked_events` prefilters on the SUBSTRING '"parked"' before json.loads, so
# a generic poison line never reaches the `.get`. Only a non-object line that
# CONTAINS that substring does — and both of these are valid JSON, non-dict,
# and match the prefilter. Using NON_OBJECTS here would pass vacuously and pin
# nothing; that false negative is the whole reason this list is separate.
PARKED_POISON = ['"parked"', '["parked"]']


@pytest.mark.parametrize("poison", PARKED_POISON)
def test_janitor_parked_events_survives(tmp_path: Path, poison: str) -> None:
    parked = dict(GOOD, event="parked", id="sha256:" + "c" * 64)
    _write(tmp_path, [poison, json.dumps(parked)])
    got = J._parked_events(tmp_path)                    # must not raise
    assert [e["id"] for e in got] == ["sha256:" + "c" * 64]


@pytest.mark.parametrize("poison", NON_OBJECTS)
def test_integrity_read_ledger_yields_only_dicts(tmp_path: Path, poison: str) -> None:
    """The auditor must never hand a non-dict to check_*(); they all do ev.get()."""
    _ledger_with(tmp_path, poison)
    events = INT.read_ledger(tmp_path)                  # must not raise
    assert all(isinstance(e, dict) for e in events), "a non-dict reached the checks"
    assert len(events) == 2                             # both real events kept


def test_integrity_surfaces_discarded_lines_rather_than_swallowing(tmp_path: Path) -> None:
    """An auditor that silently discards corruption is the defect it guards against.

    Same argument as its own zero-byte guard: returning a quietly-shortened list
    makes every downstream check pass on less than the whole ledger.
    """
    _ledger_with(tmp_path, "null")
    events = INT.read_ledger(tmp_path)
    assert getattr(events, "discarded", None) == 1, (
        "read_ledger must report how many non-object lines it dropped"
    )


@pytest.mark.parametrize("poison", NON_OBJECTS)
def test_pr_reconcile_terminal_ids_survives(tmp_path: Path, poison: str) -> None:
    _ledger_with(tmp_path, poison)
    ids = PR._ids_with_terminal_ledger_event(tmp_path)  # must not raise
    assert "sha256:" + "b" * 64 in ids                  # resumed past the poison


@pytest.mark.parametrize("poison", NON_OBJECTS)
def test_pr_reconcile_terminal_state_survives(tmp_path: Path, poison: str) -> None:
    _ledger_with(tmp_path, poison)
    ids, by_id = PR._terminal_ledger_state(tmp_path)    # must not raise
    assert "sha256:" + "b" * 64 in ids or "sha256:" + "b" * 64 in by_id


@pytest.mark.parametrize("poison", NON_OBJECTS)
def test_known_traps_readers_survive(tmp_path: Path, poison: str) -> None:
    """--stats and throughput both scan the ledger; neither guarded the shape.

    Each reader is called in its OWN test below rather than only here, because
    a grouped call cannot say WHICH repair it exercised: cross-lineage review
    (GPT-5.6-Sol) reverted the estate-rejections and offload-receipt carriers
    alone and all 45 tests stayed green. A claim of 12 ruled carriers has to be
    pinned carrier by carrier or it is an assertion, not evidence.
    """
    _ledger_with(tmp_path, poison)
    (tmp_path / "rejected").mkdir(exist_ok=True)
    KT.load_rejections(tmp_path, 0.0)                   # must not raise
    KT.stats(tmp_path, 0.0)                             # must not raise
    KT._window_events(tmp_path, 0.0, 4e9)               # must not raise


@pytest.mark.parametrize("poison", NON_OBJECTS)
def test_known_traps_queue_depth_delta_survives(tmp_path: Path, poison: str,
                                                monkeypatch) -> None:
    """The 4th known_traps carrier — the one nothing exercised.

    Two corrections to my own enumeration, both found by making the test
    actually discriminate instead of merely pass:

    * It reads OFFLOAD_RECEIPTS, a DIFFERENT jsonl from ledger.jsonl, and that
      path is module-level. Without monkeypatching it, `OFFLOAD_RECEIPTS.exists()`
      is False on a test box and the function returns None before reaching the
      decode — which is exactly why the first version of this test passed
      against fully-reverted code.
    * My enumeration listed "_queue_depth_delta" and "the offload-receipts scan"
      as two carriers. They are ONE: the offload read IS this function. `_volume`
      reads no JSON at all — it shells out to `git diff --numstat`. So the ruled
      count for known_traps is 3 ledger readers + 1 receipts reader, not 4 + 1.

    Records must be kind advise/sample with a truthy `queued` and an `at` inside
    the window, or the continue-guards skip them before the poison matters.
    """
    receipts = tmp_path / "offload-receipts.jsonl"
    receipts.write_text(
        '{"kind":"advise","queued":{"candidates":5,"proposals":2},"at":"2026-08-09T05:00:00Z"}\n'
        + poison + "\n"
        + '{"kind":"advise","queued":{"candidates":1,"proposals":1},"at":"2026-08-09T06:00:00Z"}\n'
    )
    monkeypatch.setattr(KT, "OFFLOAD_RECEIPTS", receipts)
    out = KT._queue_depth_delta(0.0, 4e9)               # must not raise
    # resumed past the poison and reached the LAST record, not just the first
    assert out == {"candidates": {"start": 5, "end": 1},
                   "proposals": {"start": 2, "end": 1}}


@pytest.mark.parametrize("poison", NON_OBJECTS)
def test_file_proposal_ledger_event_state_survives(tmp_path: Path, poison: str) -> None:
    """Filing reads the ledger back to confirm its own append is durable.

    Queries an ident that is NOT in the ledger on purpose. This reader scans in
    REVERSE and returns early on the first match, so asking for the tail event
    would return PRESENT before ever reaching the poison — a vacuous pass that
    pins nothing. Only a full scan reaches the defect.

    ABSENT is the required answer, and the direction matters: this function's
    own docstring says an unreadable ledger must be UNKNOWN and never ABSENT,
    because ABSENT on a read error orphans a durable event (R3-FI-01). A crash
    here is worse than either — it aborts the filing mid-write.
    """
    _ledger_with(tmp_path, poison)
    missing = "sha256:" + "f" * 64
    state = FP._ledger_event_state(tmp_path, missing, "merged")   # must not raise
    assert state == FP.LEDGER_EVENT_ABSENT


def _assertions(r) -> set[str]:
    return {f["assertion"] for f in r.failures}


ASSERT_NAME = "ledger.every_line_is_an_object"


def _healthy_budget(root: Path) -> None:
    """check_liveness refuses EARLY on an unreadable budget.json and returns.

    That early refusal is correct — it fails closed rather than reporting green
    — but it means a test without a budget never reaches the ledger read at
    all, and would 'pass' against a completely unwired check. Writing a healthy
    budget is what makes the liveness case actually exercise the path.
    """
    now = __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime())
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state" / "budget.json").write_text(json.dumps({
        "disk_free_gb_min": 20, "disk_free_gb": 500,
        "load_avg_1m_max": 16, "load_avg_1m": 1.0,
        "wip_cap": 24, "updated_at": now,
        "subscription": {"claude_pool": {"distinct_accounts": 4}},
    }))


@pytest.mark.parametrize("category", ["liveness", "invariants", "absence"])
def test_the_auditor_REFUSES_on_a_partial_read_not_just_counts_it(
    tmp_path: Path, category: str
) -> None:
    """The counter is not the fix; the check consulting it is.

    Raised by cross-lineage review (Grok, 2026-08-09): read_ledger counted the
    lines it discarded and NOTHING read the count, so a poisoned ledger still
    reported green. That is "built, tested, never wired" — inside the auditor
    whose job is to catch exactly that.

    Parametrised over all three categories because `--category` runs them as
    three SEPARATE launchd processes. Wiring one and calling it done would
    leave the other two grading a short ledger in silence.
    """
    _ledger_with(tmp_path, "null")
    _healthy_budget(tmp_path)
    r = INT.Result()
    getattr(INT, f"check_{category}")(tmp_path, r)
    assert ASSERT_NAME in _assertions(r), (
        f"check_{category} graded a partially-read ledger and said nothing"
    )


@pytest.mark.parametrize("category", ["liveness", "invariants", "absence"])
def test_a_clean_ledger_does_not_trip_the_partial_read_refusal(
    tmp_path: Path, category: str
) -> None:
    """The other direction. A check that always fires is not a check.

    Without this, the test above would pass just as well against a hardcoded
    failure — the counterfeit shape this estate's corpus exists to catch.
    """
    _write(tmp_path, [json.dumps(GOOD)])
    _healthy_budget(tmp_path)
    r = INT.Result()
    getattr(INT, f"check_{category}")(tmp_path, r)
    assert ASSERT_NAME not in _assertions(r)


def test_the_refusal_detail_carries_no_measurement(tmp_path: Path) -> None:
    """`emit` hashes assertion+detail into the finding's content id.

    A count in the detail mints a new id every run whose count differed, so
    dedup dies and the queue floods hourly, forever — the trap the Result.fail
    docstring names. The measurement belongs in refs, which are not hashed.
    """
    _ledger_with(tmp_path, "null")
    r = INT.Result()
    INT.check_invariants(tmp_path, r)
    fail = next(f for f in r.failures if f["assertion"] == ASSERT_NAME)
    assert not any(ch.isdigit() for ch in fail["detail"]), fail["detail"]
    assert any("discarded_lines=" in ref for ref in fail["evidence_refs"])

    # The id must be stable across runs with DIFFERENT discard counts.
    #
    # source_ref is built the way emit() builds it, INCLUDING the scope
    # override. Cross-lineage review (Grok, round 3) caught the first version
    # hardcoding "integrity/invariants/...", which is not the path production
    # takes for this assertion — it scopes to "integrity/ledger/...". The test
    # still detected a moving id, but it was pinning a string production never
    # constructs, so a scope regression would have slipped straight past it.
    from canonical import content_id

    def mint(poison_lines: list[str], category: str = "invariants") -> str:
        d = tmp_path / f"q{len(poison_lines)}-{category}"
        (d / "state").mkdir(parents=True, exist_ok=True)
        (d / "ledger.jsonl").write_text(
            json.dumps(GOOD) + "\n" + "".join(p + "\n" for p in poison_lines))
        _healthy_budget(d)
        rr = INT.Result()
        getattr(INT, f"check_{category}")(d, rr)
        f = next(x for x in rr.failures if x["assertion"] == ASSERT_NAME)
        return content_id({
            "symptom": f"integrity check failed: {f['assertion']} — {f['detail']}",
            "source": "integrity-check",
            # verbatim from emit(): the scope override is the whole point
            "source_ref": f"integrity/{f.get('scope') or category}/{f['assertion']}",
        })

    assert mint(["null"]) == mint(["null", "123", "true"]), (
        "content id moved with the discard count — this floods the queue hourly"
    )
    # and it must not move with the CATEGORY either — that was the 3-artifact bug
    ids = {mint(["null"], c) for c in ("invariants", "liveness", "absence")}
    assert len(ids) == 1, (
        "content id moved with the category — three findings for one condition"
    )


def test_the_three_categories_mint_ONE_artifact_not_three(tmp_path: Path) -> None:
    """One condition must not become three findings.

    I asserted to review that emit() would dedup this and was WRONG: emit
    builds source_ref as `integrity/{category}/{assertion}` and that string is
    inside the hashed payload, so the three category processes minted three
    distinct ids for a single condition — the exact flooding this assertion
    claims to prevent, caused by the fix for it.

    Result.fail now takes `scope`, which overrides the category in source_ref.
    A ledger-substrate assertion scopes to "ledger" so all three converge.

    Runs the whole thing TWICE: also pins cross-run dedup, so a repeat sweep
    over an unrepaired ledger adds nothing.
    """
    for d in ("findings", "rejected", "parked", "inquiries"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    _ledger_with(tmp_path, "null")
    _healthy_budget(tmp_path)

    for _run in (1, 2):
        for cat in ("liveness", "invariants", "absence"):
            r = INT.Result()
            getattr(INT, f"check_{cat}")(tmp_path, r)
            INT.emit(tmp_path, r, cat, apply=True)

    minted = [p for p in (tmp_path / "findings").glob("*.json")
              if ASSERT_NAME in json.loads(p.read_text())["title"]]
    assert len(minted) == 1, (
        f"{len(minted)} artifacts for one condition — this floods the queue"
    )


def test_scope_defaults_to_the_category_for_ordinary_assertions(tmp_path: Path) -> None:
    """The override must not become the default.

    An assertion genuinely ABOUT one category should still be scoped to it —
    otherwise two unrelated category failures could collide on one id and the
    second would be silently dropped by the dedup check.
    """
    r = INT.Result()
    r.fail("some.category_assertion", "detail")
    assert r.failures[0]["scope"] is None


def test_decoder_survives_a_line_that_blows_the_recursion_limit() -> None:
    """~200k nested brackets crash json.loads with RecursionError, not
    JSONDecodeError. Measured, not hypothesised — and it crashed janitor at
    base_sha too, so this is a pre-existing hole closed at the new chokepoint
    rather than a regression. Pinned because the module promises not to raise.
    """
    from bridge import ledger_read as LR
    events, status = LR.parse_events("[" * 200_000 + "]" * 200_000)
    assert events == []
    assert status == LR.UNDECODABLE


def test_decoder_does_not_swallow_memory_errors(monkeypatch) -> None:
    """MemoryError is a fact about the HOST, not about the line.

    Reporting it as a merely-unreadable record is the favourable-absence
    direction: retention would skip the line and carry on as though the ledger
    had been read. It must propagate.

    Patches the DECODER the module actually calls. It used to patch
    `json.loads`, which parse_events no longer goes through — a test that
    patches an uncalled function passes against code with no guard at all.
    """
    from bridge import ledger_read as LR

    class Boom:
        def raw_decode(self, _s, _i=0):
            raise MemoryError("out of memory")

    monkeypatch.setattr(LR, "_DECODER", Boom())
    with pytest.raises(MemoryError):
        LR.parse_events('{"a": 1}')


def test_a_thrown_away_ledger_is_distinguishable_from_an_empty_one(tmp_path: Path) -> None:
    """The publisher must not render corruption as healthy emptiness.

    Raised by cross-lineage review (GPT-5.6-Sol, 2026-08-09). integration's
    isinstance guard meant it never CRASHED on a non-object line — so it was
    correctly ruled not-a-carrier for the AttributeError class — but it dropped
    those lines silently. An all-malformed ledger returned (0 events,
    torn=False), byte-identical to a genuinely empty one, and publish_queue
    wrote a healthy-looking `ledger_events: 0` into queue.json. Every loop
    reads queue.json for backpressure, so the whole estate would have read a
    destroyed ledger as an idle one.

    Not crashing is not the same as not lying.
    """
    _write(tmp_path, ["null", "123", '"junk"'])
    dead, dead_torn = I.read_ledger(tmp_path)

    empty = tmp_path / "empty"
    (empty / "state").mkdir(parents=True)
    (empty / "ledger.jsonl").write_text("")
    fine, fine_torn = I.read_ledger(empty)

    assert len(dead) == len(fine) == 0        # both read zero events...
    assert dead_torn is False and fine_torn is False
    assert dead.discarded == 3                # ...but only one threw work away
    assert getattr(fine, "discarded", 0) == 0


def test_queue_json_surfaces_the_discard_count(tmp_path: Path) -> None:
    """The signal has to reach the artifact, or it is dead instrumentation.

    Exactly the defect the reviewer found in integrity's counter one round
    earlier. Counting without publishing repeats it in the publisher.
    """
    _write(tmp_path, [json.dumps(GOOD), "null"])
    view = I.LedgerView.build(tmp_path)
    assert view.discarded == 1


def test_integration_read_ledger_also_survives_recursion(tmp_path: Path) -> None:
    """The consolidation must reach the publisher too.

    Raised by cross-lineage review (Grok, 2026-08-09): integration.read_ledger
    had the isinstance guard and so was correct for the NON-OBJECT shape, but
    it still caught only JSONDecodeError and therefore still died on the
    recursion-blowing line. Being right about one shape is not being converged.

    A recursion-blowing FINAL line reads as torn — the alerting direction. The
    publisher already refuses to rebuild queue.json over a torn tail, so this
    lands in the guarded path rather than a favourable one.
    """
    _write(tmp_path, [json.dumps(GOOD), "[" * 200_000 + "]" * 200_000])
    events, torn = I.read_ledger(tmp_path)              # must not raise
    assert [e["id"] for e in events] == ["sha256:" + "a" * 64]
    assert torn is True


@pytest.mark.parametrize("poison", NON_OBJECTS)
def test_integration_read_ledger_already_correct(tmp_path: Path, poison: str) -> None:
    """The one sibling that had the guard. Pinned so the convergence is kept."""
    _ledger_with(tmp_path, poison)
    events, torn = I.read_ledger(tmp_path)
    assert all(isinstance(e, dict) for e in events)
    assert len(events) == 2
    assert torn is False                                # a non-object line is NOT a torn tail
