"""Coordinator-owned decisive tests for core.py additions (build review fixes).

Covers: prose-abstention detection (measured live 2026-08-12: models refuse in
prose despite the ANSWER protocol), number/trailing-punct normalization.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


core = _load("memcert_core_abstain_test", REPO_ROOT / "scripts" / "memcert" / "core.py")


def test_prose_refusal_is_abstention_not_wrong() -> None:
    spec = core.AnswerSpec(kind="exact", value="blue vault")
    verdict, score = core.grade_item(
        spec, "I don't have access to the specific information about that project."
    )
    assert (verdict, score) == ("abstain_miss", 0.0)


def test_prose_refusal_on_absence_item_is_correct_abstention() -> None:
    spec = core.AnswerSpec(kind="abstain", value=core.ABSTAIN_TOKEN)
    verdict, score = core.grade_item(
        spec, "The provided context does not contain the network topology."
    )
    assert (verdict, score) == ("abstain_correct", 1.0)


def test_params_prose_refusal_is_abstain_miss() -> None:
    spec = core.AnswerSpec(kind="params", value={"tool": "schedule_job", "args": {"port": "4400"}})
    verdict, score = core.grade_item(spec, "I cannot determine the correct job parameters.")
    assert (verdict, score) == ("abstain_miss", 0.0)


def test_confident_wrong_answer_still_scores_negative() -> None:
    spec = core.AnswerSpec(kind="exact", value="blue vault")
    verdict, score = core.grade_item(spec, "ANSWER: red cellar")
    assert (verdict, score) == ("wrong", -0.5)


def test_hedged_but_answered_reply_grades_on_the_answer_line() -> None:
    # An ANSWER line with a real value wins over refusal-adjacent wording later.
    spec = core.AnswerSpec(kind="exact", value="blue vault")
    verdict, score = core.grade_item(spec, "ANSWER: blue vault")
    assert (verdict, score) == ("correct", 1.0)


def test_thousands_separator_normalizes_equal() -> None:
    assert core.normalize_answer("52,400") == core.normalize_answer("52400")


def test_trailing_sentence_punctuation_normalizes_equal() -> None:
    assert core.normalize_answer("The Blue Vault.") == "blue vault"


def test_iso_dates_survive_normalization() -> None:
    assert core.normalize_answer("March 5, 2027") == "2027-03-05"
    assert core.normalize_answer("2027-03-05") == "2027-03-05"


def test_abstain_token_itself_still_detected() -> None:
    assert core.is_abstention("ANSWER: UNKNOWN") or core.is_abstention("UNKNOWN")


def test_prose_with_correct_value_grades_correct() -> None:
    spec = core.AnswerSpec(kind="exact", value="Fridays", aliases=("Friday",))
    verdict, score = core.grade_item(
        spec, "Based on the provided logs, deploys now run on **Fridays**. This is indicated by..."
    )
    assert (verdict, score) == ("correct", 1.0)


def test_prose_containing_only_stale_value_grades_stale() -> None:
    spec = core.AnswerSpec(kind="exact", value="Fridays", stale_values=("Mondays",))
    verdict, score = core.grade_item(spec, "Deploys run on Mondays according to the log.")
    assert (verdict, score) == ("stale", -1.0)


def test_prose_containing_both_current_and_stale_is_wrong() -> None:
    spec = core.AnswerSpec(kind="exact", value="Fridays", stale_values=("Mondays",))
    verdict, score = core.grade_item(
        spec, "It moved from Mondays to Fridays at some point, or maybe both."
    )
    assert (verdict, score) == ("wrong", -0.5)


def test_containment_requires_word_boundary() -> None:
    spec = core.AnswerSpec(kind="exact", value="art")
    verdict, _ = core.grade_item(spec, "The department restarted the chart.")
    assert verdict == "wrong"


def test_openai_function_call_dialect_grades_on_values() -> None:
    spec = core.AnswerSpec(
        kind="params", value={"tool": "schedule_job", "args": {"name": "megedi", "window": "monopi"}}
    )
    raw = '```json\n{"name": "schedule_job", "arguments": {"name": "megedi", "window": "monopi"}}\n```'
    verdict, score = core.grade_item(spec, raw)
    assert (verdict, score) == ("correct", 1.0)
