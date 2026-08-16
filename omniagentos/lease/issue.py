"""Lease issuance and cryptographic signing factory for Sandboxed Autonomy.

This module provides the primary entry point for constructing, configuring, and
cryptographically signing a Lease capability token. Leases are issued at the
launch seam of an adapter or process execution run to define rigorous boundaries
for filesystems, networks, capabilities, and process-level resource limits.

By enforcing the creation of a Lease prior to execution, the platform establishes
a robust audit ledger and ensures that all spawned operations are bounded by
pre-approved scopes.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence

from omniagentos.lease import config, models
from omniagentos.lease.models import Lease, LeaseCeilings, LeaseSubject


def issue_lease(
    *,
    run_id: str,
    task_id: str = "",
    session_id: str = "",
    workspace_dir: str,
    fs_write_roots: Sequence[str] = (),
    fs_read_roots: Sequence[str] = (),
    provider: str | None = None,
    timeout_ms: int | None = None,
    env_groups: Sequence[str] = (),
    capabilities: Sequence[str] = (),
    credential_handles: Sequence[str] = (),
    net_mode: str | None = None,
    net_allow_domains: Sequence[str] | None = None,
    net_allow_ports: Sequence[int] | None = None,
    ttl_s: float | None = None,
    now: float | None = None,
    generation: int | None = None,
) -> Lease:
    """Compose and cryptographically sign an execution Lease.

    This is the authoritative, single-point factory for issuing Sandboxed
    Autonomy Leases. It resolves and merges input boundaries, environment-wide
    resource ceilings, duration budgets, and capabilities, and signs the result
    using the active process-private ephemeral HMAC-SHA256 key.

    Design and Enforcement Rules:
    1. Timestamps & Lifespan:
       `issued_at` is set to the provided `now` or defaults to current epoch.
       `ttl_s` defaults to `config.lease_ttl_s()`.
       The effective lease lifetime is calculated as:
       `min(ttl_s, ceilings.wall_s)` (if wall_s is configured) and additionally
       `min(..., timeout_ms / 1000)` (if timeout_ms is a positive integer). This
       ensures a lease never outlives the wall-clock budget of the process it
       authorizes. A safety-critical floor of 1.0 second is enforced so that
       even with pathological inputs, expires_at > issued_at.
    2. Ceilings:
       LeaseCeilings are loaded from `config.lease_ceilings()`. If a positive
       timeout_ms is provided, the ceilings.wall_s attribute is updated to the
       minimum of the configured wall_s (or infinity if unset) and the converted
       timeout_ms duration.
    3. Filesystem Roots:
       `fs_write_roots` forms the ordered, realpath-resolved, de-duplicated union
       of the mandatory `workspace_dir` and any extra passed write roots. Empty
       roots are dropped.
       `fs_read_roots` undergo the exact same resolution, de-duplication, and
       empty-filtering process. An empty tuple here indicates that default
       read-profile policies apply, not that reading is fully disabled.
    4. Network Policy:
       The execution `net_mode` defaults to `config.lease_net_policy()`.
       Unrecognized mode values automatically degrade to "open".
       `net_allow_domains` is only populated and carried within the Lease when the
       mode is "proxy"; otherwise, it is stored as an empty tuple to keep the
       wire shape exact.
    5. Capabilities:
       The resulting capabilities list contains all explicitly requested capability
       strings, augmented with a derived `f"provider:{provider}"` string if a
       non-empty provider string is supplied. Duplicate entries are eliminated while
       preserving their original insertion order.
    6. Credential Handles (Names Only):
       No actual credential values, secret keys, or environment values are EVER
       placed inside a Lease. Because Leases are logged to an open audit ledger,
       storing values would represent a severe security risk. Instead, the Lease
       stores only symbolic "handles" (e.g., `f"env-group:{g}"` for each env_groups
       entry plus the provided credential_handles). The sandbox broker resolves
       these handles to real secrets within the secure parent process at spawn time.
    7. Future-proofing:
       `auto_run_effect_classes` and `approval_required_classes` are populated as
       empty tuples in this version. This maintains forward compatibility with
       upstream policy specifications, allowing downstream authorization layers
       to interact with these fields without altering the Lease structure.

    Args:
        run_id: Unique identifier for the execution run.
        task_id: Optional identifier of the task triggering execution.
        session_id: Optional session identifier binding the execution context.
        workspace_dir: Path to the active workspace directory (always added to write roots).
        fs_write_roots: Sequence of additional writable directory roots.
        fs_read_roots: Sequence of additional readable directory roots.
        provider: Optional identifier of the execution provider.
        timeout_ms: Optional execution timeout duration in milliseconds.
        env_groups: Sequence of environment group names to map to credential handles.
        capabilities: Sequence of explicitly requested system or model capabilities.
        credential_handles: Sequence of explicitly requested credential handles.
        net_mode: Optional network mode ("open" | "deny" | "proxy").
        net_allow_domains: Optional sequence of domains permitted under proxying.
        ttl_s: Optional manual time-to-live override in seconds.
        now: Optional current timestamp override in epoch seconds.
        generation: Optional lease generation identifier override.

    Returns:
        A fully constructed, validated, and signed Lease object.
    """
    # 1. Resolve timestamps and lifespans
    issued_at = now if now is not None else time.time()
    base_ttl = ttl_s if ttl_s is not None else config.lease_ttl_s()

    # 2. Resolve ceilings and adjust wall_s
    ceilings_dict = config.lease_ceilings()
    config_wall_s = ceilings_dict.get("wall_s", 0.0)

    wall_s = config_wall_s
    if timeout_ms is not None and timeout_ms > 0:
        limit_val = config_wall_s if config_wall_s > 0 else float("inf")
        wall_s = min(limit_val, timeout_ms / 1000.0)

    ceilings = LeaseCeilings(
        cpu_s=ceilings_dict.get("cpu_s", 0.0),
        rss_mb=ceilings_dict.get("rss_mb", 0.0),
        max_procs=int(ceilings_dict.get("max_procs", 0)),
        wall_s=wall_s,
    )

    # Calculate effective TTL
    effective_ttl = base_ttl
    if ceilings.wall_s > 0:
        effective_ttl = min(effective_ttl, ceilings.wall_s)
    if timeout_ms is not None and timeout_ms > 0:
        effective_ttl = min(effective_ttl, timeout_ms / 1000.0)

    # Enforce standard floor of 1.0 second
    effective_ttl = max(effective_ttl, 1.0)
    expires_at = issued_at + effective_ttl

    # 3. Resolve and clean filesystem roots
    def _clean_roots(roots: Sequence[str]) -> tuple[str, ...]:
        seen: set[str] = set()
        cleaned: list[str] = []
        for r in roots:
            if not r:
                continue
            resolved = os.path.realpath(r)
            if not resolved:
                continue
            if resolved not in seen:
                seen.add(resolved)
                cleaned.append(resolved)
        return tuple(cleaned)

    # Union of workspace_dir and fs_write_roots
    write_inputs: list[str] = []
    if workspace_dir:
        write_inputs.append(workspace_dir)
    write_inputs.extend(fs_write_roots)

    fs_write_roots_tuple = _clean_roots(write_inputs)
    fs_read_roots_tuple = _clean_roots(fs_read_roots)

    # 4. Resolve network policy and domain constraints
    resolved_net_mode = net_mode if net_mode is not None else config.lease_net_policy()
    if resolved_net_mode not in ("open", "deny", "proxy"):
        resolved_net_mode = "open"

    net_allow_domains_tuple: tuple[str, ...] = ()
    if resolved_net_mode == "proxy":
        domains_seq = (
            net_allow_domains if net_allow_domains is not None else config.lease_allowed_domains()
        )
        seen_domains: set[str] = set()
        cleaned_domains: list[str] = []
        for d in domains_seq:
            if d:
                cleaned_d = d.strip().lower()
                if cleaned_d and cleaned_d not in seen_domains:
                    seen_domains.add(cleaned_d)
                    cleaned_domains.append(cleaned_d)
        net_allow_domains_tuple = tuple(cleaned_domains)

    # Destination ports are part of the SIGNED policy (finding #6): resolve them
    # here, at issuance, so the seam never re-reads mutable config after validation.
    net_allow_ports_tuple: tuple[int, ...] = ()
    if resolved_net_mode == "proxy":
        ports_seq = net_allow_ports if net_allow_ports is not None else config.lease_proxy_ports()
        seen_ports: set[int] = set()
        cleaned_ports: list[int] = []
        for raw_port in ports_seq:
            try:
                value = int(raw_port)
            except (TypeError, ValueError):
                continue
            if 1 <= value <= 65535 and value not in seen_ports:
                seen_ports.add(value)
                cleaned_ports.append(value)
        net_allow_ports_tuple = tuple(cleaned_ports)

    # 5. Build capabilities with provider suffixing
    caps: list[str] = list(capabilities)
    if provider and isinstance(provider, str) and provider.strip():
        caps.append(f"provider:{provider.strip()}")

    seen_caps: set[str] = set()
    deduped_caps: list[str] = []
    for c in caps:
        if c and c not in seen_caps:
            seen_caps.add(c)
            deduped_caps.append(c)
    capabilities_tuple = tuple(deduped_caps)

    # 6. Build credential handles (No values stored, handles/names only for auditing safety)
    handles: list[str] = list(credential_handles)
    for g in env_groups:
        if g:
            handles.append(f"env-group:{g}")

    seen_handles: set[str] = set()
    deduped_handles: list[str] = []
    for h in handles:
        if h and h not in seen_handles:
            seen_handles.add(h)
            deduped_handles.append(h)
    credential_handles_tuple = tuple(deduped_handles)

    # 7. Default or custom generation
    if generation is None:
        generation = models.current_generation()

    # Construct the base lease object
    subject = LeaseSubject(
        run_id=run_id,
        task_id=task_id,
        session_id=session_id,
    )

    lease = Lease(
        lease_id=models.new_lease_id(),
        subject=subject,
        issued_at=issued_at,
        expires_at=expires_at,
        generation=generation,
        fs_read_roots=fs_read_roots_tuple,
        fs_write_roots=fs_write_roots_tuple,
        net_mode=resolved_net_mode,
        net_allow_domains=net_allow_domains_tuple,
        net_allow_ports=net_allow_ports_tuple,
        capabilities=capabilities_tuple,
        credential_handles=credential_handles_tuple,
        ceilings=ceilings,
        auto_run_effect_classes=(),
        approval_required_classes=(),
    )

    # Return cryptographically signed lease
    return models.sign_lease(lease)


__all__ = [
    "issue_lease",
]
