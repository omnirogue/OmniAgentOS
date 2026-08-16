"""The ONE place the autonomy lease meets a real process launch.

Everything else in :mod:`omniagentos.lease` is pure data: a signed record, its
validator, a rlimit plan, an allowlisting proxy. This module is where those become
a decision about an actual ``subprocess.Popen`` — and it is deliberately the only
module that knows how to translate a lease into launch parameters, so the adapter
chokepoint (``omniagentos.adapters.common.CliAdapter._invoke``) stays a handful of
readable lines and the security-relevant reasoning lives in one auditable file.

THE THREE MODES, stated as behavior rather than intent:

``off`` (the default)
    :func:`lease_active` returns ``False`` and :func:`plan_launch` is never called.
    Not one byte of the launch changes: same argv, same SBPL profile, same env,
    same timeout, ``preexec_fn=None``. This is the property that makes the whole
    lane safe to merge — the flag being off is indistinguishable from the code not
    existing.
``shadow``
    A lease is issued, validated, and appended to the append-only lease ledger.
    The returned plan is a NO-OP plan: every field the caller would act on is
    ``None``/empty, so the launch is byte-identical to ``off``. Any failure at all
    — issuance, validation, ledger write — is swallowed and degrades to the same
    no-op plan. Shadow mode measures; it must never be able to break a launch.
``enforce``
    The lease DRIVES the mechanisms that already exist: its ``fs_write_roots``
    become the write roots handed to ``wrap_command``; its remaining TTL clamps the
    existing timeout; its ceilings become ``resource.setrlimit`` calls in a
    ``preexec_fn``; its ``net_policy`` selects the SBPL egress mode and, for
    ``proxy``, starts the parent-side allowlisting CONNECT proxy and injects
    ``HTTPS_PROXY`` into the child. No NEW credential path is created: env scoping
    remains exactly ``_scrubbed_env`` plus the broker.

FAIL CLOSED, precisely. In ``enforce`` mode a refusal (a populated
``LaunchPlan.refusal``) is returned — never a weakened launch — when:

* lease issuance raises;
* :func:`omniagentos.lease.validate.validate_lease` rejects the lease (bad
  signature, wrong generation i.e. revoked, expired, malformed roots);
* the outer OS sandbox wrap would NOT actually engage for this argv
  (``runner.sandbox.wrap_available`` is ``False``). This check is the one that is
  easy to omit and expensive to omit: ``wrap_command`` returns the argv UNWRAPPED
  when the sandbox is unavailable, so without it an enforce-mode lease promising
  ``net_policy: deny`` would hand back a completely unconfined process while the
  ledger recorded a confinement that never happened;
* a ``proxy`` lease cannot start its proxy.

The caller turns ``refusal`` into an ERROR result exactly the way
``CliAdapter._refuse_unattended_launch`` does. This module NEVER relaxes that floor
or the ``disable_inner_sandbox`` gating — it only ever narrows.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from omniagentos.lease import config as lease_config
from omniagentos.lease.issue import issue_lease
from omniagentos.lease.models import Lease
from omniagentos.lease.record import append_lease_record
from omniagentos.lease.rlimits import limit_report, make_preexec
from omniagentos.lease.validate import LeaseInvalid, remaining_seconds, validate_lease

LOG = logging.getLogger(__name__)

__all__ = [
    "LaunchPlan",
    "close_plan",
    "confirm_at_spawn",
    "lease_active",
    "no_op_plan",
    "plan_launch",
]


@dataclass
class LaunchPlan:
    """What (if anything) the launch seam should do differently for this spawn.

    Every field defaults to "change nothing", so a caller that receives a default
    ``LaunchPlan`` performs exactly the launch it would have performed without the
    lease. That default-is-inert shape is intentional: a bug in this module should
    degrade toward today's behavior, not toward a novel one.
    """

    mode: str = "off"
    lease: Lease | None = None
    #: Non-``None`` only under ``enforce``. A populated refusal means the caller
    #: MUST NOT spawn anything and must surface an ERROR result.
    refusal: str | None = None
    #: ``None`` => the caller keeps the write roots it already computed.
    write_roots: tuple[str, ...] | None = None
    #: ``None`` => the caller keeps its own timeout.
    timeout_ms: int | None = None
    net_mode: str = "open"
    net_loopback_ports: tuple[int, ...] = ()
    #: Literal paths the profile must write-DENY on top of its normal denies.
    #: Empty for off/shadow, so the profile stays byte-identical there.
    write_deny_literals: tuple[str, ...] = ()
    #: Merged into the child env AFTER ``_scrubbed_env`` and after any caller
    #: overrides. Only ever proxy routing variables — never a credential.
    env_additions: dict[str, str] = field(default_factory=dict)
    preexec_fn: Callable[[], None] | None = None
    #: Owned by the plan; :func:`close_plan` shuts it down. Typed loosely so this
    #: module does not import the proxy at module scope (it is only needed under
    #: an enforce-mode proxy lease).
    proxy: Any = None


def no_op_plan(mode: str = "off") -> LaunchPlan:
    """A plan that changes nothing — the shadow-mode and error-degradation result."""
    return LaunchPlan(mode=mode)


def lease_active() -> bool:
    """True when the autonomy lease is in ``shadow`` or ``enforce``.

    The caller gates the ENTIRE lease block on this, so with the flag off the cost
    is one config lookup and the launch path is untouched.
    """
    return lease_config.lease_enabled()


def _refusal(provider: str, reason: str) -> str:
    """One refusal string shape, greppable and consistent with the existing floor.

    Mirrors ``unattended_no_sandbox_refused:cli-<name> ...`` from
    ``CliAdapter._refuse_unattended_launch`` so operators grep one pattern for
    "a sub-CLI launch was refused for a safety reason".
    """
    return f"autonomy_lease_refused:cli-{provider} {reason}"


def _record(lease: Lease, *, event: str, mode: str, extra: dict[str, Any]) -> None:
    """Append one ledger row; a ledger failure never affects the launch decision."""
    try:
        append_lease_record(lease, event=event, mode=mode, extra=extra)
    except Exception:  # noqa: BLE001 - telemetry must never break a launch
        LOG.warning("lease ledger append failed for %s", lease.lease_id, exc_info=True)


def plan_launch(
    *,
    provider: str,
    argv: Sequence[str],
    working_dir: str,
    workspace_writable: bool,
    write_roots: Sequence[str],
    timeout_ms: int,
    run_id: str = "",
    task_id: str = "",
    session_id: str = "",
    env_groups: Sequence[str] = (),
) -> LaunchPlan:
    """Issue a lease for one sub-CLI spawn and translate it into launch parameters.

    ``write_roots`` is what the caller ALREADY computed (the CLI's own state dirs
    plus any project-granted extra dirs); ``working_dir`` is added by the lease when
    ``workspace_writable`` — the same union ``wrap_command`` would build. Passing
    the caller's roots in rather than recomputing them here is deliberate: the lease
    must describe the confinement that is actually applied, and a second
    implementation of "what are the write roots" would be free to drift from the
    first.

    Never raises. A failure in ``shadow`` degrades to a no-op plan; a failure in
    ``enforce`` becomes a refusal.
    """
    mode = lease_config.lease_mode()
    if mode == "off":  # defensive: callers gate on lease_active() first
        return no_op_plan("off")
    enforcing = mode == "enforce"

    try:
        lease = issue_lease(
            run_id=run_id or "run_unknown",
            task_id=task_id,
            session_id=session_id,
            workspace_dir=working_dir if workspace_writable else "",
            fs_write_roots=tuple(write_roots),
            provider=provider,
            timeout_ms=timeout_ms,
            env_groups=tuple(env_groups),
        )
    except Exception as exc:  # noqa: BLE001 - issuance must fail closed, not crash
        LOG.warning("autonomy lease issuance failed (mode=%s)", mode, exc_info=True)
        if enforcing:
            return LaunchPlan(mode=mode, refusal=_refusal(provider, f"issuance failed: {exc}"))
        return no_op_plan(mode)

    try:
        validate_lease(lease)
    except LeaseInvalid as exc:
        _record(lease, event="refused", mode=mode, extra={"reason": str(exc)})
        if enforcing:
            return LaunchPlan(mode=mode, lease=lease, refusal=_refusal(provider, str(exc)))
        return no_op_plan(mode)

    report = limit_report(lease.ceilings)

    if not enforcing:
        # SHADOW: record the decision we WOULD have enforced and change nothing.
        # The record is the entire point of the mode -- an evaluation that wrote
        # nothing would measure a hypothesis rather than a decision.
        _record(
            lease,
            event="issued",
            mode=mode,
            extra={"enforced": False, "limits": report, "workspace_writable": workspace_writable},
        )
        return no_op_plan(mode)

    # ---- enforce -------------------------------------------------------------
    # The outer wrap must actually engage. wrap_command silently returns the argv
    # UNWRAPPED when the sandbox is unavailable or the program is unresolvable, so
    # an enforce-mode lease would otherwise promise confinement (write roots, egress
    # policy) that no layer delivers. Refuse instead.
    from omniagentos.runner import sandbox as os_sandbox

    if not os_sandbox.wrap_available(list(argv), working_dir):
        reason = (
            "the OS sandbox wrap would not engage for this launch, so the lease's "
            "write-root and egress confinement cannot be applied"
        )
        _record(lease, event="refused", mode=mode, extra={"reason": reason})
        return LaunchPlan(mode=mode, lease=lease, refusal=_refusal(provider, reason))

    remaining_ms = int(remaining_seconds(lease) * 1000)
    plan = LaunchPlan(
        mode=mode,
        lease=lease,
        write_roots=tuple(lease.fs_write_roots),
        timeout_ms=max(1, min(timeout_ms, remaining_ms)),
        net_mode=lease.net_mode,
        preexec_fn=make_preexec(lease.ceilings),
        # The lease's OWN config file must be unwritable by the process the lease
        # authorizes. configs/lease.yaml governs mode, ceilings, domains, ports and
        # the ledger path, and a leased child whose workspace IS this repository
        # would otherwise be able to edit it and disable or widen the lease for
        # every subsequent launch — self-disabling confinement, the same vector the
        # profile already closes for ~/.claude/settings.json (security review
        # BLOCKER #4). Enforce-only, so off/shadow profiles stay byte-identical.
        write_deny_literals=(str(lease_config.lease_config_path()),),
    )

    if lease.net_mode == "proxy":
        from omniagentos.lease.proxy import AllowlistProxy, proxy_env

        try:
            # Ports come from the SIGNED lease, not from a fresh config read: a
            # value re-read after validation is a value the signature never
            # covered, and a concurrent config edit could widen it (finding #6).
            proxy = AllowlistProxy(
                list(lease.net_allow_domains),
                allowed_ports=lease.net_allow_ports,
            )
            _host, port = proxy.start()
        except OSError as exc:
            reason = f"egress proxy failed to start: {exc}"
            _record(lease, event="refused", mode=mode, extra={"reason": reason})
            return LaunchPlan(mode=mode, lease=lease, refusal=_refusal(provider, reason))
        plan.proxy = proxy
        plan.net_loopback_ports = (port,)
        plan.env_additions = proxy_env(proxy.url)

    _record(
        lease,
        event="issued",
        mode=mode,
        extra={
            "enforced": True,
            "limits": report,
            "workspace_writable": workspace_writable,
            "timeout_ms": plan.timeout_ms,
            "net_mode": plan.net_mode,
            "proxy_port": plan.net_loopback_ports[0] if plan.net_loopback_ports else 0,
        },
    )
    return plan


def confirm_at_spawn(plan: LaunchPlan, provider: str) -> tuple[str | None, int | None]:
    """Re-validate immediately before ``Popen`` and re-derive the timeout there.

    ``plan_launch`` validates once, but real work happens after that check: the
    egress proxy binds a socket, and the ledger row is a synchronous
    ``flock``+``fsync``. Those take time, and during that window the lease can
    EXPIRE or be REVOKED (``bump_generation``) — after which the original decision
    is stale and spawning on it would let a revoked lease start a process. The
    timeout computed up front has the same problem in miniature: it is a duration
    measured from the wrong instant, so a child could outlive ``expires_at`` by
    however long preparation took.

    So the authorization is confirmed HERE, at the boundary that matters, and the
    timeout is re-derived from the absolute deadline rather than reused. Returns
    ``(refusal, timeout_ms)``; a non-``None`` refusal means do not spawn. A no-op
    or shadow plan returns ``(None, None)`` and changes nothing (security review
    finding #8).
    """
    if plan.lease is None or plan.mode != "enforce":
        return None, None
    try:
        validate_lease(plan.lease)
    except LeaseInvalid as exc:
        _record(plan.lease, event="refused", mode=plan.mode, extra={"reason": f"at-spawn: {exc}"})
        return _refusal(provider, f"revalidation at spawn failed: {exc}"), None
    remaining_ms = int(remaining_seconds(plan.lease) * 1000)
    if remaining_ms <= 0:
        reason = "the lease expired while the launch was being prepared"
        _record(plan.lease, event="refused", mode=plan.mode, extra={"reason": reason})
        return _refusal(provider, reason), None
    ceiling = plan.timeout_ms if plan.timeout_ms is not None else remaining_ms
    return None, max(1, min(ceiling, remaining_ms))


def close_plan(plan: LaunchPlan | None) -> None:
    """Release anything the plan owns (currently only the egress proxy). Never raises.

    Callers MUST invoke this in a ``finally``: the proxy holds a listening socket
    and a daemon thread, and leaking one per invocation would exhaust descriptors
    on a long-lived control plane.
    """
    if plan is None or plan.proxy is None:
        return
    try:
        plan.proxy.stop()
    except Exception:  # noqa: BLE001 - teardown must never mask the real result
        LOG.warning("autonomy lease proxy shutdown failed", exc_info=True)
    finally:
        plan.proxy = None
