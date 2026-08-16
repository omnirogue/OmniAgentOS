"""Enforcement of resource limits (rlimits) for leased execution environments.

This module provides utility functions to apply resource limits defined by
a lease's LeaseCeilings onto a child process tree at the execution boundary.
It defines standard POSIX-compliant mappings from lease ceilings to system-level
rlimits, preparing the configuration and compiling safe pre-exec closures for
spawning processes under subprocess.Popen.

POSIX Thread Safety and Fork-Without-Exec Hazards:
Using a preexec_fn in subprocess.Popen is documented as unsafe in the presence
of multi-threaded parent processes. In a multi-threaded Python program, if a
thread forks, only the executing thread is cloned into the child process. Any
other threads vanish, but their lock structures and heap objects remain in
undefined states. This introduces a risk of deadlocks if the child process
attempts to allocate memory, acquire locks, or import modules in the window
between fork and exec. To mitigate this hazard, the pre-execution closure
returned by make_preexec is strictly limited to low-level, non-allocating system
calls (resource.getrlimit and resource.setrlimit) and simple iteration. No
modules are imported, no heap allocations or string formatting are performed, and
no logging or diagnostics are written within the child context. Additionally,
this mechanism is only activated when the lease system is configured in
enforcement mode ("enforce"), which is disabled by default ("off") in production
and shadow environments.

Darwin (macOS) RLIMIT_RSS vs RLIMIT_AS Aliasing:
Under Darwin (macOS), the RLIMIT_RSS resource constant is defined as an alias for
RLIMIT_AS (address space size). Capping the address space (RLIMIT_AS) of modern,
JIT-compiled, or Node-based executables causes instant, untraceable process
termination, because these runtimes routinely reserve or map tens of gigabytes of
virtual address space upon startup without actually utilizing corresponding physical
resident set size. Consequently, ceilings.rss_mb is never applied or enforced
via setrlimit on macOS/Darwin. It remains declared within the lease payload for
auditing, telemetry, or system-level cgroup enforcement where available, but is
ignored during standard POSIX rlimit application to prevent mystery crashes.
"""

from __future__ import annotations

from collections.abc import Callable

try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]

from omniagentos.lease.models import LeaseCeilings


def rlimit_plan(ceilings: LeaseCeilings) -> list[tuple[int, str, int, int]]:
    """Determine the list of POSIX resource limits to apply to a child process.

    Analyzes the provided LeaseCeilings and constructs a plan consisting of
    resource constants, descriptive human names, soft limit targets, and hard
    limit targets.

    This plan respects the following principles:
    1. RLIMIT_CPU is mapped from ceilings.cpu_s when positive. A 5-second grace
       is added to the hard limit (soft = cpu_s, hard = cpu_s + 5) so that a
       well-behaved child process receives the SIGXCPU signal at the soft limit
       and is given an opportunity to write diagnostic dumps or exit cleanly before
       being forcibly terminated by SIGKILL at the hard limit.
    2. RLIMIT_NPROC is mapped from ceilings.max_procs when positive. Both soft and
       hard limits are configured to max_procs.
    3. RLIMIT_RSS and RLIMIT_AS are NEVER emitted. This is critical for macOS,
       where RLIMIT_RSS is a #define alias for RLIMIT_AS. Imposing virtual memory
       address space ceilings causes immediate, untraceable crashes for JIT or
       runtime interpreters. rss_mb is treated strictly as an advisory/declared
       and unenforced limit under this POSIX implementation.
    4. Unsupported resource constants on the current host platform are safely
       skipped.

    Args:
        ceilings: The lease resource limits to plan.

    Returns:
        A list of tuples of the form (resource_constant, human_name, soft, hard) to enforce.
    """
    plan: list[tuple[int, str, int, int]] = []
    if resource is None:
        return plan

    # 1. RLIMIT_CPU setup
    if ceilings.cpu_s > 0:
        cpu_const = getattr(resource, "RLIMIT_CPU", None)
        if cpu_const is not None:
            soft_cpu = int(ceilings.cpu_s)
            # The 5-second grace is deliberate: the kernel sends SIGXCPU at the
            # SOFT limit and SIGKILL only at the HARD limit. This gap lets a
            # well-behaved child observe the signal and exit cleanly.
            hard_cpu = soft_cpu + 5
            plan.append((cpu_const, "RLIMIT_CPU", soft_cpu, hard_cpu))

    # 2. RLIMIT_NPROC setup
    if ceilings.max_procs > 0:
        nproc_const = getattr(resource, "RLIMIT_NPROC", None)
        if nproc_const is not None:
            limit_val = int(ceilings.max_procs)
            plan.append((nproc_const, "RLIMIT_NPROC", limit_val, limit_val))

    # RLIMIT_RSS / RLIMIT_AS are intentionally never planned or emitted here
    # due to the alias hazard described in the module-level docstring.

    return plan


def limit_report(ceilings: LeaseCeilings) -> dict[str, str]:
    """Generate an honest descriptive dictionary of resource enforcement status.

    This report is logged into the ledger record to document exactly what limits
    were planned or skipped, including explanations of platform architectural
    limitations (such as RLIMIT_RSS aliasing RLIMIT_AS).

    READ THE WORDING LITERALLY: every applied entry says "requested", not
    "enforced". The preexec closure cannot raise (an exception between fork and
    exec kills the child with an opaque error), so a ``setrlimit`` that fails is
    swallowed and the child runs without that ceiling. Writing "enforced" into an
    audit trail that only knows what was ASKED FOR would be the ledger telling a
    story the kernel never confirmed. Turning an unapplied ceiling into a refusal
    requires a trusted exec helper that can fail BEFORE exec, which v1 does not
    ship (security review finding #3).

    This function never raises.

    Args:
        ceilings: The resource ceilings configuration.

    Returns:
        A dictionary mapping ceiling metric names (cpu_s, rss_mb, max_procs, wall_s)
        to their corresponding string descriptions of enforcement.
    """
    report: dict[str, str] = {}

    # CPU_S reporting
    if ceilings.cpu_s > 0:
        if resource is not None and getattr(resource, "RLIMIT_CPU", None) is not None:
            soft = int(ceilings.cpu_s)
            hard = soft + 5
            report["cpu_s"] = f"RLIMIT_CPU requested soft={soft} hard={hard}"
        else:
            report["cpu_s"] = (
                f"declared {ceilings.cpu_s}, NOT enforced (RLIMIT_CPU unsupported on this platform)"
            )
    else:
        report["cpu_s"] = "unset"

    # RSS_MB reporting (Never enforced via rlimit due to Darwin address space aliasing)
    if ceilings.rss_mb > 0:
        report["rss_mb"] = (
            f"declared {int(ceilings.rss_mb)}, NOT enforced (macOS RLIMIT_RSS aliases RLIMIT_AS)"
        )
    else:
        report["rss_mb"] = "unset"

    # MAX_PROCS reporting
    if ceilings.max_procs > 0:
        if resource is not None and getattr(resource, "RLIMIT_NPROC", None) is not None:
            val = int(ceilings.max_procs)
            report["max_procs"] = f"RLIMIT_NPROC requested soft={val} hard={val}"
        else:
            report["max_procs"] = (
                f"declared {ceilings.max_procs}, NOT enforced (RLIMIT_NPROC unsupported on this platform)"
            )
    else:
        report["max_procs"] = "unset"

    # WALL_S reporting (Enforced via timer/timeout paths in the calling runner, not rlimits)
    if ceilings.wall_s > 0:
        report["wall_s"] = "enforced by the caller's timeout path"
    else:
        report["wall_s"] = "unset"

    return report


def make_preexec(ceilings: LeaseCeilings) -> Callable[[], None] | None:
    """Compile a safe pre-exec closure to apply resource ceilings to a child process.

    Constructs a lightweight function suitable for the preexec_fn argument of
    subprocess.Popen. If no resource ceilings are applicable or the platform lacks
    rlimit support, this returns None to ensure the standard subprocess spawn path
    is completely untouched and byte-identical to default execution.

    The returned closure is highly optimized for fork-safety:
    - It pre-computes and closes over the limit plan list to avoid allocating data
      structures, lists, or tuples in the fork child.
    - It contains no import statements, no string formatting operations, and no lock
      acquisitions.
    - It is wrapped globally to prevent raising exceptions, ensuring any system-level
      or value mismatch degrades gracefully as telemetry rather than breaking execution.
    - It clamps limits against the existing environment limits to prevent EPERM
      failures when reducing limits for non-root processes.

    Args:
        ceilings: The resource ceilings configuration.

    Returns:
        A callable function to be executed in the child process context, or None.
    """
    plan = rlimit_plan(ceilings)
    if not plan or resource is None:
        return None

    # Get resource limits constants in scope so the closure does not do any lookups
    inf_val = getattr(resource, "RLIM_INFINITY", -1)

    def preexec() -> None:
        # Warning: Runs in the child process between fork and exec!
        # MUST NOT import, log, allocate heap variables unnecessarily, or lock.
        for const, _, soft, hard in plan:
            try:
                cur_soft, cur_hard = resource.getrlimit(const)
                # We can never raise a hard limit (would trigger EPERM for non-root).
                # Clamp our proposed hard limit to the existing system hard limit.
                if cur_hard != inf_val:
                    effective_hard = min(hard, cur_hard)
                else:
                    effective_hard = hard

                # MONOTONICITY (security review BLOCKER #2). A ceiling may only ever
                # TIGHTEN. Clamping against the hard limit alone is not enough: with
                # an inherited (soft=100, hard=1000) and a lease asking for 1800,
                # min(1800, 1000) = 1000 would RAISE the soft limit from 100 -- so
                # enforce mode would end up LESS confined than off mode, which
                # inherits 100 untouched. Clamp against the inherited SOFT limit too,
                # treating infinity as "no inherited bound".
                if cur_soft != inf_val:
                    effective_soft = min(soft, cur_soft, effective_hard)
                else:
                    effective_soft = min(soft, effective_hard)

                resource.setrlimit(const, (effective_soft, effective_hard))
            except Exception:  # noqa: BLE001 - see below; must never raise post-fork
                # A ceiling that will not apply is degraded TELEMETRY, not a launch
                # failure: raising here would kill the child with an opaque error,
                # and the OS sandbox -- not the rlimit -- is the actual confinement.
                # limit_report() therefore describes ceilings as REQUESTED rather
                # than as guaranteed; making an unapplied ceiling refuse the launch
                # needs a trusted exec helper that can fail before exec (v2).
                continue

    return preexec


__all__ = [
    "limit_report",
    "make_preexec",
    "rlimit_plan",
]
