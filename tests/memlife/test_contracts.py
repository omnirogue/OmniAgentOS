"""L0 acceptance: the memlife contracts refuse what must not be representable.

Decisive assertion: every model round-trips (model → JSON → model, identical), and
unknown fields are rejected rather than silently ignored.

Named counterfeit: a candidate JSON missing `decisions` must be REFUSED, not
defaulted to `[]`. That default is the shape of the upstream bug where a graduation
is reported without the lesson ever being written — with no history, the lie is
undetectable afterwards.

Revert-check: loosening validation to `dict.get`-style defaults must break these.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from omniagentos.memlife.contracts import (
    Candidate,
    CandidateStatus,
    CycleReport,
    CycleStatus,
    Decision,
    DecisionAction,
    EpisodicEvent,
    EventResult,
    Lesson,
    LessonStatus,
    Provenance,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _decision() -> Decision:
    return Decision(action=DecisionAction.STAGE, at=NOW, actor="dream-cycle")


def _candidate(**over: object) -> Candidate:
    base: dict[str, object] = {
        "id": "cand_1",
        "key": "swarm.coder/commit-refused",
        "claim": "Agents cannot commit inside a sandboxed worktree.",
        "cluster_size": 3,
        "status": CandidateStatus.STAGED,
        "decisions": [_decision()],
    }
    base.update(over)
    return Candidate(**base)  # type: ignore[arg-type]


class TestRoundTrip:
    """Decisive: serialise → deserialise must be the identity."""

    def test_every_model_round_trips_identically(self) -> None:
        models = [
            EpisodicEvent(
                id="ev_1", ts=NOW, skill="swarm.coder", action="commit",
                result=EventResult.DENIED, pain=8.0, importance=9.0,
            ),
            _candidate(),
            Lesson(
                id="les_1", candidate_id="cand_1", claim="Prefer clones over worktrees.",
                status=LessonStatus.ACCEPTED, graduated_at=NOW, graduated_by="owner",
            ),
            CycleReport(
                status=CycleStatus.COMPLETED, input_bytes=100, kept_bytes=60,
                archived_bytes=30, quarantined_bytes=10,
            ),
        ]
        for m in models:
            again = type(m).model_validate_json(m.model_dump_json())
            assert again == m, f"{type(m).__name__} did not round-trip"

    @pytest.mark.parametrize(
        "model,payload",
        [
            (EpisodicEvent, {"id": "e", "ts": NOW, "skill": "s", "action": "a",
                             "result": "success", "surprise": 1}),
            (Provenance, {"run_id": "r", "unexpected": True}),
        ],
    )
    def test_unknown_fields_are_rejected(self, model: type, payload: dict) -> None:
        """A silently-ignored key is schema drift you find out about in production."""
        with pytest.raises(ValidationError):
            model(**payload)

    def test_models_are_frozen(self) -> None:
        with pytest.raises(ValidationError):
            _candidate().claim = "mutated"  # type: ignore[misc]


class TestCounterfeits:
    """Each of these is a real upstream behaviour that must not be portable here."""

    def test_candidate_without_decision_history_is_refused(self) -> None:
        """THE named counterfeit for L0.

        Defaulting `decisions` to [] would present a candidate that has already been
        rejected — or graduated without its lesson written — as a fresh one.
        """
        with pytest.raises(ValidationError):
            Candidate.model_validate(
                {
                    "id": "cand_1", "key": "k", "claim": "c",
                    "cluster_size": 1, "status": "staged",
                }
            )

    def test_empty_decision_list_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            _candidate(decisions=[])

    def test_empty_claim_is_refused(self) -> None:
        """Their cluster extraction can emit `claim: ""` — a candidate that says
        nothing, which a reviewer cannot act on but which still occupies the queue."""
        for blank in ("", "   ", "\n"):
            with pytest.raises(ValidationError):
                _candidate(claim=blank)

    def test_unknown_result_is_representable_and_distinct(self) -> None:
        """An unrecorded outcome must not be storable as success.

        Three `review_denied` attempts rendered as COMPLETED today because the
        session state was read in place of the attempt outcome.
        """
        assert EventResult.UNKNOWN != EventResult.SUCCESS
        assert EventResult.DENIED != EventResult.FAILURE
        ev = EpisodicEvent(id="e", ts=NOW, skill="s", action="a", result=EventResult.UNKNOWN)
        assert ev.result is EventResult.UNKNOWN

    def test_unknown_cost_is_none_never_zero(self) -> None:
        """$6.33 was spent against a $4.00 cap because unknown read as 0.0."""
        ev = EpisodicEvent(id="e", ts=NOW, skill="s", action="a", result=EventResult.SUCCESS)
        assert ev.cost_usd is None
        assert ev.cost_usd != 0.0
        with pytest.raises(ValidationError):
            EpisodicEvent(id="e", ts=NOW, skill="s", action="a",
                          result=EventResult.SUCCESS, cost_usd=-1.0)

    def test_unknown_salience_is_none_not_zero(self) -> None:
        """Upstream returns 0.0 salience on a missing timestamp, which makes the
        malformed records the FIRST to be decayed away — losing exactly the records
        that indicate something is wrong."""
        assert _candidate().salience is None

    def test_no_input_is_distinct_from_completed(self) -> None:
        """A cycle that read nothing must not report the success path — the same
        failure as an API returning "0 pending" for a missing store."""
        assert CycleStatus.NO_INPUT != CycleStatus.COMPLETED


class TestByteConservation:
    """The decisive property for the dream cycle: nothing is silently dropped."""

    def test_conserved_when_accounted(self) -> None:
        assert CycleReport(
            status=CycleStatus.COMPLETED, input_bytes=100,
            kept_bytes=60, archived_bytes=30, quarantined_bytes=10,
        ).bytes_conserved

    def test_not_conserved_when_bytes_vanish(self) -> None:
        """Upstream deletes unparseable lines on rewrite and reports a clean run."""
        assert not CycleReport(
            status=CycleStatus.COMPLETED, input_bytes=100,
            kept_bytes=60, archived_bytes=30, quarantined_bytes=0,
        ).bytes_conserved


class TestTimezoneNormalisation:
    def test_naive_timestamps_are_normalised_not_rejected(self) -> None:
        """Naive vs aware silently compares wrong, which drifts decay windows."""
        ev = EpisodicEvent(
            id="e", ts=datetime(2026, 7, 28, 12, 0), skill="s", action="a",
            result=EventResult.SUCCESS,
        )
        assert ev.ts.tzinfo is not None
