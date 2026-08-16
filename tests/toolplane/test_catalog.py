"""Tests for the unified tool catalog.

Verifies:
- Backward compatibility: load_registry() succeeds on the real configs/connectors.yaml
- Capability defaults and resolution of properties
- Explicit field overrides on Capability
- build_catalog contains every built-in and many connector capabilities
- Veracity of built-ins risk & requires_scope matching inventory
- Presence of required fields (id, namespace, compact_hint, description)
- catalog_entry fails safe
- Registry load failure safety fallback
"""

from __future__ import annotations

import pytest

from omniagentos.connectors import (
    Capability,
    ConnectorError,
    ResultSizeClass,
    SideEffectClass,
    load_registry,
)
from omniagentos.contracts import ActionClass
from omniagentos.toolplane.catalog import (
    build_catalog,
    catalog_entry,
)
from omniagentos.toolplane.tools import CAPABILITY_INVENTORY


class TestCatalogBackwardCompatibility:
    """Test backward compatibility and default resolution properties of Capability."""

    def test_real_connectors_yaml_loads(self) -> None:
        """The real configs/connectors.yaml loads successfully and gets classified."""
        reg = load_registry()
        assert len(reg.capabilities) > 0

        # Spot-check stripe_acmeuni.read and stripe_acmeuni.refund
        assert "stripe_acmeuni.read" in reg.capabilities
        stripe_read = reg.capabilities["stripe_acmeuni.read"]
        assert stripe_read.resolved_read_only is True
        assert stripe_read.resolved_side_effect_class == SideEffectClass.NONE

        assert "stripe_acmeuni.refund" in reg.capabilities
        stripe_refund = reg.capabilities["stripe_acmeuni.refund"]
        assert stripe_refund.resolved_read_only is False
        assert stripe_refund.resolved_side_effect_class == SideEffectClass.IRREVERSIBLE

    def test_capability_with_no_new_fields(self) -> None:
        """A Capability constructed with no new fields still resolves all properties."""
        cap = Capability(
            id="test.read",
            connector="test",
            group="test-group",
            label="Test Read Capability",
            action_class=ActionClass.READ_ONLY,
        )

        assert cap.resolved_namespace == "test"
        assert cap.resolved_compact_hint == "Test Read Capability"
        assert cap.resolved_side_effect_class == SideEffectClass.NONE
        assert cap.resolved_read_only is True
        assert cap.resolved_idempotent is True
        assert cap.resolved_parallel_safe is True
        assert cap.resolved_resource_keys == ("connector:test",)
        assert cap.resolved_cancellation_group == "test"
        assert cap.resolved_credential_scope == "test"
        assert cap.resolved_result_size_class == ResultSizeClass.MEDIUM

    def test_capability_field_override(self) -> None:
        """An explicit field overrides its derived value."""
        cap = Capability(
            id="test.write",
            connector="test",
            group="test-group",
            label="Test Write Capability",
            action_class=ActionClass.READ_ONLY,
            read_only=False,  # Override read_only
            side_effect_class=SideEffectClass.EXTERNAL_WRITE,
            idempotent=False,
            parallel_safe=False,
            resource_keys=("test:explicit_resource",),
            cancellation_group="test-custom-cancellation",
            credential_scope="test-custom-credential",
            result_size_class=ResultSizeClass.SMALL,
        )

        assert cap.resolved_read_only is False
        assert cap.resolved_side_effect_class == SideEffectClass.EXTERNAL_WRITE
        assert cap.resolved_idempotent is False
        assert cap.resolved_parallel_safe is False
        assert cap.resolved_resource_keys == ("test:explicit_resource",)
        assert cap.resolved_cancellation_group == "test-custom-cancellation"
        assert cap.resolved_credential_scope == "test-custom-credential"
        assert cap.resolved_result_size_class == ResultSizeClass.SMALL


class TestUnifiedCatalogBuild:
    """Test building and querying the unified catalog."""

    def test_build_catalog_contains_expected_tools(self) -> None:
        """build_catalog contains all built-in ids and at least 100 connector ids."""
        catalog = build_catalog()

        # Check built-ins
        for tool_id in CAPABILITY_INVENTORY:
            assert tool_id in catalog
            entry = catalog[tool_id]
            assert entry.source == "builtin"
            assert entry.risk == CAPABILITY_INVENTORY[tool_id]["risk"]
            assert entry.requires_scope == CAPABILITY_INVENTORY[tool_id]["requires_scope"]
            assert entry.classified is True

        # Check connector count (the actual connectors.yaml contains over 100 capabilities)
        connector_entries = [e for e in catalog.values() if e.source == "connector"]
        assert len(connector_entries) >= 100

        # Assert all entries have non-empty required fields
        for entry in catalog.values():
            assert entry.id
            assert entry.namespace
            assert entry.compact_hint
            assert entry.description

    def test_catalog_entry_lookups(self) -> None:
        """catalog_entry() looks up tools and fails safe on unknown tool."""
        # Querying a built-in
        entry = catalog_entry("read_file")
        assert entry is not None
        assert entry.id == "read_file"
        assert entry.source == "builtin"

        # Querying an unknown tool should return None
        assert catalog_entry("does.not.exist") is None

    def test_build_catalog_survives_broken_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """build_catalog survives a broken registry and falls back to built-ins only."""

        def mock_load_registry() -> None:
            raise ConnectorError("Mock registry load failure")

        monkeypatch.setattr("omniagentos.connectors.load_registry", mock_load_registry)

        catalog = build_catalog()
        # Broken connector loading still returns every built-in.
        assert set(catalog) == set(CAPABILITY_INVENTORY)
        for tool_id in CAPABILITY_INVENTORY:
            assert tool_id in catalog
            assert catalog[tool_id].source == "builtin"
