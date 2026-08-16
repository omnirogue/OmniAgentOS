"""Corpus-level aggregation: metric distributions, taxon frequencies, and
success-vs-failure contrasts with evidence pointers back into real traces."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from statistics import mean, median

from omniagentos.tracelab.events import Outcome, Trace
from omniagentos.tracelab.metrics import TraceMetrics, compute_metrics
from omniagentos.tracelab.taxonomy import classify_trace_errors

#: Exemplar pointers kept per pattern — enough to audit, never a blob dump.
MAX_EXEMPLARS = 5

#: Metrics contrasted between outcome groups.
CONTRAST_METRICS = (
    "n_events",
    "n_tool_calls",
    "n_tool_errors",
    "error_streak_max",
    "repeat_call_max",
    "giant_results",
    "largest_result_chars",
    "verification_calls",
    "errors_recovered",
)

#: Pattern predicates used for prevalence lift (fraction of traces affected).
PATTERN_PREDICATES: dict[str, tuple[str, int]] = {
    "error_streak_ge_3": ("error_streak_max", 3),
    "repeat_call_ge_3": ("repeat_call_max", 3),
    "giant_result_present": ("giant_results", 1),
    "no_verification": ("verification_calls", 0),
}


@dataclass(slots=True)
class Exemplar:
    trace_id: str
    dataset: str
    source_path: str
    detail: str

    def to_row(self) -> dict[str, str]:
        return {
            "trace_id": self.trace_id,
            "dataset": self.dataset,
            "source_path": self.source_path,
            "detail": self.detail,
        }


@dataclass(slots=True)
class MiningResult:
    """Everything the hypothesis and report stages consume."""

    n_traces: int = 0
    n_labeled: int = 0
    outcome_counts: dict[str, int] = field(default_factory=dict)
    dataset_counts: dict[str, int] = field(default_factory=dict)
    taxon_counts: dict[str, int] = field(default_factory=dict)
    taxon_counts_by_outcome: dict[str, dict[str, int]] = field(default_factory=dict)
    metric_summary: dict[str, dict[str, float | None]] = field(default_factory=dict)
    contrasts: dict[str, dict[str, float]] = field(default_factory=dict)
    pattern_lift: dict[str, dict[str, float]] = field(default_factory=dict)
    #: pattern → dataset → prevalence stats; guards against Simpson dilution
    #: when pooling corpora with very different base rates.
    pattern_lift_by_dataset: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    exemplars: dict[str, list[Exemplar]] = field(default_factory=dict)
    per_trace: list[TraceMetrics] = field(default_factory=list)

    def add_exemplar(self, pattern: str, exemplar: Exemplar) -> None:
        bucket = self.exemplars.setdefault(pattern, [])
        if len(bucket) < MAX_EXEMPLARS:
            bucket.append(exemplar)

    def to_summary(self) -> dict[str, object]:
        return {
            "n_traces": self.n_traces,
            "n_labeled": self.n_labeled,
            "outcome_counts": self.outcome_counts,
            "dataset_counts": self.dataset_counts,
            "taxon_counts": self.taxon_counts,
            "taxon_counts_by_outcome": self.taxon_counts_by_outcome,
            "metric_summary": self.metric_summary,
            "contrasts": self.contrasts,
            "pattern_lift": self.pattern_lift,
            "pattern_lift_by_dataset": self.pattern_lift_by_dataset,
            "exemplars": {
                pattern: [e.to_row() for e in items] for pattern, items in self.exemplars.items()
            },
        }


def _metric_value(m: TraceMetrics, name: str) -> int:
    value = getattr(m, name)
    if not isinstance(value, int):
        raise TypeError(f"contrast metric {name!r} must be int, got {type(value).__name__}")
    return value


def _summarize(values: list[int]) -> dict[str, float | None]:
    # Empty sample → unknown stats, never a flattering 0.0 mean (which reads as
    # "no errors / no cost" when n=0).
    if not values:
        return {"n": 0.0, "mean": None, "median": None, "max": None}
    return {
        "n": float(len(values)),
        "mean": round(mean(values), 3),
        "median": float(median(values)),
        "max": float(max(values)),
    }


def mine_traces(traces: Iterable[Trace]) -> MiningResult:
    """Aggregate a corpus of normalized traces into a :class:`MiningResult`.

    Streams: each trace is reduced to its :class:`TraceMetrics` immediately, so
    corpus-scale runs never hold full event lists for more than one trace.
    """
    result = MiningResult()
    by_outcome: dict[str, list[TraceMetrics]] = {"success": [], "failure": []}
    by_dataset_outcome: dict[str, dict[str, list[TraceMetrics]]] = {}

    for trace in traces:
        metrics = compute_metrics(trace)
        result.per_trace.append(metrics)
        result.n_traces += 1
        result.dataset_counts[trace.dataset] = result.dataset_counts.get(trace.dataset, 0) + 1
        result.outcome_counts[trace.outcome.value] = (
            result.outcome_counts.get(trace.outcome.value, 0) + 1
        )
        if trace.outcome is not Outcome.UNKNOWN:
            result.n_labeled += 1
            by_outcome[trace.outcome.value].append(metrics)
            dataset_groups = by_dataset_outcome.setdefault(
                trace.dataset, {"success": [], "failure": []}
            )
            dataset_groups[trace.outcome.value].append(metrics)

        for taxon, count in classify_trace_errors(trace).items():
            result.taxon_counts[taxon] = result.taxon_counts.get(taxon, 0) + count
            per_outcome = result.taxon_counts_by_outcome.setdefault(trace.outcome.value, {})
            per_outcome[taxon] = per_outcome.get(taxon, 0) + count

        for pattern, (metric_name, threshold) in PATTERN_PREDICATES.items():
            if pattern == "no_verification":
                hit = metrics.verification_calls == 0 and metrics.n_tool_calls > 0
            else:
                hit = _metric_value(metrics, metric_name) >= threshold
            if hit:
                result.add_exemplar(
                    pattern,
                    Exemplar(
                        trace_id=trace.trace_id,
                        dataset=trace.dataset,
                        source_path=trace.source_path,
                        detail=f"{metric_name}={_metric_value(metrics, metric_name)}",
                    ),
                )

    for name in CONTRAST_METRICS:
        result.metric_summary[name] = _summarize([_metric_value(m, name) for m in result.per_trace])
        success_values = [_metric_value(m, name) for m in by_outcome["success"]]
        failure_values = [_metric_value(m, name) for m in by_outcome["failure"]]
        if success_values and failure_values:
            result.contrasts[name] = {
                "success_mean": round(mean(success_values), 3),
                "failure_mean": round(mean(failure_values), 3),
                "delta": round(mean(failure_values) - mean(success_values), 3),
            }

    def _prevalence(
        pattern: str, metric_name: str, threshold: int, group: list[TraceMetrics]
    ) -> float:
        if pattern == "no_verification":
            hits = sum(1 for m in group if m.verification_calls == 0 and m.n_tool_calls > 0)
        else:
            hits = sum(1 for m in group if _metric_value(m, metric_name) >= threshold)
        return round(hits / len(group), 4)

    for pattern, (metric_name, threshold) in PATTERN_PREDICATES.items():
        lift: dict[str, float] = {}
        for outcome, group in by_outcome.items():
            if group:
                lift[f"{outcome}_prevalence"] = _prevalence(pattern, metric_name, threshold, group)
        if lift:
            result.pattern_lift[pattern] = lift

        for dataset, groups in by_dataset_outcome.items():
            if not groups["success"] or not groups["failure"]:
                continue
            result.pattern_lift_by_dataset.setdefault(pattern, {})[dataset] = {
                "success_prevalence": _prevalence(
                    pattern, metric_name, threshold, groups["success"]
                ),
                "failure_prevalence": _prevalence(
                    pattern, metric_name, threshold, groups["failure"]
                ),
                "n_success": float(len(groups["success"])),
                "n_failure": float(len(groups["failure"])),
            }

    return result
