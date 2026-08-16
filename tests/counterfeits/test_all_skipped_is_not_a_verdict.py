"""A must_fail set that executed ZERO tests is an instrument error, not a verdict.

``pytest`` exits 0 both when every named node passed and when every named node was
*skipped*, so ``returncode`` alone cannot distinguish "the guard held" from "the
guard was never asked".  Both consumers of that integer used to read the second
case as the first:

* :func:`run_control` accepted an all-skipped control as green — while its own
  docstring says it exists so "a corpus entry aimed at an already-broken test"
  cannot look green.  A skipped test is precisely that case.
* :func:`evaluate_mutated` reported ``survived`` — "the named coverage is
  decoration" — for a guard that is in fact intact.

Live instance (issue #104): all seven ``tests/workfs/*.py`` modules carry
``pytestmark = pytest.mark.skipif(sys.platform != "darwin", ...)`` and CI runs on
``ubuntu-latest``, so the two WorkFS corpus entries name nodes that cannot run
there.

These tests drive the classifiers directly with synthetic ``CompletedProcess``
results, so they assert the platform-independent decision rather than depending
on the host the suite happens to run on.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.counterfeits.harness import (
    CounterfeitControlError,
    CounterfeitEntry,
    evaluate_mutated,
    run_control,
)


def _proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["pytest"], returncode=returncode, stdout=stdout, stderr=""
    )


def _entry() -> CounterfeitEntry:
    return CounterfeitEntry(
        id="cf-probe",
        patch="patches/probe.patch",
        rationale="probe",
        must_fail=("tests/workfs/test_containment.py::test_string_prefix_counterfeit_fails_loudly",),
        failure_re="assert",
    )


# --- evaluate_mutated -------------------------------------------------------


def test_all_skipped_mutated_run_is_not_reported_as_survived() -> None:
    """The exact shape CI produces for the WorkFS entries (issue #104)."""
    result = evaluate_mutated(_entry(), _proc("s\n1 skipped in 0.01s\n"))
    assert result.status != "survived", (
        "an all-skipped must_fail set means the guard was never asked; "
        f"reporting it as a survival is a false regression: {result.detail}"
    )
    assert result.status == "collection_error", result
    assert "ZERO tests" in result.detail


def test_a_genuinely_green_mutated_run_is_still_survived() -> None:
    """The real survival signal must not be swallowed by the new branch."""
    result = evaluate_mutated(_entry(), _proc("..\n2 passed in 0.10s\n"))
    assert result.status == "survived", result


def test_a_partially_skipped_mutated_run_that_still_passed_is_survived() -> None:
    """Some skips plus real execution is a verdict, not an instrument error."""
    result = evaluate_mutated(_entry(), _proc(".s\n1 passed, 1 skipped in 0.10s\n"))
    assert result.status == "survived", result


def test_a_red_mutated_run_is_still_caught() -> None:
    result = evaluate_mutated(_entry(), _proc("F\n1 failed in 0.10s\nassert True is False\n", 1))
    assert result.status == "caught", result


# --- run_control ------------------------------------------------------------


def test_control_refuses_an_all_skipped_must_fail_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skipped and passed are the same integer; the control must not conflate them."""
    monkeypatch.setattr(
        "tests.counterfeits.harness.run_pytest_nodes",
        lambda **_kwargs: _proc("ss\n2 skipped in 0.02s\n"),
    )
    with pytest.raises(CounterfeitControlError, match="ZERO tests"):
        run_control([_entry()])


def test_control_still_accepts_a_genuinely_green_must_fail_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tests.counterfeits.harness.run_pytest_nodes",
        lambda **_kwargs: _proc("..\n2 passed in 0.20s\n"),
    )
    run_control([_entry()])
