"""Knowledge capabilities are part of the ordinary connector grant catalogue."""

from __future__ import annotations

from omniagentos.connectors import load_registry
from omniagentos.contracts import ActionClass


def test_knowledge_capabilities_are_registered_with_safe_classes() -> None:
    registry = load_registry()
    assert registry.capability("knowledge.read").action_class is ActionClass.READ_ONLY
    assert registry.capability("knowledge.write").action_class is ActionClass.INTERNAL_REVERSIBLE
