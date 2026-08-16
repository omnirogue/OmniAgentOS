from __future__ import annotations

import dataclasses
import time
from typing import Any

import pytest

from omniagentos.lease.models import (
    LEASE_VERSION,
    Lease,
    LeaseCeilings,
    LeaseSubject,
    bump_generation,
    current_generation,
    sign_lease,
)
from omniagentos.lease.validate import (
    LeaseInvalid,
    lease_is_valid,
    remaining_seconds,
    validate_lease,
)


def _lease(**overrides: Any) -> Lease:
    """Helper to build a Lease instance with default valid values for tests."""
    defaults: dict[str, Any] = {
        "lease_id": "lse_test",
        "subject": LeaseSubject(run_id="run_1"),
        "issued_at": 1000.0,
        "expires_at": 1060.0,
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


def test_validate_happy_path() -> None:
    """Pin that a freshly signed, unexpired lease with correct values passes validation."""
    lease = sign_lease(_lease())
    validate_lease(lease, now=1000.0)
    assert lease_is_valid(lease, now=1000.0) is True


def test_validate_version() -> None:
    """Pin that validation fails with lease_version: if version does not match LEASE_VERSION."""
    lease = sign_lease(_lease(version=LEASE_VERSION + 1))
    with pytest.raises(LeaseInvalid, match="^lease_version:"):
        validate_lease(lease, now=1000.0)


def test_validate_id() -> None:
    """Pin that validation fails with lease_id: if lease_id is empty."""
    lease = sign_lease(_lease(lease_id=""))
    with pytest.raises(LeaseInvalid, match="^lease_id:"):
        validate_lease(lease, now=1000.0)


def test_validate_subject() -> None:
    """Pin that validation fails with lease_subject: if run_id is empty."""
    lease = sign_lease(_lease(subject=LeaseSubject(run_id="")))
    with pytest.raises(LeaseInvalid, match="^lease_subject:"):
        validate_lease(lease, now=1000.0)


def test_validate_timestamps() -> None:
    """Pin that validation fails with lease_timestamps: if expires_at <= issued_at."""
    lease = sign_lease(_lease(issued_at=1000.0, expires_at=1000.0))
    with pytest.raises(LeaseInvalid, match="^lease_timestamps:"):
        validate_lease(lease, now=1000.0)


def test_validate_roots() -> None:
    """Pin that validation fails with lease_roots: if any fs root is relative."""
    lease = sign_lease(_lease(fs_write_roots=("relative/path",)))
    with pytest.raises(LeaseInvalid, match="^lease_roots:"):
        validate_lease(lease, now=1000.0)


def test_validate_net_policy_bogus() -> None:
    """Pin that validation fails with lease_net_policy: if net_mode is unrecognized."""
    lease = sign_lease(_lease(net_mode="bogus"))
    with pytest.raises(LeaseInvalid, match="^lease_net_policy:"):
        validate_lease(lease, now=1000.0)


def test_validate_net_policy_empty_proxy() -> None:
    """Pin that validation fails with lease_net_policy: if proxy mode lacks allowed domains."""
    lease = sign_lease(_lease(net_mode="proxy", net_allow_domains=()))
    with pytest.raises(LeaseInvalid, match="^lease_net_policy:"):
        validate_lease(lease, now=1000.0)


def test_validate_signature_unsigned() -> None:
    """Pin that validation fails with lease_signature: if the lease is unsigned."""
    lease = _lease()
    with pytest.raises(LeaseInvalid, match="^lease_signature:"):
        validate_lease(lease, now=1000.0)


def test_validate_signature_tampered() -> None:
    """Pin that validation fails with lease_signature: if the lease signature is tampered."""
    lease = sign_lease(_lease())
    tampered = dataclasses.replace(lease, expires_at=1080.0)
    with pytest.raises(LeaseInvalid, match="^lease_signature:"):
        validate_lease(tampered, now=1000.0)


def test_validate_revocation_via_generation() -> None:
    """Pin that bumping system generation revokes outstanding leases, failing with lease_generation:."""
    lease = sign_lease(_lease())
    bump_generation()
    with pytest.raises(LeaseInvalid, match="^lease_generation:"):
        validate_lease(lease, now=1000.0)


def test_validate_expired() -> None:
    """Pin that validation fails with lease_expired: if the lease has expired relative to validation time."""
    now_val = time.time()
    lease = sign_lease(_lease(issued_at=now_val - 10, expires_at=now_val - 1))
    with pytest.raises(LeaseInvalid, match="^lease_expired:"):
        validate_lease(lease)


def test_validate_ordering_forged_and_expired() -> None:
    """Pin that a forged lease that is also expired fails with lease_signature: first."""
    lease = _lease(issued_at=900.0, expires_at=950.0)
    with pytest.raises(LeaseInvalid, match="^lease_signature:"):
        validate_lease(lease, now=1000.0)


def test_remaining_seconds() -> None:
    """Pin that remaining_seconds returns positive seconds for active lease and clamps to 0.0 for expired lease."""
    lease = _lease(expires_at=1060.0)
    assert remaining_seconds(lease, now=1000.0) == pytest.approx(60.0)
    assert remaining_seconds(lease, now=1100.0) == 0.0


def test_lease_is_valid_returns_false() -> None:
    """Pin that lease_is_valid returns False rather than raising an exception for invalid leases."""
    invalid_lease = _lease()
    assert lease_is_valid(invalid_lease, now=1000.0) is False
