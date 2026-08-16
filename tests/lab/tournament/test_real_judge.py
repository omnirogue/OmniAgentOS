from __future__ import annotations

from typing import Any

import pytest

from omniagentos.lab.tournament.driver import TournamentDriver


class _Judge:
    def __init__(
        self,
        lineage: str,
        score_a: float,
        score_b: float,
        *,
        validity: float = 0.95,
    ) -> None:
        self.judge_lineage = lineage
        self._result = {
            "score_a": score_a,
            "score_b": score_b,
            "validity": validity,
        }

    def judge_blind(
        self,
        *,
        output_a: dict[str, Any],
        output_b: dict[str, Any],
        arena_task: dict[str, Any],
    ) -> dict[str, Any]:
        assert set(output_a) == {"blind_token", "output"}
        assert set(output_b) == {"blind_token", "output"}
        assert "mock_outputs" not in arena_task
        return self._result


def _payload(value: Any) -> dict[str, Any]:
    return {"blind_token": "opaque", "output": value}


def test_live_judge_refuses_non_empty_text_as_a_quality_metric() -> None:
    driver = TournamentDriver(object())

    with pytest.raises(ValueError, match="deterministic task metric or at least two"):
        driver.judge_blind(
            output_a=_payload({"text": "long answer"}),
            output_b=_payload({"text": "another answer"}),
            arena_task={"prompt": "solve it"},
        )


def test_live_judge_uses_only_the_task_declared_deterministic_metric() -> None:
    result = TournamentDriver(object()).judge_blind(
        output_a=_payload({"quality": 0.9, "text": ""}),
        output_b=_payload({"quality": 0.4, "text": "lots of prose"}),
        arena_task={"deterministic_metric": "quality"},
    )

    assert (result["score_a"], result["score_b"]) == (0.9, 0.4)
    assert result["notes"].startswith("deterministic task metric quality")

    nested = TournamentDriver(object()).judge_blind(
        output_a=_payload({"json": {"metrics": {"quality": 0.8}}}),
        output_b=_payload({"json": {"metrics": {"quality": 0.3}}}),
        arena_task={"deterministic_metric": "quality"},
    )
    assert (nested["score_a"], nested["score_b"]) == (0.8, 0.3)


def test_blind_panel_requires_cross_lineage_validity_and_agreement() -> None:
    output_a = _payload({"text": "a"})
    output_b = _payload({"text": "b"})

    with pytest.raises(ValueError, match="repeats judge lineage"):
        TournamentDriver(
            object(),
            judge_panel=[_Judge("same", 0.9, 0.1), _Judge("same", 0.8, 0.2)],
        ).judge_blind(output_a=output_a, output_b=output_b, arena_task={})

    with pytest.raises(ValueError, match="validity below floor"):
        TournamentDriver(
            object(),
            judge_panel=[
                _Judge("kimi", 0.9, 0.1),
                _Judge("grok", 0.8, 0.2, validity=0.5),
            ],
        ).judge_blind(output_a=output_a, output_b=output_b, arena_task={})

    with pytest.raises(ValueError, match="agreement below floor"):
        TournamentDriver(
            object(),
            judge_panel=[_Judge("kimi", 0.9, 0.1), _Judge("grok", 0.1, 0.9)],
        ).judge_blind(output_a=output_a, output_b=output_b, arena_task={})


def test_valid_cross_lineage_panel_records_evidence() -> None:
    result = TournamentDriver(
        object(),
        judge_panel=[_Judge("kimi", 0.9, 0.2), _Judge("grok", 0.8, 0.3)],
    ).judge_blind(
        output_a=_payload({"text": "a"}),
        output_b=_payload({"text": "b"}),
        arena_task={"mock_outputs": {"identity": "must be stripped"}},
    )

    assert result["score_a"] == pytest.approx(0.85)
    assert result["score_b"] == pytest.approx(0.25)
    assert "lineages=2" in result["notes"]
    assert "agreement=1.000" in result["notes"]
    assert "validity_min=0.950" in result["notes"]
