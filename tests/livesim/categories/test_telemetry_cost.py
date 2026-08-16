"""LiveSim: cost/token/spend recording — the money-truth subsystem.

Three ledgers coexist and must each tell the truth about spend:

  * ``provider_call_usage`` (live runtime DB) — one row per provider call, with
    a tri-state ``cost_quality`` enum (exact|estimated|unknown) enforced by a
    CHECK constraint that also binds which cost columns may be populated.
    'mixed' exists ONLY as an aggregate verdict (db/store.py), never row-level.
  * ``loop_budget.py`` — the hard pre-paid admission ledger. Caps are CODE
    constants (never DB-mutable): GLOBAL_DAILY_CEILING_USD=200,
    DEFAULT_INSTANCE_CAP_USD=50, INSTANCE_CAPS['render_probe']=10. Fail-closed:
    unknown cost is refused, expiry is CHARGED at maximum, never freed.
  * LiveSim's own ledger (``var/livesim/ledger.jsonl``) — every test records
    model/provider/cost/tokens; a $0 test records model=null + cost_quality
    'n/a' faithfully (an absent model is never invented).

All budget-ledger *enforcement* tests run against SCRATCH databases under
scratch_dir with an injected clock — never the live DB. The live DB is read
strictly read-only for row-integrity observation.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.livesim

import livesim_common as lc  # noqa: E402  (conftest puts scripts/livesim on sys.path)

from omniagentos.db.store import _aggregate_provider_call_cost_rows  # noqa: E402
from omniagentos.scheduler import loop_budget  # noqa: E402

# A fixed mid-day UTC epoch so day-boundary math can never flake across the
# real midnight while a test is running.
_FIXED_NOW = 20_000 * 86_400.0 + 43_200.0  # 2024-10-04T12:00:00Z

#: Row-level cost-quality vocabulary (DB CHECK-enforced).
_ROW_QUALITIES = {"exact", "estimated", "unknown"}


class _Clock:
    """Injectable, advanceable clock for deterministic budget tests."""

    def __init__(self, start: float = _FIXED_NOW) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _scratch_ledger(scratch_dir: Path, name: str, clock: _Clock, **kw) -> loop_budget.LoopBudgetLedger:
    return loop_budget.LoopBudgetLedger(scratch_dir / f"{name}.sqlite3", clock=clock, **kw)


# ---------------------------------------------------------------------------
# Spend caps: code-defined constants (the authorization surface)
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_loop_budget_cap_constants_are_code_defined(livesim):
    """The spend caps are code constants with the documented values, ordered
    sanely (instance cap <= global ceiling), and INSTANCE_CAPS lives in code —
    a loop operator cannot raise its own ceiling by editing a row."""
    livesim.target("fs")
    caps = {
        "global_daily_ceiling_usd": loop_budget.GLOBAL_DAILY_CEILING_USD,
        "default_instance_cap_usd": loop_budget.DEFAULT_INSTANCE_CAP_USD,
        "instance_caps": dict(loop_budget.INSTANCE_CAPS),
    }
    livesim.record(inputs={"module": loop_budget.__name__}, outputs=caps)
    assert loop_budget.GLOBAL_DAILY_CEILING_USD == 200.0
    assert loop_budget.DEFAULT_INSTANCE_CAP_USD == 50.0
    assert loop_budget.INSTANCE_CAPS["render_probe"] == 10.0
    # Structural sanity: every cap is positive and no instance cap exceeds the
    # global ceiling; the default cap also fits under the ceiling.
    assert 0.0 < loop_budget.DEFAULT_INSTANCE_CAP_USD <= loop_budget.GLOBAL_DAILY_CEILING_USD
    for iid, cap in loop_budget.INSTANCE_CAPS.items():
        assert 0.0 < cap <= loop_budget.GLOBAL_DAILY_CEILING_USD, (iid, cap)
    livesim.cleanup(True)


@pytest.mark.boundary
def test_loop_budget_caps_enforced_at_the_boundary(livesim, scratch_dir):
    """SCRATCH ledger: at-cap admission is allowed, one epsilon over is refused
    (instance_cap_exceeded), and a small global ceiling refuses across
    instances (global_ceiling_exceeded). Never touches the live DB."""
    livesim.target("fs")
    clock = _Clock()
    led = _scratch_ledger(scratch_dir, "caps", clock)
    try:
        # render_probe cap is 10.0 (code-defined).
        r1 = led.reserve(instance_id="render_probe", capability_id="replicate.generate", estimated_max_usd=6.0)
        led.settle(r1.id, actual_usd=6.0, cost_quality="exact")
        with pytest.raises(loop_budget.BudgetRefused) as over:
            led.reserve(instance_id="render_probe", capability_id="replicate.generate", estimated_max_usd=5.0)
        assert over.value.reason == "instance_cap_exceeded"
        # Exactly-at-cap (6 settled + 4 = 10.0) must still be admitted.
        r2 = led.reserve(instance_id="render_probe", capability_id="replicate.generate", estimated_max_usd=4.0)
        assert r2.state == loop_budget.STATE_OPEN
        led.release(r2.id)  # provably-never-happened: freed, charges nothing
    finally:
        led.close()

    led2 = _scratch_ledger(scratch_dir, "ceiling", clock, global_ceiling_usd=15.0)
    try:
        led2.reserve(instance_id="inst_a", capability_id="cap.x", estimated_max_usd=10.0)
        with pytest.raises(loop_budget.BudgetRefused) as g:
            led2.reserve(instance_id="inst_b", capability_id="cap.x", estimated_max_usd=6.0)
        assert g.value.reason == "global_ceiling_exceeded"
    finally:
        led2.close()
    livesim.record(
        inputs={"instance_cap": 10.0, "global_ceiling": 15.0},
        outputs={"over_cap_reason": over.value.reason, "over_ceiling_reason": g.value.reason},
    )
    livesim.cleanup(True)  # scratch DBs live under the run's evidence dir


@pytest.mark.negative
def test_loop_budget_refuses_unknown_cost(livesim, scratch_dir):
    """SCRATCH ledger: None/NaN/inf/negative/zero estimated cost is refused
    with UnknownCostRefused (fail-closed: no admission without a real bound)."""
    livesim.target("fs")
    clock = _Clock()
    led = _scratch_ledger(scratch_dir, "unknown-cost", clock)
    refused = {}
    try:
        for label, bad in [
            ("none", None),
            ("nan", float("nan")),
            ("inf", float("inf")),
            ("negative", -1.0),
            ("zero", 0.0),
        ]:
            with pytest.raises(loop_budget.UnknownCostRefused) as exc:
                led.reserve(instance_id="probe", capability_id="cap.x", estimated_max_usd=bad)
            refused[label] = exc.value.reason
            assert exc.value.reason == "unknown_cost", label
    finally:
        led.close()
    livesim.record(inputs={"bad_costs": list(refused)}, outputs=refused)
    livesim.cleanup(True)


@pytest.mark.recovery
def test_loop_budget_expiry_charges_as_unknown_never_frees(livesim, scratch_dir):
    """SCRATCH ledger: an owner that dies between reserve and settle leaves a
    reservation that EXPIRES CHARGED at its full maximum with
    cost_quality='unknown', still counts against the day's caps, and is
    terminal — a late settle or release cannot lower the charge (fail-closed
    crash recovery)."""
    livesim.target("fs")
    clock = _Clock()
    led = _scratch_ledger(scratch_dir, "expiry", clock, ttl_seconds=900.0)
    try:
        res = led.reserve(instance_id="crashy", capability_id="cap.x", estimated_max_usd=7.5)
        clock.now += 901.0  # owner never came back
        reclaimed = led.reclaim_expired()
        assert res.id in reclaimed
        charged = led.get_reservation(res.id)
        assert charged.state == loop_budget.STATE_EXPIRED_UNKNOWN
        assert charged.actual_usd == 7.5  # charged at reserved maximum
        assert charged.cost_quality == "unknown"
        # Terminal: neither a late settle nor a release may hand money back.
        with pytest.raises(loop_budget.BudgetError):
            led.settle(res.id, actual_usd=0.01, cost_quality="exact")
        with pytest.raises(loop_budget.BudgetError):
            led.release(res.id)
        # The charge stays on the day's books, attributed as unaccounted.
        state = led.get_instance_state("crashy", sweep=False)
        assert state.settled_usd == 7.5
        assert state.unaccounted_usd == 7.5
        assert state.outstanding_usd == 0.0
    finally:
        led.close()
    livesim.record(
        inputs={"reserved_usd": 7.5, "ttl_s": 900},
        outputs={
            "state": charged.state,
            "charged_usd": charged.actual_usd,
            "cost_quality": charged.cost_quality,
            "unaccounted_usd": state.unaccounted_usd,
        },
    )
    livesim.cleanup(True)


@pytest.mark.concurrency
def test_loop_budget_concurrent_reserves_never_oversubscribe(livesim, scratch_dir):
    """SCRATCH ledger: 8 threads race to reserve $10 against a $50 cap —
    exactly 5 admissions succeed and committed spend never exceeds the cap
    (the module's whole reason to exist: synchronous pre-paid admission)."""
    livesim.target("fs")
    clock = _Clock()
    led = _scratch_ledger(scratch_dir, "concurrent", clock)  # default cap 50.0
    successes: list[str] = []
    refusals: list[str] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            r = led.reserve(instance_id="racer", capability_id="cap.x", estimated_max_usd=10.0)
            with lock:
                successes.append(r.id)
        except loop_budget.BudgetRefused as exc:
            with lock:
                refusals.append(exc.reason)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        state = led.get_instance_state("racer", sweep=False)
    finally:
        led.close()
    livesim.record(
        inputs={"threads": 8, "reserve_usd": 10.0, "cap_usd": 50.0},
        outputs={"admitted": len(successes), "refused": len(refusals), "committed_usd": state.committed_usd},
    )
    assert len(successes) == 5, (successes, refusals)
    assert len(refusals) == 3
    assert all(r == "instance_cap_exceeded" for r in refusals)
    assert state.committed_usd == 50.0
    assert state.committed_usd <= loop_budget.DEFAULT_INSTANCE_CAP_USD  # the invariant
    livesim.cleanup(True)


# ---------------------------------------------------------------------------
# provider_call_usage: row-level cost-quality integrity (LIVE DB, read-only)
# ---------------------------------------------------------------------------


@pytest.mark.positive
def test_provider_call_usage_rows_have_coherent_cost_quality(livesim, live_db_ro):
    """LIVE DB (ro): every provider_call_usage row carries a cost_quality from
    the row-level enum, and its cost columns match that quality tri-state
    (exact => real cost; estimated => upper bound only; unknown => no cost).
    Counts are recorded as data; the invariant is what is asserted."""
    livesim.target("db")
    rows = live_db_ro.execute(
        "SELECT call_id, cost_quality, cost_usd_decimal, cost_usd_nanos, "
        "cost_upper_bound_usd_nanos, input_tokens, output_tokens, adapter_key, "
        "request_state FROM provider_call_usage"
    ).fetchall()
    by_quality: dict[str, int] = {}
    tokens_null = 0
    adapters: set[str] = set()
    for r in rows:
        q = r["cost_quality"]
        by_quality[q] = by_quality.get(q, 0) + 1
        adapters.add(r["adapter_key"])
        assert q in _ROW_QUALITIES, f"{r['call_id']}: row-level quality {q!r} outside enum"
        if q == "exact":
            assert r["cost_usd_decimal"] is not None and r["cost_usd_nanos"] is not None
        elif q == "estimated":
            assert r["cost_upper_bound_usd_nanos"] is not None
            assert r["cost_usd_decimal"] is None and r["cost_usd_nanos"] is None
        else:  # unknown
            assert r["cost_usd_decimal"] is None and r["cost_usd_nanos"] is None
        if r["input_tokens"] is None and r["output_tokens"] is None:
            tokens_null += 1
    out = {
        "row_count": len(rows),
        "by_quality": by_quality,
        "rows_with_no_tokens": tokens_null,
        "adapter_keys": sorted(adapters),
    }
    livesim.evidence("provider-call-usage-quality.json", json.dumps(out, indent=2))
    livesim.record(inputs={"table": "provider_call_usage"}, outputs=out)
    # 'mixed' is an aggregate-only verdict and must never appear row-level.
    assert "mixed" not in by_quality
    if rows and tokens_null == len(rows):
        livesim.note(
            "OBSERVATION: every provider_call_usage row has NULL tokens and none is "
            f"cost_quality='exact' (adapters seen: {sorted(adapters)}) — no exact cost "
            "accounting is flowing into the table; cost truth is estimate-only."
        )
    livesim.cleanup(True)


@pytest.mark.boundary
def test_cost_aggregate_mixed_only_when_qualities_differ(livesim, live_db_ro, livesim_ns, scratch_dir):
    """SCRATCH DB (schema copied read-only from live): the aggregate cost
    projection reports 'mixed' ONLY when per-call qualities differ, the
    homogeneous quality otherwise, and the DB CHECK rejects a row-level
    'mixed'. All synthetic rows are tagged with the livesim namespace and live
    in a throwaway scratch DB."""
    livesim.target("db", "fs")
    ddl_row = live_db_ro.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='provider_call_usage'"
    ).fetchone()
    if ddl_row is None:
        pytest.skip("provider_call_usage table absent from live DB")
    scratch_db = scratch_dir / "agg.sqlite3"
    conn = sqlite3.connect(scratch_db)
    conn.row_factory = sqlite3.Row
    conn.execute(ddl_row["sql"])

    def insert(run_id: str, n: int, quality: str) -> None:
        for i in range(n):
            cost_dec = "0.5" if quality == "exact" else None
            cost_nanos = 500_000_000 if quality == "exact" else None
            upper = 1_000_000_000 if quality == "estimated" else None
            conn.execute(
                "INSERT INTO provider_call_usage (call_id, request_id, execution_id,"
                " run_id, stage, attempt_index, provider, transport, requested_model,"
                " effective_model, model_lineage, billing_provider, adapter_key,"
                " request_state, cost_usd_decimal, cost_usd_nanos,"
                " cost_upper_bound_usd_nanos, cost_quality, cost_source, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{livesim_ns}-{run_id}-{quality}-{i}", f"{livesim_ns}-req", f"{livesim_ns}-exec",
                    run_id, "worker", 0, "synthetic", "api", "synthetic-model",
                    "synthetic-model", "synthetic", "synthetic", "livesim-scratch",
                    "sent", cost_dec, cost_nanos, upper, quality,
                    "livesim:synthetic", "2026-08-06T00:00:00Z",
                ),
            )

    cases = {
        "all_exact": [("exact", 3)],
        "all_estimated": [("estimated", 2)],
        "exact_plus_estimated": [("exact", 2), ("estimated", 1)],
        "exact_plus_unknown": [("exact", 1), ("unknown", 1)],
        "all_unknown": [("unknown", 2)],
    }
    expected = {
        "all_exact": "exact",
        "all_estimated": "estimated",
        "exact_plus_estimated": "mixed",
        "exact_plus_unknown": "mixed",
        "all_unknown": "unknown",
    }
    got: dict[str, str] = {}
    try:
        for case, mix in cases.items():
            run_id = f"{livesim_ns}-{case}"
            for quality, n in mix:
                insert(run_id, n, quality)
            agg = _aggregate_provider_call_cost_rows(conn, run_id=run_id)
            got[case] = agg["quality"]
            if "unknown" in dict(mix):
                assert agg["accounting_incomplete"] is True, case
        # Zero rows is honestly incomplete, never a fabricated zero-dollar total.
        empty = _aggregate_provider_call_cost_rows(conn, run_id=f"{livesim_ns}-none")
        assert empty["quality"] == "unknown" and empty["accounting_incomplete"] is True
        assert empty["chargeable_usd"] is None
        # Row-level 'mixed' is impossible: the CHECK constraint rejects it.
        with pytest.raises(sqlite3.IntegrityError):
            insert(f"{livesim_ns}-bad", 1, "mixed")
        assert got == expected, got
    finally:
        conn.close()
        cleanup_ok = True
        try:
            scratch_db.unlink(missing_ok=True)
        except OSError:
            cleanup_ok = False
        livesim.cleanup(cleanup_ok)
    livesim.record(inputs=cases, outputs=got)


# ---------------------------------------------------------------------------
# The LiveSim ledger itself: model/provider/cost/tokens recorded per test
# ---------------------------------------------------------------------------


def _read_ledger() -> list[dict]:
    path = lc.ledger_path()
    if not path.exists():
        pytest.skip(f"livesim ledger not yet written at {path}")
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            records.append(json.loads(line))
    if not records:
        pytest.skip("livesim ledger exists but is empty")
    return records


@pytest.mark.positive
def test_livesim_ledger_records_cost_schema_completeness(livesim):
    """The suite's own ledger practices what it preaches: every livesim.v1
    record carries the model/provider/cost/token fields (present, possibly
    null), a numeric non-negative cost, a status from the enum, and a
    live_target list. Counts are environment-dependent data, not assertions."""
    livesim.target("fs")
    records = _read_ledger()
    required = {
        "schema", "run_id", "nodeid", "status", "model", "provider",
        "cost_usd", "cost_quality", "tokens_in", "tokens_out",
        "live_target", "notes", "cleanup_ok",
    }
    statuses: dict[str, int] = {}
    for rec in records:
        missing = required - rec.keys()
        assert not missing, f"{rec.get('nodeid')}: ledger record missing {sorted(missing)}"
        assert rec["schema"] == "livesim.v1"
        assert rec["status"] in {"pass", "fail", "skip", "xfail"}
        assert isinstance(rec["cost_usd"], (int, float)) and rec["cost_usd"] >= 0.0
        assert not math.isnan(float(rec["cost_usd"]))
        assert isinstance(rec["live_target"], list)
        assert isinstance(rec["cost_quality"], str) and rec["cost_quality"]
        for tk in ("tokens_in", "tokens_out"):
            assert rec[tk] is None or (isinstance(rec[tk], int) and rec[tk] >= 0)
        statuses[rec["status"]] = statuses.get(rec["status"], 0) + 1
    livesim.record(
        inputs={"ledger": str(lc.ledger_path())},
        outputs={"records": len(records), "statuses": statuses},
    )
    livesim.cleanup(True)


@pytest.mark.degradation
def test_livesim_ledger_zero_dollar_records_na_not_fake(livesim):
    """Degradation honesty: a $0 API/DB/proc test (no model involved) is
    recorded with model=null and cost_quality='n/a' — an absent model is never
    invented and never given a fabricated cost quality. This very test appends
    another such record when it finishes."""
    livesim.target("fs")
    records = _read_ledger()
    modelless = [r for r in records if r.get("model") is None]
    if not modelless:
        pytest.skip("no $0 (model-less) records in the ledger yet")
    bad = [
        r["nodeid"] for r in modelless
        if r.get("cost_quality") != "n/a" or float(r.get("cost_usd") or 0.0) != 0.0
    ]
    with_model = [r for r in records if r.get("model") is not None]
    livesim.record(
        inputs={"total_records": len(records)},
        outputs={
            "modelless_records": len(modelless),
            "modelless_with_fabricated_quality_or_cost": len(bad),
            "records_with_model": len(with_model),
        },
    )
    assert not bad, f"$0 records with fabricated cost data: {bad[:5]}"
    livesim.note(
        "livesim.v1 uses cost_quality vocabulary exact|approximate|unreported|n/a "
        "while provider_call_usage/loop_budget use exact|estimated|unknown(+aggregate "
        "mixed) — two vocabularies for the same concept."
    )
    livesim.cleanup(True)
