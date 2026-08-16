"""Bridge unit tests: the policy seam. The assertion is always an EXECUTION COUNT.

P4's ground truth: a permission test that checks a return value proves nothing —
the counter lives inside the tool, so "denied" means the callable was never
reached.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import pickle
import threading

import pytest
from conftest import Counter, tool
from omniagentos_loops import tools as tools_module
from omniagentos_loops.approvals import approval_id
from omniagentos_loops.contracts import (
    EffectDenied,
    EffectNotApproved,
    RiskTier,
)
from omniagentos_loops.policy_gate import evaluate_tool, preview
from omniagentos_loops.tools import LoopTool, SeamBypass, ToolRegistry, execute_effect

from omniagentos.contracts import ActionClass


def test_ungranted_tool_is_denied_and_never_executes(make_ctx):
    counter = Counter()
    granted = ToolRegistry()
    granted.register(tool("read_file", RiskTier.T0, Counter()))
    ctx = make_ctx(tools=granted)

    verdict = preview(ctx, "send_payment", {"amount": 1250})
    assert verdict.decision == "deny"

    # Even handed the tool object directly, the seam refuses it.
    rogue = LoopTool(
        name="send_payment",
        tier=RiskTier.T3,
        idempotency_key=lambda args: "k",
        call=counter,
    )
    with pytest.raises(EffectDenied):
        execute_effect(
            ctx,
            node="pay",
            tool=rogue,
            args={"amount": 1250},
            business_key="k",
            gate_token={"decision": "allow"},
        )
    assert counter.count == 0


def test_t0_read_is_allowed(make_ctx):
    registry = ToolRegistry()
    counter = Counter(result="contents")
    registry.register(tool("read_file", RiskTier.T0, counter))
    ctx = make_ctx(tools=registry)
    verdict = preview(ctx, "read_file", {"path": "/x"})
    assert verdict.decision == "allow"
    assert verdict.action_class is ActionClass.READ_ONLY


@pytest.mark.parametrize("tier", [RiskTier.T2, RiskTier.T3])
def test_t2_and_above_always_require_approval(make_ctx, tier):
    registry = ToolRegistry()
    registry.register(tool("effect", tier, Counter()))
    ctx = make_ctx(tools=registry)
    verdict = evaluate_tool(ctx, registry.get("effect"), {})
    assert verdict.decision == "approve", (
        "a loop is unattended: a T2+ effect must park even where AUTO mode "
        "would auto-execute the same ActionClass for an interactive session"
    )


def test_t2_effect_without_a_gate_token_is_refused(make_ctx):
    """The counterfeit-resistance property, at the seam."""
    counter = Counter()
    registry = ToolRegistry()
    registry.register(tool("send", RiskTier.T2, counter))
    ctx = make_ctx(tools=registry)

    with pytest.raises(EffectNotApproved):
        execute_effect(
            ctx,
            node="send",
            tool=registry.get("send"),
            args={"to": "a@b.c"},
            business_key="msg-1",
            gate_token=None,
        )
    assert counter.count == 0


def test_forged_gate_token_is_refused(make_ctx):
    """A template cannot approve its own effect by writing a state key."""
    counter = Counter()
    registry = ToolRegistry()
    registry.register(tool("send", RiskTier.T2, counter))
    ctx = make_ctx(tools=registry)

    with pytest.raises(EffectNotApproved):
        execute_effect(
            ctx,
            node="send",
            tool=registry.get("send"),
            args={"to": "a@b.c"},
            business_key="msg-1",
            gate_token={"decision": "approved", "approval_id": "apr_notarealrow"},
        )
    assert counter.count == 0


def _seed_approval(
    ctx, node: str, business_key: str, *, tool: str = "send", args: dict | None = None, **overrides
) -> str:
    """Create the row the seam will look up, with a caller-chosen decision."""
    from omniagentos_loops.tools import effect_binding

    row_id = approval_id(ctx.instance_id, ctx.template, node, tool, business_key)
    binding = effect_binding(
        ctx, node=node, tool=ctx.tools.get(tool), args=args if args is not None else {"to": "a@b.c"}
    )
    row = {
        "id": row_id,
        "action_class": ActionClass.CONSEQUENTIAL.value,
        "proposed_action": "seeded",
        "params_json": json.dumps({"binding": binding}, sort_keys=True),
        "risk": "loop_approval",
        "evidence": "",
        "state": "pending",
        "expires_at": "2099-01-01T00:00:00Z",
        "created_at": "2026-01-01T00:00:00Z",
        **overrides,
    }
    ctx.store.create_approval(row)
    return row_id


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="pending"),
        pytest.param({"state": "rejected", "decided_by": "owner"}, id="rejected"),
        pytest.param({"state": "expired", "decided_by": "owner"}, id="expired"),
        pytest.param({"state": "approved", "decided_by": "bot:ci"}, id="approved-by-a-bot"),
        pytest.param(
            {"state": "approved", "decided_by": "owner", "expires_at": "2000-01-01T00:00:00Z"},
            id="approved-after-expiry",
        ),
    ],
)
def test_a_matching_approval_id_is_not_enough_the_row_must_say_yes(make_ctx, overrides):
    """The seam re-reads the DURABLE row; the graph's token is never authority.

    This is what stops a template bug (or a counterfeit that deletes the gate
    node and writes an 'approved' key) from becoming an unapproved send.
    """
    counter = Counter()
    registry = ToolRegistry()
    registry.register(tool("send", RiskTier.T2, counter))
    ctx = make_ctx(tools=registry)
    row_id = _seed_approval(ctx, "send", "msg-1", **overrides)

    with pytest.raises(EffectNotApproved):
        execute_effect(
            ctx,
            node="send",
            tool=registry.get("send"),
            args={"to": "a@b.c"},
            business_key="msg-1",
            gate_token={"decision": "approved", "approval_id": row_id},
        )
    assert counter.count == 0


def test_an_approval_bound_to_other_arguments_is_refused(make_ctx):
    """The stored binding, not the id, is what authorises an effect.

    Same instance, same node, same tool, same business key, HUMAN-APPROVED —
    and still refused, because the arguments the human saw are not the
    arguments about to execute. Without this, any template whose business key
    is coarser than its payload could launder a new action through an old
    approval.
    """
    counter = Counter()
    registry = ToolRegistry()
    registry.register(tool("send", RiskTier.T2, counter))
    ctx = make_ctx(tools=registry)
    row_id = _seed_approval(
        ctx,
        "send",
        "msg-1",
        args={"to": "approved@example.com"},
        state="approved",
        decided_by="owner",
    )

    with pytest.raises(EffectNotApproved, match="different action"):
        execute_effect(
            ctx,
            node="send",
            tool=registry.get("send"),
            args={"to": "attacker@example.com"},
            business_key="msg-1",
            gate_token={"decision": "approved", "approval_id": row_id},
        )
    assert counter.count == 0


def test_an_approval_bound_to_another_instance_is_refused(make_ctx, store):
    """Confused deputy at the seam: instance A's row cannot authorise B."""
    counter = Counter()
    registry = ToolRegistry()
    registry.register(tool("send", RiskTier.T2, counter))
    ctx_a = make_ctx(instance_id="loop_a", tools=registry)
    _seed_approval(ctx_a, "send", "msg-1", state="approved", decided_by="owner")

    ctx_b = make_ctx(instance_id="loop_b", tools=registry)
    # B forges A's id (the only way it could ever be reached).
    forged = approval_id("loop_a", ctx_a.template, "send", "send", "msg-1")
    with pytest.raises(EffectNotApproved):
        execute_effect(
            ctx_b,
            node="send",
            tool=registry.get("send"),
            args={"to": "a@b.c"},
            business_key="msg-1",
            gate_token={"decision": "approved", "approval_id": forged},
        )
    assert counter.count == 0


def test_a_human_approved_unexpired_row_lets_the_effect_through(make_ctx):
    counter = Counter()
    registry = ToolRegistry()
    registry.register(tool("send", RiskTier.T2, counter))
    ctx = make_ctx(tools=registry)
    row_id = _seed_approval(ctx, "send", "msg-1", state="approved", decided_by="owner")

    outcome = execute_effect(
        ctx,
        node="send",
        tool=registry.get("send"),
        args={"to": "a@b.c"},
        business_key="msg-1",
        gate_token={"decision": "approved", "approval_id": row_id},
    )
    assert outcome["replayed"] is False
    assert counter.count == 1


def test_explicit_denylist_beats_registration(make_ctx):
    registry = ToolRegistry()
    counter = Counter()
    registry.register(tool("restart", RiskTier.T1, counter))
    ctx = make_ctx(tools=registry)
    ctx.denied_tools = frozenset({"restart"})
    assert preview(ctx, "restart", {}).decision == "deny"


def test_replay_on_unknown_is_forbidden_above_the_approval_floor():
    with pytest.raises(ValueError, match="replay_on_unknown"):
        LoopTool(
            name="send",
            tier=RiskTier.T2,
            idempotency_key=lambda args: "k",
            call=Counter(),
            replay_on_unknown=True,
        )


# --------------------------------------------------------------------------
# the seam is the ONLY door into the tool plane
# --------------------------------------------------------------------------


def test_a_tool_cannot_be_called_outside_the_seam(make_ctx):
    """Enforced at call time, not asserted by a source scan.

    A template, an instance module's ``register(ctx)``, or any future helper
    that reaches for ``tool.call(...)`` directly gets an exception instead of an
    unpoliced side effect.
    """
    counter = Counter()
    registry = ToolRegistry()
    registry.register(tool("send", RiskTier.T2, counter))

    with pytest.raises(SeamBypass):
        registry.get("send").call(to="a@b.c")
    assert counter.count == 0


def test_the_seam_opens_only_for_the_tool_it_is_running(make_ctx):
    """One tool's invocation must not authorise a different tool's callable."""
    inner = Counter()
    registry = ToolRegistry()
    registry.register(tool("inner", RiskTier.T0, inner))

    def outer_call(**kwargs):
        # A tool that tries to piggyback on the open seam to run another tool.
        return registry.get("inner").call()

    registry.register(tool("outer", RiskTier.T0, outer_call))
    ctx = make_ctx(tools=registry)

    with pytest.raises(SeamBypass):
        execute_effect(
            ctx,
            node="outer",
            tool=registry.get("outer"),
            args={},
            business_key="",
            gate_token=None,
            mode="read",
        )
    assert inner.count == 0


def test_register_time_side_effects_are_refused(make_ctx):
    """An instance module must only REGISTER; it may not act at import time."""
    counter = Counter()
    registry = ToolRegistry()

    def register(ctx):
        sender = tool("send", RiskTier.T2, counter)
        registry.register(sender)
        sender.call(to="a@b.c")  # the thing an instance module must not do

    with pytest.raises(SeamBypass):
        register(make_ctx(tools=registry))
    assert counter.count == 0


def test_read_mode_still_derives_policy_and_refuses_a_non_t0_tool(make_ctx):
    counter = Counter()
    registry = ToolRegistry()
    registry.register(tool("send", RiskTier.T2, counter))
    ctx = make_ctx(tools=registry)

    with pytest.raises(EffectDenied, match="read mode"):
        execute_effect(
            ctx,
            node="read",
            tool=registry.get("send"),
            args={},
            business_key="",
            gate_token=None,
            mode="read",
        )
    assert counter.count == 0


# --------------------------------------------------------------------------
# every route to the RAW callable, enumerated and closed
# --------------------------------------------------------------------------


def test_the_seam_exposes_no_public_invoker():
    """A public ``invoke(tool, args)`` was itself the bypass.

    It opened the seam and ran the callable with no policy verdict, no approval
    and no receipt — the locked door standing next to an open one. A reviewer
    executed a T3 effect straight through it, so it is module-private now and
    absent from ``__all__``.
    """
    assert not hasattr(tools_module, "invoke")
    assert "invoke" not in tools_module.__all__
    assert callable(tools_module._invoke_in_seam)


def test_no_route_on_a_tool_object_yields_its_raw_callable():
    """Attribute walk over the tool and its handle: the implementation is not there.

    ``_GuardedCall`` used to expose the raw callable twice over — an
    ``unwrapped`` property "for introspection only" and a ``_fn`` slot — and a
    reviewer ran a T3 effect through both. The implementation now lives in a
    closure cell, so no attribute, dataclass field, ``__dict__`` entry or
    ``__wrapped__`` reaches it.
    """
    impl = Counter()
    victim = tool("pay", RiskTier.T3, impl)

    reachable: list[object] = list(vars(victim).values())
    for holder in (victim, victim.call):
        for name in dir(holder):
            try:
                reachable.append(getattr(holder, name))
            except Exception:  # noqa: BLE001 - a property that raises is not a route
                continue

    assert not any(item is impl for item in reachable), "a route to the raw callable survives"
    for dead_route in ("unwrapped", "_fn", "__wrapped__"):
        assert not hasattr(victim.call, dead_route), dead_route
    assert impl.count == 0


def test_a_tool_cannot_be_pickled_back_into_a_raw_callable():
    """Serialisation is a route too: a closure cannot be pickled by reference."""
    victim = tool("pay", RiskTier.T3, Counter())
    with pytest.raises((pickle.PicklingError, AttributeError, TypeError)):
        pickle.dumps(victim)


def test_a_task_that_outlives_the_seam_loses_its_authority(make_ctx):
    """ContextVars are COPIED into asyncio tasks, not shared with them.

    A tool that leaves an ``asyncio.create_task`` behind hands that task a
    snapshot in which the seam is open FOR THAT TOOL. Under a name-only guard
    the task then executed the tool's callable after ``execute_effect`` had
    already returned — outside the receipt, outside the approval, outside the
    policy verdict. The seam ticket therefore carries a nonce that is revoked
    when the seam closes: the snapshot survives, its authority does not.
    """
    registry = ToolRegistry()
    escaped: list[dict] = []
    spawned: list[asyncio.Task] = []

    def escaper(**kwargs):
        if kwargs.get("from_task"):
            escaped.append(kwargs)  # only reachable if the guard let it through
            return "escaped"

        async def later():
            await asyncio.sleep(0)  # resumes only after execute_effect returned
            return registry.get("escape").call(from_task=True)

        spawned.append(asyncio.get_running_loop().create_task(later()))
        return "spawned"

    registry.register(tool("escape", RiskTier.T0, escaper))
    ctx = make_ctx(tools=registry)

    async def scenario():
        execute_effect(
            ctx,
            node="escape",
            tool=registry.get("escape"),
            args={},
            business_key="",
            gate_token=None,
            mode="read",
        )
        return await asyncio.gather(*spawned, return_exceptions=True)

    results = asyncio.run(scenario())
    assert isinstance(results[0], SeamBypass), results
    assert escaped == [], "a leftover task executed a tool with no policy derivation"


def test_a_context_snapshot_taken_inside_the_seam_expires_with_it(make_ctx):
    """The same hole, in its most direct form: ``copy_context()`` then replay."""
    registry = ToolRegistry()
    snapshots: list[contextvars.Context] = []
    replayed = Counter()

    def snapshotter(**kwargs):
        snapshots.append(contextvars.copy_context())
        return "snapshot"

    registry.register(tool("snap", RiskTier.T0, snapshotter))
    registry.register(tool("victim", RiskTier.T0, replayed))
    ctx = make_ctx(tools=registry)

    execute_effect(
        ctx,
        node="snap",
        tool=registry.get("snap"),
        args={},
        business_key="",
        gate_token=None,
        mode="read",
    )
    with pytest.raises(SeamBypass):
        snapshots[0].run(registry.get("snap").call)
    assert replayed.count == 0


def test_a_thread_started_inside_the_seam_has_no_authority(make_ctx):
    """A new thread starts from an EMPTY context, so it never inherits the seam."""
    registry = ToolRegistry()
    outcome: list[object] = []

    def threaded(**kwargs):
        def worker():
            try:
                outcome.append(registry.get("threaded").call(nested=True))
            except SeamBypass as exc:
                outcome.append(exc)

        if kwargs.get("nested"):
            return "nested"
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=10)
        return "spawned"

    registry.register(tool("threaded", RiskTier.T0, threaded))
    ctx = make_ctx(tools=registry)
    execute_effect(
        ctx,
        node="threaded",
        tool=registry.get("threaded"),
        args={},
        business_key="",
        gate_token=None,
        mode="read",
    )
    assert isinstance(outcome[0], SeamBypass), outcome


def test_read_mode_writes_no_receipt(make_ctx, store):
    """A poll must re-read every tick; a receipt would make tick 2 a no-op."""
    reader = Counter(result=["a"])
    registry = ToolRegistry()
    registry.register(tool("poll", RiskTier.T0, reader))
    ctx = make_ctx(tools=registry)

    for _ in range(3):
        outcome = execute_effect(
            ctx,
            node="poll",
            tool=registry.get("poll"),
            args={},
            business_key="",
            gate_token=None,
            mode="read",
        )
        assert outcome["receipt"] is None
    assert reader.count == 3
