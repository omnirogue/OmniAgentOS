"""F8 — an operator's explicit `0` on a governor knob is a VALUE, not an absence.

`X or DEFAULT` cannot tell "the key is absent" from "the operator wrote 0",
and every governor knob whose zero means HALT is therefore silently
un-set by that idiom. Two readers were already repaired for exactly this
shape and are regression-pinned in tests/test_publish_queue.py:

    F6  bridge/publish_queue.py:read_wip_cap   -> explicit None check
    F7  bridge/integration.py:read_governor    -> `4 if raw is None else int(raw)`

The F6/F7 sweep reached 2 of the 3 `wip_cap` readers and 1 of the 2
`load_avg_1m_max` readers. This file pins the survivors. It is deliberately
organised by VALUE (the knob) rather than by module, because the recurring
defect here is an incomplete propagation across a clone family — the same
one-line idiom copied into every module that reads the budget.

Severity is NOT uniform across the three, and the tests say so:

  * claim.py     — FAIL-OPEN. `wip_cap: 0` is the documented full stop, and
                   claim.py is the module that actually mints claims. The
                   halt half-engages: the published queue reports cap 0
                   while acquire() keeps granting 4 concurrent slots.
  * governor.py  — FAIL-OPEN. `load_avg_1m_max: 0` (defer on any load at
                   all) is replaced by the host's performance-core count.
  * integrity.py — `disk_free_gb: 0` (a genuinely full disk) reads as 1e9
                   free, so the "correctly stalled, say nothing" suppression
                   does not engage and the liveness check fires a false
                   "loops are silent" alarm at exactly the moment an operator
                   is dealing with a full disk.

The first round of this file called that third one "FAIL-LOUD, not fail-open,"
and the review REFUTED it (R2-F4). Correcting the disk carrier widened the set
of states reaching `if blocked: return`, and that early return also skipped the
queue-growth alarm — so the first-round fix silently traded a false silence
alarm for a MISSED growth alarm, which is fail-open. The suppression is now
scoped to the silence alerts a stall actually explains. The lesson is kept here
deliberately: a fix that makes a guard fire more often must be checked for what
that guard's success path skips.

Round 2 additions (R2-F1..F4) all come from the google-lineage review seat and
were each reproduced independently before being accepted.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PKG))

from bridge import claim as C  # noqa: E402
from bridge import governor as G  # noqa: E402
from bridge import integration as I  # noqa: E402
from bridge import integrity as N  # noqa: E402
from bridge import publish_queue as P  # noqa: E402


def _budget(root: Path, **overrides) -> Path:
    """A complete, FRESH budget.json — fresh so that the staleness guard in
    check_liveness() cannot be what makes an assertion here pass or fail."""
    state = root / "state"
    state.mkdir(parents=True, exist_ok=True)
    body = {
        "updated_at": I._iso(I._now()),
        "wip_cap": 4,
        "disk_free_gb": 100, "disk_free_gb_min": 20,
        "load_avg_1m": 0.5, "load_avg_1m_max": 20,
        "metered_usd": {}, "subscription": {"accounts": {}},
    }
    body.update(overrides)
    path = state / "budget.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------- wip_cap

def test_F8_claim_read_wip_cap_honours_an_explicit_zero(tmp_path: Path):
    """The surviving third carrier of the F6/F7 falsy-zero.

    `int(budget.get("wip_cap") or DEFAULT_WIP_CAP)` returns 4 for an operator
    who wrote 0. claim.py is the module that MINTS claims, so this is the one
    of the three readers where the wrong answer actually grants work.
    """
    root = tmp_path / "lq"
    _budget(root, wip_cap=0)
    assert C.read_wip_cap(root) == 0, (
        "claim.read_wip_cap collapsed an explicit wip_cap:0 into the default — "
        "an operator halt that grants claim slots"
    )


def test_F8_all_three_wip_cap_readers_agree_on_an_explicit_zero(tmp_path: Path):
    """F7 pinned that the two PUBLISHERS agree. The invariant it was really
    protecting is that every reader of one budget key returns one value —
    and the third reader breaks it, which is why the pair-wise pin held green.
    """
    root = tmp_path / "lq"
    _budget(root, wip_cap=0)
    gov = I.read_governor(root)
    assert C.read_wip_cap(root) == P.read_wip_cap(root) == gov.wip_cap == 0


def test_F8_acquire_refuses_at_an_explicit_wip_cap_zero(tmp_path: Path):
    """The consequence, not just the reader: with the halt in force, the very
    first acquire must be refused. This is the assertion an operator actually
    cares about — the unit test above only proves the number is right.
    """
    root = tmp_path / "lq"
    _budget(root, wip_cap=0)
    (root / "claims").mkdir(parents=True, exist_ok=True)
    (root / "state" / "landers.json").write_text(json.dumps({
        "repo": "test-repo", "last_tick_ts": C._iso(C._now()),
        "status": "ok", "pid": 1,
    }))
    with pytest.raises(C.ClaimError) as exc:
        C.acquire(root, "sha256:" + "a" * 64, actor="probe@test", role="reviewer")
    assert exc.value.code == C.EXIT_AT_CAP, (
        f"expected the at-cap refusal under a wip_cap:0 halt, got code {exc.value.code}"
    )


# -------------------------------------------------------- load_avg_1m_max

def test_F8_governor_check_honours_an_explicit_load_ceiling_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    """`budget.get("load_avg_1m_max") or perf_core_count() or ...` replaces an
    operator's 0 with the host's core count.

    Asserted on the CEILING GOVERNOR REPORTS, and deliberately UNSTUBBED.
    Stubbing the sampler here would now bypass the halt short-circuit that
    lives inside it, so the stub would manufacture the very "clear" verdict
    this test exists to forbid. Both sides run fast without it: at base the
    ceiling is the core count and ambient load clears on the first sample; on
    the candidate a ceiling of 0 short-circuits.
    """
    path = _budget(tmp_path / "lq", load_avg_1m_max=0)
    G.check(path)
    reported = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert reported["load"]["ceiling"] == 0.0, (
        f"governor reported ceiling {reported['load']['ceiling']!r} instead of the "
        "operator's explicit load_avg_1m_max:0"
    )
    assert reported["gate_clear"] is False, (
        "an explicit load halt must not report the gate as clear"
    )


# --------------------------------------------------------- disk_free_gb

def test_F8_integrity_treats_a_zero_disk_reading_as_a_genuine_stall(tmp_path: Path):
    """`(budget.get("disk_free_gb") or 1e9)` turns a full disk into infinite
    headroom, so the "correctly stalled — say nothing" branch never engages.

    The visible damage is a false silence alarm during a real disk-full stall.
    Do NOT read that as "harmless": this docstring used to say "fail-loud, not
    fail-open" and the review refuted it — see
    test_F8r2_a_governor_stall_does_not_blind_the_queue_growth_alarm, which
    pins the fail-open half that correcting this carrier exposed.

    Asserted via observable behaviour (alerts raised) rather than by reaching
    into the local `blocked` list.
    """
    root = tmp_path / "lq"
    _budget(root, disk_free_gb=0, disk_free_gb_min=20)
    # A well-formed but LONG-SILENT ledger. Deliberately not an empty file:
    # zero bytes raises LedgerTornError, which would make this test fail for
    # an instrument reason of the fixture's own making rather than for the
    # falsy-zero defect it claims to pin.
    (root / "ledger.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "event": "proposed",
                    "role": "planner", "id": "sha256:" + "b" * 64}) + "\n",
        encoding="utf-8",
    )

    r = N.Result()
    N.check_liveness(root, r)
    # `r.alert`, not `r.fail` — check_liveness raises silence via alerts, and
    # asserting on `failures` here would be green at base and after, pinning
    # nothing at all.
    assert r.alerts == [], (
        "integrity alarmed that the loops were silent 'while the governor "
        "reports no blocking limit' — but the disk was full and that IS a "
        f"blocking limit; got {r.alerts!r}"
    )


# ======================================================================
# Round 2 — raised by the google-lineage review seat (gemini-3.1-pro-preview)
# against a752486. All four reproduced independently before being accepted.
# ======================================================================

@pytest.mark.parametrize("bad", [
    "", "abc", [], {}, {"n": 1},
    # R4: the shapes the confirming round proved were unpinned. `1e400` is the
    # sharp one — json.loads turns it into float('inf') with NO parse error,
    # and int(inf) raises OverflowError, which is not a ValueError subclass and
    # escaped the round-2 handler as an unhandled traceback out of the claim
    # minter. "NaN"/"Infinity" are literal JSON extensions Python's json
    # accepts by default.
    1e400, float("inf"), float("-inf"), float("nan"), "NaN", "Infinity",
])
def test_F8r2_a_malformed_wip_cap_is_an_instrument_error_not_a_default(
    tmp_path: Path, bad
):
    """R2-F1. Replacing `or DEFAULT` with an `is None` check moved malformed
    values from "silently becomes 4" to "unhandled ValueError". NEITHER is
    right: a present-but-unusable cap is an instrument error, which is the
    class read_wip_cap already raises for a corrupt budget.json.

    The old idiom's behaviour was the worse of the two — a malformed cap read
    as four free slots, an abnormal condition rendering as a favourable value.
    """
    root = tmp_path / "lq"
    _budget(root, wip_cap=bad)
    with pytest.raises(C.ClaimError) as exc:
        C.read_wip_cap(root)
    assert exc.value.code == C.EXIT_INSTRUMENT_ERROR


def test_F8r2_a_malformed_load_ceiling_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """R2-F1, governor half. A ceiling that will not parse must stop the
    governor, not crash it and not read as headroom."""
    monkeypatch.setattr(
        G, "sample_load_until_clear",
        lambda ceiling, **kw: G.LoadCheck(clear=True, ceiling=ceiling, reason="stub"),
    )
    path = _budget(tmp_path / "lq", load_avg_1m_max="not-a-number")
    assert G.check(path) == G.CHECK_BLOCKED


def test_F8r2_a_malformed_disk_reading_fails_the_check(tmp_path: Path):
    """R2-F1, integrity half. `"" or 1e9` used to coerce garbage to a
    favourable value; it must be recorded as unusable instead."""
    root = tmp_path / "lq"
    _budget(root, disk_free_gb="")
    r = N.Result()
    N.check_liveness(root, r)
    assert any(f["assertion"] == "governor.budget_values_usable" for f in r.failures), (
        f"a malformed disk reading was not reported as unusable; got {r.failures!r}"
    )


def test_F8r2_integrity_defaults_an_absent_load_ceiling_like_its_siblings(
    tmp_path: Path,
):
    """R2-F2 — the carrier the first round MISSED, and the strongest finding
    of the review. With `load_avg_1m_max` ABSENT, governor.check() and
    integration.read_governor() both fall back to the host performance-core
    count, so a load of 20 IS a stall to them. integrity used a local 1e9 and
    therefore could not see it — then fired the exact "silent while the
    governor reports no blocking limit" alarm this function exists to suppress.

    The first round ruled this line not-applicable by checking only its
    explicit-zero behaviour and never its absent-default agreement.
    """
    root = tmp_path / "lq"
    sibling_ceiling = G.perf_core_count() or os.cpu_count() or 8
    stalled_load = float(sibling_ceiling + 1)
    b = _budget(root, load_avg_1m=stalled_load)
    body = json.loads(b.read_text())
    del body["load_avg_1m_max"]                      # genuinely ABSENT
    b.write_text(json.dumps(body), encoding="utf-8")
    (root / "ledger.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "event": "proposed",
                    "role": "planner", "id": "sha256:" + "b" * 64}) + "\n",
        encoding="utf-8",
    )
    assert stalled_load > sibling_ceiling, (
        "fixture no longer exceeds this host's core count — pick a larger load"
    )
    r = N.Result()
    N.check_liveness(root, r)
    assert r.alerts == [], (
        "integrity did not recognise a load stall that governor.py and "
        f"integration.py both would (ceiling {sibling_ceiling}); got {r.alerts!r}"
    )


def test_F8r2_a_governor_stall_does_not_blind_the_queue_growth_alarm(tmp_path: Path):
    """R2-F4 — a regression THIS candidate introduced. `if blocked: return`
    skipped the queue-growth check too, and correcting the disk carrier
    widened the set of states that reach it. A stall explains silence; it does
    not explain producers filing while nothing drains — that is most worth
    knowing during an outage, not least.
    """
    root = tmp_path / "lq"
    _budget(root, disk_free_gb=0, disk_free_gb_min=20)      # a genuine stall
    now = I._iso(I._now())
    lines = [json.dumps({"ts": now, "event": "proposed", "role": "planner",
                         "id": "sha256:" + f"{i:064d}"}) for i in range(12)]
    (root / "ledger.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    r = N.Result()
    N.check_liveness(root, r)
    growth = [a for a in r.alerts if "produced in 6h" in a]
    assert growth, (
        "the queue grew by 12 with zero drained during a disk stall and "
        f"nothing said so; alerts={r.alerts!r}"
    )
    assert not [a for a in r.alerts if "silent" in a], (
        f"silence alerts should still be suppressed by the stall; got {r.alerts!r}"
    )


def test_F8r3_an_explicit_halt_short_circuits_in_the_shared_sampler():
    """R3. The halt short-circuit belongs to sample_load_until_clear itself,
    not to a caller.

    An earlier draft put it in governor.check() alone; the xai-lineage seat
    caught that integration.read_governor(live_load=True) calls the same
    sampler and would still have paid the full ladder — the incomplete
    propagation this whole candidate is about. Pinning the SHARED function
    is what makes both callers correct, and any future one.

    UNSTUBBED and wall-clock bounded, per that seat's specific objection that
    a stubbed sampler "would not fail on an infinite wait". A sleeper tripwire
    proves no waiting happened rather than inferring it from elapsed time.
    """
    slept: list[float] = []
    lc = G.sample_load_until_clear(
        0.0,
        sampler=lambda: (_ for _ in ()).throw(
            AssertionError("sampled the load despite an explicit halt ceiling of 0")),
        sleeper=slept.append,
    )
    assert lc.clear is False
    assert lc.waited_s == 0.0 and slept == [], f"waited during a halt: {slept!r}"
    assert lc.samples == []


@pytest.mark.parametrize("bad", ["not-a-number", float("nan"), "NaN", 1e400])
def test_F8r4_a_nonfinite_load_ceiling_fails_closed(tmp_path: Path, bad):
    """R4. NaN is the dangerous shape and the one a same-lineage pass missed:
    EVERY comparison against it is False, so a NaN ceiling simultaneously
    defeats `load <= ceiling`, `load > ceiling` AND the `ceiling <= 0` halt
    check — producing no verdict anywhere instead of an obvious error.

    Measured before the fix: it did NOT bypass the gate (gate_clear was
    correctly false, refuting the reviewer's stated mechanism), but it did
    reach the sampler and pay the full 45s ladder while reporting a nonsense
    "ceiling nan". It belongs at the fail-closed branch instead.
    """
    path = _budget(tmp_path / "lq", load_avg_1m_max=bad)
    assert G.check(path) == G.CHECK_BLOCKED


def test_F8r4_finite_limit_is_the_single_shared_guard():
    """The three modules must not each grow their own coercion — that clone
    family is the whole subject of this candidate.

    Asserted on SOURCE FILE, not on object identity: this package is imported
    both as `bridge.governor` and, via each module's own sys.path.insert, as
    top-level `governor`, so the same file legitimately yields two module
    objects. Discovered by writing the identity assertion and watching it fail.
    """
    assert C._governor.__file__ == G.__file__ == N._governor.__file__
    for bad in ("", "abc", [], float("nan"), float("inf"), 1e400):
        with pytest.raises(G.MalformedLimit):
            G.finite_limit(bad, what="probe")
    assert G.finite_limit(0, what="probe") == 0.0      # an explicit 0 still passes
    assert G.finite_limit("12", what="probe") == 12.0


def test_F8r4_malformed_limit_survives_the_dual_import():
    """The load-bearing consequence of that dual import: because the same file
    is two module objects, `C._governor.MalformedLimit` is NOT the same class
    as `G.MalformedLimit`, so an `except G.MalformedLimit` at one call site
    would silently fail to catch the other's raise.

    It is safe ONLY because MalformedLimit subclasses ValueError and every
    catch site includes ValueError. That is an invariant, not a coincidence,
    so it is pinned here rather than left to be rediscovered.
    """
    assert issubclass(G.MalformedLimit, ValueError)
    assert issubclass(C._governor.MalformedLimit, ValueError)
    with pytest.raises(ValueError):
        C._governor.finite_limit("abc", what="probe")
    with pytest.raises(ValueError):
        N._governor.finite_limit(float("nan"), what="probe")


def test_F8r3_a_malformed_disk_floor_fails_closed_instead_of_crashing(tmp_path: Path):
    """PRE-EXISTING on main, found while answering the confirming round's
    question about whether `disk_free_gb_min` was covered: it was not.

    `free < floor` with a non-numeric floor raised an uncaught TypeError out
    of check() — the governor died rather than stopping, which is the worst
    of the three outcomes (a dead governor stops nobody). Same value as the
    rest of this file: a malformed limit must fail closed.
    """
    path = _budget(tmp_path / "lq", disk_free_gb_min="abc")
    assert G.check(path) == G.CHECK_BLOCKED


@pytest.mark.parametrize("caller", ["governor.check", "integration.read_governor"])
def test_F8r3_neither_load_caller_pays_the_halt_ladder(tmp_path: Path, caller: str):
    """R3, the propagation half — measured end to end with NOTHING stubbed.

    Both live-load entry points must return promptly under an explicit halt.
    The bound is deliberately loose (2s vs the measured 15s floor of the
    defect) so this pins the regression without becoming a flaky timing test.
    """
    import time
    path = _budget(tmp_path / "lq", load_avg_1m_max=0)
    root = path.parent.parent
    t0 = time.monotonic()
    if caller == "governor.check":
        G.check(path)
    else:
        gov = I.read_governor(root, live_load=True)
        assert gov.load_stops, "an explicit halt must register a load stop"
    elapsed = time.monotonic() - t0
    assert elapsed < 2.0, (
        f"{caller} spent {elapsed:.1f}s under an explicit load halt; the "
        "sampler ladder is being paid for a decision no reading can change"
    )
