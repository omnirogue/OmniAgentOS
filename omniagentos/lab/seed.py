"""Idempotent demo seed data for the live self-improvement engine."""

from __future__ import annotations

from typing import Any

from omniagentos.lab import surfaces
from omniagentos.lab.contracts import EvalCase, EvalSplit, EvalSuite, MetricSpec, SurfaceKind

DEMO_DISCIPLINE = "general"
DEMO_SUITE_ID = "evs_demo_general_v1"
_EXPECTED = {
    "evc_demo_dev_1": {"value": "concise"},
    "evc_demo_dev_2": {"value": "accurate"},
    "evc_demo_held_1": {"value": "safe"},
    "evc_demo_held_2": {"value": "useful"},
}


def _ensure_suite(store: Any) -> None:
    if store.get_eval_suite(DEMO_SUITE_ID) is not None:
        return
    store.create_eval_suite(
        EvalSuite(
            id=DEMO_SUITE_ID,
            discipline=DEMO_DISCIPLINE,
            metrics=[MetricSpec(name="accuracy", role="primary")],
            dataset_hash="demo-general-v1",
        )
    )
    for case_id, expected in _EXPECTED.items():
        held_out = "_held_" in case_id
        store.add_eval_case(
            EvalCase(
                id=case_id,
                suite=DEMO_SUITE_ID,
                split=EvalSplit.HELD_OUT if held_out else EvalSplit.DEV,
                input={"instruction": f"Return the word for {case_id}."},
                expected=None if held_out else expected,
                rubric="Return exactly one requested word.",
            )
        )


def _ensure_prompt_champion(store: Any) -> None:
    if store.get_champion(DEMO_DISCIPLINE, SurfaceKind.PROMPT.value) is not None:
        return
    prompt = surfaces.version_prompt(
        store,
        DEMO_DISCIPLINE,
        "demo-worker",
        "Answer accurately, safely, and concisely. Return only the requested value.",
    )
    surfaces.seed_champion(store, DEMO_DISCIPLINE, SurfaceKind.PROMPT, prompt.id)


def _ensure_genome_champion(store: Any) -> None:
    kind = SurfaceKind.ORCHESTRATION_GENOME
    if store.get_champion(DEMO_DISCIPLINE, kind.value) is not None:
        return
    genome = surfaces.version_genome(
        store,
        DEMO_DISCIPLINE,
        {
            "archetype": "solo",
            "roles": [{"name": "worker", "agent": "mock", "model": "mock-v1"}],
            "flow": [{"stage": "answer", "kind": "generate", "role": "worker"}],
            "judge": {"blind": True, "dimensions": ["accuracy"]},
            "review": {"adversarial": False},
            "budget": {"tokens": 1000},
        },
    )
    surfaces.seed_champion(store, DEMO_DISCIPLINE, kind, genome.id)


def ensure_demo_seeded(store: Any, grader: Any | None = None) -> None:
    """Ensure one runnable prompt/genome discipline, safely repeatable."""
    _ensure_suite(store)
    _ensure_prompt_champion(store)
    _ensure_genome_champion(store)
    if grader is not None:
        grader.put_expected_many(_EXPECTED)


__all__ = ["DEMO_DISCIPLINE", "DEMO_SUITE_ID", "ensure_demo_seeded"]
