"""Model definitions and cryptographic validation routines for execution Leases.

This module implements the core Lease model, which defines the sandboxing,
capabilities, process-level resource ceilings, and credential access boundaries
granted to an execution run. To enforce safety-critical isolation, leases are
cryptographically signed via an ephemeral process-private HMAC-SHA256 key and
validated dynamically. A generation-based revocation mechanism is also provided
to invalidate all outstanding leases concurrently.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

LEASE_VERSION: int = 1


@dataclass(frozen=True)
class LeaseSubject:
    """The identity binding for which this lease is authorized.

    A lease is bound to a specific execution run, task, and session to prevent
    privilege escalation or reuse of signed capability tokens across unrelated
    workloads. This forms the identity boundary of the execution environment.
    """

    run_id: str
    task_id: str = ""
    session_id: str = ""


@dataclass(frozen=True)
class LeaseCeilings:
    """Resource constraints imposed on the executed process.

    These fields map directly to system-level resource limit configurations,
    such as RLIMIT_CPU or max process limits. An unset limit is denoted by 0.0
    or 0, indicating that default standard system boundaries should apply.
    """

    cpu_s: float = 0.0  # RLIMIT_CPU seconds; 0.0 => unset
    rss_mb: float = 0.0  # advisory on macOS; 0.0 => unset
    max_procs: int = 0  # RLIMIT_NPROC; 0 => unset
    wall_s: float = 0.0  # enforced by the caller's timeout path; 0.0 => unset


@dataclass(frozen=True)
class Lease:
    """A cryptographic capability token authorizing a runtime execution environment.

    This class is frozen to guarantee that once signed, its configuration settings
    cannot be mutated in-memory. It encompasses filesystem root permissions,
    network policies, capabilities, credential handles, and execution ceilings.
    """

    lease_id: str
    subject: LeaseSubject
    issued_at: float  # epoch seconds, UTC
    expires_at: float  # epoch seconds, UTC
    generation: int
    fs_read_roots: tuple[str, ...] = ()
    fs_write_roots: tuple[str, ...] = ()
    net_mode: str = "open"  # "open" | "deny" | "proxy"
    net_allow_domains: tuple[str, ...] = ()
    # Destination ports the proxy may dial on the lease's behalf. SIGNED (it rides
    # in the net_policy wire shape under proxy) so the seam consumes the value the
    # lease was issued with rather than re-reading mutable config after validation
    # -- otherwise a concurrent config edit could widen the port set without
    # invalidating the HMAC (security review finding #6).
    net_allow_ports: tuple[int, ...] = ()
    capabilities: tuple[str, ...] = ()
    credential_handles: tuple[str, ...] = ()
    ceilings: LeaseCeilings = field(default_factory=LeaseCeilings)
    auto_run_effect_classes: tuple[str, ...] = ()
    approval_required_classes: tuple[str, ...] = ()
    version: int = LEASE_VERSION
    signature: str = ""  # hex HMAC-SHA256; "" => unsigned

    @property
    def net_policy(self) -> str | dict[str, list[Any]]:
        """Return the wire shape required by the execution sandbox specification.

        This property translates the stored 'net_mode' and 'net_allow_domains'
        attributes into a single JSON-serializable wire policy. Mode 'open' or
        'deny' is returned directly as a string, while 'proxy' mode is rendered
        as a dictionary carrying the allowed domain list.

        NOTE for anyone extending this: the canonical payload signs THIS derived
        value, so ``net_allow_domains`` is inside the signature only under proxy.
        ``validate.validate_lease`` therefore REJECTS a non-proxy lease that
        carries domains, which is what keeps "every field is signed or provably
        empty" a total property rather than a coincidence of current callers.
        """
        if self.net_mode == "proxy":
            return {
                "proxy": list(self.net_allow_domains),
                "ports": [int(port) for port in self.net_allow_ports],
            }
        return self.net_mode


# Ephemeral private signing key generated once at module import.
_SIGNING_KEY: bytes = secrets.token_bytes(32)


def _signing_key() -> bytes:
    """Retrieve the module-private ephemeral signing key.

    Because the key is per-process and ephemeral, a lease is only verifiable
    inside the process that issued it, and that is DELIBERATE — the lease
    authenticates the parent's own issuance decision to itself at the launch
    seam; it is not a bearer token and must never become one. A process
    restart therefore invalidates every outstanding lease, which is the correct
    fail-closed behavior.
    """
    return _SIGNING_KEY


def new_lease_id() -> str:
    """Generate a random lease identifier with a standardized prefix.

    Returns a unique string composed of the 'lse_' prefix followed by 16 hex
    characters (8 random bytes), guaranteeing distinct lease identities.
    """
    return "lse_" + secrets.token_hex(8)


def lease_payload(lease: Lease) -> dict[str, Any]:
    """Serialize all lease fields except the signature into a plain dictionary.

    This builds the exact JSON-compatible payload dict for transmission and signing.
    All tuple values are converted to standard lists to guarantee serialization
    compatibility across platforms. The stored net_mode and net_allow_domains
    fields are replaced with the derived net_policy wire representation.
    """
    return {
        "version": lease.version,
        "lease_id": lease.lease_id,
        "subject": {
            "run_id": lease.subject.run_id,
            "task_id": lease.subject.task_id,
            "session_id": lease.subject.session_id,
        },
        "issued_at": lease.issued_at,
        "expires_at": lease.expires_at,
        "generation": lease.generation,
        "fs_read_roots": list(lease.fs_read_roots),
        "fs_write_roots": list(lease.fs_write_roots),
        "net_policy": lease.net_policy,
        "capabilities": list(lease.capabilities),
        "credential_handles": list(lease.credential_handles),
        "ceilings": {
            "cpu_s": lease.ceilings.cpu_s,
            "rss_mb": lease.ceilings.rss_mb,
            "max_procs": lease.ceilings.max_procs,
            "wall_s": lease.ceilings.wall_s,
        },
        "auto_run_effect_classes": list(lease.auto_run_effect_classes),
        "approval_required_classes": list(lease.approval_required_classes),
    }


def canonical_payload(lease: Lease) -> bytes:
    """Return a deterministic JSON byte string of the lease payload for signing.

    This function serializes the dictionary produced by lease_payload into a
    deterministic byte sequence using minimal separators, sorted keys, and
    forced UTF-8 encoding. This ensures the representation is absolutely
    consistent regardless of runtime environment or dictionary ordering.
    """
    return json.dumps(
        lease_payload(lease),
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")


def sign_lease(lease: Lease) -> Lease:
    """Compute and append a cryptographic signature to the Lease.

    Returns a new Lease instance with the 'signature' field populated with the
    hex HMAC-SHA256 signature calculated over the canonical payload representation.
    """
    return dataclasses.replace(lease, signature=lease_signature(lease))


def lease_signature(lease: Lease) -> str:
    """Compute the HMAC-SHA256 hex digest over the canonical lease payload.

    The signature is derived using the ephemeral, process-private signing key
    to guarantee that the lease was generated genuinely within this exact session.
    """
    return hmac.new(
        _signing_key(),
        canonical_payload(lease),
        hashlib.sha256,
    ).hexdigest()


def signature_matches(lease: Lease) -> bool:
    """Verify that the lease signature is valid and untampered with.

    Uses hmac.compare_digest to defend against timing-attack analysis. If the
    lease's signature attribute is empty, the function automatically returns
    False to prevent verification of unsigned tokens.
    """
    return bool(lease.signature) and hmac.compare_digest(
        lease.signature,
        lease_signature(lease),
    )


def lease_record(lease: Lease) -> dict[str, Any]:
    """Generate the ledger projection record for telemetry logging and storage.

    To enforce strict safety boundaries and avoid key or signature leaks,
    this returns the standard JSON-safe payload dictionary augmented with a
    'signed' boolean verification status, but deliberately excludes the raw
    cryptographic signature string itself.
    """
    record = lease_payload(lease)
    record["signed"] = signature_matches(lease)
    return record


# Global thread-safe generation state for lease revocation.
_generation_lock = threading.Lock()
_CURRENT_GENERATION = 1


def current_generation() -> int:
    """Return the active lease generation.

    Any leases bearing an older generation identifier are treated as globally
    revoked, allowing immediate invalidation of all previously issued leases.
    """
    with _generation_lock:
        return _CURRENT_GENERATION


def bump_generation() -> int:
    """Increment the active lease generation, revoking all existing leases.

    This thread-safe operation bumps the global generation counter and returns
    the newly established generation value.
    """
    global _CURRENT_GENERATION
    with _generation_lock:
        _CURRENT_GENERATION += 1
        return _CURRENT_GENERATION


__all__ = [
    "LEASE_VERSION",
    "Lease",
    "LeaseCeilings",
    "LeaseSubject",
    "bump_generation",
    "canonical_payload",
    "current_generation",
    "lease_payload",
    "lease_record",
    "lease_signature",
    "new_lease_id",
    "sign_lease",
    "signature_matches",
]
