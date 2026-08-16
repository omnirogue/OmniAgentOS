"""Per-template behaviour: the routing decisions that keep a loop safe.

Each template gets at least one test of its distinguishing safety property —
the property its counterfeit attacks.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from conftest import Counter, tool
from omniagentos_loops.contracts import LoopStatus, RiskTier
from omniagentos_loops.runtime import run_once
from omniagentos_loops.templates import TEMPLATES, get_template
from omniagentos_loops.templates import common as template_common
from omniagentos_loops.tools import ToolRegistry

from omniagentos.contracts import ApprovalState

sys.path.insert(0, str(Path(__file__).resolve().parent / "drills"))

from template_kits import KITS  # noqa: E402


def _approve_and_retick(ctx, template, report, store, decider: str = "owner"):
    store.decide_approval(report.approval_id, ApprovalState.APPROVED.value, decider, "ok")
    return run_once(ctx, template)


# --------------------------------------------------------------------------
# catalogue
# --------------------------------------------------------------------------


def test_every_template_declares_its_required_tools_and_a_family():
    assert len(TEMPLATES) == 5
    for name, template in TEMPLATES.items():
        assert template.name == name
        assert template.required_tools, f"{name} declares no tools"
        assert template.family


def test_a_tick_that_reports_no_usable_status_fails_closed():
    """``_status_of`` is where a loop's self-report is minted. Fail CLOSED.

    A missing status, an empty status, or a string this runtime does not know
    used to become ``COMPLETED`` — the single most favourable outcome, awarded
    for saying nothing. Every layer above (routine acceptance, the 50% floor,
    the dashboard) consumes this value as if the loop had reported success.
    """
    from omniagentos_loops.runtime import _status_of

    assert _status_of({"status": LoopStatus.COMPLETED.value}) is LoopStatus.COMPLETED
    assert _status_of({"status": LoopStatus.IDLE.value}) is LoopStatus.IDLE
    for unusable in ({}, {"status": ""}, {"status": None}, {"status": "finished"}, None):
        assert _status_of(unusable) is LoopStatus.FAILED, unusable


def _seam_reports_every_effect_as_failed(monkeypatch):
    """Make the execution seam answer ``succeeded=False`` for every EFFECT.

    That is the fact a seam produces when an effect ran and did not take effect
    — the tool's own result declared failure, or its ``verify=`` predicate
    looked at the world and said no. Reads are untouched. Faking it here (rather
    than building a failing tool per template) is what lets this assert the
    property for EVERY template, including the ones with no verify node.
    """
    real = template_common.execute_effect

    def seam(ctx, **kwargs):
        outcome = real(ctx, **kwargs)
        if kwargs.get("mode", "effect") != "effect":
            return outcome
        return {**outcome, "succeeded": False, "detail": "the probe says it did not take effect"}

    monkeypatch.setattr(template_common, "execute_effect", seam)


@pytest.mark.parametrize("kit_name", sorted(KITS))
def test_an_effect_that_did_not_take_effect_never_renders_as_completed(
    kit_name, make_ctx, store, monkeypatch
):
    """Loudness is STRUCTURAL, not per-template.

    Every template is driven to its effect, the seam reports that the effect did
    not take, and no template is allowed to render the tick COMPLETED. Templates
    with a verify node say so through it (``verification_outcome``); templates
    without one park at the effect. The next loop someone writes gets the second
    behaviour for free — which is the point, because it will not have a verify
    node.
    """
    kit = KITS[kit_name]
    template = get_template(kit.template)
    effect = Counter()
    ctx = make_ctx(
        instance_id=f"loud_{kit_name}",
        template=kit.template,
        params=kit.params,
        tools=kit.registry(effect),
    )
    _seam_reports_every_effect_as_failed(monkeypatch)

    report = run_once(ctx, template)
    if report.status is LoopStatus.PARKED:  # a T2+ effect asks a human first
        report = _approve_and_retick(ctx, template, report, store)

    assert effect.count == 1, "the effect must still have been attempted"
    assert report.status is not LoopStatus.COMPLETED, (
        f"{kit.template} rendered a failed effect as completed: {report.as_dict()}"
    )
    assert report.status is LoopStatus.FAILED, (
        f"{kit.template} settled {report.status} for an effect that did not take effect"
    )
    assert report.as_dict()["accepted"] is False


def test_a_verify_tool_cannot_launder_a_failed_effect(make_ctx, store, monkeypatch):
    """Delegating the verdict to a verify node delegates the WORDING, not the veto.

    ``judged_by="verify"`` is how a template says "that node renders this
    effect's failure". If the node then reports success anyway — a verify tool
    that ignores the effect's result, which is most of them — the tick must
    still not pass.
    """
    template = get_template("poll_classify_act_verify")
    act = Counter()
    registry = ToolRegistry()
    registry.register(tool("poll", RiskTier.T0, Counter(result=[{"id": "msg-1"}])))
    registry.register(tool("classify", RiskTier.T0, Counter(result={"action": "reply"})))
    registry.register(tool("act", RiskTier.T2, act))
    registry.register(tool("verify", RiskTier.T0, Counter(result={"verified": True})))
    ctx = make_ctx(instance_id="laundry", template=template.name, tools=registry)
    _seam_reports_every_effect_as_failed(monkeypatch)

    parked = run_once(ctx, template)
    done = _approve_and_retick(ctx, template, parked, store)

    assert act.count == 1
    assert done.status is LoopStatus.FAILED
    assert "without taking effect" in done.detail


def test_verification_outcome_vetoes_on_a_failed_effect_whatever_the_verdict():
    """The veto, at the unit: a recorded effect failure outranks any verdict."""
    state = {"data": {template_common.FAILED_EFFECTS: ["act"]}}

    passed = template_common.verification_outcome({"verified": True}, state=state)
    assert passed["status"] == LoopStatus.FAILED.value
    assert "act" in passed["error"]

    clean = template_common.verification_outcome({"verified": True}, state={"data": {}})
    assert clean["status"] == LoopStatus.COMPLETED.value


def test_missing_required_tools_fails_before_any_graph_runs(make_ctx):
    template = get_template("poll_classify_act_verify")
    ctx = make_ctx(template=template.name, tools=ToolRegistry())
    report = run_once(ctx, template)
    assert report.status is LoopStatus.FAILED
    assert "missing required tools" in report.detail


# --------------------------------------------------------------------------
# poll -> classify -> act -> verify
# --------------------------------------------------------------------------


def _poll_ctx(make_ctx, act: Counter, items: list[dict[str, Any]], action: str = "reply"):
    registry = ToolRegistry()
    registry.register(tool("poll", RiskTier.T0, Counter(result=items)))
    registry.register(tool("classify", RiskTier.T0, Counter(result={"action": action})))
    registry.register(tool("act", RiskTier.T2, act))
    registry.register(tool("verify", RiskTier.T0, Counter(result={"verified": True})))
    return make_ctx(instance_id="inbox_triage", template="poll_classify_act_verify", tools=registry)


def test_poll_loop_parks_on_the_act_effect_then_acts_once(make_ctx, store):
    template = get_template("poll_classify_act_verify")
    act = Counter()
    ctx = _poll_ctx(make_ctx, act, [{"id": "msg-1"}])

    parked = run_once(ctx, template)
    assert parked.status is LoopStatus.PARKED
    assert act.count == 0

    done = _approve_and_retick(ctx, template, parked, store)
    assert done.status is LoopStatus.COMPLETED
    assert act.count == 1


def test_poll_loop_that_cannot_verify_its_send_is_not_completed(make_ctx, store):
    """R7 at the mint: the verify node's verdict IS the tick's status.

    The act effect ran and was approved; verification says it did not take. A
    tick that ends this way must not be scored as work done.
    """
    template = get_template("poll_classify_act_verify")
    act = Counter()
    registry = ToolRegistry()
    registry.register(tool("poll", RiskTier.T0, Counter(result=[{"id": "msg-1"}])))
    registry.register(tool("classify", RiskTier.T0, Counter(result={"action": "reply"})))
    registry.register(tool("act", RiskTier.T2, act))
    registry.register(
        tool("verify", RiskTier.T0, Counter(result={"verified": False, "state": "not_sent"}))
    )
    ctx = make_ctx(instance_id="inbox_triage", template=template.name, tools=registry)

    parked = run_once(ctx, template)
    done = _approve_and_retick(ctx, template, parked, store)

    assert act.count == 1
    assert done.status is LoopStatus.FAILED, "an unverified effect must not render as completed"
    assert done.as_dict()["accepted"] is False
    assert "not_sent" in done.detail


def test_a_verify_tool_that_returns_no_verdict_is_not_a_success(make_ctx, store):
    """Absence of a verdict is not a verdict — the shape most fakes return."""
    template = get_template("poll_classify_act_verify")
    registry = ToolRegistry()
    registry.register(tool("poll", RiskTier.T0, Counter(result=[{"id": "msg-1"}])))
    registry.register(tool("classify", RiskTier.T0, Counter(result={"action": "reply"})))
    registry.register(tool("act", RiskTier.T2, Counter()))
    registry.register(tool("verify", RiskTier.T0, Counter(result={"ok": True})))
    ctx = make_ctx(instance_id="inbox_triage", template=template.name, tools=registry)

    parked = run_once(ctx, template)
    done = _approve_and_retick(ctx, template, parked, store)

    assert done.status is LoopStatus.FAILED


def test_poll_loop_is_idle_with_no_items(make_ctx):
    template = get_template("poll_classify_act_verify")
    act = Counter()
    report = run_once(_poll_ctx(make_ctx, act, []), template)
    assert report.status is LoopStatus.IDLE
    assert act.count == 0


def test_poll_loop_skips_when_the_classifier_says_skip(make_ctx):
    template = get_template("poll_classify_act_verify")
    act = Counter()
    ctx = _poll_ctx(make_ctx, act, [{"id": "msg-1"}], action="skip")
    report = run_once(ctx, template)
    assert report.status is LoopStatus.IDLE
    assert act.count == 0


# --------------------------------------------------------------------------
# monitor -> diagnose -> repair -> verify
# --------------------------------------------------------------------------


def _monitor_ctx(make_ctx, repair: Counter, escalate: Counter, remedy: str, allowed: list[str]):
    registry = ToolRegistry()
    registry.register(tool("monitor", RiskTier.T0, Counter(result={"api": "down"})))
    registry.register(
        tool("diagnose", RiskTier.T0, Counter(result={"remedy": remedy, "incident": "i-1"}))
    )
    registry.register(tool("repair", RiskTier.T1, repair))
    registry.register(tool("escalate", RiskTier.T3, escalate))
    registry.register(tool("verify", RiskTier.T0, Counter(result={"verified": True})))
    return make_ctx(
        instance_id="error_monitor",
        template="monitor_diagnose_repair_verify",
        params={"allowed_remedies": allowed},
        tools=registry,
    )


def test_allowlisted_remedy_auto_repairs_without_an_approval(make_ctx):
    template = get_template("monitor_diagnose_repair_verify")
    repair, escalate = Counter(), Counter()
    ctx = _monitor_ctx(make_ctx, repair, escalate, "restart_api", ["restart_api"])
    report = run_once(ctx, template)
    assert report.status is LoopStatus.COMPLETED
    assert repair.count == 1
    assert escalate.count == 0


def test_unknown_remedy_escalates_and_does_not_auto_repair(make_ctx):
    template = get_template("monitor_diagnose_repair_verify")
    repair, escalate = Counter(), Counter()
    ctx = _monitor_ctx(make_ctx, repair, escalate, "rm_rf_var", ["restart_api"])
    report = run_once(ctx, template)
    assert report.status is LoopStatus.PARKED, "a non-allowlisted remedy must park, not run"
    assert repair.count == 0
    assert escalate.count == 0


def test_a_repair_that_did_not_take_is_not_a_completed_tick(make_ctx):
    """The live W3 shape: verify says ``repair_failed``, the tick used to pass."""
    template = get_template("monitor_diagnose_repair_verify")
    repair, escalate = Counter(), Counter()
    registry = ToolRegistry()
    registry.register(tool("monitor", RiskTier.T0, Counter(result={"api": "down"})))
    registry.register(
        tool(
            "diagnose",
            RiskTier.T0,
            Counter(result={"remedy": "restart_api", "incident": "i-1"}),
        )
    )
    registry.register(tool("repair", RiskTier.T1, repair))
    registry.register(tool("escalate", RiskTier.T3, escalate))
    registry.register(
        tool("verify", RiskTier.T0, Counter(result={"verified": False, "state": "repair_failed"}))
    )
    ctx = make_ctx(
        instance_id="error_monitor",
        template=template.name,
        params={"allowed_remedies": ["restart_api"]},
        tools=registry,
    )

    report = run_once(ctx, template)

    assert repair.count == 1
    assert report.status is LoopStatus.FAILED
    assert report.as_dict()["accepted"] is False
    assert "repair_failed" in report.detail


def test_the_repair_effect_is_handed_the_diagnosis_it_reads(make_ctx):
    """The args contract, pinned at the template level.

    A repair tool consumes the DIAGNOSIS; the monitor snapshot is not a
    substitute for it, and it must not be in the payload at all — the args
    digest is bound into the approvals row and a snapshot drifts every tick.
    """
    template = get_template("monitor_diagnose_repair_verify")
    repair, escalate = Counter(), Counter()
    diagnosis = {"remedy": "restart_api", "incident": "i-1", "label": "com.example.api"}
    registry = ToolRegistry()
    registry.register(
        tool("monitor", RiskTier.T0, Counter(result={"snapshot": {"ts": "now"}, "logs": {}}))
    )
    registry.register(tool("diagnose", RiskTier.T0, Counter(result=diagnosis)))
    registry.register(tool("repair", RiskTier.T1, repair))
    registry.register(tool("escalate", RiskTier.T3, escalate))
    registry.register(tool("verify", RiskTier.T0, Counter(result={"verified": True})))
    ctx = make_ctx(
        instance_id="error_monitor",
        template=template.name,
        params={"allowed_remedies": ["restart_api"]},
        tools=registry,
    )

    run_once(ctx, template)

    assert repair.count == 1
    assert repair.calls[0] == {"remedy": "restart_api", "diagnosis": diagnosis}


def test_healthy_snapshot_is_idle(make_ctx):
    template = get_template("monitor_diagnose_repair_verify")
    repair, escalate = Counter(), Counter()
    ctx = _monitor_ctx(make_ctx, repair, escalate, "", ["restart_api"])
    report = run_once(ctx, template)
    assert report.status is LoopStatus.IDLE
    assert repair.count == 0


# --------------------------------------------------------------------------
# generate -> evaluate -> improve
# --------------------------------------------------------------------------


def _refine_ctx(make_ctx, publish: Counter, scores: list[float]):
    seen: list[float] = []

    def evaluate(**kwargs: Any) -> dict[str, Any]:
        score = scores[min(len(seen), len(scores) - 1)]
        seen.append(score)
        return {"score": score, "feedback": "tighter"}

    registry = ToolRegistry()
    registry.register(tool("generate", RiskTier.T0, Counter(result={"text": "draft"})))
    registry.register(tool("evaluate", RiskTier.T0, evaluate))
    registry.register(tool("publish", RiskTier.T2, publish))
    return make_ctx(
        instance_id="content_loop",
        template="generate_evaluate_improve",
        params={"brief": "write a post", "max_rounds": 3, "score_threshold": 0.8},
        tools=registry,
    )


def test_improve_cycle_publishes_once_the_threshold_is_met(make_ctx, store):
    template = get_template("generate_evaluate_improve")
    publish = Counter()
    ctx = _refine_ctx(make_ctx, publish, [0.4, 0.9])
    parked = run_once(ctx, template)
    assert parked.status is LoopStatus.PARKED
    assert publish.count == 0
    done = _approve_and_retick(ctx, template, parked, store)
    assert done.status is LoopStatus.COMPLETED
    assert publish.count == 1


def test_exhausted_rounds_do_not_publish(make_ctx):
    template = get_template("generate_evaluate_improve")
    publish = Counter()
    ctx = _refine_ctx(make_ctx, publish, [0.1])
    report = run_once(ctx, template)
    assert report.status is LoopStatus.ABORTED
    assert "below threshold" in report.detail
    assert publish.count == 0


# --------------------------------------------------------------------------
# dispatch -> await -> summarize
# --------------------------------------------------------------------------


def _dispatch_ctx(make_ctx, dispatch: Counter, card_states: list[dict[str, Any]]):
    seen: list[int] = []

    def poll_card(**kwargs: Any) -> dict[str, Any]:
        state = card_states[min(len(seen), len(card_states) - 1)]
        seen.append(1)
        return state

    registry = ToolRegistry()
    registry.register(tool("dispatch", RiskTier.T2, dispatch))
    registry.register(tool("poll_card", RiskTier.T0, poll_card))
    registry.register(tool("summarize", RiskTier.T1, Counter(result="summary")))
    return make_ctx(
        instance_id="swarm_wrapper",
        template="dispatch_await_summarize",
        params={"spec": {"title": "fix the thing"}, "max_wait_ticks": 3},
        tools=registry,
    )


def test_dispatch_waits_across_ticks_and_never_dispatches_twice(make_ctx, store):
    template = get_template("dispatch_await_summarize")
    dispatch = Counter(result={"ref": "card-1"})
    ctx = _dispatch_ctx(
        make_ctx, dispatch, [{"done": False}, {"done": False}, {"done": True, "outcome": "merged"}]
    )

    parked = run_once(ctx, template)
    assert parked.status is LoopStatus.PARKED
    assert dispatch.count == 0

    waiting = _approve_and_retick(ctx, template, parked, store)
    assert waiting.status is LoopStatus.IDLE
    assert dispatch.count == 1

    still_waiting = run_once(ctx, template)
    assert still_waiting.status is LoopStatus.IDLE
    assert dispatch.count == 1, "re-entering dispatch must replay the receipt, not re-submit"

    finished = run_once(ctx, template)
    assert finished.status is LoopStatus.COMPLETED
    assert dispatch.count == 1


def test_dispatch_gives_up_after_the_wait_budget(make_ctx, store):
    template = get_template("dispatch_await_summarize")
    dispatch = Counter(result={"ref": "card-1"})
    ctx = _dispatch_ctx(make_ctx, dispatch, [{"done": False}])
    parked = run_once(ctx, template)
    _approve_and_retick(ctx, template, parked, store)
    statuses = [run_once(ctx, template).status for _ in range(4)]
    assert LoopStatus.ABORTED in statuses
    assert dispatch.count == 1
