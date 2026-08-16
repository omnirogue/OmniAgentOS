"""Turn mined corpus statistics into testable improvement hypotheses.

Generation is deterministic and threshold-gated: a hypothesis is only emitted
when the corpus actually supports it, and every row carries its numeric
evidence plus exemplar trace pointers. Rows are shaped for the improvement
lane's `configtest_hypotheses` table (migration 083) and enter at
``state='proposed'`` — the lane's own judged config tests are what promote or
disprove them. Tracelab proposes; it never confirms.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omniagentos.tracelab.mine import MiningResult

#: Minimum labeled traces per outcome group before a correlational claim.
MIN_GROUP = 10

#: Minimum prevalence gap (failure − success) before a pattern is implicated.
MIN_LIFT = 0.10

#: Minimum taxon occurrences before an intervention hypothesis is proposed.
MIN_TAXON_COUNT = 30

#: Taxon → (independent variable, intervention sketch) for the taxon rules.
TAXON_INTERVENTIONS: dict[str, tuple[str, str]] = {
    "file_not_found": (
        "preflight_path_validation=on",
        "Validate target paths (glob fallback + cwd echo) before file tools execute.",
    ),
    "permission_denied": (
        "capability_preflight_check=on",
        "Check scope/permission grants before dispatch instead of surfacing EACCES to the model.",
    ),
    "rate_limit": (
        "adaptive_throttle_backoff=on",
        "Absorb 429s in the gateway with jittered backoff instead of erroring the step.",
    ),
    "context_overflow": (
        "context_compaction_pass=on",
        "Summarize/truncate oversized tool results before appending to context.",
    ),
    "missing_dependency": (
        "environment_preflight_bootstrap=on",
        "Probe interpreter/package availability at task start and bootstrap once, not per-failure.",
    ),
    "timeout": (
        "command_timeout_wrapper=on",
        "Wrap shell steps in explicit timeouts with non-interactive flags injected.",
    ),
    "hung": (
        "command_timeout_wrapper=on",
        "Wrap shell steps in explicit timeouts with non-interactive flags injected.",
    ),
}


@dataclass(slots=True)
class Hypothesis:
    """One proposed, evidence-backed improvement theory."""

    hypothesis_id: str
    statement: str
    independent_variable: str
    confidence: str  # "correlational" | "observational"
    evidence: dict[str, object]

    def to_row(self) -> dict[str, object]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "state": "proposed",
            "statement": self.statement,
            "independent_variable": self.independent_variable,
            "confidence": self.confidence,
            "evidence": self.evidence,
        }


def _hyp_id(rule_key: str) -> str:
    digest = hashlib.sha256(rule_key.encode()).hexdigest()[:12]
    return f"hyp_trc_{digest}"


def _exemplar_rows(result: MiningResult, pattern: str) -> list[dict[str, str]]:
    return [e.to_row() for e in result.exemplars.get(pattern, [])]


def _labeled_groups_ok(result: MiningResult) -> bool:
    return (
        result.outcome_counts.get("success", 0) >= MIN_GROUP
        and result.outcome_counts.get("failure", 0) >= MIN_GROUP
    )


#: Per-dataset gap needed when the pooled gap is diluted (Simpson guard).
MIN_DATASET_LIFT = 0.15

#: Relative-lift alternative: failure prevalence at least this multiple of
#: success prevalence (and above a floor) also counts as signal.
MIN_RELATIVE_LIFT = 2.0
RELATIVE_LIFT_FLOOR = 0.05


def _best_dataset_signal(result: MiningResult, pattern: str) -> tuple[str, dict[str, float]] | None:
    """Strongest per-dataset prevalence gap with both groups big enough."""
    best: tuple[str, dict[str, float]] | None = None
    for dataset, stats in result.pattern_lift_by_dataset.get(pattern, {}).items():
        if stats["n_success"] < MIN_GROUP or stats["n_failure"] < MIN_GROUP:
            continue
        gap = stats["failure_prevalence"] - stats["success_prevalence"]
        if gap >= MIN_DATASET_LIFT and (
            best is None or gap > (best[1]["failure_prevalence"] - best[1]["success_prevalence"])
        ):
            best = (dataset, stats)
    return best


def _lift_rule(
    result: MiningResult,
    pattern: str,
    independent_variable: str,
    statement_template: str,
) -> Hypothesis | None:
    """Correlational rule: pattern prevalence gap between failed and successful
    traces. Pooled gap, relative lift, or a single strong labeled dataset all
    count (pooling corpora with different base rates dilutes real per-corpus
    signal); falls back to an observational claim on unlabeled corpora."""
    lift = result.pattern_lift.get(pattern, {})
    failure_prev = lift.get("failure_prevalence")
    success_prev = lift.get("success_prevalence")

    if failure_prev is not None and success_prev is not None and _labeled_groups_ok(result):
        gap = failure_prev - success_prev
        relative_ok = (
            failure_prev >= RELATIVE_LIFT_FLOOR
            and failure_prev >= MIN_RELATIVE_LIFT * max(success_prev, 1e-9)
        )
        dataset_signal = _best_dataset_signal(result, pattern)
        if gap < MIN_LIFT and not relative_ok and dataset_signal is None:
            return None
        if gap >= MIN_LIFT or relative_ok:
            statement = statement_template.format(
                failure=f"{failure_prev:.0%}", success=f"{success_prev:.0%}"
            )
        else:
            assert dataset_signal is not None
            dataset, stats_ds = dataset_signal
            statement = (
                statement_template.format(
                    failure=f"{stats_ds['failure_prevalence']:.0%}",
                    success=f"{stats_ds['success_prevalence']:.0%}",
                )
                + f" (signal from the {dataset} corpus; pooled gap diluted)"
            )
        confidence = "correlational"
        stats: dict[str, object] = {
            "failure_prevalence": failure_prev,
            "success_prevalence": success_prev,
            "gap": round(gap, 4),
            "relative_lift_fired": relative_ok,
            "n_labeled": result.n_labeled,
            "by_dataset": result.pattern_lift_by_dataset.get(pattern, {}),
        }
    else:
        # Unlabeled corpus: only flag patterns that are outright common.
        affected = len(result.exemplars.get(pattern, []))
        prevalence = _unlabeled_prevalence(result, pattern)
        if prevalence < 0.20 or affected == 0:
            return None
        statement = (
            f"{prevalence:.0%} of traces exhibit {pattern.replace('_', ' ')} "
            "(no usable labeled success/failure contrast; observational)."
        )
        confidence = "observational"
        stats = {"prevalence": prevalence, "n_traces": result.n_traces}

    return Hypothesis(
        hypothesis_id=_hyp_id(f"lift:{pattern}"),
        statement=statement,
        independent_variable=independent_variable,
        confidence=confidence,
        evidence={
            "rule": f"lift:{pattern}",
            "stats": stats,
            "exemplars": _exemplar_rows(result, pattern),
        },
    )


def _unlabeled_prevalence(result: MiningResult, pattern: str) -> float:
    if not result.per_trace:
        return 0.0
    from omniagentos.tracelab.mine import PATTERN_PREDICATES

    metric_name, threshold = PATTERN_PREDICATES[pattern]
    if pattern == "no_verification":
        hits = sum(1 for m in result.per_trace if m.verification_calls == 0 and m.n_tool_calls > 0)
    else:
        hits = sum(1 for m in result.per_trace if getattr(m, metric_name) >= threshold)
    return round(hits / len(result.per_trace), 4)


def generate_hypotheses(result: MiningResult) -> list[Hypothesis]:
    """Apply every threshold-gated rule to the mining result."""
    hypotheses: list[Hypothesis] = []

    rules = [
        (
            "error_streak_ge_3",
            "error_circuit_breaker=2",
            "Traces with ≥3 consecutive tool errors appear in {failure} of failed vs "
            "{success} of successful traces; breaking the streak after 2 consecutive failures "
            "(reframe, escalate, or ask) should raise completion rate.",
        ),
        (
            "repeat_call_ge_3",
            "repeat_call_detector=on",
            "Re-issuing the same tool call ≥3 times in a row occurs in {failure} of failed vs "
            "{success} of successful traces; a repeat-call detector that forces a strategy "
            "change should reduce stuck loops.",
        ),
        (
            "giant_result_present",
            "tool_output_cap_chars=20000",
            "Oversized tool results (≥20k chars) occur in {failure} of failed vs {success} of "
            "successful traces; capping/summarizing tool output before context append should "
            "improve downstream reasoning.",
        ),
        (
            "no_verification",
            "mandatory_verification_gate=on",
            "Absence of any verification step (tests/lint/build) occurs in {failure} of failed "
            "vs {success} of successful traces; requiring one verification call before "
            "completion should raise success rate.",
        ),
    ]
    for pattern, independent_variable, template in rules:
        hypothesis = _lift_rule(result, pattern, independent_variable, template)
        if hypothesis:
            hypotheses.append(hypothesis)

    for taxon, (independent_variable, sketch) in TAXON_INTERVENTIONS.items():
        count = result.taxon_counts.get(taxon, 0)
        if count < MIN_TAXON_COUNT:
            continue
        classified_total = sum(v for k, v in result.taxon_counts.items() if k != "unclassified")
        share = count / max(1, classified_total)
        hypotheses.append(
            Hypothesis(
                hypothesis_id=_hyp_id(f"taxon:{taxon}"),
                statement=(
                    f"'{taxon}' accounts for {count} classified tool errors "
                    f"({share:.0%} of all classified) across {result.n_traces} traces. "
                    f"Intervention: {sketch}"
                ),
                independent_variable=independent_variable,
                confidence="observational",
                evidence={
                    "rule": f"taxon:{taxon}",
                    "stats": {
                        "taxon_count": count,
                        "taxon_share": round(share, 4),
                        "by_outcome": {
                            outcome: counts.get(taxon, 0)
                            for outcome, counts in result.taxon_counts_by_outcome.items()
                        },
                    },
                },
            )
        )

    return hypotheses


def write_hypotheses_json(hypotheses: list[Hypothesis], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "hypotheses.json"
    path.write_text(json.dumps([h.to_row() for h in hypotheses], indent=2))
    return path


def emit_to_improvement_lane(hypotheses: list[Hypothesis], db_path: Path) -> int:
    """Upsert hypotheses as ``proposed`` rows in `configtest_hypotheses`.

    Existing rows keep their lifecycle state — only evidence and updated_at are
    refreshed, so a re-run never demotes a hypothesis the lane already tested.
    """
    now = datetime.now(UTC).isoformat()
    written = 0
    with sqlite3.connect(db_path) as conn:
        for hypothesis in hypotheses:
            evidence_json = json.dumps(
                {
                    "statement": hypothesis.statement,
                    "confidence": hypothesis.confidence,
                    **hypothesis.evidence,
                }
            )
            cursor = conn.execute(
                "INSERT INTO configtest_hypotheses "
                "(hypothesis_id, state, independent_variable, evidence_json, "
                " created_at, updated_at) "
                "VALUES (?, 'proposed', ?, ?, ?, ?) "
                "ON CONFLICT(hypothesis_id) DO UPDATE SET "
                "independent_variable = excluded.independent_variable, "
                "evidence_json = excluded.evidence_json, updated_at = excluded.updated_at",
                (
                    hypothesis.hypothesis_id,
                    hypothesis.independent_variable,
                    evidence_json,
                    now,
                    now,
                ),
            )
            written += cursor.rowcount
    return written
