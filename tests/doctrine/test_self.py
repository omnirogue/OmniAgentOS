"""Self-tests: doctrine helpers that cannot detect their own no-op are worthless.

Proves:
1. ``revert_test`` FAILS LOUDLY when the mutation does not break the named test.
2. ``revert_test`` succeeds (and returns verbatim failure text) for a real break.
3. ``counterfeit_test`` catches the known favourable-constant fake.
4. ``counterfeit_test`` FAILS LOUDLY when the suite is blind to the fake.
5. Trap guards fire on pytest|tail, assertion-count drop, and new suppressions.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.doctrine import (
    DoctrineError,
    assert_assertion_count_not_dropped,
    assert_no_new_suppressions,
    assert_no_pytest_piped_to_tail,
    count_assertions,
    counterfeit_test,
    detect_pytest_piped_to_tail,
    revert_test,
)
from tests.doctrine._mutate import TextReplace
from tests.doctrine.traps import count_suppressions

_FIXTURES = Path(__file__).resolve().parent / "_fixtures"
_SUBJECT = _FIXTURES / "subject.py"
_NODE_EMPTY = "tests/doctrine/_fixtures/test_subject.py::test_empty_denominator_returns_none"
_NODE_POSITIVE = "tests/doctrine/_fixtures/test_subject.py::test_positive_denominator_computes"

# Real break of the empty-denominator claim (reverts the fix).
_BREAK_EMPTY = TextReplace(
    old="    if denominator == 0:\n        return None",
    new="    if denominator == 0:\n        return 0.0",
)

# Favourable counterfeit: a different "healthy" constant (1.0 = perfect).
_COUNTERFEIT_FAVOURABLE = TextReplace(
    old="    if denominator == 0:\n        return None",
    new="    if denominator == 0:\n        return 1.0",
)

# No-op: whitespace-only change that cannot break any assertion.
_NOOP = TextReplace(
    old='"""Return numerator/denominator, or None when the denominator is empty."""',
    new='"""Return numerator/denominator, or None when the denominator is empty. """',
)


# ---------------------------------------------------------------------------
# revert_test self-proofs
# ---------------------------------------------------------------------------


def test_revert_helper_fails_loudly_on_noop_mutation() -> None:
    """A mutation that does not break the test must not be reportable as a revert-test."""
    with pytest.raises(DoctrineError, match="DECORATION"):
        revert_test(
            target=_SUBJECT,
            mutation=_NOOP,
            nodeid=_NODE_EMPTY,
        )


def test_revert_helper_reports_verbatim_failure_when_mutation_bites() -> None:
    report = revert_test(
        target=_SUBJECT,
        mutation=_BREAK_EMPTY,
        nodeid=_NODE_EMPTY,
    )
    assert report.mutated_failure.failed
    assert report.restored_pass.passed
    text = report.failure_text
    # Verbatim pytest body must be present — not a summary-only placeholder.
    assert text.strip(), "failure_text must not be empty"
    assert "AssertionError" in text or "assert" in text.lower()
    assert "None" in text or "0.0" in text or "is None" in text
    assert "REVERT-TEST OK" in report.summary()


def test_revert_helper_restores_file_bytes() -> None:
    before = _SUBJECT.read_bytes()
    try:
        revert_test(
            target=_SUBJECT,
            mutation=_BREAK_EMPTY,
            nodeid=_NODE_EMPTY,
        )
    finally:
        after = _SUBJECT.read_bytes()
    assert after == before, "revert_test must restore the target exactly"


# ---------------------------------------------------------------------------
# counterfeit_test self-proofs
# ---------------------------------------------------------------------------


def test_counterfeit_helper_catches_favourable_constant() -> None:
    """Known fake: return 1.0 instead of None on empty denominator."""
    report = counterfeit_test(
        target=_SUBJECT,
        counterfeit=_COUNTERFEIT_FAVOURABLE,
        nodeid=_NODE_EMPTY,
    )
    assert report.counterfeit_failure.failed
    assert report.restored_pass is not None and report.restored_pass.passed
    assert report.failure_text.strip()
    assert "COUNTERFEIT-TEST OK" in report.summary()


def test_counterfeit_helper_fails_when_suite_is_blind() -> None:
    """Positive-denominator test does not observe the empty-denominator branch.

    Applying the empty-denominator counterfeit must NOT break
    ``test_positive_denominator_computes`` — and the helper must report that
    the counterfeit was uncaught for *that* nodeid.
    """
    with pytest.raises(DoctrineError, match="COUNTERFEIT UNCAUGHT"):
        counterfeit_test(
            target=_SUBJECT,
            counterfeit=_COUNTERFEIT_FAVOURABLE,
            nodeid=_NODE_POSITIVE,
        )


def test_counterfeit_helper_restores_file_bytes() -> None:
    before = _SUBJECT.read_bytes()
    try:
        counterfeit_test(
            target=_SUBJECT,
            counterfeit=_COUNTERFEIT_FAVOURABLE,
            nodeid=_NODE_EMPTY,
        )
    finally:
        after = _SUBJECT.read_bytes()
    assert after == before


# ---------------------------------------------------------------------------
# trap guards
# ---------------------------------------------------------------------------


def test_trap_detects_pytest_piped_to_tail() -> None:
    bad_commands = [
        "uv run pytest -q tests/acceptance/test_17_control_plane.py -rx | tail -1",
        "pytest -q tests/foo | tail -5",
        "python -m pytest tests/x -q | head -3",
        "cd /tmp && pytest -q | tail -1",
    ]
    for cmd in bad_commands:
        hits = detect_pytest_piped_to_tail(cmd)
        assert hits, f"should detect pipe trap in: {cmd!r}"
        with pytest.raises(DoctrineError, match="piped into tail/head"):
            assert_no_pytest_piped_to_tail(cmd, context="self-test")


def test_trap_allows_pytest_without_pipe() -> None:
    good = [
        "uv run pytest -q tests/doctrine",
        "python -m pytest tests/doctrine/test_self.py -vv --tb=short",
        "pytest -q -x tests/foo  # review tail of log separately",
    ]
    for cmd in good:
        assert detect_pytest_piped_to_tail(cmd) == []
        assert_no_pytest_piped_to_tail(cmd)


def test_trap_assertion_count_drop() -> None:
    before = """
def test_claim():
    assert rate(0, 0) is None
    assert rate(1, 0) is None
    assert True
"""
    after = """
def test_claim():
    assert True
"""
    pre = count_assertions(before)
    assert pre.total == 3
    with pytest.raises(DoctrineError, match="assertion count DROPPED"):
        assert_assertion_count_not_dropped(before, after, path="test_claim.py")

    # Equal count is fine.
    same = assert_assertion_count_not_dropped(before, before)
    assert same.total == 3


def test_trap_new_type_ignore_or_noqa() -> None:
    before = "def f(x):\n    return x + 1\n"
    after_ignore = "def f(x):  # type: ignore[arg-type]\n    return x + 1\n"
    after_noqa = "def f(x):\n    return x + 1  # noqa: F841\n"

    with pytest.raises(DoctrineError, match="new suppression"):
        assert_no_new_suppressions(before, after_ignore, path="mod.py")
    with pytest.raises(DoctrineError, match="new suppression"):
        assert_no_new_suppressions(before, after_noqa, path="mod.py")

    # No new suppressions is fine.
    assert_no_new_suppressions(before, before)
    already = "def f(x):  # type: ignore[arg-type]\n    return x + 1\n"
    assert_no_new_suppressions(already, already)
    counts = count_suppressions(already)
    assert counts.type_ignore == 1
