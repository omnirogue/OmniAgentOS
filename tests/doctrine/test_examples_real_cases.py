"""Worked examples using real cases from this repo — copy these shapes.

Each example names:
- the **claim** (behavioural),
- the **revert mutation** (breaks the fix),
- the **counterfeit** (fakes the fix favourably),
- and runs both helpers against the doctrine fixture that encodes that claim.

Production anchors (read-only references — this file does not edit omniagentos/):
- ``omniagentos/tracelab/metrics.py`` — ``tool_error_rate`` / ``recovery_rate``
- ``tests/tracelab/test_metrics.py::test_empty_denominator_rates_are_unknown_through_row_serialization``
- ``omniagentos/pulse/aggregator.py`` — ``loops.acceptance`` empty settled → None
- ``tests/pulse/test_aggregator.py`` — empty rate metrics must be None
- Fan-out width inflation (ISSUES-REGISTER): counterfeit ``worker_count = max(..., 2)``
  passed 10/10 on a weak suite — shape shown in ``test_example_width_floor_counterfeit_shape``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.doctrine import DoctrineError, counterfeit_test, revert_test
from tests.doctrine._mutate import TextReplace

_SUBJECT = Path(__file__).resolve().parent / "_fixtures" / "subject.py"
_NODE_EMPTY = "tests/doctrine/_fixtures/test_subject.py::test_empty_denominator_returns_none"


def test_example_empty_denominator_rate_returns_none() -> None:
    """Claim: rate over an empty denominator returns None.

    Revert: ``return None`` → ``return 0.0`` (vacuous "no errors" on a blank trace).
    Counterfeit: ``return None`` → ``return 1.0`` (vacuous "perfect" — different
    favourable constant; dashboards that treat None as "—" will show 100% instead).

    Real suite twin: ``tests/tracelab/test_metrics.py`` empty-denominator test.
    """
    revert = revert_test(
        target=_SUBJECT,
        mutation=TextReplace(
            old="    if denominator == 0:\n        return None",
            new="    if denominator == 0:\n        return 0.0",
        ),
        nodeid=_NODE_EMPTY,
    )
    assert "assert" in revert.failure_text.lower() or "Error" in revert.failure_text

    counterfeit = counterfeit_test(
        target=_SUBJECT,
        counterfeit=TextReplace(
            old="    if denominator == 0:\n        return None",
            new="    if denominator == 0:\n        return 1.0",
        ),
        nodeid=_NODE_EMPTY,
    )
    assert counterfeit.counterfeit_failure.failed


def test_example_width_floor_counterfeit_shape() -> None:
    """Shape only: fan-out width inflation counterfeit that passed 10/10.

    Real defect (ISSUES-REGISTER / WAVE-2 W2.2): a suite that only asserts
    ``worker_count >= 2`` on a wide DAG cannot catch
    ``worker_count = max(worker_count, 2)``. The decisive counterfeit needs a
    **strict chain** fixture that must still return 1.

    This example encodes the same logic in a pure function so the doctrine
    helpers can be copied without depending on the allocation simulator.
    """
    width_src = Path(__file__).resolve().parent / "_fixtures" / "width_subject.py"
    width_test = "tests/doctrine/_fixtures/test_width_subject.py::test_chain_stays_sequential"
    if not width_src.is_file():
        pytest.fail("width fixture missing — doctrine package incomplete")

    # Revert: break antichain measure back to "root-layer only" (always 1).
    revert_test(
        target=width_src,
        mutation=TextReplace(
            old="    return max(levels.values()) if levels else 1",
            new="    return 1  # root-layer only (O-11 shape)",
        ),
        nodeid="tests/doctrine/_fixtures/test_width_subject.py::test_wide_body_is_parallel",
    )

    # Counterfeit: hard floor of 2 — fools ">= 2" on wide graphs, breaks chains.
    counterfeit_test(
        target=width_src,
        counterfeit=TextReplace(
            old="    return max(levels.values()) if levels else 1",
            new="    return max(2, max(levels.values()) if levels else 1)",
        ),
        nodeid=width_test,
    )

    # Prove a weak suite would be blind: wide-only node does NOT catch the floor.
    with pytest.raises(DoctrineError, match="COUNTERFEIT UNCAUGHT"):
        counterfeit_test(
            target=width_src,
            counterfeit=TextReplace(
                old="    return max(levels.values()) if levels else 1",
                new="    return max(2, max(levels.values()) if levels else 1)",
            ),
            nodeid="tests/doctrine/_fixtures/test_width_subject.py::test_wide_body_is_parallel",
        )
