"""Cost-to-green: total spend per work package, retries and escalations included.

The metric exists because cost-per-run answers the wrong question. These tests
pin the three ways a naive version would mislead: attributing an escalation's
cost to the level it escalated TO, dividing spend by attempts instead of by
packages actually finished, and presenting a partially-reported total as if it
were measured.
"""

from __future__ import annotations

from typing import Any

from omniagentos.swarm.costgreen import (
    EffortStats,
    cost_to_green_by_task,
    effort_stats_as_dict,
    summarize_by_effort,
    task_cost_to_green,
)
from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT, SOURCE_NONE, SOURCE_TOKENS_ONLY


def _attempt(
    seq: int,
    *,
    task: str = "task-1",
    effort: str = "medium",
    model: str = "sonnet",
    tier: str | None = None,
    end_reason: str = "completed",
    cost: float | None = 1.0,
    tokens: int | None = 1000,
    wall_ms: int | None = 10_000,
    source: str = SOURCE_CLI_REPORT,
) -> dict[str, Any]:
    return {
        "seq": seq,
        "board_task_id": task,
        "effort": effort,
        "model": model,
        "tier": tier,
        "end_reason": end_reason,
        "cost_usd": cost,
        "input_tokens": tokens,
        "output_tokens": tokens,
        "wall_ms": wall_ms,
        "usage_source": source,
    }


def test_single_clean_attempt_has_no_retries_or_escalations() -> None:
    record = task_cost_to_green([_attempt(0)])

    assert record.attempts == 1
    assert record.retries == 0
    assert record.escalations == 0
    assert record.reached_green is True
    assert record.total_tokens == 2000
    assert record.measured is True
    assert record.cost_measured is True
    assert record.tokens_measured is True
    assert record.wall_measured is True


def test_chain_totals_include_the_failures_that_preceded_success() -> None:
    """A failed attempt that cost real tokens must not vanish from the total."""
    record = task_cost_to_green(
        [
            _attempt(0, end_reason="crashed", cost=1.0),
            _attempt(1, end_reason="review_denied", cost=1.0),
            _attempt(2, end_reason="completed", cost=2.0),
        ]
    )

    assert record.attempts == 3
    assert record.retries == 2
    assert record.reached_green is True
    assert record.cost_usd == 4.0  # not just the winning attempt


def test_changing_the_model_counts_as_an_escalation_not_just_a_retry() -> None:
    record = task_cost_to_green(
        [
            _attempt(0, model="sonnet", end_reason="crashed"),
            _attempt(1, model="sonnet", end_reason="crashed"),  # plain retry
            _attempt(2, model="opus", end_reason="completed"),  # escalation
        ]
    )

    assert record.retries == 2
    assert record.escalations == 1


def test_unfinished_chain_is_not_green() -> None:
    record = task_cost_to_green(
        [_attempt(0, end_reason="crashed"), _attempt(1, end_reason="budget")]
    )

    assert record.reached_green is False
    assert record.cost_usd == 2.0  # still cost real money


def test_attempts_are_ordered_by_seq_not_input_order() -> None:
    record = task_cost_to_green([_attempt(2, effort="xhigh"), _attempt(0, effort="medium")])

    assert record.starting_effort == "medium"


# --------------------------------------------------- the three ways to mislead


def test_escalation_cost_is_charged_to_the_starting_effort() -> None:
    """The policy decision is where to START. If an xhigh rescue were charged to
    xhigh, the level that caused the rescue would look cheap and the level that
    fixed it would look expensive — precisely inverting the guidance."""
    records = [
        task_cost_to_green(
            [
                _attempt(0, effort="medium", model="sonnet", end_reason="crashed", cost=1.0),
                _attempt(1, effort="xhigh", model="opus", end_reason="completed", cost=5.0),
            ]
        )
    ]

    stats = summarize_by_effort(records)

    assert len(stats) == 1
    assert stats[0].effort == "medium"  # charged to the choice, not the rescue
    assert stats[0].total_cost_usd == 6.0
    assert stats[0].total_escalations == 1


def test_cost_per_green_does_not_reward_failing_cheaply() -> None:
    """Dividing by tasks would make a level that gives up cheaply look best."""
    quitter = task_cost_to_green([_attempt(0, effort="low", end_reason="budget", cost=1.0)])
    finisher = task_cost_to_green([_attempt(0, effort="high", end_reason="completed", cost=4.0)])

    by_effort = {s.effort: s for s in summarize_by_effort([quitter, finisher])}

    assert by_effort["low"].cost_per_green is None  # never finished anything
    assert by_effort["low"].green_rate == 0.0
    assert by_effort["high"].cost_per_green == 4.0


def test_a_partially_reported_chain_is_flagged_not_silently_totalled() -> None:
    record = task_cost_to_green(
        [
            _attempt(0, end_reason="crashed", source=SOURCE_NONE, cost=0.0, tokens=0),
            _attempt(1, end_reason="completed", source=SOURCE_CLI_REPORT),
        ]
    )

    assert record.measured is False  # the total is a floor, not a figure
    assert record.reached_green is True


def test_confidence_reflects_how_much_of_the_aggregate_is_measured() -> None:
    measured = task_cost_to_green([_attempt(0, effort="high")])
    unmeasured = task_cost_to_green([_attempt(0, effort="high", source=SOURCE_NONE)])

    assert summarize_by_effort([measured])[0].confidence == "measured"
    assert summarize_by_effort([measured, unmeasured])[0].confidence == "partial"
    assert summarize_by_effort([unmeasured])[0].confidence == "sparse"
    assert summarize_by_effort([]) == []


def test_cost_per_green_is_unknown_when_any_chain_is_unmeasured() -> None:
    """Unknown cost must not present as a favourable $0.00-per-green figure.

    Tokens-only / unreported attempts total as a floor of 0.0. Publishing that
    floor through ``cost_per_green`` is the same defect class as recording
    unknown spend as 0.0 against a budget cap: the comparator looks cheap and
    the optimizer can learn from a lie.

    Counterfeits this catches:
    - return 0.0 on sparse data (falsy, looks free) — must be ``is None``
    - return None always (including fully measured) — measured path still numbers
    - only blank when fully_measured_tasks == 0 — partial mix still unknown
    """
    unmeasured_green = task_cost_to_green(
        [
            _attempt(
                0,
                effort="medium",
                end_reason="completed",
                source=SOURCE_TOKENS_ONLY,
                cost=None,
                tokens=17_464,
            )
        ]
    )
    sparse = summarize_by_effort([unmeasured_green])[0]
    assert sparse.green == 1
    assert sparse.confidence == "sparse"
    assert sparse.cost_per_green is None
    # Tokens/wall ARE measured on a tokens-only row — do not hide them behind cost.
    assert sparse.tokens_per_green == 34_928.0
    assert sparse.wall_ms_per_green == 10_000.0

    measured_green = task_cost_to_green(
        [_attempt(0, effort="medium", end_reason="completed", cost=5.0)]
    )
    partial = summarize_by_effort([unmeasured_green, measured_green])[0]
    assert partial.green == 2
    assert partial.confidence == "partial"
    assert partial.total_cost_usd == 5.0  # floor still totals; just not comparable
    assert partial.cost_per_green is None  # not 2.5 (= 5/2) pretending completeness

    full = summarize_by_effort([measured_green])[0]
    assert full.confidence == "measured"
    assert full.cost_per_green == 5.0
    assert full.tokens_per_green == 2000.0
    assert full.wall_ms_per_green == 10_000.0


def test_absent_or_unparseable_values_inside_cli_report_are_unknown_not_zero() -> None:
    """SOURCE_CLI_REPORT alone must not launder missing fields into $0 / 0 tokens.

    usage_capture assigns SOURCE_CLI_REPORT whenever cost is present, even when
    token fields are absent. The completeness predicate must inspect values,
    not just the source tag — otherwise unknown renders as favourable.

    Counterfeits this catches:
    - measured=True from source alone with cost_usd="?" → cost_per_green 0.0
    - measured=True with tokens/wall keys missing → tokens/wall per-green 0.0
    - one shared fully_measured flag blanking tokens when only cost is bad
    """
    unparseable_cost = task_cost_to_green(
        [
            {
                "seq": 0,
                "board_task_id": "t-cost",
                "effort": "medium",
                "model": "sonnet",
                "end_reason": "completed",
                "cost_usd": "not-a-number",  # present but unparseable
                "input_tokens": 100,
                "output_tokens": 20,
                "wall_ms": 1_000,
                "usage_source": SOURCE_CLI_REPORT,
            }
        ]
    )
    cost_stats = summarize_by_effort([unparseable_cost])[0]
    assert unparseable_cost.measured is False
    assert unparseable_cost.cost_measured is False
    assert unparseable_cost.tokens_measured is True
    assert unparseable_cost.wall_measured is True
    assert cost_stats.cost_per_green is None  # not 0.0
    assert cost_stats.tokens_per_green == 120.0
    assert cost_stats.wall_ms_per_green == 1_000.0

    absent_tokens_wall = task_cost_to_green(
        [
            {
                "seq": 0,
                "board_task_id": "t-tokens",
                "effort": "medium",
                "model": "sonnet",
                "end_reason": "completed",
                "cost_usd": 3.0,
                # input_tokens / output_tokens / wall_ms keys absent
                "usage_source": SOURCE_CLI_REPORT,
            }
        ]
    )
    tok_stats = summarize_by_effort([absent_tokens_wall])[0]
    assert absent_tokens_wall.measured is True  # cost is complete
    assert absent_tokens_wall.cost_measured is True
    assert absent_tokens_wall.tokens_measured is False
    assert absent_tokens_wall.wall_measured is False
    assert tok_stats.cost_per_green == 3.0
    assert tok_stats.tokens_per_green is None  # not 0.0
    assert tok_stats.wall_ms_per_green is None  # not 0.0


def test_tokens_only_row_surfaces_tokens_and_wall_not_cost() -> None:
    """SOURCE_TOKENS_ONLY records exact tokens/wall; only cost is unknown.

    A single fully_measured_tasks proxy would hide real tokens and wall behind
    the unpriced cost — conservative, but semantically wrong.
    """
    record = task_cost_to_green(
        [
            _attempt(
                0,
                end_reason="completed",
                source=SOURCE_TOKENS_ONLY,
                cost=None,
                tokens=60,
                wall_ms=1_000,
            )
        ]
    )
    stats = summarize_by_effort([record])[0]

    assert record.measured is False
    assert record.cost_measured is False
    assert record.tokens_measured is True
    assert record.wall_measured is True
    assert record.total_tokens == 120
    assert record.wall_ms == 1_000
    assert stats.cost_per_green is None
    assert stats.tokens_per_green == 120.0
    assert stats.wall_ms_per_green == 1_000.0
    assert stats.confidence == "sparse"


# ------------------------------------------------------------------ grouping


def test_grouping_splits_by_board_task() -> None:
    rows = [
        _attempt(0, task="a", effort="medium"),
        _attempt(1, task="a", effort="medium"),
        _attempt(0, task="b", effort="xhigh"),
    ]

    records = {r.board_task_id: r for r in cost_to_green_by_task(rows)}

    assert records["a"].attempts == 2
    assert records["b"].attempts == 1


def test_effort_levels_sort_cheapest_first() -> None:
    records = [
        task_cost_to_green([_attempt(0, task="c", effort="xhigh")]),
        task_cost_to_green([_attempt(0, task="a", effort="low")]),
        task_cost_to_green([_attempt(0, task="b", effort="high")]),
    ]

    assert [s.effort for s in summarize_by_effort(records)] == ["low", "high", "xhigh"]


def test_missing_effort_does_not_crash_the_aggregate() -> None:
    """Rows written before migration 049 carry NULL effort and must still total."""
    record = task_cost_to_green([{"seq": 0, "board_task_id": "t", "end_reason": "completed"}])

    stats = summarize_by_effort([record])

    assert record.starting_effort is None
    assert stats[0].effort is None
    assert stats[0].tasks == 1
    # No parseable cost/tokens/wall → every per-green rate is unknown, not 0.0.
    assert stats[0].cost_per_green is None
    assert stats[0].tokens_per_green is None
    assert stats[0].wall_ms_per_green is None


def test_empty_chain_is_inert() -> None:
    record = task_cost_to_green([])

    assert record.attempts == 0
    assert record.reached_green is False


def test_one_sided_token_report_is_unknown_not_a_partial_total() -> None:
    """A single token field must not publish as a complete tokens_per_green.

    usage_capture.extract permits input_tokens without output_tokens (or the
    reverse). The absent half floors to 0 in chain totals; treating either
    side as complete would present an incomplete total as a favourable rate.

    Counterfeits this catches:
    - tokens_measured=True when only input_tokens is set → tokens_per_green=100
    - tokens_measured=True when only output_tokens is set → same
    - both present still measures (not always-None)
    """
    input_only = task_cost_to_green(
        [
            {
                "seq": 0,
                "board_task_id": "t-in",
                "effort": "medium",
                "model": "sonnet",
                "end_reason": "completed",
                "cost_usd": None,
                "input_tokens": 100,
                "output_tokens": None,
                "wall_ms": 1_000,
                "usage_source": SOURCE_TOKENS_ONLY,
            }
        ]
    )
    input_stats = summarize_by_effort([input_only])[0]
    assert input_only.tokens_measured is False
    assert input_only.total_tokens == 100  # floor still totals the known half
    assert input_stats.tokens_per_green is None  # not 100.0 pretending complete

    output_only = task_cost_to_green(
        [
            {
                "seq": 0,
                "board_task_id": "t-out",
                "effort": "medium",
                "model": "sonnet",
                "end_reason": "completed",
                "cost_usd": None,
                "input_tokens": None,
                "output_tokens": 50,
                "wall_ms": 1_000,
                "usage_source": SOURCE_TOKENS_ONLY,
            }
        ]
    )
    output_stats = summarize_by_effort([output_only])[0]
    assert output_only.tokens_measured is False
    assert output_stats.tokens_per_green is None  # not 50.0

    both = task_cost_to_green(
        [
            _attempt(
                0,
                source=SOURCE_TOKENS_ONLY,
                cost=None,
                tokens=40,
                wall_ms=500,
            )
        ]
    )
    both_stats = summarize_by_effort([both])[0]
    assert both.tokens_measured is True
    assert both_stats.tokens_per_green == 80.0


def test_bool_and_nan_are_unparseable_not_measured() -> None:
    """Bool/NaN must not launder into measured zero or non-finite rates.

    Counterfeits this catches:
    - isinstance(True, int) accepted → cost_measured=True, cost_usd floor 1.0
    - float('nan') accepted → cost_per_green becomes NaN (JSON-hostile, not a rate)
    """
    bool_row = task_cost_to_green(
        [
            {
                "seq": 0,
                "board_task_id": "t-bool",
                "effort": "medium",
                "model": "sonnet",
                "end_reason": "completed",
                "cost_usd": True,  # int subclass — must not count as $1
                "input_tokens": False,
                "output_tokens": False,
                "wall_ms": False,
                "usage_source": SOURCE_CLI_REPORT,
            }
        ]
    )
    assert bool_row.cost_measured is False
    assert bool_row.tokens_measured is False
    assert bool_row.wall_measured is False
    assert bool_row.cost_usd == 0.0  # unknown floors; completeness says so
    bool_stats = summarize_by_effort([bool_row])[0]
    assert bool_stats.cost_per_green is None
    assert bool_stats.tokens_per_green is None
    assert bool_stats.wall_ms_per_green is None

    nan_row = task_cost_to_green(
        [
            {
                "seq": 0,
                "board_task_id": "t-nan",
                "effort": "medium",
                "model": "sonnet",
                "end_reason": "completed",
                "cost_usd": float("nan"),
                "input_tokens": 10,
                "output_tokens": 10,
                "wall_ms": 100,
                "usage_source": SOURCE_CLI_REPORT,
            }
        ]
    )
    assert nan_row.cost_measured is False
    nan_stats = summarize_by_effort([nan_row])[0]
    assert nan_stats.cost_per_green is None  # not NaN
    # tokens/wall still complete on this row
    assert nan_stats.tokens_per_green == 20.0
    assert nan_stats.wall_ms_per_green == 100.0


def test_infinity_and_negative_usage_are_unparseable_not_measured() -> None:
    """±Inf and negative cost/tokens/wall must not certify as measured rates.

    Counterfeits this catches (the residual hole after bool/NaN-only rejection):
    - float('inf') accepted → cost_measured=True, cost_per_green=Infinity
    - float('-inf') accepted → cost_measured=True, cost_per_green=-Infinity
    - cost_usd=-1.0 accepted → cost_measured=True, cost_per_green=-1.0 (favourable)
    - negative tokens/wall accepted → tokens_per_green / wall_ms_per_green negative
    """
    for bad_cost in (float("inf"), float("-inf"), -1.0):
        row = task_cost_to_green(
            [
                {
                    "seq": 0,
                    "board_task_id": f"t-bad-{bad_cost}",
                    "effort": "medium",
                    "model": "sonnet",
                    "end_reason": "completed",
                    "cost_usd": bad_cost,
                    "input_tokens": 10,
                    "output_tokens": 10,
                    "wall_ms": 100,
                    "usage_source": SOURCE_CLI_REPORT,
                }
            ]
        )
        assert row.cost_measured is False, f"cost={bad_cost!r} must not measure"
        assert row.cost_usd == 0.0  # unknown floors; completeness says so
        stats = summarize_by_effort([row])[0]
        assert stats.cost_per_green is None, f"cost={bad_cost!r} must not publish a rate"
        assert stats.cost_measured_tasks == 0
        assert stats.confidence == "sparse"  # cost incomplete → never "measured"
        # Tokens/wall still complete when only cost is malformed.
        assert stats.tokens_per_green == 20.0
        assert stats.wall_ms_per_green == 100.0
        payload = effort_stats_as_dict(stats)
        assert payload["cost_per_green"] is None
        assert payload["total_cost_usd"] == 0.0
        assert payload["confidence"] == "sparse"

    neg_tokens_wall = task_cost_to_green(
        [
            {
                "seq": 0,
                "board_task_id": "t-neg-tw",
                "effort": "medium",
                "model": "sonnet",
                "end_reason": "completed",
                "cost_usd": 1.0,
                "input_tokens": -10,
                "output_tokens": -10,
                "wall_ms": -5,
                "usage_source": SOURCE_CLI_REPORT,
            }
        ]
    )
    assert neg_tokens_wall.cost_measured is True
    assert neg_tokens_wall.tokens_measured is False
    assert neg_tokens_wall.wall_measured is False
    neg_stats = summarize_by_effort([neg_tokens_wall])[0]
    assert neg_stats.cost_per_green == 1.0
    assert neg_stats.tokens_per_green is None  # not -20.0 / favourable free
    assert neg_stats.wall_ms_per_green is None  # not -5.0
    payload = effort_stats_as_dict(neg_stats)
    assert payload["tokens_per_green"] is None
    assert payload["wall_ms_per_green"] is None


def test_empty_effort_bucket_green_rate_is_unknown_not_zero() -> None:
    """tasks==0 must not serialize green_rate as 0.0 (empty-denominator counterfeit)."""
    empty = EffortStats(effort="medium")
    assert empty.tasks == 0
    assert empty.green_rate is None
    assert empty.confidence == "empty"
    payload = effort_stats_as_dict(empty)
    assert payload["green_rate"] is None
    assert payload["confidence"] == "empty"
