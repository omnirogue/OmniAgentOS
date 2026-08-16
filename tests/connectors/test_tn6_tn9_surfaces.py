"""TN.6/TN.9 connector declarations retain reviewed, callable boundaries.

Grok full-auto product: local Globex media generation is
``external_reversible`` (broker-audited, no human gate for local studio).
Outbound customer-visible CRM/email remains ``consequential``.
"""

from __future__ import annotations

from omniagentos.connectors import load_registry
from omniagentos.contracts import ActionClass


def test_tn6_tn9_capabilities_action_classes_and_callable() -> None:
    registry = load_registry()
    expected = {
        "globex.generate_image": (
            ActionClass.EXTERNAL_REVERSIBLE,
            r"^/v1/generate/(image|images)$",
        ),
        "globex.generate_video": (
            ActionClass.EXTERNAL_REVERSIBLE,
            r"^/v1/generate/(video|videos)$",
        ),
        "customerio_acmeuni.trigger_broadcast": (
            ActionClass.CONSEQUENTIAL,
            r"^/v1/campaigns/[0-9]+/triggers/?$",
        ),
        "piedpiper_acmeuni.conversation_send": (
            ActionClass.CONSEQUENTIAL,
            r"^/conversations/messages/?$",
        ),
    }

    for cap_id, (action_class, path_regex) in expected.items():
        capability = registry.capability(cap_id)
        assert capability.action_class is action_class, (
            f"{cap_id}: expected {action_class}, got {capability.action_class}"
        )
        assert capability.callable_now
        assert capability.http is not None
        assert capability.http.path_regex == path_regex
