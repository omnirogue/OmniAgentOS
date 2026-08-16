"""normalize_diff + majority vote + tie-break selection."""

from __future__ import annotations

from omniagentos.agentless.contracts import CandidatePatch, VerifiedCandidate
from omniagentos.agentless.select import normalize_diff, select_candidate

_DIFF_A = (
    "diff --git a/foo.py b/foo.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -10,3 +10,3 @@\n"
    "-def a():\n"
    "+def b():\n"
    "     pass\n"
)

# Same semantic edit as _DIFF_A but different blob hash and hunk line numbers
# (as if the same fix landed at a slightly different offset in a re-generation).
_DIFF_A_VARIANT = (
    "diff --git a/foo.py b/foo.py\n"
    "index 3333333..4444444 100644\n"
    "--- a/foo.py\n"
    "+++ b/foo.py\n"
    "@@ -12,3 +12,3 @@\n"
    "-def a():\n"
    "+def b():\n"
    "     pass\n"
)

_DIFF_B = (
    "diff --git a/bar.py b/bar.py\n"
    "index 5555555..6666666 100644\n"
    "--- a/bar.py\n"
    "+++ b/bar.py\n"
    "@@ -1,2 +1,3 @@\n"
    " x = 1\n"
    "+y = 2\n"
)


def _candidate(
    index: int, diff: str | None, tests_passed: bool | None, applied: bool = True
) -> VerifiedCandidate:
    patch = CandidatePatch(
        index=index,
        diff=diff,
        adapter="mock",
        model=None,
        raw_output=f"```diff\n{diff}```",
        gen_seconds=0.1,
        usage={},
    )
    return VerifiedCandidate(
        patch=patch,
        applied=applied,
        tests_passed=tests_passed,
        test_output_tail="ok" if tests_passed else "fail",
        returncode=0 if tests_passed else 1,
        verify_seconds=0.1,
    )


def test_normalize_diff_strips_index_line_and_hunk_numbers() -> None:
    normalized_a = normalize_diff(_DIFF_A)
    normalized_variant = normalize_diff(_DIFF_A_VARIANT)
    assert normalized_a == normalized_variant
    assert "index " not in normalized_a
    assert "@@ -10,3 +10,3 @@" not in normalized_a
    assert "@@ @@" in normalized_a


def test_normalize_diff_distinguishes_different_content() -> None:
    assert normalize_diff(_DIFF_A) != normalize_diff(_DIFF_B)


def test_select_candidate_majority_vote_picks_the_agreed_fix() -> None:
    candidates = [
        _candidate(0, _DIFF_A, tests_passed=True),
        _candidate(1, _DIFF_A_VARIANT, tests_passed=True),
        _candidate(2, _DIFF_B, tests_passed=True),
    ]
    winner, reason = select_candidate(candidates)
    assert winner is not None
    assert winner.patch.index in (0, 1)  # the two-vote cluster (A / A-variant)
    assert "majority" in reason.lower()


def test_select_candidate_ignores_failing_candidates() -> None:
    candidates = [
        _candidate(0, _DIFF_A, tests_passed=False),
        _candidate(1, _DIFF_B, tests_passed=True),
    ]
    winner, reason = select_candidate(candidates)
    assert winner is not None
    assert winner.patch.index == 1
    assert reason  # non-empty


def test_select_candidate_none_when_no_passers_reports_failure_modes() -> None:
    candidates = [
        _candidate(0, _DIFF_A, tests_passed=False),
        _candidate(1, None, tests_passed=None, applied=False),
    ]
    winner, reason = select_candidate(candidates)
    assert winner is None
    assert "tests failed" in reason
    assert "did not apply" in reason


def test_select_candidate_empty_list_returns_none_with_reason() -> None:
    winner, reason = select_candidate([])
    assert winner is None
    assert reason


def test_select_candidate_tie_break_smallest_diff_then_lowest_index() -> None:
    # Two candidates, each in their OWN singleton cluster (1 vote each) — a tie.
    # _DIFF_B has fewer changed (+/-) lines than _DIFF_A, so it should win EVEN
    # THOUGH it has the HIGHER sample index — isolates the diff-size criterion
    # from the index tie-break (a lowest-index-only rule would wrongly pick #1).
    candidates = [
        _candidate(1, _DIFF_A, tests_passed=True),
        _candidate(9, _DIFF_B, tests_passed=True),
    ]
    winner, reason = select_candidate(candidates)
    assert winner is not None
    assert winner.patch.index == 9
    assert "tie" in reason.lower()


def test_select_candidate_tie_break_lowest_index_when_diff_size_equal() -> None:
    candidates = [
        _candidate(9, _DIFF_A, tests_passed=True),
        _candidate(3, _DIFF_A.replace("2222222", "9999999"), tests_passed=True),
        _candidate(1, _DIFF_B, tests_passed=True),
    ]
    # _DIFF_A and its blob-hash-only variant normalize to the SAME thing, forming
    # a 2-vote cluster against _DIFF_B's 1-vote cluster -> majority wins, not tie.
    winner, _ = select_candidate(candidates)
    assert winner is not None
    assert winner.patch.index in (9, 3)
