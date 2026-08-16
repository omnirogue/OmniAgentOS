"""The optimizer playbook must not reach a planning prompt while the objective
function that produces it is inverted.

Provenance: `summary.py` computes `first_attempt_rate = ... if tasks_with_attempts
else 1.0` — an empty denominator returns the most flattering possible value, so a
run that did no work scores as though every task succeeded first try. A zero-work
run scored 75/100. `optimize._aggregate_sizing` selects those runs as exemplars and
writes their concurrency into `var/swarm/learned.json`, which `build_lessons_block`
injected into every swarm-planning prompt. Closed loop, no human, pointed at a
metric that rewards doing less.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omniagentos.swarm.planner import (
    PLAYBOOK_FEED_ENV,
    build_lessons_block,
    playbook_feed_enabled,
)

PLAYBOOK_MARKER = "OPTIMIZER PLAYBOOK"
POISON = {"concurrency_by_plan_size": {"5": 1}, "runs_analyzed_total": 9}


@pytest.fixture
def playbook(tmp_path: Path) -> Path:
    path = tmp_path / "learned.json"
    path.write_text(json.dumps(POISON), encoding="utf-8")
    return path


class TestPlaybookFeedGuard:
    def test_disarmed_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default must be OFF — a fresh deployment inherits the safe state."""
        monkeypatch.delenv(PLAYBOOK_FEED_ENV, raising=False)
        assert playbook_feed_enabled() is False

    def test_playbook_absent_from_prompt_when_disarmed(
        self, playbook: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The decisive assertion: a real, non-empty playbook on disk does not
        reach the prompt."""
        monkeypatch.delenv(PLAYBOOK_FEED_ENV, raising=False)
        block = build_lessons_block("any goal", playbook_path=str(playbook))
        assert PLAYBOOK_MARKER not in block
        # Not merely the header — none of the payload leaks by another route.
        assert "concurrency_by_plan_size" not in block

    def test_playbook_present_when_explicitly_armed(
        self, playbook: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Counterfeit guard: deleting the read entirely would satisfy every
        assertion above while silently removing the feature.

        The guard must be a switch, not a deletion — C1 landing has to be able to
        turn it back on.
        """
        monkeypatch.setenv(PLAYBOOK_FEED_ENV, "1")
        block = build_lessons_block("any goal", playbook_path=str(playbook))
        assert PLAYBOOK_MARKER in block
        assert "concurrency_by_plan_size" in block

    @pytest.mark.parametrize("value", ["0", "", "true", "yes", "TRUE", "on"])
    def test_only_exactly_one_arms_it(
        self, value: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed on anything but "1".

        A truthy-string check (`if os.environ.get(...)`) would arm the feed on "0",
        which is how a disarm becomes decorative — the operator sets it to "0"
        believing that is off.
        """
        monkeypatch.setenv(PLAYBOOK_FEED_ENV, value)
        assert playbook_feed_enabled() is False
