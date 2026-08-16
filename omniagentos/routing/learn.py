"""Pure, deterministic learner over ``omniagentos.routing.cascade`` traces.

WHY separate from ``cascade.py``: the cascade decides win/loss for ONE task,
right now, by objective verification. This module decides where the NEXT
task of the SAME class should START, mined from the recorded history of
wins/losses across every past cascade run -- pure arithmetic over JSONL
rows, no I/O besides reading the trace file, no adapter/network calls, and
fully unit-testable with synthetic trace dicts (no cascade run needed).

This is the RouteLLM (arXiv:2406.18665) half of the package: a lightweight,
data-driven router that gets smarter about *starting* tier choice as traces
accumulate, while ``cascade.run_cascade`` stays the FrugalGPT (arXiv:2305.05176)
half that always does the final, objective escalation.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omniagentos.routing.cascade import CascadeTier


def read_traces(trace_path: str, task_class: str, window: int = 200) -> list[dict[str, Any]]:
    """Read up to the last ``window`` trace rows for ``task_class`` from
    ``trace_path`` (JSONL written by ``cascade.run_cascade``, oldest first).

    A missing file means "no history yet for this class" -> ``[]``, not an
    error. Malformed/partial lines (e.g. a concurrent writer caught
    mid-flush) are skipped rather than raising -- a learner reading history
    must never be the thing that breaks a cascade run.
    """
    if not os.path.exists(trace_path):
        return []
    matched: list[dict[str, Any]] = []
    with open(trace_path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict) or row.get("task_class") != task_class:
                continue
            matched.append(row)
    return matched[-window:] if window > 0 else matched


def task_class_hierarchy(task_class: str) -> tuple[str, ...]:
    """Return the supported routing-class fallback chain, leaf first.

    Orchestrator classes use ``orch:<lane>:<complexity>``.  Their historical
    rows may use the older ``orch:<lane>`` shape, so the hierarchy deliberately
    retains both that lane level and the global ``orch`` level.
    """
    parts = [part for part in task_class.split(":") if part]
    if not parts:
        return ()
    if parts[0] != "orch":
        return (task_class,)
    depth = min(len(parts), 3)
    return tuple(":".join(parts[:size]) for size in range(depth, 0, -1))


def read_trace_hierarchy(
    trace_path: str, task_class: str, window: int = 200
) -> list[list[dict[str, Any]]]:
    """Read trace levels for ``task_class`` in leaf -> lane -> global order.

    Broader levels aggregate descendants as well as exact legacy rows.  Thus
    ``orch:superfast`` learns from old two-component rows and all current
    ``orch:superfast:<complexity>`` rows, while ``orch`` learns across lanes.
    """
    hierarchy = task_class_hierarchy(task_class)
    if not hierarchy or not os.path.exists(trace_path):
        return [[] for _ in hierarchy]
    levels: list[list[dict[str, Any]]] = [[] for _ in hierarchy]
    with open(trace_path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                row = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            row_class = row.get("task_class")
            if not isinstance(row_class, str):
                continue
            for index, level_class in enumerate(hierarchy):
                if row_class == level_class or row_class.startswith(level_class + ":"):
                    levels[index].append(row)
    if window > 0:
        return [rows[-window:] for rows in levels]
    return levels


def wilson_lower_bound(wins: float, n: float, z: float = 1.96) -> float:
    """Lower bound of the Wilson score confidence interval for the binomial
    proportion ``wins / n``.

    More conservative than the raw rate at small sample counts (it will not
    recommend a tier off e.g. 2 lucky wins in 2 tries) and converges toward
    the raw rate as ``n`` grows. Closed form (see e.g. Wilson 1927):

        p_hat  = wins / n
        center = p_hat + z**2 / (2n)
        margin = z * sqrt(p_hat*(1 - p_hat)/n + z**2/(4n**2))
        lower  = (center - margin) / (1 + z**2/n)

    ``n <= 0`` -> ``0.0``: no evidence means no confidence, never a basis to
    recommend a tier.
    """
    if n <= 0:
        return 0.0
    p_hat = wins / n
    z2 = z * z
    denominator = 1 + z2 / n
    center = p_hat + z2 / (2 * n)
    margin = z * math.sqrt((p_hat * (1 - p_hat) / n) + (z2 / (4 * n * n)))
    return (center - margin) / denominator


def recommend_start_tier(
    traces: list[dict[str, Any]],
    ladder: Sequence[CascadeTier],
    *,
    target_win_rate: float = 0.6,
    min_samples: int = 5,
    parent_traces: Sequence[list[dict[str, Any]]] | None = None,
    now_ts: float | None = None,
) -> int:
    """Recommend which ``ladder`` index a FUTURE task of this class should
    start at, given its recorded win/loss trace.

    Fewer than ``min_samples`` total observations for the class -> ``0``
    (FrugalGPT's cheap-first default; not enough evidence to override it).
    An empty ``ladder`` -> ``0`` (nothing to recommend).

    Otherwise, per tier ``i`` (in ladder order, cheapest first), compute
    ``wins_i`` (verified attempts recorded at that tier) over ``n_i`` (total
    attempts recorded at that tier) and take the Wilson LOWER BOUND of that
    rate. Using the lower bound rather than the raw rate means a tier is
    only recommended once its win rate is CONFIDENTLY at or above
    ``target_win_rate``, not merely observed at or above it on a small or
    lucky sample. Return the first (cheapest) tier index whose lower bound
    clears ``target_win_rate``.

    If no tier clears the bar but there IS data, fall back to the tier that
    minimizes expected chained cost, computed backward from the end of the
    ladder:

        expected_cost(last) = cost_rank(last)
        expected_cost(i)    = cost_rank(i) + fail_rate(i) * expected_cost(i + 1)

    i.e. the cost of trying tier ``i`` itself, plus -- weighted by how often
    it fails -- the expected cost of falling through to the rest of the
    ladder. ``fail_rate(i)`` uses Laplace (add-one) smoothing,
    ``(n_i - wins_i + 1) / (n_i + 2)``, so a tier with a handful of samples
    never reports an absolute 0% or 100% fail rate purely from small-sample
    luck. Returns the ``argmin`` over ``i`` of ``expected_cost(i)`` -- i.e.
    "which tier is cheapest to START at, given how the rest of the ladder
    is expected to perform from there".
    """
    if not ladder:
        return 0
    if parent_traces is not None:
        return _recommend_start_tier_hierarchical(
            [traces, *parent_traces],
            ladder,
            target_win_rate=target_win_rate,
            min_samples=min_samples,
            now_ts=time.time() if now_ts is None else now_ts,
        )
    if len(traces) < min_samples:
        return 0

    wins_by_tier: dict[str, int] = {tier.name: 0 for tier in ladder}
    n_by_tier: dict[str, int] = {tier.name: 0 for tier in ladder}
    for row in traces:
        tier_name = row.get("tier_name")
        if tier_name not in n_by_tier:
            continue
        n_by_tier[tier_name] += 1
        if row.get("verified"):
            wins_by_tier[tier_name] += 1

    for index, tier in enumerate(ladder):
        n = n_by_tier[tier.name]
        if n == 0:
            continue
        lower_bound = wilson_lower_bound(wins_by_tier[tier.name], n, z=1.96)
        if lower_bound >= target_win_rate:
            return index

    fail_rates = [
        (n_by_tier[tier.name] - wins_by_tier[tier.name] + 1) / (n_by_tier[tier.name] + 2)
        for tier in ladder
    ]
    expected_costs = [0.0] * len(ladder)
    expected_costs[-1] = ladder[-1].cost_rank
    for index in range(len(ladder) - 2, -1, -1):
        expected_costs[index] = (
            ladder[index].cost_rank + fail_rates[index] * expected_costs[index + 1]
        )

    return min(range(len(ladder)), key=lambda i: expected_costs[i])


def _recommend_start_tier_hierarchical(
    trace_levels: Sequence[list[dict[str, Any]]],
    ladder: Sequence[CascadeTier],
    *,
    target_win_rate: float,
    min_samples: int,
    now_ts: float,
) -> int:
    """Wilson-gated recommendation with decayed hierarchical fallback.

    The first level with enough effective samples wins the fallback decision
    (fine class, then lane, then global).  Its per-tier observations are
    Beta-Binomial blended toward each broader parent using
    ``(wins + k*parent_rate)/(n+k)``.  The confidence gate remains the same
    95% Wilson lower bound; shrinkage supplies the estimated wins, never a
    weaker acceptance threshold.
    """
    if not ladder or not trace_levels:
        return 0
    tier_names = [tier.name for tier in ladder]
    counts = [_decayed_trace_counts(level, tier_names, now_ts=now_ts) for level in trace_levels]
    threshold = max(float(min_samples), 0.0)
    selected = next(
        (
            index
            for index, level_counts in enumerate(counts)
            if sum(n for _, n in level_counts.values()) >= threshold
        ),
        None,
    )
    if selected is None:
        return 0

    selected_counts = counts[selected:]
    rates = hierarchical_tier_rates(selected_counts, tier_names)
    evidence = selected_counts[0]
    for index, tier in enumerate(ladder):
        _, n = evidence[tier.name]
        if n <= 0:
            continue
        estimated_wins = rates[tier.name] * n
        if wilson_lower_bound(estimated_wins, n, z=1.96) >= target_win_rate:
            return index

    fail_rates = [1.0 - rates[tier.name] for tier in ladder]
    expected_costs = [0.0] * len(ladder)
    expected_costs[-1] = ladder[-1].cost_rank
    for index in range(len(ladder) - 2, -1, -1):
        expected_costs[index] = (
            ladder[index].cost_rank + fail_rates[index] * expected_costs[index + 1]
        )
    return min(range(len(ladder)), key=lambda i: expected_costs[i])


def _decayed_trace_counts(
    traces: Sequence[dict[str, Any]],
    tier_names: Sequence[str],
    *,
    now_ts: float,
) -> dict[str, tuple[float, float]]:
    samples: list[dict[str, Any]] = []
    for row in traces:
        samples.append(
            {
                "tier_name": row.get("tier_name"),
                "win": bool(row.get("verified")),
                "ts": row.get("ts"),
            }
        )
    return decayed_tier_counts(samples, tier_names, now_ts=now_ts)


# ---------------------------------------------------------------------------
# Beta-Binomial hierarchical shrinkage (WP5b swarm start-tier learner).
#
# ADDITIVE to the Wilson-bound path above (which stays byte-identical -- plan
# A7: do NOT weaken the bound to force the confident path). The swarm router
# has far finer task classes (``swarm:<complexity>``) than the cascade
# learner, so waiting for ``min_samples`` per class starves it. Shrinkage
# produces sane estimates from sample #1 by blending each level's observed
# rate toward its parent:
#
#     rate = (wins + k * parent_rate) / (n + k)
#
# chained leaf <- parent <- ... <- global prior, with a 7-14 day linear time
# decay on samples so stale history stops steering routing. Pure arithmetic,
# no I/O -- callers supply the (already decayed) counts.
# ---------------------------------------------------------------------------

DECAY_FULL_DAYS = 7.0
DECAY_ZERO_DAYS = 14.0
SHRINKAGE_K = 5.0
GLOBAL_PRIOR_RATE = 0.5


def decay_weight(
    age_days: float, *, full_days: float = DECAY_FULL_DAYS, zero_days: float = DECAY_ZERO_DAYS
) -> float:
    """Linear 7-14 day decay: full weight up to ``full_days``, linearly down
    to zero at ``zero_days``, zero after. Negative ages clamp to full weight
    (clock skew must not un-count a fresh sample)."""
    if zero_days <= full_days:
        return 1.0 if age_days <= full_days else 0.0
    if age_days <= full_days:
        return 1.0
    if age_days >= zero_days:
        return 0.0
    return (zero_days - age_days) / (zero_days - full_days)


def decayed_tier_counts(
    samples: Sequence[dict[str, Any]],
    tier_names: Sequence[str],
    *,
    now_ts: float,
    full_days: float = DECAY_FULL_DAYS,
    zero_days: float = DECAY_ZERO_DAYS,
) -> dict[str, tuple[float, float]]:
    """Time-decayed ``{tier_name: (wins, n)}`` over win/loss samples.

    Each sample is ``{"tier_name": str, "win": bool, "ts": float}`` (``ts`` =
    epoch seconds of the outcome). Samples for unknown tiers or without a
    parseable timestamp are skipped -- a learner reading history must never
    be the thing that breaks routing."""
    counts: dict[str, tuple[float, float]] = {name: (0.0, 0.0) for name in tier_names}
    for sample in samples:
        tier_name = sample.get("tier_name")
        if tier_name not in counts:
            continue
        try:
            age_days = (now_ts - float(sample["ts"])) / 86400.0
        except (KeyError, TypeError, ValueError):
            continue
        weight = decay_weight(age_days, full_days=full_days, zero_days=zero_days)
        if weight <= 0.0:
            continue
        wins, n = counts[tier_name]
        counts[tier_name] = (wins + (weight if sample.get("win") else 0.0), n + weight)
    return counts


def beta_binomial_rate(wins: float, n: float, parent_rate: float, k: float = SHRINKAGE_K) -> float:
    """Shrunk win-rate estimate ``(wins + k*parent_rate) / (n + k)``.

    ``k`` is the pseudo-sample count of the parent prior: at ``n == 0`` the
    estimate IS the parent rate; as ``n`` grows the observed rate dominates
    and the estimate converges to ``wins / n``. ``k <= 0`` degrades to the
    raw rate (0-division-safe)."""
    if k <= 0:
        return (wins / n) if n > 0 else parent_rate
    return (wins + k * parent_rate) / (n + k)


def hierarchical_tier_rates(
    level_counts: Sequence[dict[str, tuple[float, float]]],
    tier_names: Sequence[str],
    *,
    k: float = SHRINKAGE_K,
    global_prior: float = GLOBAL_PRIOR_RATE,
) -> dict[str, float]:
    """Per-tier shrunk win rates over a class hierarchy, leaf level FIRST.

    ``level_counts`` is ordered leaf -> broader (e.g. ``swarm:<complexity>``
    then all-swarm); the chain seeds from ``global_prior`` at the broad end
    and shrinks each level toward its parent's estimate, so a leaf with no
    samples inherits its parent's rate and a leaf with many samples speaks
    for itself."""
    rates: dict[str, float] = {}
    for tier_name in tier_names:
        rate = global_prior
        for counts in reversed(level_counts):
            wins, n = counts.get(tier_name, (0.0, 0.0))
            rate = beta_binomial_rate(wins, n, rate, k)
        rates[tier_name] = rate
    return rates


def recommend_start_tier_shrunk(
    level_counts: Sequence[dict[str, tuple[float, float]]],
    ladder: Sequence[CascadeTier],
    *,
    target_win_rate: float = 0.6,
    k: float = SHRINKAGE_K,
    global_prior: float = GLOBAL_PRIOR_RATE,
) -> int:
    """``recommend_start_tier``'s decision rule over Beta-Binomial rates.

    Same two-step contract as the Wilson-bound learner, with shrunk rates in
    place of small-sample bounds: return the first (cheapest) ladder index
    whose shrunk win rate clears ``target_win_rate``; when none clears, fall
    back to the expected-chained-cost argmin with ``fail_rate = 1 - rate``
    (shrinkage already smooths away absolute 0%/100% rates, so no extra
    Laplace term is needed). Empty ladder -> 0."""
    if not ladder:
        return 0
    rates = hierarchical_tier_rates(
        level_counts, [tier.name for tier in ladder], k=k, global_prior=global_prior
    )
    for index, tier in enumerate(ladder):
        if rates[tier.name] >= target_win_rate:
            return index
    fail_rates = [1.0 - rates[tier.name] for tier in ladder]
    expected_costs = [0.0] * len(ladder)
    expected_costs[-1] = ladder[-1].cost_rank
    for index in range(len(ladder) - 2, -1, -1):
        expected_costs[index] = (
            ladder[index].cost_rank + fail_rates[index] * expected_costs[index + 1]
        )
    return min(range(len(ladder)), key=lambda i: expected_costs[i])


__all__ = [
    "DECAY_FULL_DAYS",
    "DECAY_ZERO_DAYS",
    "GLOBAL_PRIOR_RATE",
    "SHRINKAGE_K",
    "beta_binomial_rate",
    "decay_weight",
    "decayed_tier_counts",
    "hierarchical_tier_rates",
    "read_trace_hierarchy",
    "read_traces",
    "recommend_start_tier",
    "recommend_start_tier_shrunk",
    "task_class_hierarchy",
    "wilson_lower_bound",
]
