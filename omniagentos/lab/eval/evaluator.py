"""ProtectedEvaluator — composes ``ProtectedGrader`` (deterministic) with
blind judging. Given ONLY candidate outputs; ``expected`` never enters this
class's address space (it stays inside ``ProtectedGrader``, which this class
holds but never asks for anything but scores). (blueprint Section 11.7)
"""

from __future__ import annotations

import math
import os
from collections import defaultdict
from typing import Any, Protocol, TypedDict

from omniagentos.lab.contracts import (
    EvalResult,
    EvalSplit,
    JudgeRecord,
    Scorecard,
    promotion_utility,
)
from omniagentos.lab.db import LabStore
from omniagentos.lab.eval.blind import BlindPair, build_blind_pairs
from omniagentos.lab.eval.grader import ProtectedGrader
from omniagentos.lab.eval.scorecard import (
    aggregate_replicates,
    confidence_interval_95,
    eval_result_from_row,
    latest,
    parse_metric_specs,
    primary_metric_name,
    regularizer_delta,
    safety_regression,
    signed_delta,
)
from omniagentos.lab.eval.scorecard import (
    efficiency_penalty as _efficiency_penalty,
)

ARMS: tuple[str, str] = ("champion", "challenger")
RUBRIC_VALIDITY_ENV = "OMNIAGENTOS_RUBRIC_VALIDITY_MODE"
RUBRIC_VALIDITY_MODES = frozenset({"off", "shadow", "enforce"})
LENGTH_CORRELATION_LIMIT = 0.3


class JudgeView(TypedDict):
    """Exactly what a judge is shown for one case: candidate-safe
    ``input``/``rubric`` (never ``expected``) plus the blinded ``output`` —
    no ``arm``, no candidate/agent/lab identity, no prior score."""

    input: dict[str, Any]
    rubric: str
    output: dict[str, Any]


class JudgeFn(Protocol):
    """Pluggable judge scorer. Sees ONLY a ``JudgeView`` — never ``arm``,
    candidate identity, or ``expected``. Real campaigns (L04/L05, outside
    this package's ownership) inject a model-backed judge that calls out to
    an H1 harness/adapter; the default below is a deterministic, model-free
    stand-in so L02 is fully unit-testable offline."""

    def __call__(self, judge_lineage: str, view: JudgeView) -> tuple[float, str]: ...


def offline_heuristic_judge(judge_lineage: str, view: JudgeView) -> tuple[float, str]:
    """Deterministic default ``judge_fn`` (dry-run/tests): scores 1.0 for any
    non-empty output text, else 0.0. Exercises the full blind pipeline
    without a live model call; NOT a quality judge — real campaigns must
    inject a model-backed ``judge_fn``."""
    text = str(view["output"].get("text", ""))
    if text.strip():
        return 1.0, f"[{judge_lineage}] offline heuristic judge"
    return 0.0, f"[{judge_lineage}] offline heuristic judge: empty output"


def _rubric_validity_mode() -> str:
    mode = os.getenv(RUBRIC_VALIDITY_ENV, "off").strip().lower()
    if mode not in RUBRIC_VALIDITY_MODES:
        return "off"
    return mode


def _pearson_correlation(xs: list[float], ys: list[float]) -> float | None:
    """Return Pearson's r, or None when the sample has no usable variance."""
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    x_deviations = [value - mean_x for value in xs]
    y_deviations = [value - mean_y for value in ys]
    denominator = math.sqrt(
        sum(value**2 for value in x_deviations)
        * sum(value**2 for value in y_deviations)
    )
    if denominator == 0.0:
        return None
    return sum(x * y for x, y in zip(x_deviations, y_deviations, strict=True)) / denominator


class ProtectedEvaluator:
    """Composes ``ProtectedGrader`` (deterministic) + blind judging. Given
    ONLY outputs."""

    def __init__(self, store: LabStore, grader: ProtectedGrader) -> None:
        self._store = store
        self._grader = grader
        # blind_token -> (case_id, arm). Populated by `make_blind_pairs`,
        # consulted by `judge_blind` AFTER judging (HD-015) — never exposed.
        self._blind_tokens: dict[str, tuple[str, str]] = {}

    def run_deterministic(
        self, suite_id: str, split: str, arm: str, outputs: dict[str, dict[str, Any]]
    ) -> EvalResult:
        """Delegates to the grader, then fills in ``suite_version`` from the
        (candidate-safe) suite record — the one piece of context the grader
        itself cannot resolve (it holds no store handle at all). Does NOT
        persist: the caller attaches ``experiment_id`` and calls
        ``LabStore.record_eval_result`` once it has experiment context."""
        result = self._grader.score_outputs(suite_id, split, arm, outputs)
        suite = self._store.get_eval_suite(suite_id)
        if suite is not None:
            result = result.model_copy(update={"suite_version": int(suite["version"])})
        return result

    def make_blind_pairs(
        self,
        case_outputs: dict[str, dict[str, dict[str, Any]]],
        *,
        seed: int | None = None,
    ) -> tuple[list[BlindPair], int]:
        """Build the randomized, blind-tokenized pairs a judge may see from
        ``{case_id: {arm: output}}``. The arm<->blind_token map is retained
        INSIDE this evaluator instance (never returned) until ``judge_blind``
        resolves it after scoring."""
        pairs, token_map, used_seed = build_blind_pairs(case_outputs, seed=seed)
        self._blind_tokens.update(token_map)
        return pairs, used_seed

    def judge_blind(
        self,
        suite_id: str,
        split: str,
        pairs: list[BlindPair],
        judges: list[str],
        *,
        judge_fn: JudgeFn = offline_heuristic_judge,
    ) -> list[JudgeRecord]:
        """Score every ``(judge, pair)`` combination. ``judge_fn`` sees ONLY
        a ``JudgeView`` (candidate-safe ``input``/``rubric`` + the blinded
        ``output``); ``arm`` is resolved from the internal token map and
        written to the returned ``JudgeRecord`` AFTER judging — never handed
        to ``judge_fn`` (HD-015)."""
        cases = {case.id: case for case in self._store.candidate_cases(suite_id, split)}
        records: list[JudgeRecord] = []
        output_lengths: dict[str, int] = {}
        for pair in pairs:
            case = cases.get(pair["case_id"])
            if case is None:
                raise LookupError(
                    f"judge_blind: case {pair['case_id']!r} is not a candidate_cases({suite_id!r}, "
                    f"{split!r}) member"
                )
            resolved = self._blind_tokens.get(pair["blind_token"])
            if resolved is None:
                raise LookupError(
                    f"judge_blind: blind_token {pair['blind_token']!r} was not minted by "
                    "make_blind_pairs on this evaluator"
                )
            resolved_case_id, arm = resolved
            view: JudgeView = {"input": case.input, "rubric": case.rubric, "output": pair["output"]}
            output_lengths[pair["blind_token"]] = len(str(pair["output"].get("text", "")))
            for judge_lineage in judges:
                score, notes = judge_fn(judge_lineage, view)
                records.append(
                    JudgeRecord(
                        case_id=resolved_case_id,
                        arm=arm,
                        judge_lineage=judge_lineage,
                        blind_token=pair["blind_token"],
                        score=score,
                        notes=notes,
                        rubric_dimension=case.rubric,
                    )
                )
        mode = _rubric_validity_mode()
        if mode == "off":
            return records

        by_dimension: dict[str, list[JudgeRecord]] = defaultdict(list)
        for record in records:
            by_dimension[record.rubric_dimension].append(record)

        findings: dict[str, float] = {}
        for dimension, dimension_records in by_dimension.items():
            correlation = _pearson_correlation(
                [float(output_lengths[record.blind_token]) for record in dimension_records],
                [record.score for record in dimension_records],
            )
            if correlation is not None and abs(correlation) > LENGTH_CORRELATION_LIMIT:
                findings[dimension] = correlation

        if not findings:
            return records

        for record in records:
            correlation = findings.get(record.rubric_dimension)
            if correlation is not None:
                finding = (
                    f"rubric_validity:length_correlation={correlation:.3f};"
                    f"limit={LENGTH_CORRELATION_LIMIT:.1f};mode={mode}"
                )
                record.notes = f"{record.notes}; {finding}" if record.notes else finding
        if mode == "enforce":
            return [record for record in records if record.rubric_dimension not in findings]
        return records

    def score_experiment(self, exp_id: str, dev_only: bool) -> Scorecard:
        """Aggregate already-recorded ``EvalResult`` rows for ``exp_id`` into
        a ``Scorecard``. Reads ONLY ``metrics``/``per_case`` numbers already
        persisted by ``LabStore.record_eval_result`` — never touches
        ``expected``. "dev then held-out gated": ``dev_only=True`` scores on
        DEV results alone (the pre-held-out gate); ``dev_only=False``
        additionally REQUIRES held-out results to already exist (raises
        otherwise) — you cannot get a held-out-graded scorecard without the
        held-out phase having actually run.
        """
        experiment = self._store.get_experiment(exp_id)
        if experiment is None:
            raise LookupError(f"unknown experiment: {exp_id!r}")

        results = [eval_result_from_row(row) for row in self._store.eval_results(exp_id)]
        champion_dev = [r for r in results if r.arm == "champion" and r.split == EvalSplit.DEV]
        challenger_dev = [r for r in results if r.arm == "challenger" and r.split == EvalSplit.DEV]
        if not champion_dev or not challenger_dev:
            raise ValueError(
                f"score_experiment({exp_id!r}): missing dev-split eval_results for both arms "
                "— run the dev phase before scoring"
            )

        champion_results, challenger_results = champion_dev, challenger_dev
        champion_held: list[EvalResult] = []
        challenger_held: list[EvalResult] = []
        if not dev_only:
            champion_held = [
                r for r in results if r.arm == "champion" and r.split == EvalSplit.HELD_OUT
            ]
            challenger_held = [
                r for r in results if r.arm == "challenger" and r.split == EvalSplit.HELD_OUT
            ]
            if not champion_held or not challenger_held:
                raise ValueError(
                    f"score_experiment({exp_id!r}, dev_only=False): held-out gate — no held-out "
                    "eval_results recorded yet for both arms"
                )
            champion_results, challenger_results = champion_held, challenger_held

        champion_metrics = aggregate_replicates(champion_results)
        challenger_metrics = aggregate_replicates(challenger_results)

        specs = parse_metric_specs(self._store.get_eval_suite(experiment["eval_suite_id"]))
        primary_name = experiment["primary_metric"] or primary_metric_name(specs)

        primary_delta = signed_delta(primary_name, specs, champion_metrics, challenger_metrics)
        regression = safety_regression(specs, champion_metrics, challenger_metrics)
        complexity_delta = regularizer_delta(specs, champion_metrics, challenger_metrics)
        cost_penalty = _efficiency_penalty(specs, champion_metrics, challenger_metrics)

        audit_flags = self._grader.audit_metric_jump(
            exp_id, latest(champion_results), latest(challenger_results)
        )
        if not dev_only:
            audit_flags.extend(
                self._grader.audit_generalization_gap(
                    exp_id,
                    champion_dev,
                    challenger_dev,
                    champion_held,
                    challenger_held,
                )
            )
        audit_flags = list(dict.fromkeys(audit_flags))
        # Non-empty audit_flags directly taxes utility (defense in depth on
        # top of L04-campaign's disposition gate, which forces HUMAN_REVIEW
        # whenever `Scorecard.audit_flags` is non-empty regardless of the
        # numeric gates below).
        risk_penalty = float(len(audit_flags))

        utility = promotion_utility(
            quality_gain=primary_delta or 0.0,
            cost_penalty=cost_penalty,
            latency_penalty=0.0,
            complexity_penalty=complexity_delta or 0.0,
            risk_penalty=risk_penalty,
        )

        return Scorecard(
            champion=champion_metrics,
            challenger=challenger_metrics,
            primary_delta=primary_delta,
            utility=utility,
            complexity_delta=complexity_delta,
            confidence_interval=confidence_interval_95(primary_name, challenger_results),
            safety_regression=regression,
            audit_flags=audit_flags,
        )
