"""Champion/challenger experiment orchestration for the self-improvement lab.

This module deliberately owns decisions, not execution or grading.  The executor and
protected evaluator are imported at the point of use: importing the campaign must not
give candidate-facing code a route to the protected evaluator or its environment.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
import os
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any, Literal, Protocol, cast

from omniagentos.contracts import utc_now_iso
from omniagentos.lab.contracts import (
    Disposition,
    EvalResult,
    EvalSplit,
    Experiment,
    ExperimentStatus,
    ExplorePolicy,
    MetricSpec,
    PromotionThreshold,
    Scorecard,
    SurfaceKind,
    SurfaceStatus,
    promotion_utility,
    surface_requires_human,
)
from omniagentos.lab.eval.evaluator import JudgeFn

_DEFAULT_POLICY_MIX: dict[ExplorePolicy, float] = {
    ExplorePolicy.EXPLOIT: 0.60,
    ExplorePolicy.EXPLORE: 0.25,
    ExplorePolicy.SIMPLIFY: 0.10,
    ExplorePolicy.RED_TEAM: 0.05,
}
_PORTFOLIO_SIZE = 20
_STALL_WINDOW = 30
_STALL_MIN_PROMOTIONS = 1

FeatureMode = Literal["off", "shadow", "enforce"]

LAB_VALIDITY_ENV = "OMNIAGENTOS_LAB_VALIDITY_MODE"
METHODOLOGY_REVIEW_ENV = "OMNIAGENTOS_METHODOLOGY_REVIEW_MODE"
MODEL_JUDGE_ENV = "OMNIAGENTOS_MODEL_JUDGE_MODE"
VALID_FEATURE_MODES = frozenset({"off", "shadow", "enforce"})

# Evidence floors are staged under LAB_VALIDITY at the numeric gate and remain
# an always-on promotion backstop for enforced model judging.
MIN_JUDGE_AGREEMENT = 0.60
MIN_JUDGE_VALIDITY = 0.80

OFFLINE_HEURISTIC_FLAG = "blind-judging-used-offline-heuristic-judge"
PROMOTION_FLOOR_FLAG = "promotion-blocked-missing-validity-evidence"


@dataclass(frozen=True, slots=True)
class MethodologyReview:
    """A review of experiment design, distinct from artifact judging."""

    audit_flags: tuple[str, ...] = ()


class MethodologyReviewer(Protocol):
    """Callable contract for an independently supplied design reviewer."""

    def __call__(
        self, experiment: Experiment, suite: Mapping[str, Any]
    ) -> MethodologyReview | Mapping[str, Any]: ...


class PairwiseJudgeFn(JudgeFn, Protocol):
    """Model-backed extension required for forced pairwise campaign judging."""

    @property
    def judge_identity(self) -> str: ...

    def register_pairs(self, pairs: Sequence[Mapping[str, Any]]) -> None: ...


@dataclass(slots=True)
class _BlindJudgmentOutcome:
    """Which scorer actually produced panel judgments on the live path."""

    used_model_judge: bool = False
    used_offline_heuristic: bool = False
    ineligible_reason: str | None = None


_methodology_reviewer_context: ContextVar[MethodologyReviewer | None] = ContextVar(
    "campaign_methodology_reviewer", default=None
)


def _feature_mode(variable: str) -> FeatureMode:
    """Read a defensive off/shadow/enforce flag, defaulting invalid values to off."""
    value = os.getenv(variable, "off").strip().lower()
    if value not in VALID_FEATURE_MODES:
        return "off"
    return cast(FeatureMode, value)


def _run_experiment(
    store: Any,
    evaluator: Any,
    exp_id: str,
    *,
    dry_run: bool = False,
    judge_fn: JudgeFn | None = None,
) -> Disposition:
    """Run, score, and decide an experiment without exposing held-out answers.

    A failure to execute, score, or unlock held-out grading is an ``INVALID``
    disposition.  Numeric misses in otherwise intact data are ordinary rejections.

    Optional ``judge_fn`` injects a model-backed blind scorer. When it is ``None``
    or fails selection/execution, the evaluator's offline heuristic runs exactly
    as before and the integrity flag records that fact.
    """
    exp_row = store.get_experiment(exp_id)
    if exp_row is None:
        raise ValueError(f"unknown experiment: {exp_id}")
    exp = _experiment(exp_row)
    raw_exp = _model_or_mapping(exp_row)
    champion = store.get_surface(exp.champion_surface_id)
    challenger = store.get_surface(exp.challenger_surface_id)
    suite = store.get_eval_suite(exp.eval_suite_id)
    if champion is None or challenger is None or suite is None:
        return _decide_invalid(store, exp, "missing experiment dependency")

    snapshot_hash = _snapshot_hash(exp, champion, challenger, suite)
    _update(store, exp.id, status=ExperimentStatus.RUNNING_BASELINE, snapshot_hash=snapshot_hash)
    blind_required, blind_judges, blind_panel_fallback = _blind_policy(
        raw_exp, champion, challenger
    )
    used_model_judge = False
    used_offline_heuristic = False
    judge_mode = _feature_mode(MODEL_JUDGE_ENV)

    try:
        # Kept lazy so L04x may be monkeypatched and so importing campaign cannot
        # transitively start an adapter/executor process.
        from omniagentos.lab.executor import run_surface_over_cases  # type: ignore[import-untyped]

        champion_results: list[EvalResult] = []
        challenger_results: list[EvalResult] = []
        replicates = max(1, exp.budgets.replicates)
        for replicate in range(replicates):
            _update(
                store,
                exp.id,
                status=(
                    ExperimentStatus.RUNNING_BASELINE
                    if replicate == 0
                    else ExperimentStatus.REPLICATING
                ),
            )
            ordered_surfaces = [
                ("champion", champion),
                ("challenger", challenger),
            ]
            if replicate % 2:
                ordered_surfaces.reverse()
            dev_runs: dict[str, dict[str, Any]] = {}
            for arm, surface in ordered_surfaces:
                _update(
                    store,
                    exp.id,
                    status=(
                        ExperimentStatus.RUNNING_CHALLENGER
                        if arm == "challenger"
                        else (
                            ExperimentStatus.RUNNING_BASELINE
                            if replicate == 0
                            else ExperimentStatus.REPLICATING
                        )
                    ),
                )
                run = run_surface_over_cases(
                    store,
                    surface,
                    exp.eval_suite_id,
                    EvalSplit.DEV,
                    exp.budgets,
                    env_scrub=True,
                    dry_run=dry_run,
                )
                _assert_valid_run(run)
                dev_runs[arm] = run

            champion_run = dev_runs["champion"]
            challenger_run = dev_runs["challenger"]

            champ_result = _with_result_context(
                evaluator.run_deterministic(
                    exp.eval_suite_id, EvalSplit.DEV, "champion", _outputs(champion_run)
                ),
                exp,
                "champion",
                suite,
                replicate,
            )
            chal_result = _with_result_context(
                evaluator.run_deterministic(
                    exp.eval_suite_id, EvalSplit.DEV, "challenger", _outputs(challenger_run)
                ),
                exp,
                "challenger",
                suite,
                replicate,
            )
            store.record_eval_result(champ_result)
            store.record_eval_result(chal_result)
            champion_results.append(champ_result)
            challenger_results.append(chal_result)

            if blind_required:
                outcome = _record_blind_judgments(
                    store,
                    evaluator,
                    exp,
                    _outputs(champion_run),
                    _outputs(challenger_run),
                    blind_judges,
                    judge_fn=judge_fn,
                )
                if outcome.ineligible_reason is not None:
                    scorecard, reproducible = _scorecard(
                        exp, suite, champion_results, challenger_results
                    )
                    return _decide_invalid(store, exp, outcome.ineligible_reason, scorecard)
                used_model_judge = used_model_judge or outcome.used_model_judge
                used_offline_heuristic = used_offline_heuristic or outcome.used_offline_heuristic

        _update(store, exp.id, status=ExperimentStatus.EVALUATING)
        scorecard, reproducible = _scorecard(exp, suite, champion_results, challenger_results)
        dev_scorecard = evaluator.score_experiment(exp.id, dev_only=True)
        scorecard.audit_flags = list(dev_scorecard.audit_flags)
        if blind_required:
            # Reflect which judge ACTUALLY scored on the live path. Keep the
            # offline-heuristic integrity flag whenever the heuristic produced
            # any panel judgments (mode off, shadow, or enforce-fallback).
            # Strip/omit ONLY when a real model-backed judge_fn produced the
            # judgments AND enforcement mode is enforce.
            model_enforced = (
                used_model_judge and judge_mode == "enforce" and not used_offline_heuristic
            )
            if not model_enforced:
                scorecard.audit_flags.append(OFFLINE_HEURISTIC_FLAG)
            if blind_panel_fallback:
                scorecard.audit_flags.append("blind-judge-panel-empty-fallback")
            scorecard.audit_flags = list(dict.fromkeys(scorecard.audit_flags))

        # The protected evaluator is the only component that may unlock held-out
        # scoring.  Campaign never asks it to return held-out cases or expected data.
        agreement, validity = _judge_evidence(store, exp.id, scorecard)
        dev_gates_pass = (
            _meets_numeric_gate(
                scorecard,
                exp.promotion_threshold,
                agreement=agreement,
                validity=validity,
            )
            and reproducible
        )
        if not dev_gates_pass:
            return _finish(
                store,
                exp,
                scorecard,
                reproducible,
                used_model_judge=used_model_judge,
                model_judge_enforced=(judge_mode == "enforce" and used_model_judge),
            )

        held_champion_results: list[EvalResult] = []
        held_challenger_results: list[EvalResult] = []
        for replicate in range(replicates):
            ordered_surfaces = [
                ("champion", champion),
                ("challenger", challenger),
            ]
            if replicate % 2:
                ordered_surfaces.reverse()
            for arm, surface in ordered_surfaces:
                held_run = run_surface_over_cases(
                    store,
                    surface,
                    exp.eval_suite_id,
                    EvalSplit.HELD_OUT,
                    exp.budgets,
                    env_scrub=True,
                    dry_run=dry_run,
                )
                _assert_valid_run(held_run)
                held_result = _with_result_context(
                    evaluator.run_deterministic(
                        exp.eval_suite_id,
                        EvalSplit.HELD_OUT,
                        arm,
                        _outputs(held_run),
                    ),
                    exp,
                    arm,
                    suite,
                    replicate,
                    split=EvalSplit.HELD_OUT,
                )
                store.record_eval_result(held_result)
                if arm == "champion":
                    held_champion_results.append(held_result)
                else:
                    held_challenger_results.append(held_result)

        held_out = evaluator.score_experiment(exp.id, dev_only=False)
        if held_out is None:
            return _decide_invalid(
                store, exp, "held-out evaluation did not return a scorecard", scorecard
            )
        held_scorecard, held_reproducible = _scorecard(
            exp,
            suite,
            held_champion_results,
            held_challenger_results,
        )
        # Protected-evaluator audit/safety evidence augments the independently
        # calculated held-out card. Dev flags are retained, but held-out quality,
        # utility, confidence, and reproducibility now decide promotion directly.
        held_scorecard.audit_flags = list(scorecard.audit_flags)
        _merge_scorecard(held_scorecard, held_out)
        return _finish(
            store,
            exp,
            held_scorecard,
            reproducible and held_reproducible,
            used_model_judge=used_model_judge,
            model_judge_enforced=(judge_mode == "enforce" and used_model_judge),
        )
    except (LookupError, RuntimeError, ValueError) as error:
        # Candidate/executor integrity failures, missing protected expectations,
        # and invalid evaluation data are experiment failures. Programming errors
        # such as AttributeError/TypeError/KeyError intentionally escape loudly.
        return _decide_invalid(
            store,
            exp,
            f"evaluation failure: {type(error).__name__}: {error}",
        )


def run_experiment(
    store: Any,
    evaluator: Any,
    exp_id: str,
    *,
    dry_run: bool = False,
    judge_fn: JudgeFn | None = None,
    methodology_reviewer: MethodologyReviewer | None = None,
) -> Disposition:
    """Run a campaign with optional model judge and methodology reviewer."""
    reviewer_token = _methodology_reviewer_context.set(methodology_reviewer)
    try:
        return _run_experiment(
            store,
            evaluator,
            exp_id,
            dry_run=dry_run,
            judge_fn=judge_fn,
        )
    finally:
        _methodology_reviewer_context.reset(reviewer_token)


def propose_experiments(
    store: Any, discipline: str, policy_mix: dict[Any, float] | None = None
) -> list[Experiment]:
    """Create a deterministic 20-slot explore/exploit portfolio for a discipline."""
    champion_entry, champion = _find_champion(store, discipline)
    if champion_entry is None or champion is None:
        return []

    mix = _normalise_policy_mix(policy_mix)
    failed = _failed_experiments(store, discipline)
    ledger = _recent_ledger()
    suite_id, dataset_hash, primary_metric = _proposal_eval_context(store, discipline, failed)
    if suite_id is None:
        return []

    policies = _portfolio_policies(mix)
    proposals: list[Experiment] = []
    for index, policy in enumerate(policies):
        challenger = _version_challenger(
            store,
            discipline,
            champion,
            policy,
            index,
            failed,
            ledger,
        )
        if challenger is None:
            continue
        challenger_data = _model_or_mapping(challenger)
        proposal = Experiment(
            hypothesis=_proposal_hypothesis(policy, failed, ledger),
            discipline=discipline,
            explore_policy=policy,
            mutable_surface_kind=SurfaceKind(str(champion["kind"])),
            champion_surface_id=str(champion["id"]),
            challenger_surface_id=str(challenger_data["id"]),
            eval_suite_id=suite_id,
            dataset_hash=dataset_hash,
            primary_metric=primary_metric,
            status=ExperimentStatus.PROPOSED,
        )
        store.create_experiment(proposal)
        proposals.append(proposal)
    return proposals


def stall_check(store: Any, discipline: str) -> bool:
    """Return whether the last campaign window has fewer than one promotion."""
    decided = store.list_experiments(
        discipline=discipline, status=ExperimentStatus.DECIDED, limit=_STALL_WINDOW
    )
    promotions = sum(str(row.get("disposition", "")) == Disposition.PROMOTE for row in decided)
    return promotions < _STALL_MIN_PROMOTIONS


def _experiment(row: Experiment | Mapping[str, Any]) -> Experiment:
    if isinstance(row, Experiment):
        return row
    data = dict(row)
    for stored, field in (
        ("budgets_json", "budgets"),
        ("promotion_json", "promotion_threshold"),
        ("scorecard_json", "scorecard"),
    ):
        if stored in data:
            raw = data.pop(stored)
            data[field] = json.loads(raw) if isinstance(raw, str) and raw else raw
    return Experiment.model_validate(data)


def _outputs(run: Any) -> dict[str, Any]:
    if not isinstance(run, Mapping) or not isinstance(run.get("outputs"), Mapping):
        raise ValueError("executor returned no outputs")
    return dict(run["outputs"])


def _assert_valid_run(run: Any) -> None:
    """Translate executor integrity signals into the invalid-experiment path."""
    if not isinstance(run, Mapping):
        raise ValueError("executor returned an invalid run record")
    for key in ("crashed", "contaminated", "over_budget"):
        if run.get(key):
            raise ValueError(f"executor reported {key}")


def _with_result_context(
    result: EvalResult | Mapping[str, Any],
    exp: Experiment,
    arm: str,
    suite: Mapping[str, Any],
    replicate: int,
    *,
    split: EvalSplit = EvalSplit.DEV,
) -> EvalResult:
    if isinstance(result, EvalResult):
        return result.model_copy(
            update={
                "experiment_id": exp.id,
                "arm": arm,
                "suite_id": exp.eval_suite_id,
                "suite_version": int(suite.get("version", 1)),
                "split": split,
                "replicate": replicate,
            }
        )
    data = dict(result)
    data.update(
        {
            "experiment_id": exp.id,
            "arm": arm,
            "suite_id": exp.eval_suite_id,
            "suite_version": int(suite.get("version", 1)),
            "split": split,
            "replicate": replicate,
        }
    )
    return EvalResult.model_validate(data)


def _requires_blind_judging(
    exp: Mapping[str, Any], champion: Mapping[str, Any], challenger: Mapping[str, Any]
) -> bool:
    return any(
        bool(item.get(key))
        for item in (exp, champion, challenger)
        for key in ("blind_judge", "requires_blind_judging", "blind")
    )


def _blind_policy(
    exp: Mapping[str, Any], champion: Mapping[str, Any], challenger: Mapping[str, Any]
) -> tuple[bool, list[str], bool]:
    required = _requires_blind_judging(exp, champion, challenger)
    raw_judges = exp.get("judges", exp.get("judge_lineages", []))
    judges = [str(item) for item in raw_judges] if isinstance(raw_judges, list) else []
    for surface in (champion, challenger):
        if str(surface.get("kind")) != SurfaceKind.ORCHESTRATION_GENOME.value:
            continue
        from omniagentos.lab.surfaces import load_surface_content

        content = load_surface_content(dict(surface))
        if not isinstance(content, Mapping):
            continue
        policy = content.get("judge", {})
        if not isinstance(policy, Mapping):
            continue
        required = required or bool(policy.get("blind"))
        panel = policy.get("panel", [])
        if isinstance(panel, list):
            judges.extend(str(item) for item in panel)
    fallback = required and not judges
    if fallback:
        judges.append("offline-heuristic")
    return required, list(dict.fromkeys(judges)), fallback


_LINEAGE_FAMILIES = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "opus": "anthropic",
    "sonnet": "anthropic",
    "haiku": "anthropic",
    "openai": "openai",
    "gpt": "openai",
    "codex": "openai",
    "sol": "openai",
    "xai": "xai",
    "grok": "xai",
    "google": "google",
    "gemini": "google",
    "meta": "meta",
    "llama": "meta",
    "mistral": "mistral",
    "deepseek": "deepseek",
    "qwen": "qwen",
    "kimi": "moonshot",
    "moonshot": "moonshot",
}


def _lineage_family(lineage: str) -> str:
    """Resolve model ancestry without importing the optional judge package."""
    normalized = lineage.strip().lower()
    for separator in ("/", ":", "_"):
        normalized = normalized.replace(separator, "-")
    tokens = [token for token in normalized.split("-") if token]
    if not tokens:
        return ""
    return next(
        (_LINEAGE_FAMILIES[token] for token in tokens if token in _LINEAGE_FAMILIES),
        tokens[0],
    )


def _cross_lineage_panel(judges: Sequence[str]) -> bool:
    families = {
        family
        for judge in judges
        if judge != "offline-heuristic"
        for family in [_lineage_family(judge)]
        if family
    }
    return len(families) >= 2


def review_methodology(experiment: Experiment, suite: Mapping[str, Any]) -> MethodologyReview:
    """Perform conservative, deterministic checks of experiment design."""
    flags: list[str] = []
    if (
        not experiment.champion_surface_id
        or experiment.champion_surface_id == experiment.challenger_surface_id
    ):
        flags.append("methodology:no-independent-control")
    if experiment.budgets.replicates < 2:
        flags.append("methodology:underpowered-replicates")
    raw_metrics = suite.get("metrics", suite.get("metrics_json"))
    if not raw_metrics:
        flags.append("methodology:no-declared-metrics")
    return MethodologyReview(tuple(flags))


def _review_flags(
    reviewer: MethodologyReviewer,
    experiment: Experiment,
    suite: Mapping[str, Any],
) -> list[str]:
    review = reviewer(experiment, suite)
    raw_flags = review.get("audit_flags", []) if isinstance(review, Mapping) else review.audit_flags
    if not isinstance(raw_flags, (list, tuple)):
        raise TypeError("methodology reviewer audit_flags must be a list or tuple")
    return [str(flag) for flag in raw_flags if str(flag)]


def _apply_methodology_review(
    scorecard: Scorecard,
    experiment: Experiment,
    suite: Mapping[str, Any],
    reviewer: MethodologyReviewer | None = None,
) -> None:
    """Run design review; only enforce mode changes disposition-driving flags."""
    mode = _feature_mode(METHODOLOGY_REVIEW_ENV)
    if mode == "off":
        return
    flags = _review_flags(reviewer or review_methodology, experiment, suite)
    if not flags:
        return
    if mode == "shadow":
        logging.getLogger(__name__).warning("methodology shadow flags: %s", flags)
        return
    scorecard.audit_flags = list(dict.fromkeys([*scorecard.audit_flags, *flags]))


def _model_judge_ineligibility(judge_fn: JudgeFn | None, judges: Sequence[str]) -> str | None:
    identity = str(getattr(judge_fn, "judge_identity", ""))
    register_pairs = getattr(judge_fn, "register_pairs", None)
    if (
        judge_fn is None
        or not identity.startswith("model-backed:")
        or not callable(register_pairs)
        or not _cross_lineage_panel(judges)
    ):
        return (
            "model judge requires a model-backed forced-pairwise JudgeFn and "
            "at least two judge lineage families"
        )
    return None


def _pairwise_model_judge(
    judge_fn: JudgeFn | None,
    pairs: Sequence[Mapping[str, Any]],
    judges: Sequence[str],
) -> JudgeFn | None:
    """Select and prime a real forced-pairwise judge under mode discipline.

    Never raises for ineligible panels: callers fall back to the offline
    heuristic or record an INVALID disposition through the normal path.
    """
    mode = _feature_mode(MODEL_JUDGE_ENV)
    if mode == "off":
        return None

    message = _model_judge_ineligibility(judge_fn, judges)
    if message is not None:
        if mode == "enforce":
            logging.getLogger(__name__).warning(
                "model judge enforce rejected ineligible panel: %s", message
            )
        else:
            logging.getLogger(__name__).warning("model judge shadow failure: %s", message)
        return None

    cast(PairwiseJudgeFn, judge_fn).register_pairs(pairs)
    return judge_fn


def _record_blind_judgments(
    store: Any,
    evaluator: Any,
    exp: Experiment,
    champion_outputs: Mapping[str, Any],
    challenger_outputs: Mapping[str, Any],
    judges: list[str],
    *,
    judge_fn: JudgeFn | None = None,
) -> _BlindJudgmentOutcome:
    """Record forced champion/challenger pairs; prefer injected model JudgeFn.

    Returns which scorer actually produced judgments. Ineligible panels set
    ``ineligible_reason`` instead of raising mid-run so the campaign can record
    an INVALID disposition with a scorecard and complete cleanly.
    """
    outcome = _BlindJudgmentOutcome()
    # No pair includes an arm label.  The evaluator owns the arm/token mapping until
    # it writes JudgeRecords after the blind decision.
    case_outputs = {
        case_id: {
            "champion": dict(champion_outputs[case_id]),
            "challenger": dict(challenger_outputs[case_id]),
        }
        for case_id in champion_outputs.keys() & challenger_outputs.keys()
    }
    if not case_outputs:
        outcome.ineligible_reason = "ineligible panel: no overlapping champion/challenger cases"
        return outcome
    if not judges:
        outcome.ineligible_reason = "ineligible panel: empty judge panel"
        return outcome

    try:
        pairs, used_seed = evaluator.make_blind_pairs(case_outputs)
    except (LookupError, RuntimeError, TypeError, ValueError) as error:
        outcome.ineligible_reason = f"broken blind pairing: {type(error).__name__}: {error}"
        return outcome

    if not pairs:
        outcome.ineligible_reason = "ineligible panel: empty blind pairs"
        return outcome

    # Presentation-order seed, kept out of judge sight; recorded for audit
    # replay (schema-level provenance persistence arrives with migration 082).
    logging.getLogger(__name__).info(
        "blind pairs for experiment %s used presentation seed %d", exp.id, used_seed
    )

    mode = _feature_mode(MODEL_JUDGE_ENV)
    ineligibility = _model_judge_ineligibility(judge_fn, judges)
    if mode == "enforce" and ineligibility is not None:
        outcome.ineligible_reason = f"ineligible panel: {ineligibility}"
        return outcome
    selected = _pairwise_model_judge(
        judge_fn,
        cast(Sequence[Mapping[str, Any]], pairs),
        judges,
    )
    records: list[Any] | None = None
    if selected is not None and mode in {"shadow", "enforce"}:
        try:
            model_records = evaluator.judge_blind(
                exp.eval_suite_id,
                EvalSplit.DEV,
                pairs,
                judges,
                judge_fn=selected,
            )
            if model_records:
                outcome.used_model_judge = True
                if mode == "enforce":
                    records = model_records
            else:
                logging.getLogger(__name__).warning(
                    "model-backed judge returned no judgments for experiment %s",
                    exp.id,
                )
        except (LookupError, RuntimeError, TypeError, ValueError) as error:
            logging.getLogger(__name__).warning(
                "model-backed judge failed for experiment %s; "
                "falling back to offline heuristic: %s",
                exp.id,
                error,
            )
            selected = None

    # Shadow mode observes the model-backed judge but preserves established
    # runtime behavior by recording offline-heuristic judgments as authoritative.
    # This also keeps OFFLINE_HEURISTIC_FLAG factually tied to heuristic use.
    if mode != "enforce" or not outcome.used_model_judge:
        try:
            records = evaluator.judge_blind(exp.eval_suite_id, EvalSplit.DEV, pairs, judges)
            outcome.used_offline_heuristic = True
        except (LookupError, RuntimeError, TypeError, ValueError) as error:
            outcome.ineligible_reason = f"broken blind pairing: {type(error).__name__}: {error}"
            return outcome

    if not records:
        outcome.ineligible_reason = "ineligible panel: insufficient judgments"
        return outcome

    for record in records:
        store.record_judge(record.model_copy(update={"experiment_id": exp.id}))
    return outcome


def _scorecard(
    exp: Experiment,
    suite: Mapping[str, Any],
    champion_results: list[EvalResult],
    challenger_results: list[EvalResult],
) -> tuple[Scorecard, bool]:
    champion = _mean_metrics(champion_results)
    challenger = _mean_metrics(challenger_results)
    specs = _metric_specs(suite)
    primary = exp.primary_metric or next((s.name for s in specs if s.role == "primary"), "")
    direction = next((s.direction for s in specs if s.name == primary), "maximize")
    primary_delta = _directional_delta(
        champion.get(primary, 0.0), challenger.get(primary, 0.0), direction
    )
    safety_regression = _safety_regression(champion, challenger, specs)
    complexity_delta = _complexity_delta(champion, challenger, specs)
    cost_penalty = _named_penalty(champion, challenger, specs, "cost")
    latency_penalty = _named_penalty(champion, challenger, specs, "latency")
    risk_penalty = _named_penalty(champion, challenger, specs, "risk")
    utility = promotion_utility(
        primary_delta,
        cost_penalty,
        latency_penalty,
        max(0.0, complexity_delta),
        risk_penalty,
    )
    deltas = [
        _directional_delta(
            champion_result.metrics.get(primary, 0.0),
            challenger_result.metrics.get(primary, 0.0),
            direction,
        )
        for champion_result, challenger_result in zip(
            champion_results, challenger_results, strict=True
        )
    ]
    confidence_interval = _confidence_interval(deltas)
    reproducible = all(
        result.deterministic_passed for result in [*champion_results, *challenger_results]
    ) and _stable(deltas, confidence_interval)
    return (
        Scorecard(
            champion=champion,
            challenger=challenger,
            primary_delta=primary_delta,
            utility=utility,
            cost_delta=cost_penalty,
            complexity_delta=complexity_delta,
            confidence_interval=confidence_interval,
            safety_regression=safety_regression,
        ),
        reproducible,
    )


def _metric_specs(suite: Mapping[str, Any]) -> list[MetricSpec]:
    raw = suite.get("metrics", suite.get("metrics_json", []))
    if isinstance(raw, str):
        raw = json.loads(raw)
    return [
        item if isinstance(item, MetricSpec) else MetricSpec.model_validate(item) for item in raw
    ]


def _mean_metrics(results: Iterable[EvalResult]) -> dict[str, float]:
    values: dict[str, list[float]] = {}
    for result in results:
        for name, value in result.metrics.items():
            values.setdefault(name, []).append(float(value))
    return {name: fmean(items) for name, items in values.items()}


def _directional_delta(champion: float, challenger: float, direction: str) -> float:
    return champion - challenger if direction == "minimize" else challenger - champion


def _safety_regression(
    champion: Mapping[str, float], challenger: Mapping[str, float], specs: Iterable[MetricSpec]
) -> bool:
    for spec in specs:
        if spec.role not in {"guardrail", "hard_constraint"}:
            continue
        value = challenger.get(spec.name)
        baseline = champion.get(spec.name)
        if value is None:
            return True
        if spec.threshold is not None:
            if spec.direction == "minimize" and value > spec.threshold:
                return True
            if spec.direction != "minimize" and value < spec.threshold:
                return True
        if baseline is not None and _directional_delta(baseline, value, spec.direction) < 0:
            return True
    return False


def _complexity_delta(
    champion: Mapping[str, float], challenger: Mapping[str, float], specs: Iterable[MetricSpec]
) -> float:
    names = [s.name for s in specs if s.role == "regularizer" or "complex" in s.name.lower()]
    if not names:
        return 0.0
    return sum(challenger.get(name, 0.0) - champion.get(name, 0.0) for name in names)


def _named_penalty(
    champion: Mapping[str, float],
    challenger: Mapping[str, float],
    specs: Iterable[MetricSpec],
    token: str,
) -> float:
    directions = {spec.name: spec.direction for spec in specs}
    names = [name for name in challenger if token in name.lower()]
    return sum(
        max(
            0.0,
            -_directional_delta(
                champion.get(name, 0.0), challenger[name], directions.get(name, "minimize")
            ),
        )
        for name in names
    )


def _confidence_interval(values: list[float]) -> list[float] | None:
    """Delegate replicate-mean intervals to the shared statistics contract."""
    stats = importlib.import_module("omniagentos.lab.stats")
    estimate = stats.mean_confidence_interval(values)
    return cast(list[float] | None, estimate.as_list())


def _stable(values: list[float], interval: list[float] | None) -> bool:
    if len(values) < 2 or interval is None:
        return False
    if not all(math.isfinite(value) for value in values):
        return False
    # A confidence interval spanning a regression, or variation larger than the
    # observed gain, is not a reproducible improvement.
    return interval[0] >= 0.0 and (interval[1] - interval[0]) <= max(abs(fmean(values)), 0.01)


def _merge_scorecard(scorecard: Scorecard, evaluated: Any) -> None:
    if isinstance(evaluated, Scorecard):
        other = evaluated
    elif isinstance(evaluated, Mapping):
        other = Scorecard.model_validate(evaluated)
    else:
        return
    if scorecard.utility is None and other.utility is not None:
        scorecard.utility = other.utility
    if scorecard.cost_delta is None and other.cost_delta is not None:
        scorecard.cost_delta = other.cost_delta
    if scorecard.complexity_delta is None and other.complexity_delta is not None:
        scorecard.complexity_delta = other.complexity_delta
    scorecard.audit_flags = list(dict.fromkeys([*scorecard.audit_flags, *other.audit_flags]))
    scorecard.safety_regression = scorecard.safety_regression or other.safety_regression


def _metric_evidence(scorecard: Scorecard, name: str) -> float | None:
    """Read forward-compatible gate evidence from a scorecard."""
    direct = getattr(scorecard, name, None)
    if isinstance(direct, int | float) and not isinstance(direct, bool):
        return float(direct)
    for metrics in (scorecard.challenger, scorecard.champion):
        value = metrics.get(name)
        if isinstance(value, int | float) and not isinstance(value, bool):
            return float(value)
    return None


def _provenance_evidence(store: Any, experiment_id: str) -> tuple[float | None, float | None]:
    getter = getattr(store, "get_verdict_provenance", None)
    if not callable(getter):
        return None, None
    provenance = getter(experiment_id)
    if provenance is None:
        return None, None

    def field(name: str, default: Any = None) -> Any:
        if isinstance(provenance, Mapping):
            return provenance.get(name, default)
        return getattr(provenance, name, default)

    agreement = field("agreement")
    explicit_validity = field("validity")
    if isinstance(explicit_validity, int | float) and not isinstance(explicit_validity, bool):
        validity = float(explicit_validity)
    else:
        effective_n = int(field("effective_n", 0) or 0)
        effect = abs(float(field("observed_effect", 0.0) or 0.0))
        mde_raw = field("mde", math.inf)
        mde = float(mde_raw) if isinstance(mde_raw, int | float) else math.inf
        valid = field("invalidation_status", "invalidated") == "valid"
        validity = float(valid and effective_n >= 2 and effect >= mde)
    return (
        float(agreement)
        if isinstance(agreement, int | float) and not isinstance(agreement, bool)
        else None,
        validity,
    )


def _evidence_floor_met(agreement: float | None, validity: float | None) -> bool:
    """Return whether finite agreement and validity evidence clears both floors."""
    return (
        agreement is not None
        and math.isfinite(agreement)
        and agreement >= MIN_JUDGE_AGREEMENT
        and validity is not None
        and math.isfinite(validity)
        and validity >= MIN_JUDGE_VALIDITY
    )


def _judge_evidence(
    store: Any, experiment_id: str, scorecard: Scorecard
) -> tuple[float | None, float | None]:
    agreement, validity = _provenance_evidence(store, experiment_id)
    if agreement is None:
        agreement = _metric_evidence(scorecard, "judge_agreement")
    if validity is None:
        validity = _metric_evidence(scorecard, "judge_validity")
    return agreement, validity


def _finish(
    store: Any,
    exp: Experiment,
    scorecard: Scorecard,
    reproducible: bool,
    *,
    used_model_judge: bool = False,
    model_judge_enforced: bool = False,
) -> Disposition:
    """Apply disposition gates including the model-judge promotion backstop.

    Stripping the offline-heuristic integrity flag must not by itself open
    promotion: a model-backed enforce path still requires agreement/validity
    evidence (or is routed to HUMAN_REVIEW), independent of LAB_VALIDITY.
    """
    if model_judge_enforced and used_model_judge:
        # Defense in depth: never keep the offline-heuristic flag when a real
        # model-backed judge produced enforce-mode judgments.
        scorecard.audit_flags = [
            flag for flag in scorecard.audit_flags if flag != OFFLINE_HEURISTIC_FLAG
        ]

    suite = store.get_eval_suite(exp.eval_suite_id) or {}
    _apply_methodology_review(
        scorecard,
        exp,
        suite,
        reviewer=_methodology_reviewer_context.get(),
    )
    agreement, validity = _judge_evidence(store, exp.id, scorecard)

    # Always-on backstop (not behind LAB_VALIDITY): enforced model-backed scores
    # without the offline-heuristic flag cannot promote without evidence. This
    # blocks unanimous-1.0 fake panels even when LAB_VALIDITY defaults to off.
    heuristic_flag_present = OFFLINE_HEURISTIC_FLAG in scorecard.audit_flags
    if (
        model_judge_enforced
        and used_model_judge
        and not heuristic_flag_present
        and not _evidence_floor_met(agreement, validity)
    ):
        if PROMOTION_FLOOR_FLAG not in scorecard.audit_flags:
            scorecard.audit_flags.append(PROMOTION_FLOOR_FLAG)

    challenger = store.get_surface(exp.challenger_surface_id) or {}
    requires_human = exp.promotion_threshold.require_human or surface_requires_human(
        SurfaceKind(str(challenger.get("kind", exp.mutable_surface_kind))),
        bool(challenger.get("safety_relevant", False)),
    )
    rollback_ok = _rollback_target_is_valid(store, exp, challenger)
    numeric_pass = _meets_numeric_gate(
        scorecard,
        exp.promotion_threshold,
        agreement=agreement,
        validity=validity,
    )
    if scorecard.audit_flags or requires_human:
        final = Disposition.HUMAN_REVIEW
    elif numeric_pass and reproducible and rollback_ok:
        final = Disposition.PROMOTE
    else:
        final = Disposition.REJECT
    _update(
        store,
        exp.id,
        status=ExperimentStatus.DECIDED,
        disposition=final,
        scorecard=scorecard,
    )
    return final


def _meets_numeric_gate(
    scorecard: Scorecard,
    threshold: PromotionThreshold,
    *,
    agreement: float | None = None,
    validity: float | None = None,
) -> bool:
    """Apply numeric thresholds and enforce staged evidence floors when enabled."""
    numeric_pass = (
        scorecard.primary_delta is not None
        and scorecard.primary_delta >= threshold.primary_delta_min
        and (not threshold.no_safety_regressions or not scorecard.safety_regression)
        and scorecard.utility is not None
        and scorecard.utility >= threshold.min_utility
        and scorecard.cost_delta is not None
        and scorecard.cost_delta <= threshold.cost_increase_max
        and scorecard.complexity_delta is not None
        and scorecard.complexity_delta <= threshold.max_complexity_delta
    )
    if not numeric_pass:
        return False
    if _feature_mode(LAB_VALIDITY_ENV) != "enforce":
        return True
    return _evidence_floor_met(agreement, validity)


def _rollback_target_is_valid(store: Any, exp: Experiment, challenger: Mapping[str, Any]) -> bool:
    entry = store.get_champion(exp.discipline, str(exp.mutable_surface_kind))
    if entry is None:
        return False
    target_id = entry.get("rollback_to_surface_id") or entry.get("surface_id")
    target = store.get_surface(target_id) if target_id else None
    if target is None or target.get("id") == challenger.get("id"):
        return False
    # The current champion is the outgoing immutable rollback target; L03 archives it
    # atomically when promote() advances the pointer.  Historical targets must already
    # be archived.
    if entry.get("rollback_to_surface_id"):
        return str(target.get("status")) == SurfaceStatus.ARCHIVED
    return str(target.get("status")) in {SurfaceStatus.CHAMPION, SurfaceStatus.ARCHIVED}


def _decide_invalid(
    store: Any, exp: Experiment, reason: str, scorecard: Scorecard | None = None
) -> Disposition:
    card = scorecard or Scorecard(audit_flags=[reason])
    if reason not in card.audit_flags:
        card.audit_flags.append(reason)
    _update(
        store,
        exp.id,
        status=ExperimentStatus.DECIDED,
        disposition=Disposition.INVALID,
        scorecard=card,
    )
    return Disposition.INVALID


def _update(store: Any, exp_id: str, **fields: Any) -> None:
    fields["updated_at"] = utc_now_iso()
    store.update_experiment(exp_id, fields)


def _snapshot_hash(
    exp: Experiment,
    champion: Mapping[str, Any],
    challenger: Mapping[str, Any],
    suite: Mapping[str, Any],
) -> str:
    source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    payload = {
        "code": source_hash,
        "config": champion.get("config_hash", champion.get("content_hash", "")),
        "prompt": challenger.get("content_hash", ""),
        "tools": champion.get("tool_hash", challenger.get("tool_hash", "")),
        "dataset": exp.dataset_hash or suite.get("dataset_hash", ""),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _find_champion(
    store: Any, discipline: str
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    for kind in SurfaceKind:
        entry = store.get_champion(discipline, str(kind))
        if entry is not None:
            surface = store.get_surface(entry["surface_id"])
            return entry, surface
    return None, None


def _normalise_policy_mix(policy_mix: dict[Any, float] | None) -> dict[ExplorePolicy, float]:
    """Normalize a policy mix dict to ExplorePolicy enum keys with normalized weights."""
    # Start with defaults, then override with provided mix
    raw = policy_mix or {}
    weights: dict[ExplorePolicy, float] = {}

    for policy in ExplorePolicy:
        # Try to get value from raw dict using policy enum or string value
        value = None
        if policy in raw:
            value = raw[policy]
        elif policy.value in raw:
            value = raw[policy.value]

        # Convert to float, default to 0.0
        if value is not None:
            weights[policy] = max(0.0, float(value))
        else:
            # If no override, use default
            weights[policy] = max(0.0, float(_DEFAULT_POLICY_MIX.get(policy, 0.0)))

    total = sum(weights.values())
    if total <= 0:
        raise ValueError("policy_mix must contain at least one positive weight")
    return {policy: weight / total for policy, weight in weights.items()}


def _portfolio_policies(mix: Mapping[ExplorePolicy, float]) -> list[ExplorePolicy]:
    exact = {policy: weight * _PORTFOLIO_SIZE for policy, weight in mix.items()}
    counts = {policy: math.floor(value) for policy, value in exact.items()}
    for policy, _ in sorted(
        exact.items(), key=lambda item: (item[1] % 1, item[0].value), reverse=True
    )[: _PORTFOLIO_SIZE - sum(counts.values())]:
        counts[policy] += 1
    # Stable interleaving prevents a proposal burst from becoming a policy-local
    # optimizer; it also makes the split deterministic from discipline-independent
    # policy weights.
    remaining = Counter(counts)
    policies: list[ExplorePolicy] = []
    while sum(remaining.values()):
        for policy in ExplorePolicy:
            if remaining[policy]:
                policies.append(policy)
                remaining[policy] -= 1
    return policies


def _failed_experiments(store: Any, discipline: str) -> list[Mapping[str, Any]]:
    rows = list(
        store.list_experiments(discipline=discipline, status=ExperimentStatus.FAILED, limit=50)
    )
    # LabStore cannot filter by disposition; inspect the recent window for rejections.
    rows.extend(
        row
        for row in store.list_experiments(discipline=discipline, limit=50)
        if str(row.get("disposition", "")) == Disposition.REJECT
    )
    return rows[:50]


def _recent_ledger() -> list[Any]:
    try:
        from omniagentos.contracts import default_ledger_dir
        from omniagentos.ledger import read_manifests

        return list(read_manifests(default_ledger_dir(), limit=50))
    except (OSError, ImportError):
        return []


def _proposal_eval_context(
    store: Any, discipline: str, failed: list[Mapping[str, Any]]
) -> tuple[str | None, str, str]:
    for row in failed:
        if row.get("eval_suite_id"):
            return (
                str(row["eval_suite_id"]),
                str(row.get("dataset_hash", "")),
                str(row.get("primary_metric", "")),
            )
    # A normal store has no suite-listing API; any recent experiment is the
    # authoritative campaign context.  Optional test/in-memory stores may expose
    # list_eval_suites.
    for row in store.list_experiments(discipline=discipline, limit=50):
        if row.get("eval_suite_id"):
            return (
                str(row["eval_suite_id"]),
                str(row.get("dataset_hash", "")),
                str(row.get("primary_metric", "")),
            )
    suites: list[Any] = getattr(store, "list_eval_suites", lambda _discipline: [])(discipline)
    if suites:
        suite = suites[0]
        return str(suite["id"]), str(suite.get("dataset_hash", "")), ""
    return None, "", ""


def _version_challenger(
    store: Any,
    discipline: str,
    champion: Mapping[str, Any],
    policy: ExplorePolicy,
    index: int,
    failed: list[Mapping[str, Any]],
    ledger: list[Any],
) -> Any | None:
    # Import the module lazily rather than functions so tests can monkeypatch its
    # public API and callers never load mutable-surface code during import.
    from omniagentos.lab import surfaces  # type: ignore[import-untyped]

    kind = SurfaceKind(str(champion["kind"]))
    seed = hashlib.sha256(f"{discipline}:{utc_now_iso()[:13]}:{index}".encode()).hexdigest()[:12]
    if kind == SurfaceKind.PROMPT:
        role = str(champion.get("label") or Path(str(champion.get("path", "prompt"))).stem)
        content = (
            f"Candidate mutation {seed}. Policy: {policy.value}. "
            f"Observed failed trials: {len(failed)}; recent ledger entries: {len(ledger)}."
        )
        return surfaces.version_prompt(  # type: ignore[attr-defined]
            store, discipline, role, content, parent_version=champion.get("version")
        )
    if kind == SurfaceKind.ORCHESTRATION_GENOME:
        genome = {
            "id": f"candidate-{seed}",
            "archetype": "pipeline" if policy != ExplorePolicy.EXPLORE else "panel",
            "roles": [{"name": "candidate", "agent": "mock"}],
            "flow": [{"stage": "solve", "kind": "generate", "role": "candidate"}],
        }
        return surfaces.version_genome(  # type: ignore[attr-defined]
            store, discipline, genome, parent_version=champion.get("version")
        )
    # MODEL_ASSIGNMENT/ROUTING_POLICY/REVIEW_RUBRIC are currently surface records but
    # do not have a frozen versioning API beyond genome/prompt.  Do not fabricate an
    # unversioned challenger.
    return None


def _proposal_hypothesis(
    policy: ExplorePolicy, failed: list[Mapping[str, Any]], ledger: list[Any]
) -> str:
    evidence = f"{len(failed)} failed trials and {len(ledger)} recent ledger entries"
    return f"{policy.value} mutation addresses patterns observed in {evidence}."


def _model_or_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise ValueError("surface versioner returned an invalid surface")
