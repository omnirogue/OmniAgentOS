"""Test doctrine: executable helpers that prove a suite binds to real behaviour.

Import from here in lane briefs and review scripts::

    from tests.doctrine import (
        revert_test,
        counterfeit_test,
        assert_no_pytest_piped_to_tail,
        assert_assertion_count_not_dropped,
        assert_no_new_suppressions,
        DoctrineError,
    )

Run the self-suite with ``make test-doctrine``.
"""

from __future__ import annotations

from tests.doctrine._mutate import TextReplace
from tests.doctrine.counterfeit import CounterfeitReport, counterfeit_test
from tests.doctrine.errors import DoctrineError
from tests.doctrine.revert import RevertReport, revert_test
from tests.doctrine.traps import (
    assert_assertion_count_not_dropped,
    assert_no_new_suppressions,
    assert_no_pytest_piped_to_tail,
    count_assertions,
    count_suppressions,
    detect_pytest_piped_to_tail,
)

__all__ = [
    "CounterfeitReport",
    "DoctrineError",
    "RevertReport",
    "TextReplace",
    "assert_assertion_count_not_dropped",
    "assert_no_new_suppressions",
    "assert_no_pytest_piped_to_tail",
    "count_assertions",
    "count_suppressions",
    "counterfeit_test",
    "detect_pytest_piped_to_tail",
    "revert_test",
]
