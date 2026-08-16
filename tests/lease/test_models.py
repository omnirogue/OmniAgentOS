from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from omniagentos.lease.models import (
    LEASE_VERSION,
    Lease,
    LeaseCeilings,
    LeaseSubject,
    bump_generation,
    canonical_payload,
    current_generation,
    lease_record,
    new_lease_id,
    sign_lease,
    signature_matches,
)


def _lease(**overrides: Any) -> Lease:
    """Helper to build a Lease instance with default values for tests."""
    defaults: dict[str, Any] = {
        "lease_id": "lse_test",
        "subject": LeaseSubject(run_id="run_1"),
        "issued_at": 1000.0,
        "expires_at": 2000.0,
        "generation": current_generation(),
        "fs_read_roots": (),
        "fs_write_roots": (),
        "net_mode": "open",
        "net_allow_domains": (),
        "capabilities": (),
        "credential_handles": (),
        "ceilings": LeaseCeilings(),
        "auto_run_effect_classes": (),
        "approval_required_classes": (),
        "version": LEASE_VERSION,
        "signature": "",
    }
    defaults.update(overrides)
    return Lease(**defaults)


def test_new_lease_id() -> None:
    """Pin that new_lease_id starts with lse_, is 20 chars, and returns distinct values."""
    id1 = new_lease_id()
    id2 = new_lease_id()
    assert id1.startswith("lse_")
    assert len(id1) == 20
    assert id1 != id2


def test_canonical_payload_is_deterministic() -> None:
    """Pin that canonical_payload is deterministic and does not contain raw signature."""
    lease1 = _lease()
    lease2 = _lease()
    payload1 = canonical_payload(lease1)
    payload2 = canonical_payload(lease2)
    assert payload1 == payload2
    assert b"signature" not in payload1


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("fs_write_roots", ("/etc",)),
        ("net_mode", "deny"),
        ("expires_at", 3000.0),
        ("generation", 2),
        ("subject", LeaseSubject(run_id="run_2")),
        ("ceilings", LeaseCeilings(cpu_s=10.0)),
    ],
)
def test_canonical_payload_changes_on_security_field_change(field_name: str, value: Any) -> None:
    """Pin that canonical_payload changes when any security-relevant field changes."""
    base_lease = _lease()
    modified_lease = dataclasses.replace(base_lease, **{field_name: value})
    assert canonical_payload(base_lease) != canonical_payload(modified_lease)


def test_sign_lease_populates_signature() -> None:
    """Pin that sign_lease populates the signature and signature_matches verifies it."""
    unsigned = _lease()
    assert unsigned.signature == ""
    assert not signature_matches(unsigned)

    signed = sign_lease(unsigned)
    assert signed.signature != ""
    assert signature_matches(signed)


def test_tamper_detection() -> None:
    """Pin that tampering with key fields in a signed lease invalidates the signature."""
    signed = sign_lease(_lease())
    assert signature_matches(signed)

    # Tamper with fs_write_roots
    tampered_roots = dataclasses.replace(signed, fs_write_roots=("/etc",))
    assert not signature_matches(tampered_roots)

    # Tamper with expires_at
    tampered_expires = dataclasses.replace(signed, expires_at=3000.0)
    assert not signature_matches(tampered_expires)

    # Tamper with generation
    tampered_gen = dataclasses.replace(signed, generation=signed.generation + 1)
    assert not signature_matches(tampered_gen)


def test_wrong_hand_crafted_signature() -> None:
    """Pin that a wrong hand-crafted signature is correctly identified as invalid."""
    signed = sign_lease(_lease())
    tampered = dataclasses.replace(signed, signature="00" * 32)
    assert not signature_matches(tampered)


def test_net_policy_wire_shape() -> None:
    """Pin the net_policy wire shape logic under different network modes."""
    assert _lease(net_mode="open").net_policy == "open"
    assert _lease(net_mode="deny").net_policy == "deny"

    # Under proxy the wire shape carries BOTH the domains and the ports, so both
    # ride inside the signed canonical payload (security review finding #6).
    proxy_lease = _lease(net_mode="proxy", net_allow_domains=("a.example",), net_allow_ports=(443,))
    assert proxy_lease.net_policy == {"proxy": ["a.example"], "ports": [443]}


def test_lease_record() -> None:
    """Pin that lease_record outputs a serializable dict without signature leaks."""
    signed = sign_lease(_lease())
    record = lease_record(signed)

    assert record["signed"] is True
    assert "net_policy" in record
    assert "signature" not in record

    # Should serialize cleanly to JSON
    dumped = json.dumps(record)
    assert isinstance(dumped, str)


def test_bump_generation() -> None:
    """Pin that bump_generation strictly increases the active lease generation."""
    gen_before = current_generation()
    new_gen = bump_generation()
    assert new_gen == gen_before + 1
    assert current_generation() == new_gen
