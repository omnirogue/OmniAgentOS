"""Tests for run-role-based toolplane exposure subtraction."""

from __future__ import annotations

from typing import Literal

from omniagentos.connectors import ResultSizeClass, SideEffectClass
from omniagentos.contracts import ActionClass
from omniagentos.toolplane.catalog import CatalogEntry, RiskLevel
from omniagentos.toolplane.exposure import ExposureContext, compute_exposure


def make_catalog_entry(
    id: str,
    *,
    source: Literal["builtin", "connector"] = "builtin",
    action_class: ActionClass = ActionClass.READ_ONLY,
    risk: RiskLevel = "low",
) -> CatalogEntry:
    """Create a catalog entry with the requested action class."""
    return CatalogEntry(
        id=id,
        namespace="test",
        label=id,
        compact_hint="Mock hint",
        description="Mock description",
        source=source,
        action_class=action_class,
        read_only=action_class is ActionClass.READ_ONLY,
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


def test_coordinator_only_sees_read_only_tools_after_grant_and_risk_filtering():
    catalog = {
        "read_low": make_catalog_entry("read_low"),
        "read_high": make_catalog_entry("read_high", risk="high"),
        "write_low": make_catalog_entry(
            "write_low", action_class=ActionClass.INTERNAL_REVERSIBLE
        ),
        "connector_write": make_catalog_entry(
            "connector_write",
            source="connector",
            action_class=ActionClass.EXTERNAL_REVERSIBLE,
        ),
    }

    decision = compute_exposure(
        ExposureContext(role="coordinator"),
        grants=["connector_write"],
        risk="high",
        catalog=catalog,
    )

    assert decision.visible == ("read_high", "read_low")
    assert {"write_low", "connector_write"}.issubset(decision.hidden)


def test_coordinator_role_subtraction_happens_before_core_and_deferral(monkeypatch):
    catalog = {
        "read": make_catalog_entry("read"),
        "write": make_catalog_entry("write", action_class=ActionClass.INTERNAL_REVERSIBLE),
    }
    monkeypatch.setattr("omniagentos.toolplane.exposure.core_tools", lambda: ("write", "read"))
    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tools", lambda: 0)
    monkeypatch.setattr("omniagentos.toolplane.exposure.small_catalog_max_tokens", lambda: 0)

    decision = compute_exposure(ExposureContext(role="coordinator"), grants=[], catalog=catalog)

    assert decision.core_tools == ("read",)
    assert decision.allowed == ("read",)
    assert decision.deferred == ()
    assert decision.hidden == ("write",)


def test_classifier_role_hides_every_tool():
    catalog = {
        "read": make_catalog_entry("read"),
        "write": make_catalog_entry("write", action_class=ActionClass.INTERNAL_REVERSIBLE),
        "connector": make_catalog_entry("connector", source="connector"),
    }

    decision = compute_exposure(
        ExposureContext(role="classifier"), grants=["connector"], catalog=catalog
    )

    assert decision.visible == ()
    assert decision.allowed == ()
    assert decision.deferred == ()
    assert decision.core_tools == ()
    assert decision.hidden == tuple(sorted(catalog))


def test_role_subtraction_remains_fail_closed_in_fallback(monkeypatch):
    catalog = {
        "read": make_catalog_entry("read"),
        "write": make_catalog_entry("write", action_class=ActionClass.INTERNAL_REVERSIBLE),
    }

    def raise_error(_entries):
        raise RuntimeError("intentional failure")

    monkeypatch.setattr("omniagentos.toolplane.exposure.estimate_schema_tokens", raise_error)

    decision = compute_exposure(ExposureContext(role="coordinator"), grants=[], catalog=catalog)

    assert decision.fallback is True
    assert decision.visible == ("read",)
    assert decision.hidden == ("write",)


def test_unspecified_or_empty_role_preserves_existing_exposure_output():
    catalog = {
        "read": make_catalog_entry("read"),
        "write": make_catalog_entry("write", action_class=ActionClass.INTERNAL_REVERSIBLE),
    }

    baseline = compute_exposure(ExposureContext(), grants=[], catalog=catalog)
    explicit_none = compute_exposure(ExposureContext(role=None), grants=[], catalog=catalog)
    explicit_empty = compute_exposure(ExposureContext(role=""), grants=[], catalog=catalog)

    assert baseline == explicit_none == explicit_empty
