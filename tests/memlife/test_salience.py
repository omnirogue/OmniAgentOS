"""L2 acceptance: pure salience scoring.

Decisive: salience is monotone increasing in pain and in importance
(asserted with concrete pairs).

Named counterfeit: an event with no/unusable timestamp must yield
``None`` (unknown → kept), never ``0.0``. Upstream returns 0.0, which
makes malformed records the first to be decayed away.

Second named counterfeit (W2.2): the *same* rule applies to ``pain`` and
``importance``, the two sibling factors in the same expression. They were
read as ``float(getattr(event, "pain", 0.0) or 0.0)``, so an unrecorded
score was written down as a measured zero — and because salience is a
product, that one zero annihilated recency and recurrence with it. In the
live store, 904 of 904 distinct archived events (910 archive lines, 6 of them
duplicate ids) and 210 of 210 candidate rows scored exactly 0.0 — measured
2026-08-06T18:36Z on a ``VACUUM INTO`` copy of the runtime DB.

Revert-check: restore the ``or 0.0`` coercion on either factor → the
``TestCounterfeitUnknownScores`` cases must fail. Restore the
0.0-on-missing-timestamp behaviour → ``TestCounterfeitMissingTimestamp``
must fail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from omniagentos.memlife.contracts import EpisodicEvent, EventResult
from omniagentos.memlife.salience import salience_score

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _event(
    *,
    pain: float = 5.0,
    importance: float = 5.0,
    age_days: float = 0.0,
    event_id: str = "ev_1",
) -> EpisodicEvent:
    return EpisodicEvent(
        id=event_id,
        ts=NOW - timedelta(days=age_days),
        skill="swarm.coder",
        action="commit",
        result=EventResult.DENIED,
        pain=pain,
        importance=importance,
    )


class TestDecisiveMonotone:
    """Higher pain / higher importance must never lower the score, all else equal."""

    def test_monotone_increasing_in_pain(self) -> None:
        low = salience_score(_event(pain=2.0, importance=5.0), NOW)
        high = salience_score(_event(pain=8.0, importance=5.0), NOW)
        assert low is not None and high is not None
        assert low < high, f"pain 2.0 → {low}, pain 8.0 → {high}; expected low < high"

    def test_monotone_increasing_in_importance(self) -> None:
        low = salience_score(_event(pain=5.0, importance=2.0), NOW)
        high = salience_score(_event(pain=5.0, importance=9.0), NOW)
        assert low is not None and high is not None
        assert low < high, (
            f"importance 2.0 → {low}, importance 9.0 → {high}; expected low < high"
        )

    def test_shape_is_recency_times_pain_times_importance_times_recurrence(self) -> None:
        """Concrete arithmetic check of the documented product shape."""
        event = _event(pain=10.0, importance=10.0, age_days=0.0)
        # recency=1, pain/10=1, importance/10=1, min(recurrence,3)=1 → 1.0
        assert salience_score(event, NOW, recurrence=1) == 1.0
        # recurrence caps at 3
        assert salience_score(event, NOW, recurrence=5) == 3.0
        # half-decay window: 45 days of 90 → recency 0.5
        aged = _event(pain=10.0, importance=10.0, age_days=45.0)
        score = salience_score(aged, NOW, recurrence=1)
        assert score is not None
        assert abs(score - 0.5) < 1e-9


class TestCounterfeitMissingTimestamp:
    """THE named counterfeit for salience.

    Upstream returns 0.0 when the timestamp is missing. That makes the
    broken records the most decayable — we return None (unknown/kept).
    """

    def test_missing_timestamp_yields_none_not_zero(self) -> None:
        event = SimpleNamespace(pain=8.0, importance=9.0, ts=None)
        score = salience_score(event, NOW)
        assert score is None, (
            f"missing timestamp must be unknown (None), not a favourable number; got {score!r}"
        )
        assert score != 0.0

    def test_absent_timestamp_attribute_yields_none(self) -> None:
        event = SimpleNamespace(pain=5.0, importance=5.0)  # no ts
        score = salience_score(event, NOW)
        assert score is None

    def test_unusable_timestamp_type_yields_none(self) -> None:
        event = SimpleNamespace(pain=5.0, importance=5.0, ts="not-a-datetime")
        score = salience_score(event, NOW)
        assert score is None


class TestCounterfeitUnknownScores:
    """THE W2.2 counterfeit: unknown pain/importance must not read as 0.0.

    Sibling of the timestamp counterfeit, on the same expression. The rule was
    applied to ``ts`` and never carried to the two factors beside it.
    """

    def test_absent_pain_attribute_yields_none(self) -> None:
        event = SimpleNamespace(ts=NOW, importance=9.0)  # no pain
        score = salience_score(event, NOW)
        assert score is None, (
            f"unrecorded pain must be unknown (None), not a measured 0.0; got {score!r}"
        )
        assert score != 0.0

    def test_absent_importance_attribute_yields_none(self) -> None:
        event = SimpleNamespace(ts=NOW, pain=9.0)  # no importance
        score = salience_score(event, NOW)
        assert score is None
        assert score != 0.0

    def test_none_pain_yields_none(self) -> None:
        assert salience_score(SimpleNamespace(ts=NOW, pain=None, importance=9.0), NOW) is None

    def test_none_importance_yields_none(self) -> None:
        assert salience_score(SimpleNamespace(ts=NOW, pain=9.0, importance=None), NOW) is None

    def test_non_numeric_score_is_unusable_not_zero(self) -> None:
        """Mirrors the non-datetime ``ts`` branch: malformed → unknown, not 0.0."""
        assert salience_score(SimpleNamespace(ts=NOW, pain="high", importance=9.0), NOW) is None
        assert salience_score(SimpleNamespace(ts=NOW, pain=9.0, importance=[1]), NOW) is None

    def test_out_of_contract_range_is_unusable(self) -> None:
        """``contracts.Score`` bounds scores to 0–10; outside that is malformed."""
        assert salience_score(SimpleNamespace(ts=NOW, pain=11.0, importance=5.0), NOW) is None
        assert salience_score(SimpleNamespace(ts=NOW, pain=-1.0, importance=5.0), NOW) is None
        assert salience_score(
            SimpleNamespace(ts=NOW, pain=float("inf"), importance=5.0), NOW
        ) is None

    def test_high_recurrence_is_not_annihilated_by_unknown(self) -> None:
        """The live symptom: the 205-member recurring cluster scored 0.0.

        Largest of the five clusters that were all at 0.0 — 205, 73, 69, 53, 52
        members, measured 2026-08-06T18:36Z.
        """
        recurring = SimpleNamespace(ts=NOW, pain=None, importance=None)
        assert salience_score(recurring, NOW, recurrence=205) is None


class TestMeasuredZeroSurvives:
    """The round trip: unknown and measured-zero must stay distinguishable.

    Fixing "unknown reads as zero" must not ship "zero reads as unknown" in its
    place. A genuinely measured 0.0 is a number we took, and it must come back
    as 0.0.
    """

    def test_measured_zero_pain_scores_zero_not_none(self) -> None:
        score = salience_score(SimpleNamespace(ts=NOW, pain=0.0, importance=9.0), NOW)
        assert score == 0.0
        assert score is not None

    def test_measured_zero_importance_scores_zero_not_none(self) -> None:
        score = salience_score(SimpleNamespace(ts=NOW, pain=9.0, importance=0.0), NOW)
        assert score == 0.0
        assert score is not None
