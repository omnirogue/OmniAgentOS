from __future__ import annotations

import pytest

from omniagentos.vaultgraph import FactClass, classify_fact
from omniagentos.vaultgraph.classify import classify_against_many


def test_identical_fact_is_duplicate() -> None:
    verdict = classify_fact(
        "Model A context window is 500000 tokens.",
        "model a context window is 500000 tokens",
    )
    assert verdict.label is FactClass.DUPLICATE


def test_unrelated_fact_is_new() -> None:
    verdict = classify_fact(
        "Model A context window is 500000 tokens.",
        "Composting turns kitchen scraps into soil.",
    )
    assert verdict.label is FactClass.NEW


def test_changed_number_same_subject_is_update() -> None:
    verdict = classify_fact(
        "Model A warm latency measured at 51932 ms.",
        "Model A warm latency measured at 42000 ms.",
    )
    assert verdict.label is FactClass.UPDATE
    assert "51932" in verdict.reason


def test_opposed_negation_is_contradiction() -> None:
    verdict = classify_fact(
        "Model A supports streaming responses.",
        "Model A does not support streaming responses.",
    )
    assert verdict.label is FactClass.CONTRADICTION


def test_refined_wording_same_subject_is_update() -> None:
    verdict = classify_fact(
        "Model A is strong at coding tasks.",
        "Model A is strong at coding and debugging tasks reliably.",
    )
    assert verdict.label is FactClass.UPDATE


def test_classify_against_many_flags_contradiction_over_update() -> None:
    existing = [
        "Model A warm latency measured at 51932 ms.",
        "Model A supports streaming responses.",
    ]
    verdict = classify_against_many(existing, "Model A does not support streaming responses.")
    assert verdict.label is FactClass.CONTRADICTION


def test_classify_against_many_short_circuits_duplicate() -> None:
    existing = ["Model A supports streaming responses."]
    verdict = classify_against_many(existing, "Model A supports streaming responses.")
    assert verdict.label is FactClass.DUPLICATE


def test_classify_against_empty_is_new() -> None:
    assert classify_against_many([], "anything").label is FactClass.NEW


# -- F1: contradiction detection hardening ------------------------------------


def test_antonym_same_polarity_is_contradiction() -> None:
    # "supports" vs "blocks": no negation on either side, opposite meaning.
    verdict = classify_fact("Model A supports streaming.", "Model A blocks streaming.")
    assert verdict.label is FactClass.CONTRADICTION


def test_double_negation_is_contradiction() -> None:
    # "does not support" (odd negations) vs "is not without support" (even).
    verdict = classify_fact(
        "Model A does not support streaming.",
        "Model A is not without support for streaming.",
    )
    assert verdict.label is FactClass.CONTRADICTION


def test_negated_antonym_agrees_not_contradiction() -> None:
    # "does not support" and "blocks" assert the same thing -> not a contradiction.
    verdict = classify_fact("Model A does not support streaming.", "Model A blocks streaming.")
    assert verdict.label is not FactClass.CONTRADICTION


def test_punctuation_only_difference_is_duplicate() -> None:
    verdict = classify_fact("Model A supports streaming.", "Model A supports streaming!")
    assert verdict.label is FactClass.DUPLICATE


def test_numeric_conflict_same_subject_is_update() -> None:
    verdict = classify_fact(
        "Model A context window is 200000 tokens.",
        "Model A context window is 1000000 tokens.",
    )
    assert verdict.label is FactClass.UPDATE
    assert "200000" in verdict.reason


def test_unicode_accented_antonym_is_contradiction() -> None:
    verdict = classify_fact("Café supports imports.", "Café blocks imports.")
    assert verdict.label is FactClass.CONTRADICTION


def test_unicode_nfc_nfd_equivalent_is_duplicate() -> None:
    nfc = "Café is fast."  # composed é
    nfd = "Café is fast."  # decomposed e + combining acute
    assert classify_fact(nfc, nfd).label is FactClass.DUPLICATE


@pytest.mark.parametrize("existing, incoming", [("", ""), ("", "the"), ("fact", "   ")])
def test_empty_input_is_rejected(existing: str, incoming: str) -> None:
    with pytest.raises(ValueError):
        classify_fact(existing, incoming)


def test_classify_against_many_rejects_empty_incoming() -> None:
    with pytest.raises(ValueError):
        classify_against_many(["some fact"], "")


# -- F2: contradiction must not be masked by a duplicate elsewhere ------------


def test_duplicate_and_contradiction_in_set_surfaces_contradiction() -> None:
    existing = [
        "Model A supports streaming responses.",
        "Model A does not support streaming responses.",
    ]
    # The incoming fact duplicates the first existing fact, but contradicts the
    # second — the contradiction must win, not be short-circuited by the dup.
    verdict = classify_against_many(existing, "Model A supports streaming responses.")
    assert verdict.label is FactClass.CONTRADICTION


# -- Empty-denominator / non-result-as-favourable -----------------------------
# _jaccard({}, {}) used to return 1.0 (vacuous identity). That made two
# contentless statements look like a perfect subject match, so stopword-only
# and number-only pairs were scored UPDATE with overlap 1.0 — a zero-signal
# run taught as a strong same-subject update.


def test_stopword_only_statements_are_not_same_subject_update() -> None:
    """Stopwords extract to empty token sets; must not flaunt perfect overlap.

    Counterfeit: keep empty∩empty Jaccard at 1.0 (or any value ≥ threshold)
    and special-case only one phrase — this asserts NEW *and* overlap == 0.0.
    """
    verdict = classify_fact("This is that", "It are the")
    assert verdict.label is FactClass.NEW
    assert verdict.overlap == 0.0


def test_pure_number_change_without_subject_is_not_update() -> None:
    """Bare numbers leave no subject tokens; a numeric delta is not an UPDATE.

    Counterfeit: fix only stopword pairs, leave empty-subject numeric path
    claiming 'same subject (overlap 1.00); numeric value changed'.
    """
    verdict = classify_fact("100", "200")
    assert verdict.label is FactClass.NEW
    assert verdict.overlap == 0.0


def test_punctuation_only_is_rejected_not_duplicate() -> None:
    """Normalize strips sentence punctuation — '!!!' and '???' both become ''.

    Counterfeit: reject only whitespace empties via .strip(), still treat
    punctuation-only as DUPLICATE via empty-string equality after normalize.
    """
    with pytest.raises(ValueError):
        classify_fact("!!!", "???")


def test_jaccard_empty_sets_is_zero_not_one() -> None:
    """Direct unit of the empty-denominator counterfeit surface."""
    from omniagentos.vaultgraph.classify import _jaccard

    assert _jaccard(set(), set()) == 0.0
    # One empty / one non-empty still 0; two non-empty still real Jaccard.
    assert _jaccard(set(), {"a"}) == 0.0
    assert _jaccard({"a"}, {"a"}) == 1.0
    assert _jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1 / 3)
