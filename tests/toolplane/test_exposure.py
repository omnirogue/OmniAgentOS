"""Tests for the Phase-3 toolplane exposure computation module.

Verifies:
- The escalation contract, its documentation and logical alignment
- Policy filtering of connector vs built-in tools based on grants and risk
- Small-catalog bypass behavior on low limits
- Deferral logic under large catalogs and low schema limits
- Determinism and correctness of properties
- Fallback mode resilience and behavior
- Observation/logging constraints (scrubbing, metadata-only format)
- Enforce seam argv patching behavior and native tool inversion
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Literal

from omniagentos.connectors import ResultSizeClass, SideEffectClass
from omniagentos.contracts import ActionClass
from omniagentos.toolplane.catalog import CatalogEntry, RiskLevel
from omniagentos.toolplane.config import DEFAULT_SMALL_CATALOG_TOKENS
from omniagentos.toolplane.exposure import (
    ExposureContext,
    ExposureDecision,
    _fallback_decision,
    compute_exposure,
    enforce_argv_patch,
    log_exposure_decision,
    native_tools_for_hidden,
)


def make_mock_catalog_entry(
    id: str,
    source: Literal["builtin", "connector"] = "connector",
    risk: RiskLevel = "low",
) -> CatalogEntry:
    """Create a mock CatalogEntry directly for isolated tests."""
    return CatalogEntry(
        id=id,
        namespace="test",
        label=id.capitalize(),
        compact_hint="Mock hint",
        description="Mock description",
        source=source,
        action_class=ActionClass.READ_ONLY if risk == "low" else ActionClass.EXTERNAL_REVERSIBLE,
        read_only=True,
        side_effect_class=SideEffectClass.NONE,
        resource_keys=(),
        idempotent=True,
        parallel_safe=True,
        cancellation_group="mock",
        credential_scope="mock",
        result_size_class=ResultSizeClass.SMALL,
        risk=risk,
        requires_scope=True,
        input_examples=(),
        parameter_names=(),
        callable_now=True,
        classified=True,
    )


def test_escalation_contract_documented_and_verified():
    """Verify the four tenets of the escalation contract are documented in the module."""
    import omniagentos.toolplane.exposure as exposure

    doc = exposure.__doc__
    assert doc is not None
    assert "Deferred is not denied" in doc
    assert "Hidden is invisible AND fail-closed" in doc
    assert "Not in the registry at all" in doc
    assert "Search outage" in doc


def test_ungranted_connector_hidden():
    """An ungranted connector entry is in hidden and appears in NEITHER allowed NOR deferred."""
    cat = {
        "builtin_tool": make_mock_catalog_entry("builtin_tool", source="builtin"),
        "conn_tool": make_mock_catalog_entry("conn_tool", source="connector"),
    }
    ctx = ExposureContext()
    decision = compute_exposure(ctx, grants=[], catalog=cat)

    assert "conn_tool" in decision.hidden
    assert "conn_tool" not in decision.allowed
    assert "conn_tool" not in decision.deferred


def test_granted_connector_visible():
    """A granted connector entry is visible."""
    cat = {
        "conn_tool": make_mock_catalog_entry("conn_tool", source="connector"),
    }
    ctx = ExposureContext()
    decision = compute_exposure(ctx, grants=["conn_tool"], catalog=cat)

    assert "conn_tool" in decision.visible
    assert "conn_tool" not in decision.hidden


def test_builtins_visible_empty_grants():
    """Built-ins are visible with an EMPTY grant list."""
    cat = {
        "builtin_tool": make_mock_catalog_entry("builtin_tool", source="builtin"),
    }
    ctx = ExposureContext()
    decision = compute_exposure(ctx, grants=[], catalog=cat)

    assert "builtin_tool" in decision.visible
    assert "builtin_tool" not in decision.hidden


def test_risk_filtering():
    """A risk='high' entry is hidden when risk='low' is passed, and visible at risk='high'."""
    cat = {
        "high_risk_tool": make_mock_catalog_entry("high_risk_tool", source="builtin", risk="high"),
        "low_risk_tool": make_mock_catalog_entry("low_risk_tool", source="builtin", risk="low"),
    }
    ctx = ExposureContext()

    # risk="low" passed -> high_risk_tool should be hidden
    decision_low = compute_exposure(ctx, grants=[], risk="low", catalog=cat)
    assert "high_risk_tool" in decision_low.hidden
    assert "low_risk_tool" in decision_low.visible

    # risk="high" passed -> high_risk_tool should be visible
    decision_high = compute_exposure(ctx, grants=[], risk="high", catalog=cat)
    assert "high_risk_tool" in decision_high.visible
    assert "high_risk_tool" not in decision_high.hidden


def test_small_catalog_bypass_fired(monkeypatch):
    """Small-catalog bypass fires at <= 10 tools: deferred == () and bypassed is True."""
    cat = {f"tool_{i}": make_mock_catalog_entry(f"tool_{i}", source="builtin") for i in range(5)}
    ctx = ExposureContext()
    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tools", lambda: 10)
    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tokens", lambda: 10000)

    decision = compute_exposure(ctx, grants=[], catalog=cat)
    assert decision.bypassed is True
    assert decision.deferred == ()
    assert len(decision.allowed) == 5
    assert decision.reason == "small-catalog-bypass"


def test_large_catalog_deferred(monkeypatch):
    """With a large catalog and small_catalog_max_tokens monkeypatched low, allowed == core_tools and everything else is deferred."""
    cat = {f"tool_{i}": make_mock_catalog_entry(f"tool_{i}", source="builtin") for i in range(60)}
    ctx = ExposureContext()

    # Monkeypatch low thresholds so small-catalog bypass doesn't fire
    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tools", lambda: 5)
    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tokens", lambda: 5)

    # Core tools
    monkeypatch.setattr("omniagentos.toolplane.exposure.core_tools", lambda: ("tool_1", "tool_3"))

    decision = compute_exposure(ctx, grants=[], catalog=cat)
    assert decision.bypassed is False
    assert decision.reason == "deferred"
    assert decision.allowed == ("tool_1", "tool_3")
    assert "tool_0" in decision.deferred
    assert "tool_1" not in decision.deferred


def test_core_tools_is_subset_of_allowed(monkeypatch):
    """core_tools is always a subset of allowed."""
    cat = {f"tool_{i}": make_mock_catalog_entry(f"tool_{i}", source="builtin") for i in range(5)}
    ctx = ExposureContext()
    monkeypatch.setattr("omniagentos.toolplane.exposure.core_tools", lambda: ("tool_1", "tool_2"))

    # Case 1: Bypassed
    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tools", lambda: 10)
    decision_bypassed = compute_exposure(ctx, grants=[], catalog=cat)
    assert set(decision_bypassed.core_tools).issubset(set(decision_bypassed.allowed))

    # Case 2: Deferred
    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tools", lambda: 2)
    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tokens", lambda: 2)
    decision_deferred = compute_exposure(ctx, grants=[], catalog=cat)
    assert set(decision_deferred.core_tools).issubset(set(decision_deferred.allowed))


def test_visible_and_hidden_properties():
    """visible == sorted(allowed + deferred), and hidden is disjoint from both."""
    cat = {
        "builtin_1": make_mock_catalog_entry("builtin_1", source="builtin"),
        "builtin_2": make_mock_catalog_entry("builtin_2", source="builtin"),
        "conn_1": make_mock_catalog_entry("conn_1", source="connector"),
        "conn_2": make_mock_catalog_entry("conn_2", source="connector"),
    }
    ctx = ExposureContext()
    decision = compute_exposure(ctx, grants=["conn_1"], catalog=cat)

    assert decision.visible == tuple(sorted(set(decision.allowed) | set(decision.deferred)))
    assert set(decision.hidden).isdisjoint(set(decision.allowed))
    assert set(decision.hidden).isdisjoint(set(decision.deferred))
    assert set(decision.hidden).isdisjoint(set(decision.visible))


def test_determinism():
    """Determinism: two identical calls produce equal decisions."""
    cat = {
        "builtin_1": make_mock_catalog_entry("builtin_1", source="builtin"),
        "conn_1": make_mock_catalog_entry("conn_1", source="connector"),
    }
    ctx = ExposureContext(session_id="sess-1")
    grants = ["conn_1"]

    d1 = compute_exposure(ctx, grants, risk="high", catalog=cat)
    d2 = compute_exposure(ctx, grants, risk="high", catalog=cat)

    assert d1 == d2


def test_fallback_mode(monkeypatch):
    """Fallback: monkeypatch estimate_schema_tokens to raise; assert fallback is True, hidden == (), deferred == () and allowed contains correct tools."""
    cat = {
        "builtin_1": make_mock_catalog_entry("builtin_1", source="builtin"),
        "conn_granted": make_mock_catalog_entry("conn_granted", source="connector"),
        "conn_ungranted": make_mock_catalog_entry("conn_ungranted", source="connector"),
    }

    def mock_estimate_schema_tokens(entries):
        raise RuntimeError("Failed intentionally")

    monkeypatch.setattr(
        "omniagentos.toolplane.exposure.estimate_schema_tokens", mock_estimate_schema_tokens
    )

    ctx = ExposureContext()
    decision = compute_exposure(ctx, grants=["conn_granted"], catalog=cat)

    assert decision.fallback is True
    assert decision.hidden == ()
    assert decision.deferred == ()
    assert "builtin_1" in decision.allowed
    assert "conn_granted" in decision.allowed
    assert "conn_ungranted" not in decision.allowed
    assert decision.reason.startswith("fallback:")


def test_fallback_decision_never_raises():
    """_fallback_decision never raises even with a catalog of None."""
    decision = _fallback_decision(grants=["conn_1"], catalog=None, exc=ValueError("Test exception"))
    assert decision.fallback is True
    assert decision.allowed == ("conn_1",)
    assert decision.deferred == ()
    assert decision.hidden == ()


def test_log_exposure_decision(monkeypatch):
    """log_exposure_decision returns a bool, never raises, and doesn't record hidden/deferred tool names."""
    emitted_records = []

    def mock_emit_observation(record):
        emitted_records.append(record)
        return True

    monkeypatch.setattr("omniagentos.toolplane.exposure.emit_observation", mock_emit_observation)

    ctx = ExposureContext(session_id="session-test", run_id="run-test")
    decision = ExposureDecision(
        core_tools=("read_file",),
        allowed=("read_file", "write_file"),
        deferred=("edit_file",),
        hidden=("delete_file",),
        reason="deferred",
        mode="enforce",
        bypassed=False,
        fallback=False,
        estimated_tokens=42,
    )

    res = log_exposure_decision(decision, ctx)
    assert res is True
    assert len(emitted_records) == 1

    rec = emitted_records[0]
    assert rec["tool"] == "<exposure>"
    assert rec["source"] == "toolplane-exposure"
    assert rec["n_deferred"] == 1
    assert rec["n_hidden"] == 1
    assert rec["core_tools"] == ["read_file"]

    # The record should NOT contain any of the deferred/hidden tool names
    serialized = json.dumps(rec)
    assert "edit_file" not in serialized
    assert "delete_file" not in serialized

    # Test that it never raises when emit_observation raises
    def mock_raising_emit_observation(record):
        raise RuntimeError("Sink failure")

    monkeypatch.setattr(
        "omniagentos.toolplane.exposure.emit_observation", mock_raising_emit_observation
    )
    res_fail = log_exposure_decision(decision, ctx)
    assert res_fail is False


def test_enforce_argv_patch_no_hidden():
    """enforce_argv_patch with no hidden native tools returns the argv unchanged (purely)."""
    decision = ExposureDecision(
        core_tools=(),
        allowed=(),
        deferred=(),
        hidden=(),
        reason="deferred",
        mode="enforce",
        bypassed=False,
        fallback=False,
        estimated_tokens=0,
    )
    argv = ["--other", "val"]
    res = enforce_argv_patch(argv, decision)
    assert res == argv
    assert res is not argv


def test_enforce_argv_patch_preserves_task_and_idempotent():
    """enforce_argv_patch preserves Task while adding mapped names, and is idempotent."""
    decision = ExposureDecision(
        core_tools=(),
        allowed=(),
        deferred=(),
        hidden=("edit_file",),
        reason="deferred",
        mode="enforce",
        bypassed=False,
        fallback=False,
        estimated_tokens=0,
    )

    argv = ["--disallowedTools", "Task"]
    res1 = enforce_argv_patch(argv, decision)

    assert "Task" in res1[1]
    assert "Edit" in res1[1]
    assert "MultiEdit" in res1[1]
    assert "NotebookEdit" in res1[1]

    parts = res1[1].split(",")
    assert parts == sorted(parts)

    res2 = enforce_argv_patch(res1, decision)
    assert res2 == res1


def test_enforce_argv_patch_appends_flag_when_absent():
    """enforce_argv_patch appends the flag when it is absent."""
    decision = ExposureDecision(
        core_tools=(),
        allowed=(),
        deferred=(),
        hidden=("read_file",),
        reason="deferred",
        mode="enforce",
        bypassed=False,
        fallback=False,
        estimated_tokens=0,
    )
    argv = ["--foo", "bar"]
    res = enforce_argv_patch(argv, decision)

    assert res[:-2] == argv
    assert res[-2] == "--disallowedTools"
    assert res[-1] == "Read"


def test_enforce_argv_patch_flag_is_last():
    """enforce_argv_patch appends the value when the flag is present but last (no value)."""
    decision = ExposureDecision(
        core_tools=(),
        allowed=(),
        deferred=(),
        hidden=("read_file",),
        reason="deferred",
        mode="enforce",
        bypassed=False,
        fallback=False,
        estimated_tokens=0,
    )
    argv = ["--foo", "--disallowedTools"]
    res = enforce_argv_patch(argv, decision)
    assert res[:-1] == argv
    assert res[-1] == "Read"


def test_native_tools_for_hidden_only_mapped_tools():
    """native_tools_for_hidden never returns a native tool that has no SESSION_TOOL_CAPABILITY entry."""
    decision = ExposureDecision(
        core_tools=(),
        allowed=(),
        deferred=(),
        hidden=("read_file", "unknown_capability_not_mapped"),
        reason="deferred",
        mode="enforce",
        bypassed=False,
        fallback=False,
        estimated_tokens=0,
    )
    natives = native_tools_for_hidden(decision)
    assert natives == ("Read",)


def test_small_catalog_bypass_exceptions_fallback_to_defaults(monkeypatch):
    """A config read that raises falls back to DEFAULT_SMALL_CATALOG_TOOLS/TOKENS.

    The bypass predicate is an OR of the two bounds -- either one being generous is
    enough to load everything -- so proving the fallback bounds are in force means
    proving BOTH arms. A catalog of 15 tiny tools trips neither bound (15 > 10 tools,
    but ~245 estimated tokens is far under 10k), which is why the large case here
    needs entries big enough to blow the token bound as well.
    """

    def raising_fn():
        raise RuntimeError("Config failure")

    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tools", raising_fn)
    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tokens", raising_fn)

    ctx = ExposureContext()

    # Under BOTH default bounds (5 tools, tiny schemas) -> bypass.
    cat_small = {
        f"tool_{i}": make_mock_catalog_entry(f"tool_{i}", source="builtin") for i in range(5)
    }
    decision_small = compute_exposure(ctx, grants=[], catalog=cat_small)
    assert decision_small.bypassed is True
    assert decision_small.reason == "small-catalog-bypass"

    # Over the tool bound but UNDER the token bound -> still bypasses, because the
    # predicate is an OR. This is the arm the first draft of this test got wrong.
    cat_many_tiny = {
        f"tool_{i}": make_mock_catalog_entry(f"tool_{i}", source="builtin") for i in range(15)
    }
    decision_many_tiny = compute_exposure(ctx, grants=[], catalog=cat_many_tiny)
    assert decision_many_tiny.bypassed is True

    # Over BOTH default bounds -> deferral. Each entry carries a large description so
    # the estimated schema cost clears DEFAULT_SMALL_CATALOG_TOKENS.
    cat_large = {}
    for i in range(15):
        entry = make_mock_catalog_entry(f"tool_{i}", source="builtin")
        cat_large[f"tool_{i}"] = replace(entry, description="x" * 4000)
    decision_large = compute_exposure(ctx, grants=[], catalog=cat_large)
    assert decision_large.estimated_tokens > DEFAULT_SMALL_CATALOG_TOKENS
    assert decision_large.bypassed is False
    assert decision_large.reason == "deferred"
