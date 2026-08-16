"""Idempotency receipts — the P7 guarded pattern against the real table."""

from __future__ import annotations

import pytest
from conftest import Counter, tool
from omniagentos_loops.contracts import EffectStateUnknown, RiskTier
from omniagentos_loops.receipts import guarded, receipt_exists, receipt_key
from omniagentos_loops.tools import ToolRegistry


def _ctx(make_ctx, effect: Counter, **kwargs):
    registry = ToolRegistry()
    registry.register(tool("effect", RiskTier.T1, effect, **kwargs))
    return make_ctx(tools=registry), registry.get("effect")


def test_first_call_executes_and_records_a_completed_receipt(make_ctx, store):
    effect = Counter(result={"ok": True})
    ctx, tool_obj = _ctx(make_ctx, effect)
    key = receipt_key(ctx.instance_id, ctx.template, "node", "effect", "biz-1")

    outcome = guarded(ctx, key=key, node="node", tool=tool_obj, execute=effect)

    assert outcome.result == {"ok": True}
    assert outcome.replayed is False
    assert effect.count == 1
    assert receipt_exists(ctx, key)
    assert store.idem_get(key)["run_id"] == ctx.instance_id


def test_second_call_replays_the_recorded_result_without_executing(make_ctx):
    effect = Counter(result={"ok": True})
    ctx, tool_obj = _ctx(make_ctx, effect)
    key = receipt_key(ctx.instance_id, ctx.template, "node", "effect", "biz-1")

    guarded(ctx, key=key, node="node", tool=tool_obj, execute=effect)
    outcome = guarded(ctx, key=key, node="node", tool=tool_obj, execute=effect)

    assert outcome.replayed is True
    assert outcome.result == {"ok": True}
    assert effect.count == 1, "the external effect must happen exactly once"


def test_a_claimed_but_uncompleted_receipt_fails_closed(make_ctx, store):
    """The crash-between-claim-and-complete case: state UNKNOWN, never re-run."""
    effect = Counter()
    ctx, tool_obj = _ctx(make_ctx, effect)
    key = receipt_key(ctx.instance_id, ctx.template, "node", "effect", "biz-1")
    store.idem_insert(key, ctx.instance_id, "node")  # claim, then "crash"

    with pytest.raises(EffectStateUnknown):
        guarded(ctx, key=key, node="node", tool=tool_obj, execute=effect)
    assert effect.count == 0


def test_replay_on_unknown_tools_may_retry(make_ctx, store):
    effect = Counter(result="fetched")
    ctx, tool_obj = _ctx(make_ctx, effect, replay_on_unknown=True)
    key = receipt_key(ctx.instance_id, ctx.template, "node", "effect", "biz-1")
    store.idem_insert(key, ctx.instance_id, "node")

    outcome = guarded(ctx, key=key, node="node", tool=tool_obj, execute=effect)
    assert outcome.result == "fetched"
    assert outcome.replayed is False
    assert effect.count == 1


def test_distinct_business_keys_are_distinct_effects(make_ctx):
    effect = Counter()
    ctx, tool_obj = _ctx(make_ctx, effect)
    for business_key in ("a", "b"):
        key = receipt_key(ctx.instance_id, ctx.template, "node", "effect", business_key)
        guarded(ctx, key=key, node="node", tool=tool_obj, execute=effect)
    assert effect.count == 2


def test_receipt_key_is_scoped_by_template_instance_node_and_tool():
    assert receipt_key("i", "tpl", "n", "t", "b") == "loop:tpl:i:n:t:b"
    base = receipt_key("i", "tpl", "n", "t", "b")
    # Every axis must be able to distinguish two receipts, or one loop's
    # completed receipt silently suppresses another loop's effect.
    assert base != receipt_key("i2", "tpl", "n", "t", "b")
    assert base != receipt_key("i", "tpl2", "n", "t", "b")
    assert base != receipt_key("i", "tpl", "n2", "t", "b")
    assert base != receipt_key("i", "tpl", "n", "t2", "b")
    assert base != receipt_key("i", "tpl", "n", "t", "b2")
