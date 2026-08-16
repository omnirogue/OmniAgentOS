"""Tests for S1-selfgrant security fix in toolplane connector_invoke.

This test suite verifies that the vulnerability where a caller could assert
arbitrary connector grants to itself has been fixed.

The fix ensures that:
1. Caller-supplied grant lists are rejected (ignored)
2. Grants are derived SERVER-SIDE from the manifest
3. If no grants are in the manifest, the call fails closed
4. Legitimate server-derived grants still work
"""

from unittest.mock import MagicMock, patch

import pytest

from omniagentos.toolplane.manifest import load_manifest
from omniagentos.toolplane.tools import ToolplaneError, _connector_invoke


def manifest_for(tmp_path, **overrides):
    """Helper to build a test manifest."""
    data = {
        "run_id": "run-1",
        "session_id": "session-1",
        "holder_generation": 3,
        "read_roots": [str(tmp_path / "read")],
        "write_roots": [str(tmp_path / "write")],
        "allowed_ops": ["connector_invoke"],
        "connector_grants": [],  # Server-derived grants
    }
    data.update(overrides)
    (tmp_path / "read").mkdir(exist_ok=True)
    (tmp_path / "write").mkdir(exist_ok=True)
    return load_manifest(data)


def test_connector_invoke_requires_cap_id(tmp_path):
    """Test that cap_id is required."""
    manifest = manifest_for(tmp_path, connector_grants=["stripe.read"])

    # Missing cap_id - raises ToolplaneError
    with pytest.raises(ToolplaneError) as exc_info:
        _connector_invoke(manifest, {})
    assert exc_info.value.error == "invalid_args"

    # Empty cap_id - raises ToolplaneError
    with pytest.raises(ToolplaneError) as exc_info:
        _connector_invoke(manifest, {"cap_id": ""})
    assert exc_info.value.error == "invalid_args"


def test_connector_invoke_fails_closed_without_manifest_grants(tmp_path):
    """Prove: without grants in manifest, call fails closed (unauthorized).

    This is the fix: even if a caller tries to assert grants via args,
    they are ignored and the manifest grants (empty) are used, causing denial.
    """
    manifest = manifest_for(tmp_path, connector_grants=[])  # No grants!

    # Caller tries to assert they have stripe.read (self-grant attack)
    # The args include "granted" but it should be ignored - only manifest grants matter
    with pytest.raises(ToolplaneError) as exc_info:
        _connector_invoke(manifest, {
            "cap_id": "stripe.read",
            "granted": ["stripe.read"],  # Caller asserts this - should be IGNORED
        })

    # The fix: manifest grants (empty) are used, not the caller's asserted list
    assert exc_info.value.error == "unauthorized"
    assert "no connector capabilities granted" in exc_info.value.detail


def test_connector_invoke_with_manifest_grants_attempts_broker_call(tmp_path):
    """Prove: with server-derived grants in manifest, call proceeds to broker.

    This demonstrates that legitimate grants (provided by the framework,
    not the caller) still work.
    """
    manifest = manifest_for(tmp_path, connector_grants=["stripe.read"])

    # Mock the broker module where it's imported from
    with patch("omniagentos.connectors.broker") as mock_broker_module:
        mock_broker_module.authorize.return_value = MagicMock()
        mock_broker_module.call.return_value = {"status": "ok", "body": "test"}

        # Caller provides cap_id; manifest has the grant
        _connector_invoke(manifest, {
            "cap_id": "stripe.read",
            # No "granted" in args - broker will use manifest.connector_grants
        })

        # The call proceeds to the broker (which may still deny based on other checks)
        mock_broker_module.authorize.assert_called_once()
        call_args = mock_broker_module.authorize.call_args
        # Verify the granted list comes from manifest, not args
        cap_id_arg, granted_arg = call_args[0][:2]
        assert cap_id_arg == "stripe.read"
        assert granted_arg == ["stripe.read"]  # From manifest!


def test_caller_supplied_granted_arg_is_ignored(tmp_path):
    """Prove: caller-supplied "granted" argument is completely ignored.

    The attacker tries to pass granted=["superadmin.all"] but the manifest
    only has ["stripe.read"]. The fix uses only the manifest, proving the
    caller's assertion is rejected.
    """
    manifest = manifest_for(tmp_path, connector_grants=["stripe.read"])

    with patch("omniagentos.connectors.broker") as mock_broker_module:
        mock_broker_module.authorize.return_value = MagicMock()
        mock_broker_module.call.return_value = {"status": "ok"}

        # Attacker tries to self-grant with a dangerous capability
        _connector_invoke(manifest, {
            "cap_id": "superadmin.all",  # Claiming unrestricted access!
            "granted": ["superadmin.all"],  # Self-asserted - should be ignored
        })

        # Since manifest only has ["stripe.read"], this call to broker.authorize
        # will include only ["stripe.read"] in the granted list, not the attacker's claim
        call_args = mock_broker_module.authorize.call_args
        cap_id_arg, granted_arg = call_args[0][:2]
        assert granted_arg == ["stripe.read"]  # NOT ["superadmin.all"]!


def test_empty_manifest_grants_fails_closed(tmp_path):
    """Prove: even with valid cap_id, empty manifest grants deny the call."""
    manifest = manifest_for(tmp_path, connector_grants=[])

    # This should fail regardless of what cap_id or granted args are provided
    with pytest.raises(ToolplaneError) as exc_info:
        _connector_invoke(manifest, {
            "cap_id": "echo.ping",
            "granted": ["echo.ping", "stripe.read", "admin.all"],  # Attacker claims all
        })

    assert exc_info.value.error == "unauthorized"


def test_connector_invoke_respects_broker_denial(tmp_path):
    """Prove: even with correct manifest grants, broker can still deny."""
    manifest = manifest_for(tmp_path, connector_grants=["stripe.read"])

    from omniagentos.connectors.broker import BrokerDenied

    with patch("omniagentos.connectors.broker") as mock_broker_module:
        # Broker denies even though manifest grants match
        mock_broker_module.authorize.side_effect = BrokerDenied("not_granted", "stripe.read")

        result = _connector_invoke(manifest, {"cap_id": "stripe.read"})

        assert result["ok"] is False
        assert result["error"] == "broker_denied"


def test_multiple_manifest_grants_restricts_to_granted_list(tmp_path):
    """Prove: caller cannot request capabilities beyond manifest grants."""
    manifest = manifest_for(tmp_path, connector_grants=["stripe.read", "nmi.read"])

    with patch("omniagentos.connectors.broker") as mock_broker_module:
        mock_broker_module.authorize.return_value = MagicMock()
        mock_broker_module.call.return_value = {"status": "ok"}

        # Attempt to use a capability not in manifest grants
        _connector_invoke(manifest, {
            "cap_id": "paypal.write",  # Not in manifest!
            "granted": ["paypal.write"],  # Self-asserted
        })

        # Broker should be called with only manifest grants
        call_args = mock_broker_module.authorize.call_args
        cap_id_arg, granted_arg = call_args[0][:2]
        assert granted_arg == ["stripe.read", "nmi.read"]  # NOT paypal.write


def test_fix_prevents_self_assertion_attack_vector(tmp_path):
    """Prove: the core vulnerability (self-assertion) is fixed.

    This is the principal test: a caller that constructs arbitrary "granted"
    lists to claim capabilities it doesn't have is now denied, because grants
    come from the manifest (server-side), never from the caller's args.
    """
    # Scenario: framework creates a manifest with only read-only access
    manifest = manifest_for(tmp_path, connector_grants=["stripe.read", "nmi.read"])

    from omniagentos.connectors.broker import BrokerDenied

    with patch("omniagentos.connectors.broker") as mock_broker_module:
        # Broker denies because stripe.write is not in manifest grants
        mock_broker_module.authorize.side_effect = BrokerDenied(
            "not_granted", "stripe.write",
            "agent holds 2 capabilities; 'stripe.write' is not among them"
        )

        # Attack: caller tries to escalate to write access
        result = _connector_invoke(manifest, {
            "cap_id": "stripe.write",  # Trying to escalate
            "granted": ["stripe.read", "stripe.write", "admin.all"],  # Claims more than manifest
        })

        # It's denied because the broker receives only manifest.connector_grants
        # and stripe.write is not in there
        assert result["ok"] is False
        assert result["error"] == "broker_denied"

        # Verify the broker was called with only manifest grants, never the caller's claimed list
        call_args = mock_broker_module.authorize.call_args
        cap_id_arg, granted_arg = call_args[0][:2]
        assert granted_arg == ["stripe.read", "nmi.read"]  # NOT the caller's claimed list!
