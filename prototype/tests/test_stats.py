"""The four pure functions in :mod:`selfloop.stats`, and the one that starved a system.

No fixtures and no storage here: every function under test is pure, so these
tests parametrise over values rather than over adapters.

The test that carries the most weight is :func:`test_wilson_is_zero_with_no_evidence`.
It looks like a triviality — of course a bound over no samples is zero — and it
is the arithmetic behind the defect the whole package was rebuilt around: the
predecessor gated promotion on ``wilson_lower_bound(helped, used) >= threshold``
while ``helped`` and ``used`` were only ever written AFTER a lesson was promoted
and injected. At first promotion ``used == 0``, so the bound is 0.0, so the
condition was unsatisfiable, so 207 candidates staged and none ever promoted.
The gate was correctly wired and mathematically always closed.
"""

from __future__ import annotations

import pytest
from selfloop.stats import decay_weight, jaccard, normalise_tokens, wilson_lower_bound

# ---------------------------------------------------------------------------
# wilson_lower_bound
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [0, -1, -100])
def test_wilson_is_zero_with_no_evidence(n: int) -> None:
    """No samples is no confidence — and therefore never a basis to promote.

    Pinned as its own test because this value is what makes the bound unusable
    as a promotion admission test. Anything that reintroduces
    ``wilson_lower_bound(helped, used) >= threshold`` into ``learn.promote()``
    is gating on a number that is 0.0 for every candidate that has never been
    injected, which is every candidate at the moment it would first be promoted.
    """
    assert wilson_lower_bound(0, n) == 0.0


def test_wilson_is_conservative_at_small_n() -> None:
    """Two wins out of two is not endorsement. That is the point of the bound."""
    assert wilson_lower_bound(2, 2) < 0.4


def test_wilson_converges_upward_toward_the_raw_rate() -> None:
    """More evidence at the same rate raises the bound, never lowers it."""
    bounds = [wilson_lower_bound(n, n) for n in (2, 5, 20, 100, 1000)]
    assert bounds == sorted(bounds)
    assert bounds[-1] > 0.99
    assert all(bound < 1.0 for bound in bounds)


@pytest.mark.parametrize(
    ("wins", "n"),
    [(0, 5), (1, 5), (3, 5), (5, 5), (7, 10), (99, 100), (1, 1000)],
)
def test_wilson_never_exceeds_the_observed_rate(wins: int, n: int) -> None:
    """A LOWER bound sits at or below the point estimate, and never below zero.

    The zero-floor is asserted to a tolerance rather than exactly, and the reason
    is a real wart rather than test hygiene: at ``wins == 0`` the closed form
    computes ``center - margin`` where the two terms are equal in exact
    arithmetic, so ``sqrt`` rounding leaves a value a few units in the last place
    BELOW zero (measured: ``wilson_lower_bound(0, 5) == -3.1e-17``). Nothing in
    the shipped package compares the bound against 0.0, so it changes no
    behaviour today; it is recorded here so that a caller who does — a
    ``promote_threshold`` of exactly 0.0 meaning "no floor" — finds this test
    rather than a mystery.
    """
    bound = wilson_lower_bound(wins, n)
    assert bound <= wins / n
    assert bound > -1e-12


def test_wilson_ranks_a_worse_record_lower() -> None:
    """Ordering is the property recall ranking depends on."""
    assert wilson_lower_bound(9, 10) > wilson_lower_bound(5, 10) > wilson_lower_bound(1, 10)


# ---------------------------------------------------------------------------
# decay_weight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("age", [0.0, 1.0, 6.9, 7.0])
def test_decay_is_full_inside_the_full_window(age: float) -> None:
    assert decay_weight(age) == 1.0


@pytest.mark.parametrize("age", [-0.5, -3.0, -400.0])
def test_a_negative_age_clamps_to_full_weight(age: float) -> None:
    """A sample stamped in the future is a clock that skewed, not a future sample.

    Two machines are rarely in perfect agreement, and letting skew un-count a
    fresh observation is a failure that only ever appears in production — where
    the writer and the reader are different hosts.
    """
    assert decay_weight(age) == 1.0


@pytest.mark.parametrize("age", [14.0, 14.1, 900.0])
def test_decay_is_zero_at_and_after_the_zero_day(age: float) -> None:
    assert decay_weight(age) == 0.0


def test_decay_is_linear_between_the_two_windows() -> None:
    """Halfway between full and zero is half weight."""
    assert decay_weight(10.5) == pytest.approx(0.5)
    assert decay_weight(8.75) == pytest.approx(0.75)


def test_the_retire_floor_lands_where_the_context_says_it_does() -> None:
    """``LoopContext.retire_floor`` of 0.2 documents "about 12.6 days unused".

    A docstring that states a number is a claim, and this is the arithmetic that
    makes it one an integrator can rely on rather than a rounded guess.
    """
    assert decay_weight(12.6) == pytest.approx(0.2)


@pytest.mark.parametrize("zero_days", [7.0, 3.0, 0.0])
def test_a_degenerate_window_becomes_a_step_rather_than_dividing_by_zero(
    zero_days: float,
) -> None:
    """A caller who asks for no decay window gets no decay window."""
    assert decay_weight(1.0, full_days=7.0, zero_days=zero_days) == 1.0
    assert decay_weight(7.5, full_days=7.0, zero_days=zero_days) == 0.0


# ---------------------------------------------------------------------------
# normalise_tokens
# ---------------------------------------------------------------------------


def test_tokens_are_lowercased_and_stripped_of_punctuation() -> None:
    """``Timeout:`` and ``timeout`` must be the same token or nothing clusters."""
    assert normalise_tokens("Timeout: connect") == normalise_tokens("timeout connect")
    assert normalise_tokens("Timeout: connect") == frozenset({"timeout", "connect"})


def test_tokens_are_a_set_so_repetition_carries_no_weight() -> None:
    """One verbose stack trace must not dominate a comparison by repeating itself."""
    assert normalise_tokens("error error error") == frozenset({"error"})


def test_an_empty_string_yields_the_empty_set() -> None:
    assert normalise_tokens("") == frozenset()
    assert normalise_tokens("--- !!! ---") == frozenset()


# ---------------------------------------------------------------------------
# jaccard
# ---------------------------------------------------------------------------


def test_two_empty_sets_are_identical_and_the_caller_owes_a_check() -> None:
    """1.0 is the mathematically correct answer, and it is a trap for the caller.

    Identical emptiness is identical. The obligation it creates lives one layer
    up: an all-empty cluster must be REJECTED by the clustering code, because
    the lesson it would emit has the empty string for a claim.
    """
    assert jaccard(frozenset(), frozenset()) == 1.0


def test_one_empty_side_shares_nothing() -> None:
    assert jaccard(frozenset({"timeout"}), frozenset()) == 0.0


def test_identical_and_disjoint_sets_sit_at_the_ends_of_the_scale() -> None:
    tokens = normalise_tokens("connection reset by peer")
    assert jaccard(tokens, tokens) == 1.0
    assert jaccard(tokens, normalise_tokens("quota exhausted")) == 0.0


def test_partial_overlap_is_intersection_over_union() -> None:
    left = frozenset({"a", "b", "c"})
    right = frozenset({"b", "c", "d"})
    assert jaccard(left, right) == pytest.approx(2 / 4)


def test_similarity_alone_would_conflate_unrelated_failures() -> None:
    """The reason clustering partitions by ``(scope, failure_tag)`` FIRST.

    These two reports share nothing but the vocabulary every failure report has,
    and they still clear the 0.3 threshold the clusterer uses. Token similarity
    is not evidence that two failures are the same failure; a shared structured
    tag is, and this is the measurement that says so.
    """
    auth = normalise_tokens("error: failed in line 12")
    disk = normalise_tokens("error: failed in line 88")
    assert jaccard(auth, disk) > 0.3
