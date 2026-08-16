"""A lightweight, reproducible evaluation suite for knowledge recall."""

from __future__ import annotations

from typing import Any

from omniagentos.knowledge.recall import recall
from omniagentos.knowledge.store import KnowledgeStore


def _mean_defined(values: list[float | None]) -> float | None:
    """Macro-average only over defined rates; empty denominator → unknown (None).

    Returning 0.0 for an empty suite or empty denominator is a non-result dressed
    as a measured score — optimizers can learn from it. None is not a number.
    """
    defined = [value for value in values if value is not None]
    if not defined:
        return None
    return sum(defined) / len(defined)


def eval_suite(
    store: KnowledgeStore,
    cases: list[dict[str, Any]] | None = None,
    *,
    k: int = 5,
    discipline: str | None = None,
) -> dict[str, Any]:
    """Run labelled recall cases and report macro precision@k/recall@k.

    A case is intentionally plain data so it can live in a dev fixture or a lab
    dataset: ``{"id": "...", "input": {"prompt": "..."},
    "expected_fact_ids": [1, 2]}``.  ``expected`` and ``expected_ids`` are accepted
    aliases for easy use from existing lab fixtures.

    Rates over an empty denominator are ``None`` (unknown), never ``0.0``. An empty
    case list likewise reports unknown macro metrics with ``cases: 0``.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    case_rows: list[dict[str, Any]] = []
    precisions: list[float | None] = []
    recalls: list[float | None] = []
    for index, case in enumerate(cases or []):
        input_data = dict(case.get("input") or {})
        prompt = str(input_data.get("prompt", case.get("prompt", "")))
        expected = case.get("expected_fact_ids", case.get("expected_ids", case.get("expected", [])))
        expected_ids = {int(value) for value in expected or []}
        result = recall(
            store,
            prompt=prompt,
            discipline=input_data.get("discipline", discipline),
            agent_id=input_data.get("agent_id"),
        )
        retrieved = [item.fact.id for item in result.facts[:k]]
        hits = len(expected_ids.intersection(retrieved))
        # Empty denominator → unknown. Do not invent 0.0 for optimizers to learn.
        precision: float | None = hits / len(retrieved) if retrieved else None
        recall_at_k: float | None = hits / len(expected_ids) if expected_ids else None
        precisions.append(precision)
        recalls.append(recall_at_k)
        case_rows.append(
            {
                "id": str(case.get("id", f"case-{index + 1}")),
                "input": {"prompt": prompt, **input_data},
                "output": {"fact_ids": retrieved},
                "metrics": {"precision_at_k": precision, "recall_at_k": recall_at_k},
            }
        )
    count = len(case_rows)
    return {
        "name": "knowledge-recall",
        "version": "0.1.0",
        "metrics": {
            "precision_at_k": _mean_defined(precisions),
            "recall_at_k": _mean_defined(recalls),
            "k": k,
            "cases": count,
        },
        "cases": case_rows,
    }


__all__ = ["eval_suite"]
