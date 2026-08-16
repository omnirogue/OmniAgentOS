"""M-19 / OA-020 — previous_actions must drive a repeated-action signal.

Focused regressions:
- evaluate(previous_actions=...) is not a no-op; identical action histories
  raise repetition_score and emit reason_code ``repeated_action``.
- Diverse action histories stay below the stall.repetition_threshold and do
  not force a switch solely from previous_actions.
- Empty / single-action lists produce no repeated-action signal.
- Output-text repetition and action repetition remain independent channels
  (both can fire; action-only is sufficient).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.metacog.config import clear_metacog_config_cache, stall_config
from omniagentos.metacog.service import (
    MetacogService,
    _action_repetition_score,
    _normalize_action,
)
from omniagentos.metacog.store import MetacogStore


@pytest.fixture()
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetacogService:
    monkeypatch.setenv("OMNIAGENTOS_METACOG_ARTIFACTS_ROOT", str(tmp_path / "arts"))
    monkeypatch.delenv("OMNIAGENTOS_METACOG_MODE", raising=False)
    clear_metacog_config_cache()
    return MetacogService(store=MetacogStore(str(tmp_path / "metacog.db")))


# --- pure score helpers ----------------------------------------------------


def test_action_repetition_score_empty_and_single() -> None:
    assert _action_repetition_score(None) == 0.0
    assert _action_repetition_score([]) == 0.0
    assert _action_repetition_score(["retry_patch"]) == 0.0
    assert _action_repetition_score(["", "  "]) == 0.0


def test_action_repetition_score_identical_history() -> None:
    score = _action_repetition_score(["retry_patch", "retry_patch", "retry_patch", "retry_patch"])
    assert score == 1.0
    # Whitespace / case normalize to the same action.
    assert _normalize_action("  Retry_Patch ") == "retry_patch"
    assert _action_repetition_score(["Retry_Patch", "retry_patch", "RETRY_PATCH"]) == 1.0


def test_action_repetition_score_diverse_history() -> None:
    score = _action_repetition_score(["search", "edit", "test", "commit", "push"])
    assert score == 0.0
    # Only the last action is unique vs earlier ones → still 0.
    mixed = _action_repetition_score(["search", "edit", "test", "search"])
    # last=search, 1 of 3 prior match → ~0.333, below default 0.55 threshold
    assert 0.0 < mixed < float(stall_config()["repetition_threshold"])


# --- evaluate integration --------------------------------------------------


def test_previous_actions_drives_repeated_action_signal(svc: MetacogService) -> None:
    """Same tool/action replayed with no output-text loop still fires."""
    state = svc.evaluate(
        task_id="m19_repeat",
        criteria_total=4,
        criteria_passed=1,
        previous_progress=0.25,  # no new evidence
        previous_actions=["retry_failing_fix"] * 5,
        recent_outputs=["unique output A", "unique output B"],  # not text-loop
    )
    assert state.repetition_score >= float(stall_config()["repetition_threshold"])
    assert "repeated_action" in state.reason_codes
    # Must not CONTINUE while replaying the same action without new evidence.
    assert state.next_control_action != "continue"
    assert state.next_control_action in {"switch", "replan", "stop", "prune", "checkpoint"}
    # Output channel should NOT also claim text repetition here.
    assert "repetition" not in state.reason_codes


def test_diverse_previous_actions_do_not_force_switch(svc: MetacogService) -> None:
    state = svc.evaluate(
        task_id="m19_diverse",
        criteria_total=4,
        criteria_passed=2,
        previous_progress=0.25,  # improving
        previous_actions=["search", "edit", "test", "commit"],
        recent_outputs=["fresh notes each time"],
    )
    assert state.repetition_score < float(stall_config()["repetition_threshold"])
    assert "repeated_action" not in state.reason_codes
    assert state.next_control_action == "continue"
    assert state.quality_trend == "improving"


def test_previous_actions_none_is_backward_compatible(svc: MetacogService) -> None:
    """Callers that omit previous_actions keep the pre-M-19 behavior."""
    state = svc.evaluate(
        task_id="m19_compat",
        criteria_total=4,
        criteria_passed=2,
        previous_progress=0.25,
        recent_outputs=["ok"],
    )
    assert "repeated_action" not in state.reason_codes
    assert state.next_control_action == "continue"


def test_output_and_action_repetition_are_independent(svc: MetacogService) -> None:
    """Text-loop alone still emits ``repetition``; action-loop emits
    ``repeated_action``; both can co-exist."""
    text_only = svc.evaluate(
        task_id="m19_text",
        criteria_total=4,
        criteria_passed=1,
        previous_progress=0.25,
        previous_actions=["search", "edit", "test"],
        recent_outputs=["identical block of text that is long enough xyz"] * 5,
    )
    assert "repetition" in text_only.reason_codes
    assert "repeated_action" not in text_only.reason_codes

    both = svc.evaluate(
        task_id="m19_both",
        criteria_total=4,
        criteria_passed=1,
        previous_progress=0.25,
        previous_actions=["retry_patch"] * 4,
        recent_outputs=["identical block of text that is long enough xyz"] * 5,
    )
    assert "repetition" in both.reason_codes
    assert "repeated_action" in both.reason_codes
    assert both.repetition_score >= float(stall_config()["repetition_threshold"])
    assert both.next_control_action != "continue"


def test_repeated_action_with_progress_still_signals(svc: MetacogService) -> None:
    """Action loop is observed even if criteria progress somehow advanced."""
    state = svc.evaluate(
        task_id="m19_progress",
        criteria_total=10,
        criteria_passed=7,
        previous_progress=0.2,  # improving
        previous_actions=["same_tool_call"] * 6,
    )
    assert "repeated_action" in state.reason_codes
    assert state.repetition_score >= float(stall_config()["repetition_threshold"])
    # Progress improvement still reports improving; signal is additive.
    assert state.quality_trend == "improving"
    # CONTINUE is refused because repeated_action forces switch.
    assert state.next_control_action == "switch"
