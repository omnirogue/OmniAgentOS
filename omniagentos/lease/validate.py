"""Validation logic and lease policy enforcement for execution Leases.

This module enforces the safety-critical constraints specified by an autonomy lease
before authorizing runtime execution. It acts as the gateway or guardrail for any sandboxed
process, ensuring that the lease's cryptographic signature matches, the lease version is
correct, the lease has not expired or been revoked, and its path and network policies
comply with security invariants.

By validating leases strictly, the system guarantees that execution settings cannot
be bypassed or forged. Any failure in lease verification raises a LeaseInvalid exception,
which must immediately halt the launch flow.
"""

from __future__ import annotations

import math
import os
import time

from omniagentos.lease.models import LEASE_VERSION, Lease, current_generation, signature_matches

CLOCK_SKEW_S: float = 60.0
"""A clock skew tolerance in seconds.

This tolerates a small forward clock jump on the issuing host but never extends
an EXPIRY (expiry is checked strictly, with no skew grace — an expired lease must
fail closed).
"""

NET_MODES: tuple[str, ...] = ("open", "deny", "proxy")


class LeaseInvalid(RuntimeError):
    """A lease failed validation; the caller MUST refuse the launch.

    This exception signals that the lease has breached security, cryptographic,
    or structural invariants, and the associated sandboxed process execution
    must be aborted to prevent unauthorized access or operations.
    """


def validate_lease(lease: Lease, *, now: float | None = None) -> None:
    """Validate that the given lease satisfies all security constraints.

    This function executes an ordered sequence of checks to confirm the structural,
    path-level, policy-level, cryptographic, and temporal validity of a lease.
    It raises a `LeaseInvalid` exception with a short, machine-greppable reason
    prefix on the very first failing check to aid in telemetry and failure diagnosis.

    The validation checks are executed in the following strict order:
    1. Lease version matches `LEASE_VERSION`.
    2. Lease ID is a non-empty string.
    3. Lease subject's run_id is a non-empty string.
    4. Timestamps (`issued_at` and `expires_at`) are finite positive floats, and
       `expires_at` is strictly greater than `issued_at`.
    5. All read/write roots are non-empty absolute paths.
    6. Network mode is valid, and proxy mode contains at least one allowed domain.
    7. Cryptographic signature matches.
    8. Generation matches the currently active system generation (not revoked).
    9. Lease is not being used before its validity window starts (accounting for clock skew).
    10. Lease has not expired.

    The signature verification is explicitly sequenced before the expiry verification.
    This guarantees that an unsigned or forged lease is correctly reported as a
    forged lease ("lease_signature:") rather than merely as an expired lease, which
    prevents attackers from discovering valid expiration checks on forged tokens.

    Args:
        lease: The Lease instance to validate.
        now: The epoch timestamp in seconds to use as the current time. If None,
            defaults to time.time().

    Raises:
        LeaseInvalid: If any check fails, containing a machine-greppable prefix.
    """
    if now is None:
        now = time.time()

    # 1. lease_version:
    if lease.version != LEASE_VERSION:
        raise LeaseInvalid(
            f"lease_version: lease version {lease.version} does not match {LEASE_VERSION}"
        )

    # 2. lease_id:
    if not isinstance(lease.lease_id, str) or not lease.lease_id:
        raise LeaseInvalid("lease_id: lease_id must be a non-empty string")

    # 3. lease_subject:
    if not isinstance(lease.subject.run_id, str) or not lease.subject.run_id:
        raise LeaseInvalid("lease_subject: run_id must be a non-empty string")

    # 4. lease_timestamps:
    # Check issued_at is a finite positive float
    if (
        not isinstance(lease.issued_at, (int, float))
        or isinstance(lease.issued_at, bool)
        or lease.issued_at <= 0.0
        or not math.isfinite(lease.issued_at)
    ):
        raise LeaseInvalid("lease_timestamps: issued_at must be a finite positive float")

    # Check expires_at is a finite positive float
    if (
        not isinstance(lease.expires_at, (int, float))
        or isinstance(lease.expires_at, bool)
        or lease.expires_at <= 0.0
        or not math.isfinite(lease.expires_at)
    ):
        raise LeaseInvalid("lease_timestamps: expires_at must be a finite positive float")

    # Check expires_at is strictly greater than issued_at
    if lease.expires_at <= lease.issued_at:
        raise LeaseInvalid(
            f"lease_timestamps: expires_at ({lease.expires_at}) must be greater than issued_at ({lease.issued_at})"
        )

    # 5. lease_roots:
    for root in lease.fs_read_roots:
        if not isinstance(root, str) or not root or not os.path.isabs(root):
            raise LeaseInvalid(
                f"lease_roots: fs_read_roots entry must be a non-empty absolute path: {root!r}"
            )

    for root in lease.fs_write_roots:
        if not isinstance(root, str) or not root or not os.path.isabs(root):
            raise LeaseInvalid(
                f"lease_roots: fs_write_roots entry must be a non-empty absolute path: {root!r}"
            )

    # 6. lease_net_policy:
    if lease.net_mode not in NET_MODES:
        raise LeaseInvalid(f"lease_net_policy: net_mode {lease.net_mode!r} not in {NET_MODES}")

    if lease.net_mode == "proxy":
        if not lease.net_allow_domains or not any(
            isinstance(d, str) and d.strip() for d in lease.net_allow_domains
        ):
            raise LeaseInvalid(
                "lease_net_policy: proxy net_mode requires at least one allowed domain"
            )
    elif lease.net_allow_domains:
        # SIGNATURE-COVERAGE INVARIANT. The canonical payload carries the derived
        # ``net_policy`` wire shape, which folds net_allow_domains in ONLY under
        # proxy -- so under open/deny the domain tuple sits outside the signature.
        # Today nothing reads it there, which makes the gap inert rather than
        # exploitable, but "inert because no current caller looks at it" is an
        # argument that expires the moment someone adds a caller. Rejecting the
        # combination outright upgrades it to a total property: every field this
        # lease carries is either covered by the signature or provably empty.
        raise LeaseInvalid(
            f"lease_net_policy: net_mode {lease.net_mode!r} must carry no allowed "
            "domains (they would sit outside the signature)"
        )

    # 6b. lease_ceilings: every ceiling must be a finite, non-negative number.
    # A SIGNED lease carrying NaN or a negative ceiling would sail through the
    # ``> 0`` guards in rlimits.rlimit_plan and silently produce NO limit, which
    # is an enforce-mode lease that is quietly less confined than it claims
    # (security review finding #9). Reject it rather than let it degrade.
    for name in ("cpu_s", "rss_mb", "max_procs", "wall_s"):
        value = getattr(lease.ceilings, name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise LeaseInvalid(f"lease_ceilings: {name} must be a number, got {value!r}")
        if not math.isfinite(value) or value < 0:
            raise LeaseInvalid(
                f"lease_ceilings: {name} must be finite and non-negative, got {value!r}"
            )

    # 6c. lease_net_ports: proxy ports must be real ports, and a NON-proxy lease
    # must carry none -- they only enter the signature under proxy, so the same
    # "signed or provably empty" rule that governs the domains governs these.
    if lease.net_mode == "proxy":
        if not lease.net_allow_ports:
            raise LeaseInvalid("lease_net_ports: proxy net_mode requires at least one allowed port")
        for port in lease.net_allow_ports:
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise LeaseInvalid(f"lease_net_ports: invalid port {port!r}")
    elif lease.net_allow_ports:
        raise LeaseInvalid(
            f"lease_net_ports: net_mode {lease.net_mode!r} must carry no allowed ports"
        )

    # 7. lease_signature:
    if not signature_matches(lease):
        raise LeaseInvalid("lease_signature: cryptographic signature verification failed")

    # 8. lease_generation:
    active_gen = current_generation()
    if lease.generation != active_gen:
        raise LeaseInvalid(
            f"lease_generation: lease generation {lease.generation} != current generation {active_gen}"
        )

    # 9. lease_not_yet_valid:
    if lease.issued_at > now + CLOCK_SKEW_S:
        raise LeaseInvalid(
            f"lease_not_yet_valid: lease issued_at {lease.issued_at} is too far in the future compared to now {now}"
        )

    # 10. lease_expired:
    if lease.expires_at <= now:
        raise LeaseInvalid(f"lease_expired: lease expired at {lease.expires_at} (now is {now})")


def lease_is_valid(lease: Lease, *, now: float | None = None) -> bool:
    """Check whether the given lease is valid, returning a boolean.

    This wraps `validate_lease` in a try/except block. It catches only
    `LeaseInvalid` exceptions, returning `False` in that case, and `True`
    on success. Any other unexpected exceptions (such as structure errors)
    are not caught and will propagate.

    Args:
        lease: The Lease instance to check.
        now: The epoch timestamp in seconds to use as the current time. If None,
            defaults to time.time().

    Returns:
        True if the lease is valid, False otherwise.
    """
    try:
        validate_lease(lease, now=now)
        return True
    except LeaseInvalid:
        return False


def remaining_seconds(lease: Lease, *, now: float | None = None) -> float:
    """Return the number of seconds remaining until the lease expires.

    Args:
        lease: The Lease instance.
        now: The epoch timestamp in seconds to use as the current time. If None,
            defaults to time.time().

    Returns:
        The remaining duration in seconds, clamped to a minimum of 0.0.
    """
    if now is None:
        now = time.time()
    return max(0.0, lease.expires_at - now)


__all__ = [
    "CLOCK_SKEW_S",
    "LeaseInvalid",
    "NET_MODES",
    "lease_is_valid",
    "remaining_seconds",
    "validate_lease",
]
