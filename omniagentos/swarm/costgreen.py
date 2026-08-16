"""Cost-to-green: what a work package actually cost to finish, retries included.

The metric that answers "is xhigh worth it over medium". Cost-per-run cannot,
because it prices a single attempt and the effort tradeoff is not monotonic: a
cheap attempt that fails twice and then escalates can cost more, in tokens and
in wall-clock, than one expensive attempt that lands first time. Only totalling
the whole attempt chain per board task surfaces that.

Grouping is by the **first** effort in the chain, not by per-attempt effort.
The policy decision under evaluation is "what do we start this class of work
at" -- escalation is the system's reaction to that choice, so its cost belongs
to the choice, not to the level it escalated to. Attributing an xhigh rescue to
xhigh would make xhigh look expensive and medium look cheap, which is exactly
backwards.

Completeness is per dimension. ``usage_capture`` tags a row
``SOURCE_CLI_REPORT`` whenever cost is present, even when token fields are
absent; ``SOURCE_TOKENS_ONLY`` records exact tokens (and often wall time) with
cost unknown. A single all-or-nothing flag would either publish favourable
zeroes for missing fields or hide real tokens/wall behind an unpriced cost.

Pure functions over attempt rows, so the analysis is testable without a DB and
the same code serves the optimizer, a report, and an ad-hoc query.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from omniagentos.swarm.usage_capture import SOURCE_CLI_REPORT, SOURCE_TOKENS_ONLY

# ``swarm_attempts.end_reason`` meaning "this attempt finished the work".
GREEN_REASON = "completed"

# Sources that can carry provider-reported token counts.
_TOKEN_SOURCES = frozenset({SOURCE_CLI_REPORT, SOURCE_TOKENS_ONLY})


@dataclass
class TaskCostToGreen:
    """One board task's full attempt chain, totalled."""

    board_task_id: str
    attempts: int = 0
    retries: int = 0  # attempts beyond the first
    escalations: int = 0  # attempts that changed model or tier
    effort_path: list[str | None] = field(default_factory=list)
    reached_green: bool = False
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_ms: int = 0
    #: True only when EVERY attempt carried a parseable provider-reported cost
    #  (``SOURCE_CLI_REPORT``). Cost-confidence / ``fully_measured_tasks`` rest
    #  on this. Tokens and wall time have their own flags — a tokens-only row
    #  is cost-unmeasured without being tokens-unmeasured.
    measured: bool = False
    cost_measured: bool = False
    tokens_measured: bool = False
    wall_measured: bool = False

    @property
    def starting_effort(self) -> str | None:
        return self.effort_path[0] if self.effort_path else None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class EffortStats:
    """Aggregate across every task that STARTED at one effort level."""

    effort: str | None
    tasks: int = 0
    green: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    total_wall_ms: int = 0
    total_retries: int = 0
    total_escalations: int = 0
    #: Tasks whose *cost* chain was fully measured (drives ``confidence``).
    fully_measured_tasks: int = 0
    cost_measured_tasks: int = 0
    tokens_measured_tasks: int = 0
    wall_measured_tasks: int = 0

    @property
    def green_rate(self) -> float | None:
        # Empty denominator is unknown, not a flattering 0.0 success rate.
        return self.green / self.tasks if self.tasks else None

    def _per_green(self, total: float, measured_tasks: int) -> float | None:
        """Comparable per-green rate, or unknown when *this* total is incomplete.

        Unmeasured chains still accumulate a floor of 0.0 into the totals (see
        ``TaskCostToGreen.measured``). Publishing that floor as cost/tokens/
        wall-per-green would present unknown spend as free — the same defect
        class as treating missing cost as $0.00 against a budget cap.

        Completeness is dimension-specific: cost, tokens, and wall each gate
        their own rate so a tokens-only row can still surface tokens/wall.
        """
        if not self.green:
            return None
        if measured_tasks < self.tasks:
            return None
        return total / self.green

    @property
    def cost_per_green(self) -> float | None:
        """Spend per package actually finished — the comparable number.

        Dividing by tasks rather than greens would reward an effort level that
        fails cheaply; the point is what it costs to actually get work done.
        Incomplete cost measurement yields ``None``, never a flattering floor.
        """
        return self._per_green(self.total_cost_usd, self.cost_measured_tasks)

    @property
    def tokens_per_green(self) -> float | None:
        return self._per_green(float(self.total_tokens), self.tokens_measured_tasks)

    @property
    def wall_ms_per_green(self) -> float | None:
        return self._per_green(float(self.total_wall_ms), self.wall_measured_tasks)

    @property
    def confidence(self) -> str:
        """How much of this aggregate rests on measured rather than missing cost data."""
        if not self.tasks:
            return "empty"
        ratio = self.fully_measured_tasks / self.tasks
        if ratio == 1.0:
            return "measured"
        if ratio >= 0.5:
            return "partial"
        return "sparse"


def _parse_num(value: Any) -> float | None:
    """Parse a numeric field. ``None``/unparseable stay unknown — never 0.0.

    Explicit ``0`` / ``0.0`` is a real report ("cost nothing" / "no tokens").
    Bool is rejected because ``True``/``False`` are ``int`` subclasses.
    Non-finite values (NaN, ±Infinity) and negatives are rejected: they are not
    valid usage measurements and must not publish as favourable measured rates
    (e.g. cost_per_green of -1.0 or Infinity with confidence="measured").
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or result < 0:
        return None
    return result


def _num(row: dict[str, Any], key: str) -> float:
    """Floor for chain totals. Unknown contributes 0.0; completeness flags say so."""
    parsed = _parse_num(row.get(key))
    return 0.0 if parsed is None else parsed


def _attempt_cost_measured(row: dict[str, Any]) -> bool:
    """Cost is measured only from a CLI cost report with a parseable value.

    ``SOURCE_CLI_REPORT`` alone is not enough: the data model can carry that
    source with a missing or garbage ``cost_usd`` (see usage_capture's
    source assignment vs. row-level nulls after joins).
    """
    return (
        row.get("usage_source") == SOURCE_CLI_REPORT and _parse_num(row.get("cost_usd")) is not None
    )


def _attempt_tokens_measured(row: dict[str, Any]) -> bool:
    """Tokens are measured only when BOTH sides of the pair are parseable.

    ``usage_capture.extract`` accepts a one-sided report (input without output,
    or the reverse). ``_num`` floors the absent half to 0, so treating either
    side as complete would publish a partial total as a favourable numeric
    ``tokens_per_green``. Both fields must be present; one missing means
    unknown, not free.
    """
    if row.get("usage_source") not in _TOKEN_SOURCES:
        return False
    return (
        _parse_num(row.get("input_tokens")) is not None
        and _parse_num(row.get("output_tokens")) is not None
    )


def _attempt_wall_measured(row: dict[str, Any]) -> bool:
    """Wall time is process-timed and independent of usage source."""
    return _parse_num(row.get("wall_ms")) is not None


def task_cost_to_green(attempts: Sequence[dict[str, Any]]) -> TaskCostToGreen:
    """Total one board task's chain. ``attempts`` must be seq-ordered."""
    ordered = sorted(attempts, key=lambda row: row.get("seq") or 0)
    if not ordered:
        return TaskCostToGreen(board_task_id="")

    record = TaskCostToGreen(board_task_id=str(ordered[0].get("board_task_id") or ""))
    record.attempts = len(ordered)
    record.retries = max(0, len(ordered) - 1)
    record.effort_path = [row.get("effort") for row in ordered]
    record.reached_green = any(row.get("end_reason") == GREEN_REASON for row in ordered)

    previous: tuple[Any, Any] | None = None
    cost_all = True
    tokens_all = True
    wall_all = True
    for row in ordered:
        record.cost_usd += _num(row, "cost_usd")
        record.input_tokens += int(_num(row, "input_tokens"))
        record.output_tokens += int(_num(row, "output_tokens"))
        record.wall_ms += int(_num(row, "wall_ms"))
        if not _attempt_cost_measured(row):
            cost_all = False
        if not _attempt_tokens_measured(row):
            tokens_all = False
        if not _attempt_wall_measured(row):
            wall_all = False
        signature = (row.get("model"), row.get("tier"))
        # A rerouted retry on the same model is a retry; changing what runs it
        # is an escalation. Counting both as retries would hide the escalation
        # rate, which is the cost signal the starting effort is responsible for.
        if previous is not None and signature != previous:
            record.escalations += 1
        previous = signature

    record.cost_measured = cost_all
    record.tokens_measured = tokens_all
    record.wall_measured = wall_all
    # Historical name: "measured" means the cost figure is complete.
    record.measured = cost_all
    return record


def cost_to_green_by_task(
    attempts: Iterable[dict[str, Any]],
) -> list[TaskCostToGreen]:
    """Group a flat attempt list (e.g. ``dal.attempts_for_run``) by board task."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in attempts:
        grouped.setdefault(str(row.get("board_task_id") or ""), []).append(row)
    return [task_cost_to_green(rows) for rows in grouped.values()]


def summarize_by_effort(records: Iterable[TaskCostToGreen]) -> list[EffortStats]:
    """Aggregate tasks by the effort they STARTED at, cheapest label first."""
    buckets: dict[str | None, EffortStats] = {}
    for record in records:
        effort = record.starting_effort
        stats = buckets.setdefault(effort, EffortStats(effort=effort))
        stats.tasks += 1
        stats.green += 1 if record.reached_green else 0
        stats.total_cost_usd += record.cost_usd
        stats.total_tokens += record.total_tokens
        stats.total_wall_ms += record.wall_ms
        stats.total_retries += record.retries
        stats.total_escalations += record.escalations
        stats.fully_measured_tasks += 1 if record.measured else 0
        stats.cost_measured_tasks += 1 if record.cost_measured else 0
        stats.tokens_measured_tasks += 1 if record.tokens_measured else 0
        stats.wall_measured_tasks += 1 if record.wall_measured else 0

    order = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
    return sorted(buckets.values(), key=lambda s: (order.get(s.effort or "", 99), s.effort or ""))


def summarize_run(dal: Any, swarm_run_id: str) -> list[EffortStats]:
    """Cost-to-green by starting effort for one swarm run.

    Reads ``attempts_with_usage`` so the session-side numbers (tokens, cost,
    wall-clock) are joined onto the attempt-side ones (effort, tier); falls back
    to the raw attempt rows on a DAL that predates it.
    """
    reader = getattr(dal, "attempts_with_usage", None)
    rows = reader(swarm_run_id) if callable(reader) else dal.attempts_for_run(swarm_run_id)
    return summarize_by_effort(cost_to_green_by_task(rows))


def effort_stats_as_dict(stats: EffortStats) -> dict[str, Any]:
    """JSON-ready view of one effort bucket, including three-valued rates.

    Callers must treat ``green_rate`` / ``cost_per_green`` / ``tokens_per_green`` /
    ``wall_ms_per_green`` as three-valued (``float | None``) — never with bare
    truthiness, or unknown silently takes the false branch. Empty buckets
    (``tasks == 0``) publish ``green_rate=None``, not a counterfeit 0.0.
    """
    return {
        "effort": stats.effort,
        "tasks": stats.tasks,
        "green": stats.green,
        "green_rate": stats.green_rate,
        "total_cost_usd": stats.total_cost_usd,
        "total_tokens": stats.total_tokens,
        "total_wall_ms": stats.total_wall_ms,
        "total_retries": stats.total_retries,
        "total_escalations": stats.total_escalations,
        "cost_per_green": stats.cost_per_green,
        "tokens_per_green": stats.tokens_per_green,
        "wall_ms_per_green": stats.wall_ms_per_green,
        "confidence": stats.confidence,
        "fully_measured_tasks": stats.fully_measured_tasks,
        "cost_measured_tasks": stats.cost_measured_tasks,
        "tokens_measured_tasks": stats.tokens_measured_tasks,
        "wall_measured_tasks": stats.wall_measured_tasks,
    }


__all__ = [
    "EffortStats",
    "GREEN_REASON",
    "TaskCostToGreen",
    "cost_to_green_by_task",
    "effort_stats_as_dict",
    "summarize_by_effort",
    "summarize_run",
    "task_cost_to_green",
]
