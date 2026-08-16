"""Low-confidence binding + notes-only parse (Opus caveats cleanup)."""

from __future__ import annotations

from omniagentos.api.routes.swarm import _parse_formation_from_plan
from omniagentos.swarm.planner import build_plan, formation_swarm_json_fields


def test_build_plan_low_conf_clears_implementers() -> None:
    plan = build_plan(
        "asdf qwerty zxcv nonsense gibberish no keywords",
        [
            {
                "id": "main",
                "title": "Main",
                "description": "asdf qwerty zxcv",
                "depends_on": [],
                "owned_paths": ["a.py"],
                "est_agent_minutes": 5,
                "est_manual_minutes": 10,
                "acceptance": "x",
                "verify_command": "true",
            }
        ],
    )
    assert plan.formation is not None
    assert plan.formation.low_confidence is True
    assert plan.formation.implementers == []
    assert plan.formation.mechanical_gate is False
    fields = formation_swarm_json_fields(plan.formation, role="implementer")
    assert fields.get("formation_low_confidence") is True
    # Notes use empty implementers= not a placeholder model name
    note = next(n for n in plan.assumptions if str(n).startswith("formation:"))
    assert "implementers= " in note  # empty list → "implementers= reviewer=..."
    assert "(none-low-conf)" not in note
    assert "implementers=grok" not in note


def test_notes_parser_empty_implementers_is_low_conf() -> None:
    plan = {
        "assumptions": [
            "formation: coding implementers= reviewer=opus mechanical=False "
            "confidence=0.4 reason=default_fallback; LOW_CONFIDENCE"
        ]
    }
    parsed = _parse_formation_from_plan(plan)
    assert parsed is not None
    assert parsed["implementers"] == []
    assert parsed["low_confidence"] is True
    assert parsed["mechanical_gate"] is False


def test_notes_parser_placeholder_token_not_model_name() -> None:
    plan = {
        "assumptions": [
            "formation: coding implementers=(none-low-conf) reviewer=opus "
            "mechanical=False confidence=0.4; LOW_CONFIDENCE"
        ]
    }
    parsed = _parse_formation_from_plan(plan)
    assert parsed is not None
    assert parsed["implementers"] == []
    assert parsed["low_confidence"] is True
