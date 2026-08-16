"""K11: ``_metacog_evaluate_active_tasks`` — where the 0/2 placeholders lived.

U-N3's honesty fix was "an unmeasured card is recorded as UNMEASURED, never as
a fabricated 0/2", and it landed here, at the reconciler boundary that feeds
metacog. The caller itself had zero coverage, so a revert that resumed
fabricating measured tuples from a board status would have stayed green.

These tests assert on the arguments that reach ``MetacogService.evaluate``,
because that is the whole contract: what the stall accounting is told.
"""

from __future__ import annotations

from typing import Any

import pytest

from omniagentos.intake import service as intake_service


class _RecordingMetacog:
    """Captures every ``evaluate`` call instead of touching a store."""

    calls: list[dict[str, Any]] = []

    def __init__(self) -> None:
        pass

    def evaluate(self, **kwargs: Any) -> None:
        type(self).calls.append(kwargs)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    _RecordingMetacog.calls = []
    monkeypatch.setattr(
        "omniagentos.metacog.service.MetacogService", _RecordingMetacog, raising=True
    )
    return _RecordingMetacog.calls


def _card(task_id: str, status: str, run_id: str = "") -> dict[str, Any]:
    return {"id": task_id, "status": status, "run_id": run_id, "title": f"card {task_id}"}


def test_a_card_without_step_counts_is_recorded_as_unmeasured(
    recorded: list[dict[str, Any]],
) -> None:
    """No evidence is not zero evidence.

    A 0/0 recorded as MEASURED is the fabricated-progress defect: stall
    accounting then sees a run making no progress rather than a run it cannot
    see, and escalates on a signal nobody produced.
    """
    intake_service._metacog_evaluate_active_tasks(
        [_card("btk_1", "in_progress", run_id="run_1")],
        step_counts={},  # the batched map has nothing for this run
    )

    assert len(recorded) == 1
    call = recorded[0]
    assert call["task_id"] == "btk_1"
    assert call["progress_measured"] is False
    assert (call["criteria_passed"], call["criteria_total"]) == (0, 0)


def test_a_card_with_step_counts_reports_the_real_tuple_as_measured(
    recorded: list[dict[str, Any]],
) -> None:
    """The counterweight: real evidence must arrive intact and flagged measured."""
    intake_service._metacog_evaluate_active_tasks(
        [_card("btk_2", "in_progress", run_id="run_2")],
        step_counts={"run_2": (3, 7)},
    )

    assert len(recorded) == 1
    call = recorded[0]
    assert call["progress_measured"] is True
    assert (call["criteria_passed"], call["criteria_total"]) == (3, 7)


def test_a_card_with_no_run_id_cannot_borrow_another_runs_counts(
    recorded: list[dict[str, Any]],
) -> None:
    """Step counts are keyed by RUN. A card with no run has no counts."""
    intake_service._metacog_evaluate_active_tasks(
        [_card("btk_3", "claimed", run_id="")],
        step_counts={"run_other": (5, 5)},
    )

    assert len(recorded) == 1
    assert recorded[0]["progress_measured"] is False
    assert (recorded[0]["criteria_passed"], recorded[0]["criteria_total"]) == (0, 0)


def test_absent_step_counts_entirely_is_still_unmeasured(
    recorded: list[dict[str, Any]],
) -> None:
    """``step_counts=None`` (an unbatched caller) must not become 0/0 measured."""
    intake_service._metacog_evaluate_active_tasks(
        [_card("btk_4", "blocked", run_id="run_4")],
        step_counts=None,
    )

    assert len(recorded) == 1
    assert recorded[0]["progress_measured"] is False


def test_only_active_cards_are_evaluated_and_the_batch_is_bounded(
    recorded: list[dict[str, Any]],
) -> None:
    """Terminal cards are not live progress signals, and the fan-out is capped."""
    cards = [_card(f"btk_{i}", "in_progress", run_id=f"run_{i}") for i in range(30)]
    cards.append(_card("btk_done", "done", run_id="run_done"))
    cards.append(_card("btk_cancelled", "cancelled", run_id="run_cancelled"))

    intake_service._metacog_evaluate_active_tasks(cards, step_counts={})

    evaluated = {call["task_id"] for call in recorded}
    assert "btk_done" not in evaluated
    assert "btk_cancelled" not in evaluated
    assert len(recorded) == 25, "the 25-card cap bounds one reconcile pass"


def test_a_metacog_fault_never_reaches_the_reconciler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Self-repair telemetry must not be able to fail a board reconcile."""

    class _Exploding:
        def __init__(self) -> None:
            pass

        def evaluate(self, **_kwargs: Any) -> None:
            raise RuntimeError("metacog store is unavailable")

    monkeypatch.setattr(
        "omniagentos.metacog.service.MetacogService", _Exploding, raising=True
    )

    intake_service._metacog_evaluate_active_tasks(
        [_card("btk_5", "in_progress", run_id="run_5")], step_counts={"run_5": (1, 2)}
    )
