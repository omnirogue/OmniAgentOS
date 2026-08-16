"""Pure fan-in adjudication — prefer deterministic scores over majority vote.

This module supports flag-gated conflict-preserving synthesis.
Retrieval path for shadow mode synthesis evidence:
    from omniagentos.fanin.adjudicate import get_last_synthesis_evidence
    evidence = get_last_synthesis_evidence()
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from omniagentos.fanin.normalize import content_hash, normalize_candidates
from omniagentos.fanin.types import (
    Candidate,
    FanInMode,
    FanInResult,
    NormalizedCandidate,
)

__all__ = [
    "adjudicate",
    "rank_key",
    "MergeEscalationExhausted",
    "SECOND_SYNTHESIS_WORD_THRESHOLD",
    "get_last_synthesis_evidence",
]

SECOND_SYNTHESIS_WORD_THRESHOLD = 3000

_LAST_SYNTHESIS_EVIDENCE: dict[str, Any] | None = None


class MergeEscalationExhausted(Exception):
    """Exception raised when merge budget is exhausted during synthesis."""

    def __init__(self, attempts: int, reason: str) -> None:
        super().__init__(f"Merge escalation exhausted: {attempts} conflicts. Reason: {reason}")
        self.attempts = attempts
        self.reason = reason


def get_last_synthesis_evidence() -> dict[str, Any] | None:
    """Retrieve the synthesis evidence from the last run in shadow mode.

    The returned dictionary contains:
        - "evidence": The evidence dictionary of the conflict-preserving synthesis.
        - "content": The merged content (including any structured conflicts) of the synthesized candidate.
        - "result": The FanInResult object of the conflict-preserving synthesis.
    """
    return _LAST_SYNTHESIS_EVIDENCE


def rank_key(candidate: NormalizedCandidate) -> tuple[int, float, int, float, str]:
    """Sort key: score desc, confidence desc, content_hash asc.

    ``None`` scores/confidences sort last (worse than any real number).
    """
    score = candidate.score
    conf = candidate.confidence
    return (
        0 if score is not None else 1,
        -(float(score) if score is not None else 0.0),
        0 if conf is not None else 1,
        -(float(conf) if conf is not None else 0.0),
        candidate.content_hash,
    )


def _rank(candidates: Sequence[NormalizedCandidate]) -> list[NormalizedCandidate]:
    return sorted(candidates, key=rank_key)


def _empty(mode: FanInMode, *, needs_replan: bool) -> FanInResult:
    return FanInResult(
        mode=mode,
        selected=(),
        rejected=(),
        decision="no_candidates",
        rationale="No valid candidates after normalization.",
        evidence={"count": 0},
        needs_replan=needs_replan,
    )


def _select_best(
    ranked: Sequence[NormalizedCandidate],
    *,
    mode: FanInMode,
    decision: str,
    rationale: str,
    evidence: dict[str, Any] | None = None,
) -> FanInResult:
    best = ranked[0]
    rejected = tuple(ranked[1:])
    return FanInResult(
        mode=mode,
        selected=(best,),
        rejected=rejected,
        decision=decision,
        rationale=rationale,
        evidence={
            "winner_id": best.id,
            "winner_score": best.score,
            "winner_hash": best.content_hash,
            **(evidence or {}),
        },
        needs_replan=False,
    )


def _synthesize(ranked: Sequence[NormalizedCandidate]) -> FanInResult:
    if not all(isinstance(c.content, dict) for c in ranked):
        return _select_best(
            ranked,
            mode=FanInMode.SYNTHESIZE,
            decision="synthesize_fallback_select",
            rationale="Not all contents are dicts; fell back to SELECT_BEST ranking.",
        )
    # Lowest rank first, highest last so higher scores win on key collision.
    merged: dict[str, Any] = {}
    for cand in reversed(list(ranked)):
        if isinstance(cand.content, dict):
            merged.update(cand.content)
    joined = "|".join(c.content_hash for c in ranked)
    digest = content_hash(merged)
    synth = NormalizedCandidate(
        id=f"synth:{content_hash(joined)[:16]}",
        content=merged,
        content_hash=digest,
        score=ranked[0].score,
        confidence=ranked[0].confidence,
        source="synthesize",
        metadata={"source_ids": [c.id for c in ranked]},
    )
    return FanInResult(
        mode=FanInMode.SYNTHESIZE,
        selected=(synth,),
        rejected=tuple(ranked),
        decision="synthesize",
        rationale=f"Shallow-merged {len(ranked)} dict candidates (higher score wins on key collision).",
        evidence={"source_ids": [c.id for c in ranked], "merged_keys": sorted(merged)},
        needs_replan=False,
    )


def _adjudicate_with_criteria(
    ranked: Sequence[NormalizedCandidate],
    criteria: Mapping[str, Any],
) -> FanInResult:
    pool = list(ranked)
    evidence: dict[str, Any] = {"criteria": dict(criteria)}

    min_score = criteria.get("min_score")
    if isinstance(min_score, (int, float)):
        threshold = float(min_score)
        kept = [c for c in pool if c.score is not None and float(c.score) >= threshold]
        rejected_low = [c for c in pool if c not in kept]
        evidence["min_score"] = threshold
        evidence["filtered_out"] = [c.id for c in rejected_low]
        if not kept:
            return FanInResult(
                mode=FanInMode.ADJUDICATE,
                selected=(),
                rejected=tuple(pool),
                decision="reject_replan",
                rationale=f"No candidate met min_score={threshold}.",
                evidence=evidence,
                needs_replan=True,
            )
        pool = _rank(kept)

    prefer_field = criteria.get("prefer_field")
    if isinstance(prefer_field, str) and prefer_field:
        preferred = [c for c in pool if isinstance(c.content, dict) and c.content.get(prefer_field)]
        if preferred:
            pool = _rank(preferred)
            evidence["prefer_field"] = prefer_field
            evidence["preferred_ids"] = [c.id for c in preferred]

    if criteria.get("require_agreement"):
        hashes = {c.content_hash for c in pool}
        top = pool[0]
        second = pool[1] if len(pool) > 1 else None
        clear_winner = second is None or (
            top.score is not None
            and second.score is not None
            and float(top.score) > float(second.score)
        )
        evidence["require_agreement"] = True
        evidence["distinct_hashes"] = len(hashes)
        if len(hashes) > 1 and not clear_winner:
            return FanInResult(
                mode=FanInMode.ADJUDICATE,
                selected=(),
                rejected=tuple(pool),
                decision="reject_replan",
                rationale="Candidates disagree and no strict score winner under require_agreement.",
                evidence=evidence,
                needs_replan=True,
            )

    return _select_best(
        pool,
        mode=FanInMode.ADJUDICATE,
        decision="adjudicated",
        rationale="Adjudicated by deterministic score/confidence ranking under criteria.",
        evidence=evidence,
    )


def _get_merge_budget(param_budget: int | None = None) -> int:
    """Resolve the merge budget from parameter or environment variable."""
    if param_budget is not None:
        return param_budget
    env_val = os.environ.get("OMNIAGENTOS_FANIN_MERGE_BUDGET")
    if env_val is not None:
        try:
            return int(env_val)
        except ValueError:
            pass
    return 3


def _get_aggregate_word_count(contents: Iterable[Any]) -> int:
    """Compute the aggregate whitespace-token word count of all worker content."""

    def _count_words(val: Any) -> int:
        if val is None:
            return 0
        if isinstance(val, str):
            return len(val.split())
        elif isinstance(val, dict):
            return sum(_count_words(k) + _count_words(v) for k, v in val.items())
        elif isinstance(val, (list, tuple, set)):
            return sum(_count_words(item) for item in val)
        else:
            return len(str(val).split())

    return sum(_count_words(content) for content in contents)


def _run_conflict_preserving_synthesis(
    ranked: Sequence[NormalizedCandidate],
    *,
    merge_budget: int = 3,
    is_second_pass: bool = False,
    aggregate_word_count: int | None = None,
) -> FanInResult:
    """Execute the conflict-preserving synthesis merge.

    Raises ``MergeEscalationExhausted`` unconditionally when the merge
    budget is exceeded — mode-specific handling (enforce propagates,
    shadow records ``escalated=True`` and keeps the legacy merge) is the
    caller's responsibility, not this function's.
    """
    if not all(isinstance(c.content, dict) for c in ranked):
        return _select_best(
            ranked,
            mode=FanInMode.SYNTHESIZE,
            decision="synthesize_fallback_select",
            rationale="Not all contents are dicts; fell back to SELECT_BEST ranking.",
        )

    all_keys: set[str] = set()
    for cand in ranked:
        if isinstance(cand.content, dict):
            all_keys.update(cand.content.keys())

    conflicting_keys = []
    merged_content: dict[str, Any] = {}

    for k in sorted(all_keys):
        cand_values = []
        for rank_idx, cand in enumerate(ranked):
            if isinstance(cand.content, dict) and k in cand.content:
                cand_values.append((rank_idx, cand, cand.content[k]))

        first_val = cand_values[0][2]
        has_conflict = False
        for _, _, val in cand_values[1:]:
            if val != first_val:
                has_conflict = True
                break

        if has_conflict:
            conflicting_keys.append(k)
            merged_content[k] = {
                "primary": first_val,
                "conflict": True,
                "candidates": [
                    {
                        "worker_id": cand.id,
                        "candidate_id": cand.id,
                        "rank": rank_idx,
                        "value": val,
                    }
                    for rank_idx, cand, val in cand_values
                ],
            }
        else:
            merged_content[k] = first_val

    if len(conflicting_keys) > merge_budget:
        raise MergeEscalationExhausted(
            attempts=len(conflicting_keys),
            reason=f"Number of conflicting keys ({len(conflicting_keys)}) exceeds budget ({merge_budget}).",
        )

    joined = "|".join(c.content_hash for c in ranked)
    digest = content_hash(merged_content)
    synth = NormalizedCandidate(
        id=f"synth:{content_hash(joined)[:16]}",
        content=merged_content,
        content_hash=digest,
        score=ranked[0].score,
        confidence=ranked[0].confidence,
        source="synthesize",
        metadata={"source_ids": [c.id for c in ranked]},
    )

    evidence: dict[str, Any] = {
        "source_ids": [c.id for c in ranked],
        "merged_keys": sorted(merged_content.keys()),
        "conflicting_keys": sorted(conflicting_keys),
        "escalated": False,
        "second_synthesis_triggered": False,
    }

    if not is_second_pass:
        word_count = (
            aggregate_word_count
            if aggregate_word_count is not None
            else _get_aggregate_word_count(c.content for c in ranked)
        )
        if word_count >= SECOND_SYNTHESIS_WORD_THRESHOLD:
            evidence["second_synthesis_triggered"] = True
            second_pass_result = _run_conflict_preserving_synthesis(
                [synth],
                merge_budget=merge_budget,
                is_second_pass=True,
            )
            second_pass_result.evidence.update(
                {
                    "second_synthesis_triggered": True,
                    "original_word_count": word_count,
                }
            )
            return second_pass_result

    return FanInResult(
        mode=FanInMode.SYNTHESIZE,
        selected=(synth,),
        rejected=tuple(ranked),
        decision="synthesize",
        rationale=f"Conflict-preserving merged {len(ranked)} dict candidates.",
        evidence=evidence,
        needs_replan=False,
    )


def adjudicate(
    candidates: Iterable[Candidate | Mapping[str, Any]],
    criteria: Mapping[str, Any] | None = None,
    *,
    mode: FanInMode | str = FanInMode.ADJUDICATE,
    merge_budget: int | None = None,
) -> FanInResult:
    """Normalize then apply a fan-in mode. Pure and deterministic.

    Prefer explicit ``score`` fields over majority vote for factual selection.
    Tie-break: higher confidence, then lower content_hash.
    """
    mode_e = FanInMode(mode) if not isinstance(mode, FanInMode) else mode
    synthesis_mode: str | None = None
    original_candidates: tuple[Candidate | Mapping[str, Any], ...] | None = None
    if mode_e is FanInMode.SYNTHESIZE:
        synthesis_mode = os.environ.get("OMNIAGENTOS_FANIN_SYNTHESIS_MODE", "off").lower()
        if synthesis_mode in ("shadow", "enforce"):
            original_candidates = tuple(candidates)
            candidates = original_candidates

    normalized = normalize_candidates(candidates)
    if not normalized:
        needs = mode_e in (FanInMode.REJECT_REPLAN, FanInMode.ADJUDICATE)
        return _empty(mode_e, needs_replan=needs)

    ranked = _rank(normalized)

    if mode_e is FanInMode.UNION:
        return FanInResult(
            mode=FanInMode.UNION,
            selected=tuple(normalized),
            rejected=(),
            decision="union",
            rationale=f"Union of {len(normalized)} unique candidates.",
            evidence={"ids": [c.id for c in normalized]},
            needs_replan=False,
        )

    if mode_e is FanInMode.SELECT_BEST:
        best = ranked[0]
        return _select_best(
            ranked,
            mode=FanInMode.SELECT_BEST,
            decision="selected",
            rationale=(
                f"Selected {best.id!r} by score/confidence/hash ranking (score={best.score!r})."
            ),
        )

    if mode_e is FanInMode.SYNTHESIZE:
        if synthesis_mode not in ("shadow", "enforce"):
            return _synthesize(ranked)

        assert original_candidates is not None
        aggregate_word_count = _get_aggregate_word_count(
            raw.content
            if isinstance(raw, Candidate)
            else raw["content"]
            if "content" in raw
            else raw["body"]
            for raw in original_candidates
        )
        resolved_budget = _get_merge_budget(merge_budget)
        global _LAST_SYNTHESIS_EVIDENCE
        try:
            synth_result = _run_conflict_preserving_synthesis(
                ranked,
                merge_budget=resolved_budget,
                is_second_pass=False,
                aggregate_word_count=aggregate_word_count,
            )
        except MergeEscalationExhausted as exc:
            if synthesis_mode == "enforce":
                raise
            # shadow: record escalation as evidence, keep the legacy merge.
            _LAST_SYNTHESIS_EVIDENCE = {
                "evidence": {
                    "escalated": True,
                    "attempts": exc.attempts,
                    "reason": exc.reason,
                },
                "content": None,
                "result": None,
            }
            return _synthesize(ranked)

        _LAST_SYNTHESIS_EVIDENCE = {
            "evidence": dict(synth_result.evidence),
            "content": synth_result.selected[0].content if synth_result.selected else None,
            "result": synth_result,
        }

        if synthesis_mode == "shadow":
            return _synthesize(ranked)
        else:
            return synth_result

    if mode_e is FanInMode.ADJUDICATE:
        if criteria:
            return _adjudicate_with_criteria(ranked, criteria)
        return _select_best(
            ranked,
            mode=FanInMode.ADJUDICATE,
            decision="adjudicated",
            rationale="Default ADJUDICATE without criteria equals SELECT_BEST.",
        )

    if mode_e is FanInMode.INTEGRATE:
        ordered_ids = [c.id for c in ranked]
        return FanInResult(
            mode=FanInMode.INTEGRATE,
            selected=tuple(normalized),
            rejected=(),
            decision="integrate",
            rationale=f"Integration plan over {len(normalized)} candidates by score rank.",
            evidence={
                "integration_order": ordered_ids,
                "ids": [c.id for c in normalized],
            },
            needs_replan=False,
        )

    return FanInResult(
        mode=FanInMode.REJECT_REPLAN,
        selected=(),
        rejected=tuple(normalized),
        decision="reject_replan",
        rationale="Explicit REJECT_REPLAN: all candidates rejected; replan required.",
        evidence={"rejected_ids": [c.id for c in normalized]},
        needs_replan=True,
    )
