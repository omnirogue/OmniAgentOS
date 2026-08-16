"""Phase B — formation binds to plan, routing preference, and provision stamps.

Proves structured ``SwarmPlan.formation`` (not notes-only), topology/confidence,
plan_json round-trip, implementer preference on candidates, and swarm_json stamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from omniagentos.collab.store import CollabStore
from omniagentos.formation import (
    FORMATION_TOPOLOGY,
    prefer_implementers,
    select_formation,
    select_formation_with_confidence,
    topology_for_formation,
)
from omniagentos.swarm.contracts import FormationBinding, SwarmPlan, SwarmTaskSpec
from omniagentos.swarm.dal import SwarmDal
from omniagentos.swarm.planner import (
    INTEGRATION_TASK_ID,
    build_plan,
    formation_swarm_json_fields,
    plan_fingerprint,
    plan_payload,
    provision_run,
)

# ---------------------------------------------------------------------------
# 1. Selection still works
# ---------------------------------------------------------------------------


class TestFormationSelection:
    def test_coding_goal(self) -> None:
        assert select_formation(goal="fix the login bug").id == "coding"

    def test_marketing_goal(self) -> None:
        assert select_formation(goal="launch FB ad campaign").id == "marketing"

    def test_research_goal(self) -> None:
        assert select_formation(goal="research competitive landscape").id == "research"

    def test_task_class_research(self) -> None:
        assert select_formation(task_class="research").id == "research"

    def test_confidence_keyword(self) -> None:
        sel = select_formation_with_confidence(goal="implement a new API feature")
        assert sel.formation.id == "coding"
        assert sel.confidence >= 0.85
        assert sel.reason == "keyword_match"

    def test_confidence_task_class(self) -> None:
        sel = select_formation_with_confidence(task_class="marketing")
        assert sel.formation.id == "marketing"
        assert sel.confidence == pytest.approx(0.95)
        assert sel.reason == "task_class_match"

    def test_confidence_default_fallback(self) -> None:
        sel = select_formation_with_confidence(goal="do the thing quietly")
        assert sel.formation.id == "coding"
        assert sel.confidence == pytest.approx(0.4)
        assert sel.reason in {"default_fallback", "low_confidence_fallback"}

    def test_design_landing_is_creative_not_marketing(self) -> None:
        """F7: best-match scoring — design/hero beats landing/ad for creative."""
        sel = select_formation_with_confidence(goal="Design a landing page hero and ad concepts")
        assert sel.formation.id == "creative"

    def test_low_confidence_flag(self) -> None:
        from omniagentos.formation import CONFIDENCE_THRESHOLD, is_low_confidence

        sel = select_formation_with_confidence(goal="asdf qwerty zxcv")
        assert is_low_confidence(sel)
        assert sel.confidence < CONFIDENCE_THRESHOLD

    def test_topology_map(self) -> None:
        assert topology_for_formation("coding") == "hierarchical"
        assert topology_for_formation("research") == "map_reduce"
        assert topology_for_formation("creative") == "generator_critic"
        assert topology_for_formation("marketing") == "generator_critic"
        assert topology_for_formation("operations") == "sequential"
        assert set(FORMATION_TOPOLOGY) >= {
            "coding",
            "research",
            "creative",
            "marketing",
            "operations",
        }


# ---------------------------------------------------------------------------
# 2. Structured field on build_plan
# ---------------------------------------------------------------------------


def _raw_task(task_id: str, paths: tuple[str, ...] = ()) -> dict:
    return {
        "id": task_id,
        "title": task_id.upper(),
        "description": f"do {task_id}",
        "depends_on": [],
        "owned_paths": list(paths) or [f"src/{task_id}.py"],
        "est_agent_minutes": 10,
        "est_manual_minutes": 30,
        "acceptance": f"{task_id} done",
        "verify_command": f"pytest tests/{task_id}",
    }


class TestStructuredFormationOnPlan:
    def test_build_plan_binds_coding_formation(self) -> None:
        plan = build_plan(
            "fix the login bug in auth",
            [_raw_task("a"), _raw_task("b")],
        )
        assert plan.formation is not None
        assert plan.formation.id == "coding"
        assert plan.formation.implementers  # non-empty
        assert plan.formation.reviewer
        assert plan.formation.topology == "hierarchical"
        assert plan.formation.confidence is not None
        assert plan.formation.confidence >= 0.4
        # Human note still present, structured field is source of truth.
        assert any(n.startswith("formation: coding") for n in plan.assumptions)

    def test_build_plan_binds_marketing_formation(self) -> None:
        plan = build_plan(
            "launch FB ad campaign for the funnel",
            [_raw_task("copy")],
        )
        assert plan.formation is not None
        assert plan.formation.id == "marketing"
        assert plan.formation.topology == "generator_critic"

    def test_build_plan_binds_research_formation(self) -> None:
        plan = build_plan(
            "research competitive evidence for pricing",
            [_raw_task("r1")],
        )
        assert plan.formation is not None
        assert plan.formation.id == "research"
        assert plan.formation.topology == "map_reduce"


# ---------------------------------------------------------------------------
# 3. plan_json round-trip
# ---------------------------------------------------------------------------


class TestPlanJsonRoundTrip:
    def test_plan_payload_contains_formation(self) -> None:
        plan = build_plan(
            "implement feature X",
            [_raw_task("a")],
        )
        payload = plan_payload(plan)
        assert "formation" in payload
        assert payload["formation"] is not None
        assert payload["formation"]["id"] == "coding"
        assert "implementers" in payload["formation"]
        assert "plan_hash" in payload

    def test_model_validate_preserves_formation(self) -> None:
        plan = build_plan(
            "fix the API bug",
            [_raw_task("a")],
        )
        dump = plan.model_dump(mode="json")
        restored = SwarmPlan.model_validate(dump)
        assert restored.formation is not None
        assert restored.formation.id == plan.formation.id  # type: ignore[union-attr]
        assert restored.formation.implementers == plan.formation.implementers  # type: ignore[union-attr]
        assert plan_fingerprint(restored) == plan_fingerprint(plan)

    def test_old_plan_json_without_formation_still_validates(self) -> None:
        legacy = {
            "goal": "legacy goal",
            "tasks": [
                {
                    "id": "t1",
                    "title": "T1",
                    "depends_on": [],
                    "owned_paths": ["src/t1.py"],
                }
            ],
            "assumptions": [],
            "version": 1,
            "target_n": 1,
            "mode": "solo",
        }
        plan = SwarmPlan.model_validate(legacy)
        assert plan.formation is None


# ---------------------------------------------------------------------------
# 4. Observable routing preference
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Cand:
    provider: str
    model: str = "x"
    score: float = 0.5


class TestPreferImplementers:
    def test_grok_before_claude_when_implementers_prefer_grok(self) -> None:
        candidates = [_Cand("claude"), _Cand("grok")]
        ordered = prefer_implementers(candidates, ["grok", "gemini"])
        assert [c.provider for c in ordered] == ["grok", "claude"]

    def test_stable_order_among_non_preferred(self) -> None:
        candidates = [_Cand("claude"), _Cand("codex"), _Cand("kimi")]
        ordered = prefer_implementers(candidates, ["grok"])
        assert [c.provider for c in ordered] == ["claude", "codex", "kimi"]

    def test_implementer_order_preserved(self) -> None:
        candidates = [_Cand("gemini"), _Cand("claude"), _Cand("grok")]
        ordered = prefer_implementers(candidates, ["grok", "gemini"])
        assert [c.provider for c in ordered] == ["grok", "gemini", "claude"]

    def test_empty_implementers_identity(self) -> None:
        candidates = [_Cand("claude"), _Cand("grok")]
        assert prefer_implementers(candidates, []) == list(candidates)

    def test_dict_candidates(self) -> None:
        candidates = [{"provider": "claude"}, {"provider": "grok"}]
        ordered = prefer_implementers(candidates, ["grok", "gemini"])
        assert [c["provider"] for c in ordered] == ["grok", "claude"]

    def test_router_reexport(self) -> None:
        from omniagentos.swarm.router import prefer_implementers as router_prefer

        candidates = [_Cand("claude"), _Cand("grok")]
        ordered = router_prefer(candidates, ["grok", "gemini"])
        assert ordered[0].provider == "grok"


# ---------------------------------------------------------------------------
# 5. provision stamps swarm_json
# ---------------------------------------------------------------------------


class TestProvisionStamps:
    @pytest.fixture
    def db_path(self, tmp_path: Path) -> str:
        db = str(tmp_path / "formation-bind.db")
        CollabStore(db)
        return db

    def test_formation_swarm_json_fields_helper(self) -> None:
        binding = FormationBinding(
            id="coding",
            implementers=["grok", "gemini"],
            reviewer="opus",
            planner="opus",
            mechanical_gate=True,
            confidence=0.85,
            topology="hierarchical",
            reason="keyword_match",
        )
        fields = formation_swarm_json_fields(binding, role="implementer")
        assert fields["formation_id"] == "coding"
        assert fields["formation_implementers"] == ["grok", "gemini"]
        assert fields["formation_reviewer"] == "opus"
        assert fields["formation_mechanical_gate"] is True
        assert fields["formation_role"] == "implementer"
        assert fields["formation_topology"] == "hierarchical"
        assert fields["formation_confidence"] == pytest.approx(0.85)
        assert formation_swarm_json_fields(None) == {}

    def test_provision_stamps_child_and_root(self, db_path: str, tmp_path: Path) -> None:
        dal = SwarmDal(db_path)
        plan = build_plan(
            "fix the login bug",
            [_raw_task("a", paths=("src/a.py",)), _raw_task("b", paths=("src/b.py",))],
        )
        assert plan.formation is not None
        workspace = tmp_path / "ws"
        workspace.mkdir()
        for task_id in ("a", "b"):
            verifier = workspace / "tests" / task_id / "test_smoke.py"
            verifier.parent.mkdir(parents=True)
            verifier.write_text(
                "def test_smoke() -> None:\n    assert True\n",
                encoding="utf-8",
            )
        result = provision_run(plan, dal=dal, working_dir=str(workspace), write_plan_doc=False)

        # plan_json carries structured formation
        run = result["run"]
        import json

        payload = json.loads(run["plan_json"])
        assert payload["formation"]["id"] == "coding"
        assert payload["formation"]["implementers"]

        # root card summary
        root_json = dal.get_swarm_json(result["root_card_id"])
        assert root_json is not None
        assert root_json.get("formation_id") == "coding"
        assert root_json.get("formation_implementers") == list(plan.formation.implementers)
        assert root_json.get("formation_role") == "summary"

        # worker card
        worker_json = dal.get_swarm_json(result["card_ids"]["a"])
        assert worker_json is not None
        assert worker_json["formation_id"] == "coding"
        assert worker_json["formation_implementers"] == list(plan.formation.implementers)
        assert worker_json["formation_reviewer"] == plan.formation.reviewer
        assert worker_json["formation_mechanical_gate"] is plan.formation.mechanical_gate
        assert worker_json["formation_role"] == "implementer"

        # integration card → reviewer role
        if INTEGRATION_TASK_ID in result["card_ids"]:
            integ = dal.get_swarm_json(result["card_ids"][INTEGRATION_TASK_ID])
            assert integ is not None
            assert integ["formation_id"] == "coding"
            assert integ["formation_role"] == "integrator"


# ---------------------------------------------------------------------------
# 6. Manual FormationBinding on SwarmPlan still fingerprints cleanly
# ---------------------------------------------------------------------------


def test_explicit_formation_binding_on_plan() -> None:
    plan = SwarmPlan(
        goal="manual",
        tasks=[SwarmTaskSpec(id="t", title="T")],
        formation=FormationBinding(
            id="operations",
            implementers=["grok"],
            reviewer="opus",
            topology="sequential",
            confidence=0.95,
            reason="task_class_match",
        ),
    )
    dump = plan_payload(plan)
    assert dump["formation"]["id"] == "operations"
    assert SwarmPlan.model_validate(dump).formation.id == "operations"  # type: ignore[union-attr]
