"""AT3 area 12 — FORMATION FAIRNESS (the anti-confound test).

Claim under test: when two formations compete, they must receive the SAME
prompts, models, tools, budgets and environment. **Only the formation differs.**

A test that merely compares config keys is worthless here, so every assertion
below compares what an arm actually RECEIVES:

  * §1 the full task contract each arm's workers are handed (``build_plan``
    driven twice with byte-identical inputs and only the selected formation
    swapped);
  * §2 the executor step payload each arm is dispatched with (``_case_plan``);
  * §3 the blind presentation each judge is shown (``lab/eval/blind.py``);
  * §4 the coin-flipped presentation ORDER and the score un-swap
    (``lab/tournament/core.py``);
  * §5 one whole dry-run tournament, asserting both arms were run against the
    same hash-pinned arena task.

Ground truth: ``omniagentos/formation/selector.py``, ``configs/formations.yaml``,
``omniagentos/lab/eval/blind.py``, ``omniagentos/lab/tournament/core.py``,
``omniagentos/lab/executor/__init__.py``.

Hermetic: pure functions and in-memory doubles. No network, no LLM.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from omniagentos.formation import selector as selector_module
from omniagentos.formation.selector import (
    Formation,
    FormationSelection,
    select_formation_with_confidence,
)
from omniagentos.lab.contracts import CandidateEvalCase, EvalSplit, SurfaceKind
from omniagentos.lab.eval.blind import build_blind_pairs, unlinkable_token
from omniagentos.lab.executor import _case_plan
from omniagentos.lab.tournament import core as tournament_core
from omniagentos.swarm.planner import build_plan

ARM_A = "research"
ARM_B = "creative"


@pytest.fixture(autouse=True)
def _planner_flags_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "OMNIAGENTOS_CREATIVE_TOPOLOGY_MODE",
        "OMNIAGENTOS_TASK_SHAPE_FANOUT_MODE",
        "OMNIAGENTOS_SWARM_TARGET_CAP",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# §1 — The work contract handed to each arm's workers
# ---------------------------------------------------------------------------

_GOAL = "ship the widget end to end"
_RAW_TASKS: list[dict[str, Any]] = [
    {
        "id": tid,
        "title": tid.upper(),
        "description": f"do {tid}",
        "depends_on": [],
        "owned_paths": [f"src/{tid}.py"],
        "est_agent_minutes": 10,
        "est_manual_minutes": 30,
        "acceptance": f"{tid} done",
        "verify_command": f"pytest tests/{tid}",
    }
    for tid in ("alpha", "beta", "gamma", "delta")
]


def _pin_formation(monkeypatch: pytest.MonkeyPatch, formation_id: str) -> Formation:
    """Force ``build_plan``'s formation choice, changing NOTHING else.

    ``build_plan`` imports ``select_formation_with_confidence`` from the
    ``omniagentos.formation`` package at call time, so patching the package
    attribute is the single-variable seam.
    """
    formation = selector_module._all()[formation_id]
    selection = FormationSelection(formation=formation, confidence=0.95, reason="task_class_match")
    monkeypatch.setattr(
        "omniagentos.formation.select_formation_with_confidence",
        lambda **_kwargs: selection,
    )
    return formation


def _plan_for(monkeypatch: pytest.MonkeyPatch, formation_id: str) -> Any:
    _pin_formation(monkeypatch, formation_id)
    return build_plan(
        _GOAL,
        copy.deepcopy(_RAW_TASKS),
        suite_command="pytest -q",
        category=None,
    )


class TestTheWorkContractIsIdenticalAcrossFormations:
    def test_two_formations_receive_byte_identical_task_contracts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The independent variable moved; the work did not."""
        plan_a = _plan_for(monkeypatch, ARM_A)
        plan_b = _plan_for(monkeypatch, ARM_B)

        # The independent variable really did change (otherwise this is vacuous).
        assert plan_a.formation is not None and plan_b.formation is not None
        assert plan_a.formation.id == ARM_A
        assert plan_b.formation.id == ARM_B
        assert plan_a.formation.id != plan_b.formation.id

        tasks_a = {t.id: t.model_dump(mode="json") for t in plan_a.tasks}
        tasks_b = {t.id: t.model_dump(mode="json") for t in plan_b.tasks}
        assert tasks_a.keys() == tasks_b.keys()
        for task_id in tasks_a:
            assert tasks_a[task_id] == tasks_b[task_id], (
                f"formation changed the contract of task {task_id!r} — "
                "prompts/paths/acceptance/verify must not depend on the arm"
            )

    def test_goal_and_verification_commands_do_not_depend_on_the_formation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan_a = _plan_for(monkeypatch, ARM_A)
        plan_b = _plan_for(monkeypatch, ARM_B)

        assert plan_a.goal == plan_b.goal == _GOAL
        assert plan_a.category == plan_b.category
        assert [t.verify_command for t in plan_a.tasks] == [
            t.verify_command for t in plan_b.tasks
        ]
        assert [t.owned_paths for t in plan_a.tasks] == [t.owned_paths for t in plan_b.tasks]
        assert [t.depends_on for t in plan_a.tasks] == [t.depends_on for t in plan_b.tasks]
        assert plan_a.integration_task_id == plan_b.integration_task_id

    def test_the_only_differing_plan_fields_are_formation_owned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whole-object diff: name every field the arm is allowed to move."""
        dump_a = _plan_for(monkeypatch, ARM_A).model_dump(mode="json")
        dump_b = _plan_for(monkeypatch, ARM_B).model_dump(mode="json")

        differing = {k for k in dump_a if dump_a[k] != dump_b[k]}
        # ``assumptions`` carries the human-readable formation note; ``target_n``
        # and ``mode`` follow from the formation's topology. Everything else —
        # goal, tasks, category, integration_task_id, parallelism_ratio, version
        # — must be identical.
        assert differing <= {"formation", "assumptions", "target_n", "mode"}
        assert "formation" in differing, "the arms were not actually different"
        assert "tasks" not in differing

    def test_formation_selection_is_deterministic_for_identical_inputs(self) -> None:
        """No hidden RNG, clock, or global state may decide the arm."""
        first = select_formation_with_confidence(goal=_GOAL, task_class="research")
        second = select_formation_with_confidence(goal=_GOAL, task_class="research")
        assert first.formation.id == second.formation.id
        assert first.confidence == second.confidence
        assert first.reason == second.reason

    def test_every_competing_formation_exposes_the_same_role_shape(self) -> None:
        """One arm having a role the other lacks would confound any comparison."""
        formations = selector_module._all()
        assert len(formations) >= 2
        shapes = {fid: [role.name for role in f.roles()] for fid, f in formations.items()}
        reference = shapes[ARM_A]
        assert reference == ["planner", "implementer", "reviewer"]
        for fid, shape in shapes.items():
            assert shape == reference, f"formation {fid} has a different role shape"

    def test_every_formation_fields_a_complete_team(self) -> None:
        """An arm with no implementer is not a competitor, it is a forfeit."""
        for fid, formation in selector_module._all().items():
            assert formation.implementers, f"formation {fid} has no implementers"
            assert formation.reviewer, f"formation {fid} has no reviewer"
            assert formation.planner, f"formation {fid} has no planner"


# ---------------------------------------------------------------------------
# §2 — The executor payload each arm is dispatched with
# ---------------------------------------------------------------------------


def _case() -> CandidateEvalCase:
    return CandidateEvalCase(
        id="case-1", split=EvalSplit.DEV, input={"n": 7}, rubric="be correct"
    )


class TestDispatchPayloadParity:
    def test_two_arms_receive_identical_payloads_except_their_own_content(self) -> None:
        case = _case()
        surface = {"kind": SurfaceKind.PROMPT.value, "agent": "cli-claude"}

        plan_a, harness_a, budget_a = _case_plan(
            surface, "ARM A SYSTEM PROMPT", case, "/wd", dry_run=True
        )
        plan_b, harness_b, budget_b = _case_plan(
            dict(surface), "ARM B SYSTEM PROMPT", case, "/wd", dry_run=True
        )

        assert harness_a == harness_b, "arms were dispatched to different adapters"
        assert budget_a == budget_b, "arms received different budgets"
        assert len(plan_a) == len(plan_b) == 1

        step_a, step_b = plan_a[0], plan_b[0]
        assert step_a["kind"] == step_b["kind"]
        assert step_a["action_class"] == step_b["action_class"]
        assert step_a["params"]["adapter"] == step_b["params"]["adapter"]
        assert step_a["params"]["working_dir"] == step_b["params"]["working_dir"]

        exec_a = step_a["params"]["metadata"]["executor"]
        exec_b = step_b["params"]["metadata"]["executor"]
        # The CASE the arms are graded on is identical, field for field.
        assert exec_a["case_input"] == exec_b["case_input"]
        assert exec_a["rubric"] == exec_b["rubric"]
        assert exec_a["mode"] == exec_b["mode"]
        # ... and the arm's own content is the ONLY difference in the payload.
        differing = {k for k in exec_a if exec_a[k] != exec_b[k]}
        assert differing == {"system_prompt"}

    def test_neither_arm_may_be_handed_extra_tools(self) -> None:
        """No tool allowlist is threaded per arm — a difference would confound."""
        case = _case()
        surface = {"kind": SurfaceKind.PROMPT.value, "agent": "cli-claude"}
        plan, _, _ = _case_plan(surface, "prompt", case, "/wd", dry_run=True)
        params = plan[0]["params"]
        assert "tools_allowed" not in params
        assert "tools" not in params

    def test_a_case_carries_no_expected_answer_to_either_arm(self) -> None:
        """Structural: leaking ``expected`` to one arm would be the confound."""
        assert "expected" not in CandidateEvalCase.model_fields
        case = _case()
        plan, _, _ = _case_plan(
            {"kind": SurfaceKind.PROMPT.value}, "prompt", case, "/wd", dry_run=True
        )
        assert "expected" not in str(plan)

    def test_the_environment_scrub_list_is_arm_independent(self) -> None:
        from omniagentos.lab.executor import _SCRUBBED_CANDIDATE_ENV_VARS

        assert _SCRUBBED_CANDIDATE_ENV_VARS, "no env scrubbing at all would be a confound"
        # A single module-level tuple: there is no per-arm branch to diverge on.
        assert isinstance(_SCRUBBED_CANDIDATE_ENV_VARS, tuple)
        assert "TMPDIR" in _SCRUBBED_CANDIDATE_ENV_VARS


# ---------------------------------------------------------------------------
# §3 — The blind presentation the judge is shown
# ---------------------------------------------------------------------------


class TestBlindPresentation:
    def test_a_blind_token_carries_no_information_about_its_arm(self) -> None:
        tokens = [unlinkable_token() for _ in range(256)]
        assert len(set(tokens)) == len(tokens), "tokens repeat — they are not independent draws"
        assert all(len(t) >= 24 for t in tokens)

    def test_the_same_case_and_arm_never_produce_a_reproducible_token(self) -> None:
        """A token derived from (case_id, arm) would be trivially unblindable."""
        outputs = {"case-1": {"champion": {"quality": 1}, "challenger": {"quality": 1}}}
        pairs_1, map_1, _ = build_blind_pairs(copy.deepcopy(outputs), seed=7)
        pairs_2, map_2, _ = build_blind_pairs(copy.deepcopy(outputs), seed=7)

        assert set(map_1) & set(map_2) == set(), (
            "tokens repeated across runs — they are derived, not freshly drawn"
        )
        # Order IS reproducible from the seed; identity is not.
        assert [p["case_id"] for p in pairs_1] == [p["case_id"] for p in pairs_2]

    def test_what_the_judge_is_shown_contains_no_arm_label(self) -> None:
        outputs = {
            "case-1": {"champion": {"text": "A"}, "challenger": {"text": "B"}},
            "case-2": {"champion": {"text": "C"}, "challenger": {"text": "D"}},
        }
        pairs, token_map, _ = build_blind_pairs(outputs, seed=11)

        assert len(pairs) == 4
        for pair in pairs:
            assert set(pair) == {"case_id", "blind_token", "output"}
            assert "arm" not in pair
            assert "champion" not in str(pair["blind_token"])
            assert "challenger" not in str(pair["blind_token"])
        # The link exists only in the caller-held map.
        assert {arm for _case, arm in token_map.values()} == {"champion", "challenger"}
        assert len(set(token_map)) == 4

    def test_the_payload_handed_to_a_judge_is_stripped_of_config_identity(self) -> None:
        payload = tournament_core._blind_payload(
            {"config_id": "cfg_champion", "id": "row-1", "quality": 0.9}, "tok"
        )
        assert payload["blind_token"] == "tok"
        assert "config_id" not in payload["output"]
        assert "id" not in payload["output"]
        assert payload["output"] == {"quality": 0.9}


# ---------------------------------------------------------------------------
# §4 — Presentation order must not change the recorded score
# ---------------------------------------------------------------------------


class _OrderRecordingJudge:
    """Records the order it was shown and always prefers whatever it sees first."""

    def __init__(self) -> None:
        self.seen: list[list[Any]] = []

    def judge_blind(
        self, *, output_a: Any, output_b: Any, arena_task: dict[str, Any]
    ) -> dict[str, Any]:
        self.seen.append([output_a["output"], output_b["output"]])
        return {
            "score_a": output_a["output"]["quality"],
            "score_b": output_b["output"]["quality"],
            "notes": "judged in shown order",
        }


class TestPresentationOrderFairness:
    @pytest.mark.parametrize("forced_flip", [0, 1])
    def test_scores_are_unswapped_back_to_canonical_arm_order(
        self, monkeypatch: pytest.MonkeyPatch, forced_flip: int
    ) -> None:
        monkeypatch.setattr(
            tournament_core.secrets, "randbits", lambda _n, _f=forced_flip: _f
        )
        judge = _OrderRecordingJudge()
        arm_a = {"quality": 0.9}
        arm_b = {"quality": 0.4}

        score_a, score_b, notes = tournament_core._call_judge(
            judge, arm_a, arm_b, {"prompt": "x"}
        )

        # The verdict is attributed to the ARM, never to the slot.
        assert (score_a, score_b) == (0.9, 0.4)
        assert ("[presentation-order swapped]" in notes) is bool(forced_flip)
        # And the flip genuinely happened — otherwise the un-swap is untested.
        assert judge.seen[0] == ([arm_b, arm_a] if forced_flip else [arm_a, arm_b])

    def test_the_coin_flip_actually_varies_presentation_order(self) -> None:
        """Guard against a degenerate RNG that always shows the same order."""
        judge = _OrderRecordingJudge()
        for _ in range(64):
            tournament_core._call_judge(judge, {"quality": 0.9}, {"quality": 0.4}, {"p": "x"})
        first_slots = [shown[0]["quality"] for shown in judge.seen]
        assert set(first_slots) == {0.9, 0.4}, "presentation order never varied"

    def test_each_judged_pair_gets_freshly_drawn_tokens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen_tokens: list[str] = []

        class _TokenJudge:
            def judge_blind(
                self, *, output_a: Any, output_b: Any, arena_task: dict[str, Any]
            ) -> dict[str, Any]:
                seen_tokens.extend([output_a["blind_token"], output_b["blind_token"]])
                return {"score_a": 1.0, "score_b": 0.0, "notes": ""}

        monkeypatch.setattr(tournament_core.secrets, "randbits", lambda _n: 0)
        for _ in range(8):
            tournament_core._call_judge(_TokenJudge(), {"q": 1}, {"q": 0}, {"p": "x"})
        assert len(set(seen_tokens)) == len(seen_tokens) == 16


# ---------------------------------------------------------------------------
# §5 — One whole tournament: both arms run the SAME hash-pinned task
# ---------------------------------------------------------------------------


class _FakeLabStore:
    def __init__(self) -> None:
        self.tournaments: list[Any] = []
        self.matches: list[Any] = []
        self.elos: dict[tuple[str, str], Any] = {}
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def create_tournament(self, tournament: Any) -> None:
        self.tournaments.append(tournament)

    def get_elo(self, subject: str, config_id: str) -> Any:
        return self.elos.get((subject, config_id))

    def upsert_elo(self, elo: Any) -> None:
        self.elos[(elo.subject, elo.config_id)] = elo

    def record_match(self, match: Any) -> None:
        self.matches.append(match)

    def update_tournament(self, tournament_id: str, updates: dict[str, Any]) -> None:
        self.updates.append((tournament_id, updates))


class _ArenaRecordingJudge:
    def __init__(self) -> None:
        self.arena_tasks: list[dict[str, Any]] = []

    def judge_blind(
        self, *, output_a: Any, output_b: Any, arena_task: dict[str, Any]
    ) -> dict[str, Any]:
        self.arena_tasks.append(arena_task)
        return {
            "score_a": float(output_a["output"]["quality"]),
            "score_b": float(output_b["output"]["quality"]),
            "notes": "",
        }


class TestTournamentRunsBothArmsOnTheSameTask:
    def test_every_arm_is_judged_against_one_hash_pinned_arena_task(self) -> None:
        store = _FakeLabStore()
        judge = _ArenaRecordingJudge()
        arena_task = {
            "prompt": "solve it",
            "mock_outputs": {
                "cfg_a": {"config_id": "cfg_a", "quality": 0.9},
                "cfg_b": {"config_id": "cfg_b", "quality": 0.4},
            },
        }

        tournament = tournament_core.run_tournament(
            store, judge, "orchestration", "coding", ["cfg_a", "cfg_b"], arena_task, dry_run=True
        )

        assert tournament.arena_task_hash == tournament_core._arena_task_hash(arena_task)
        assert judge.arena_tasks and all(t == arena_task for t in judge.arena_tasks)
        assert len(store.matches) == 1
        match = store.matches[0]
        assert match.blind is True
        assert (match.config_a, match.config_b) == ("cfg_a", "cfg_b")
        assert (match.score_a, match.score_b) == (0.9, 0.4)
        assert match.winner == "cfg_a"
        assert tournament.winner_config_id == "cfg_a"

    def test_the_arena_hash_is_order_insensitive_but_content_sensitive(self) -> None:
        left = tournament_core._arena_task_hash({"a": 1, "b": 2})
        right = tournament_core._arena_task_hash({"b": 2, "a": 1})
        assert left == right, "key order must not change the fairness pin"
        assert left != tournament_core._arena_task_hash({"a": 1, "b": 3})

    def test_a_one_arm_tournament_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least two"):
            tournament_core.run_tournament(
                _FakeLabStore(), _ArenaRecordingJudge(), "s", "d", ["only"], {"p": "x"}
            )

    def test_the_same_config_may_not_be_entered_twice(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            tournament_core.run_tournament(
                _FakeLabStore(), _ArenaRecordingJudge(), "s", "d", ["a", "a"], {"p": "x"}
            )
