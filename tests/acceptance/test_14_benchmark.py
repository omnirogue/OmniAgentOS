"""AT4 area 14 — Benchmark integrity.

Acceptance claim: for every benchmarked run the system records **quality, time,
cost, pass/fail and confidence** — and never substitutes a fabricated value for
a missing measurement.

Coverage map (each dimension is asserted in at least two places, at the schema
layer and at the aggregation layer):

  quality     scorecard deltas / ``EffortStats.green_rate``
  time        ``configtest_runs.wall_ms`` / ``TaskCostToGreen.wall_ms``
  cost        ``configtest_runs.cost_usd`` + ``tokens_in``/``tokens_out``
              / ``EffortStats.cost_per_green``
  pass/fail   ``configtest_runs.status`` CHECK / ``reached_green``
  confidence  Wilson interval, replicate interval, ``EffortStats.confidence``

The recurring theme is fail-closed: a benchmark that cannot measure something
must say so (``None``, ``"sparse"``, a raised ``ValueError``) rather than
emitting a plausible-looking zero.

Hermetic: migrated tmp SQLite plus pure functions. No network, no model call.
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

from omniagentos.lab import stats as lab_stats
from omniagentos.lab.contracts import EvalResult, EvalSplit, MetricSpec
from omniagentos.lab.eval import scorecard as lab_scorecard
from omniagentos.swarm.costgreen import (
    EffortStats,
    cost_to_green_by_task,
    summarize_by_effort,
    summarize_run,
    task_cost_to_green,
)
from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT, SOURCE_NONE

# ---------------------------------------------------------------------------
# The benchmark row: every dimension has a column, and pass/fail is constrained
# ---------------------------------------------------------------------------

_RUN_COLUMNS = (
    "run_id, test_id, category, tier, formation_json, models_json, "
    "bracket_budget_json, status, gate_results_json, wall_ms, tokens_in, "
    "tokens_out, cost_usd, escalation_events_json, created_at"
)


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _insert_run(connection: sqlite3.Connection, run_id: str, **overrides: Any) -> None:
    values: dict[str, Any] = {
        "run_id": run_id,
        "test_id": "bench-1",
        "category": "refactor",
        "tier": "T1",
        "formation_json": "{}",
        "models_json": "[]",
        "bracket_budget_json": "{}",
        "status": "pass",
        "gate_results_json": '{"lint": "pass"}',
        "wall_ms": 12_500,
        "tokens_in": 4_000,
        "tokens_out": 1_200,
        "cost_usd": 0.42,
        "escalation_events_json": "[]",
        "created_at": "2026-07-27T09:00:00Z",
    }
    values.update(overrides)
    columns = [name.strip() for name in _RUN_COLUMNS.split(",")]
    connection.execute(
        f"INSERT INTO configtest_runs ({_RUN_COLUMNS}) VALUES ({', '.join('?' * len(columns))})",
        tuple(values[name] for name in columns),
    )


@pytest.mark.acceptance_smoke
def test_a_benchmark_run_row_records_time_cost_tokens_and_outcome(migrated_db: str) -> None:
    """All five dimensions round-trip on one row, with their real values."""
    connection = _connect(migrated_db)
    try:
        _insert_run(connection, "run-bench")
        row = dict(
            connection.execute(
                "SELECT * FROM configtest_runs WHERE run_id = 'run-bench'"
            ).fetchone()
        )

        assert row["wall_ms"] == 12_500  # time
        assert row["tokens_in"] == 4_000 and row["tokens_out"] == 1_200  # cost inputs
        assert row["cost_usd"] == pytest.approx(0.42)  # cost
        assert row["status"] == "pass"  # pass/fail
        assert row["gate_results_json"] == '{"lint": "pass"}'  # quality evidence
    finally:
        connection.close()


@pytest.mark.parametrize("status", ["pass", "fail", "timeout", "crash", "invalid"])
def test_every_terminal_benchmark_outcome_is_representable(migrated_db: str, status: str) -> None:
    """A timeout and a crash are distinct from a plain failure.

    Collapsing them would hide exactly the failures a benchmark exists to find.
    """
    connection = _connect(migrated_db)
    try:
        _insert_run(connection, f"run-{status}", status=status)
        stored = connection.execute(
            "SELECT status FROM configtest_runs WHERE run_id = ?", (f"run-{status}",)
        ).fetchone()
        assert stored["status"] == status
    finally:
        connection.close()


@pytest.mark.acceptance_daily
def test_an_unrecognised_outcome_is_rejected_rather_than_stored(migrated_db: str) -> None:
    """The pass/fail column is a closed set; a typo cannot become a new state."""
    connection = _connect(migrated_db)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_run(connection, "run-bogus", status="mostly_ok")
        assert (
            connection.execute(
                "SELECT COUNT(*) AS n FROM configtest_runs WHERE run_id = 'run-bogus'"
            ).fetchone()["n"]
            == 0
        )
    finally:
        connection.close()


@pytest.mark.acceptance_daily
def test_unmeasured_cost_is_null_not_zero(migrated_db: str) -> None:
    """A run with no reported usage stores NULL, so "free" is never inferred.

    ``cost_usd``/``wall_ms``/``tokens_*`` are deliberately nullable: a zero
    would make an unmeasured provider look like the cheapest one.
    """
    connection = _connect(migrated_db)
    try:
        _insert_run(
            connection,
            "run-unmeasured",
            wall_ms=None,
            tokens_in=None,
            tokens_out=None,
            cost_usd=None,
        )
        row = connection.execute(
            "SELECT wall_ms, tokens_in, tokens_out, cost_usd FROM configtest_runs "
            "WHERE run_id = 'run-unmeasured'"
        ).fetchone()
        assert dict(row) == {
            "wall_ms": None,
            "tokens_in": None,
            "tokens_out": None,
            "cost_usd": None,
        }
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Cost-to-green: the aggregation that answers "was the expensive tier worth it"
# ---------------------------------------------------------------------------


def _attempt(
    seq: int,
    *,
    effort: str,
    end_reason: str = "failed",
    model: str = "model-a",
    tier: str = "T1",
    cost: float = 1.0,
    wall_ms: int = 1_000,
    measured: bool = True,
    board_task_id: str = "task-1",
) -> dict[str, Any]:
    return {
        "seq": seq,
        "board_task_id": board_task_id,
        "effort": effort,
        "end_reason": end_reason,
        "model": model,
        "tier": tier,
        "cost_usd": cost,
        "input_tokens": 100,
        "output_tokens": 50,
        "wall_ms": wall_ms,
        "usage_source": SOURCE_CLI_REPORT if measured else SOURCE_NONE,
    }


@pytest.mark.acceptance_daily
def test_cost_to_green_totals_the_whole_retry_chain_not_one_attempt() -> None:
    """Retries and escalations are charged to the STARTING effort.

    Attributing the rescue to the tier that rescued it would make the expensive
    tier look expensive and the cheap tier look cheap -- exactly backwards.
    """
    record = task_cost_to_green(
        [
            _attempt(1, effort="medium", cost=1.0, wall_ms=1_000),
            _attempt(2, effort="medium", cost=2.0, wall_ms=2_000),
            _attempt(
                3, effort="xhigh", model="model-b", cost=5.0, wall_ms=4_000, end_reason="completed"
            ),
        ]
    )

    assert record.starting_effort == "medium", "the chain belongs to where it started"
    assert record.attempts == 3
    assert record.retries == 2
    assert record.escalations == 1, "one model change, not three retries"
    assert record.cost_usd == pytest.approx(8.0)  # cost: whole chain
    assert record.wall_ms == 7_000  # time: whole chain
    assert record.total_tokens == 450
    assert record.reached_green is True  # pass/fail
    assert record.measured is True  # confidence


def test_a_partially_measured_chain_is_marked_unmeasured() -> None:
    """One attempt without provider-reported usage taints the whole total.

    The totals still compute -- but they are a floor, not a figure, and the
    flag is the only thing that says so.
    """
    record = task_cost_to_green(
        [
            _attempt(1, effort="medium", measured=True),
            _attempt(2, effort="medium", measured=False, end_reason="completed"),
        ]
    )

    assert record.measured is False
    assert record.cost_usd == pytest.approx(2.0), "totals still add up; only trust drops"


@pytest.mark.acceptance_smoke
def test_effort_summary_reports_quality_time_cost_and_confidence() -> None:
    """One aggregate carries all five benchmark dimensions."""
    records = cost_to_green_by_task(
        [
            _attempt(1, effort="medium", end_reason="completed", board_task_id="t1", cost=2.0),
            _attempt(1, effort="medium", end_reason="failed", board_task_id="t2", cost=3.0),
            _attempt(1, effort="xhigh", end_reason="completed", board_task_id="t3", cost=9.0),
        ]
    )
    [medium, xhigh] = summarize_by_effort(records)

    assert medium.effort == "medium" and xhigh.effort == "xhigh"
    assert medium.green_rate == pytest.approx(0.5)  # quality
    assert medium.total_wall_ms == 2_000  # time
    assert medium.cost_per_green == pytest.approx(5.0)  # cost per finished package
    assert medium.green == 1 and medium.tasks == 2  # pass/fail
    assert medium.confidence == "measured"  # confidence
    # cost_per_green divides by GREENS, not tasks: failing cheaply must not win.
    assert medium.cost_per_green != medium.total_cost_usd / medium.tasks


def test_cost_per_green_is_undefined_rather_than_zero_when_nothing_finished() -> None:
    """No successes means no cost-per-success, not a free lunch."""
    stats = summarize_by_effort(
        cost_to_green_by_task([_attempt(1, effort="low", end_reason="failed")])
    )[0]

    assert stats.green == 0
    assert stats.cost_per_green is None
    assert stats.tokens_per_green is None
    assert stats.wall_ms_per_green is None


@pytest.mark.parametrize(
    ("tasks", "fully_measured", "expected"),
    [
        (0, 0, "empty"),
        (4, 4, "measured"),
        (4, 2, "partial"),
        (4, 3, "partial"),
        (4, 1, "sparse"),
        (4, 0, "sparse"),
    ],
)
@pytest.mark.acceptance_daily
def test_confidence_label_tracks_how_much_data_was_actually_measured(
    tasks: int, fully_measured: int, expected: str
) -> None:
    """Every band is exercised, so a constant-returning implementation fails."""
    stats = EffortStats(effort="medium", tasks=tasks, fully_measured_tasks=fully_measured)
    assert stats.confidence == expected


def test_summarize_run_prefers_the_usage_joined_reader() -> None:
    """The aggregate reads session-side tokens/cost, not just attempt rows.

    Falling back silently to un-joined attempt rows would drop the cost column
    entirely and report every run as free.
    """

    class _Dal:
        def __init__(self) -> None:
            self.used: list[str] = []

        def attempts_with_usage(self, run_id: str) -> list[dict[str, Any]]:
            self.used.append("with_usage")
            return [_attempt(1, effort="high", end_reason="completed", cost=4.0)]

        def attempts_for_run(self, run_id: str) -> list[dict[str, Any]]:
            self.used.append("raw")
            return [_attempt(1, effort="high", end_reason="completed", cost=0.0)]

    dal = _Dal()
    [stats] = summarize_run(dal, "run-1")

    assert dal.used == ["with_usage"]
    assert stats.total_cost_usd == pytest.approx(4.0)

    class _LegacyDal:
        def attempts_for_run(self, run_id: str) -> list[dict[str, Any]]:
            return [_attempt(1, effort="high", end_reason="completed", cost=7.0)]

    [legacy] = summarize_run(_LegacyDal(), "run-1")
    assert legacy.total_cost_usd == pytest.approx(7.0), "a legacy DAL must still summarize"


# ---------------------------------------------------------------------------
# Confidence: the statistics must be real, and must refuse to fake an estimate
# ---------------------------------------------------------------------------


@pytest.mark.acceptance_daily
def test_wilson_interval_matches_a_hand_checked_reference() -> None:
    """A published reference point, not a self-consistent tautology.

    Wilson 95% for 8/10 is approximately (0.4902, 0.9433).
    """
    low, high = lab_stats.wilson_confidence_interval(8, 10)

    assert low == pytest.approx(0.4902, abs=5e-4)
    assert high == pytest.approx(0.9433, abs=5e-4)
    assert low < 0.8 < high


def test_wilson_interval_narrows_as_evidence_accumulates() -> None:
    """More trials at the same rate must mean a tighter claim."""
    widths = []
    for trials in (10, 100, 1_000):
        low, high = lab_stats.wilson_confidence_interval(trials * 0.8, trials)
        widths.append(high - low)

    assert widths[0] > widths[1] > widths[2]
    assert widths[2] < 0.06


def test_wilson_interval_stays_inside_the_unit_range_at_the_extremes() -> None:
    """0/n and n/n must not produce an interval outside [0, 1]."""
    zero_low, zero_high = lab_stats.wilson_confidence_interval(0, 20)
    all_low, all_high = lab_stats.wilson_confidence_interval(20, 20)

    # The lower/upper bound at these extremes is computed from two formula
    # paths (center, margin) that only cancel to exactly 0.0/1.0 in exact
    # arithmetic; the last-bit result of the transcendental z-score
    # (statistics.NormalDist().inv_cdf) differs by up to 1 ULP across libm
    # implementations (e.g. glibc vs Apple libm), which can leave a
    # ~1e-17-scale residual instead of an exact cancellation. Tolerate that
    # platform-dependent float noise while still catching a real defect.
    assert zero_low == pytest.approx(0.0, abs=1e-9) and 0.0 < zero_high < 1.0
    assert all_high == pytest.approx(1.0, abs=1e-9) and 0.0 < all_low < 1.0


def test_wilson_refuses_to_invent_an_interval_from_no_trials() -> None:
    """Zero observations is an error, not a (0, 1) shrug that reads as data."""
    with pytest.raises(ValueError, match="trials must be positive"):
        lab_stats.wilson_confidence_interval(0, 0)
    with pytest.raises(ValueError, match="successes must be finite"):
        lab_stats.wilson_confidence_interval(11, 10)


@pytest.mark.acceptance_daily
def test_minimum_detectable_effect_is_the_inverse_of_power() -> None:
    """MDE and power must agree, or one of them is decorative.

    Feeding the MDE back into ``statistical_power`` must return the target
    power it was computed for.
    """
    mde = lab_stats.minimum_detectable_effect(200, baseline_rate=0.5, target_power=0.8)

    assert lab_stats.statistical_power(mde, 200, baseline_rate=0.5) == pytest.approx(0.8, abs=0.02)
    # And a bigger sample detects a smaller effect.
    assert lab_stats.minimum_detectable_effect(800, baseline_rate=0.5) < mde


def test_minimum_detectable_effect_rejects_impossible_designs() -> None:
    with pytest.raises(ValueError, match="sample_size_per_arm must be positive"):
        lab_stats.minimum_detectable_effect(0)
    with pytest.raises(ValueError, match="baseline_rate must be strictly between"):
        lab_stats.minimum_detectable_effect(100, baseline_rate=1.0)


@pytest.mark.acceptance_smoke
def test_a_single_replicate_yields_no_interval_rather_than_a_zero_width_one() -> None:
    """The exact failure mode that would let one lucky run look reproducible."""
    single = lab_stats.mean_confidence_interval([0.25])
    pair = lab_stats.mean_confidence_interval([0.25, 0.25])

    assert single.bounds is None
    assert single.stable is False
    assert single.reason == "fewer_than_two_observations"
    assert single.as_list() is None, "a JSON null, never [0.25, 0.25]"
    assert pair.stable is True and pair.as_list() is not None


def _result(arm: str, metrics: dict[str, float], replicate: int = 0) -> EvalResult:
    return EvalResult(
        experiment_id="exp-bench",
        arm=arm,
        suite_id="evs-bench",
        suite_version=1,
        split=EvalSplit.DEV,
        metrics=metrics,
        replicate=replicate,
    )


def test_scorecard_confidence_interval_needs_two_observations() -> None:
    """The scorecard layer inherits the same fail-closed rule."""
    one = [_result("challenger", {"accuracy": 0.8})]
    two = [
        _result("challenger", {"accuracy": 0.8}, replicate=0),
        _result("challenger", {"accuracy": 0.9}, replicate=1),
    ]

    assert lab_scorecard.confidence_interval_95("accuracy", one) is None
    interval = lab_scorecard.confidence_interval_95("accuracy", two)
    assert interval is not None and interval[0] < 0.85 < interval[1]


def test_signed_delta_is_direction_aware_and_absent_when_unmeasured() -> None:
    """A "lower is better" metric must not read as a regression when it improves."""
    specs = [
        MetricSpec(name="accuracy", role="primary", direction="maximize"),
        MetricSpec(name="latency_ms", role="efficiency", direction="minimize"),
    ]
    champion = {"accuracy": 0.5, "latency_ms": 900.0}
    challenger = {"accuracy": 0.7, "latency_ms": 600.0}

    assert lab_scorecard.signed_delta("accuracy", specs, champion, challenger) == pytest.approx(0.2)
    assert lab_scorecard.signed_delta("latency_ms", specs, champion, challenger) == pytest.approx(
        300.0
    ), "a latency DROP is an improvement, so the delta is positive"
    # Missing from either arm -> no delta at all, never 0.0.
    assert lab_scorecard.signed_delta("accuracy", specs, {}, challenger) is None
    assert lab_scorecard.signed_delta("unmeasured", specs, champion, challenger) is None


@pytest.mark.acceptance_daily
def test_safety_regression_is_detected_by_threshold_and_by_baseline() -> None:
    """A guardrail metric fails either by breaching its threshold or by slipping."""
    threshold_spec = [
        MetricSpec(name="unsafe_rate", role="hard_constraint", direction="minimize", threshold=0.05)
    ]
    baseline_spec = [MetricSpec(name="unsafe_rate", role="guardrail", direction="minimize")]

    assert (
        lab_scorecard.safety_regression(
            threshold_spec, {"unsafe_rate": 0.01}, {"unsafe_rate": 0.09}
        )
        is True
    )
    assert (
        lab_scorecard.safety_regression(
            threshold_spec, {"unsafe_rate": 0.01}, {"unsafe_rate": 0.02}
        )
        is False
    )
    # No threshold declared: any slip against the champion is a regression.
    assert (
        lab_scorecard.safety_regression(baseline_spec, {"unsafe_rate": 0.01}, {"unsafe_rate": 0.02})
        is True
    )
    assert (
        lab_scorecard.safety_regression(baseline_spec, {"unsafe_rate": 0.02}, {"unsafe_rate": 0.01})
        is False
    )


@pytest.mark.acceptance_daily
def test_efficiency_penalty_never_lets_a_win_offset_a_loss() -> None:
    """Utility is a regularizer, not a trade fund.

    Getting much faster must not buy the right to get more expensive.
    """
    specs = [
        MetricSpec(name="cost_usd", role="efficiency", direction="minimize"),
        MetricSpec(name="latency_ms", role="efficiency", direction="minimize"),
    ]
    champion = {"cost_usd": 1.0, "latency_ms": 1_000.0}
    challenger = {"cost_usd": 3.0, "latency_ms": 100.0}

    penalty = lab_scorecard.efficiency_penalty(specs, champion, challenger)

    assert penalty == pytest.approx(2.0), "the 900ms latency win must not cancel the $2 regression"
