"""Session supervisor lifecycle boundary (H-39 / OA-005).

This module is the contract L17 (Toolplane / observation) and other attachers
must use when they bind to the real session path.

Lifecycle interface
-------------------
1. Construct / obtain a long-lived :class:`SessionSupervisor` instance.
2. Attach Toolplane, observation adapters, and any other session-path
   instrumentation **before** the first ``spawn`` / ``serve_forever``.
3. All session work (spawn, poll, reader, finalizer, manifest write) is valid
   only while the supervisor is open (``is_closed`` is false and ``_stop`` is
   clear).
4. Teardown MUST go through :meth:`SessionSupervisor.close` (or
   :meth:`SessionSupervisor.shutdown` followed by ``close`` when DAL ownership
   is external). Ordered steps inside ``close``:

   a. stop accepting work (set stop; refuse new spawns/launches);
   b. drain in-flight launches so a child created before registration cannot
      escape the close (see "Launch coordination" below);
   c. signal producers / children (optional terminate);
   d. join every registered reader and finalizer thread with a **bounded**
      timeout;
   e. flush terminal manifests / final events, or explicitly fail sessions
      whose finalization could not complete;
   f. close the DAL only after joins succeed -- or **defer** the DAL close to
      the last background thread when ``close`` was invoked from inside a
      reader/finalizer (see "Deferred DAL close" below).

5. After ``close`` returns successfully, no further DAL or session I/O may
   occur on that instance. Repeated and concurrent ``close`` calls are safe.
6. Do **not** call :meth:`SessionsDal.close` while a supervisor may still have
   live readers or finalizers -- that is the race H-39 forbids. Always close
   through the supervisor lifecycle first.

Launch coordination
-------------------
A launch is "in flight" from just before the child process is created until the
reader thread is registered *and started*. ``close`` waits (bounded) for the
in-flight set to drain before snapshotting threads, and terminates any child
recorded by an in-flight launch that could not be drained. A launch that loses
the race refuses registration and terminates its own child. Registered-but-never-
started threads are reported as timed out rather than silently skipped --
``Thread.is_alive()`` is ``False`` before ``start()``, which would otherwise make
close believe there was nothing to join.

Deferred DAL close
------------------
When ``close`` is called from a reader/finalizer thread, that thread cannot join
itself and is still going to touch the DAL after ``close`` returns. Closing the
DAL there is exactly the OA-005 use-after-close. Instead the close is *deferred*:
``dal_close_pending`` becomes true and the last live background thread performs
the real DAL close on its way out. No reader or finalizer may start or resume
work once ``dal_closed`` is set.

Manifest obligations survive the DAL
------------------------------------
The close-time flush cannot be driven off the database alone: by the time
``close`` runs, the DAL may already be closed (by the supervisor itself) or
externally broken, and a supervisor that can no longer *ask* which sessions it
owned would report success over a missing manifest. Every terminal state the
supervisor observes for a session it owns is therefore mirrored into an
in-memory :class:`ManifestObligation` ledger carrying the session's identity.

The ledger is authoritative for *what is owed*; the filesystem is authoritative
for *what has been discharged*. Satisfaction is re-proved by stat-ing
``SessionManifest.path_for(session_id)`` at close time -- never from a "written"
flag -- so a manifest that was written and later removed is still owed.
``SessionSupervisor.manifest_obligations()`` and
``SessionSupervisor.owned_session_ids`` stay readable after the DAL is gone;
that is the recovery evidence a failed close leaves behind.

Default join budget is :data:`DEFAULT_JOIN_TIMEOUT_S`. Bounded failure surfaces
as :class:`SessionSupervisorCloseError` rather than a silent post-success
background exception. A close over an unusable DAL that still owes a manifest
fails; it does not quietly succeed.

Adopted PID identity
--------------------
A PID read back from the durable sessions row after a supervisor restart is
**not** a process handle -- the PID may have been recycled by an unrelated
process. Before signalling such a PID the supervisor must corroborate it against
durable launch provenance (the session's ``session_ref``, which the bridge argv
carries as ``--session-id``/``--resume``) using the *full* command line, not the
executable name: a real session's group leader is ``sandbox-exec``/``node``, so
an executable-name test both misses owned children and matches strangers.

:func:`verify_adopted_pid` returns an explicit :class:`AdoptedPidVerdict`. Only
``OWNED`` authorises a signal. ``UNKNOWN`` -- observation failed, or the
provenance is too weak to corroborate -- **fails closed**: the supervisor logs
recovery evidence and leaves the process alone. Processes the supervisor holds a
live :class:`subprocess.Popen` handle for are never routed through this path;
they are owned by construction and are terminated directly, so a real owned
Node/Claude child can never be leaked by an identity check.

Process-exit integration
------------------------
:func:`drain_open_supervisors` is registered via :mod:`atexit` to ensure all
live supervisors are closed before the interpreter shuts down. This prevents
the DAL from being garbage-collected while reader/finalizer threads are still
running. Drain failures are not swallowed into a log line and forgotten: each
one is recorded as a :class:`DrainFailure` retrievable via :func:`drain_failures`
so the recovery evidence survives the drain, and the failed supervisor stays in
the open registry so a later close can retry it.
"""

from __future__ import annotations

import atexit
import logging
import shlex
import subprocess
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from omniagentos.sessions.manifest import SessionManifest

LOG = logging.getLogger(__name__)

DEFAULT_JOIN_TIMEOUT_S = 5.0

# Stable attribution written onto sessions that cannot finish before close.
SHUTDOWN_KILLED_BY = "supervisor-shutdown"

# Budget for a single `ps` observation. Deliberately small: this runs on the
# close path, where the whole join budget is only a few seconds.
PROCESS_OBSERVATION_TIMEOUT_S = 3.0

# Shortest launch provenance token we will accept as corroborating evidence.
# Session refs are UUID4s (36 chars); anything shorter than this could collide
# with unrelated argv content, so we fail closed instead of guessing.
MIN_PROVENANCE_TOKEN_LEN = 8


class SessionSupervisorCloseError(RuntimeError):
    """Bounded shutdown could not finish all background work cleanly.

    Raised when reader/finalizer threads remain alive after the join budget,
    when a required final flush fails after stop, or when an in-flight launch
    could not be drained. Callers must treat the supervisor as failed-closed: do
    not assume manifests for the listed sessions were written. The supervisor
    stays retryable -- a later ``close`` can finish the joins, re-attempt the
    manifests, and close the DAL.
    """

    def __init__(
        self,
        message: str,
        *,
        timed_out_sessions: list[str] | None = None,
        failed_manifests: list[str] | None = None,
        stranded_launches: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.timed_out_sessions = list(timed_out_sessions or [])
        self.failed_manifests = list(failed_manifests or [])
        self.stranded_launches = list(stranded_launches or [])


# ---------------------------------------------------------------------------
# Manifest obligations (identity that outlives the DAL)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ManifestObligation:
    """One terminal session this supervisor owes a durable manifest for.

    Recorded the moment the supervisor observes an owned session in a terminal
    state, and never dropped for the lifetime of the supervisor. The identity
    fields are copied out of the session row on the way past **because the DAL
    may be unusable by the time ``close`` has to report the obligation** -- that
    is the entire point of this record. A close that can no longer query the
    database must still be able to name every manifest it cannot flush; without
    it, a previously owned session with a missing manifest is invisible and the
    close reports false success.

    Satisfaction is deliberately **not** memoised on this record. It is re-proved
    against the filesystem at close time, so a manifest that was written and
    later removed is still reported as owed.
    """

    session_id: str
    session_ref: str = ""
    project_dir: str = ""
    final_state: str = ""
    killed_by: str | None = None
    recorded_at: str = ""

    def manifest_path(self, manifest: SessionManifest) -> Path:
        """Where this obligation's record must live. Needs no DAL, by design."""
        return manifest.path_for(self.session_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "session_ref": self.session_ref,
            "project_dir": self.project_dir,
            "final_state": self.final_state,
            "killed_by": self.killed_by,
            "recorded_at": self.recorded_at,
        }


# ---------------------------------------------------------------------------
# atexit draining with durable recovery evidence
# ---------------------------------------------------------------------------

# Module-level registry for atexit draining. Imported and used by supervisor.py.
_OPEN_SUPERVISORS: set[Any] = set()
_OPEN_SUPERVISORS_LOCK = threading.Lock()
_ATEXIT_REGISTERED = False


@dataclass(frozen=True)
class DrainFailure:
    """Evidence that one supervisor could not be drained at process exit.

    ``atexit`` runs after the last test/operator statement, so an exception
    raised there is invisible. Recording the failure keeps the recovery
    evidence -- which sessions may have lost a manifest, which launches were
    stranded -- readable after the drain returns.
    """

    supervisor: str
    error: str
    timed_out_sessions: tuple[str, ...] = ()
    failed_manifests: tuple[str, ...] = ()
    stranded_launches: tuple[str, ...] = ()
    dal_close_pending: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "supervisor": self.supervisor,
            "error": self.error,
            "timed_out_sessions": list(self.timed_out_sessions),
            "failed_manifests": list(self.failed_manifests),
            "stranded_launches": list(self.stranded_launches),
            "dal_close_pending": self.dal_close_pending,
        }


_DRAIN_FAILURES: list[DrainFailure] = []
_DRAIN_FAILURES_LOCK = threading.Lock()


def drain_failures() -> list[DrainFailure]:
    """Return recovery evidence recorded by :func:`drain_open_supervisors`."""
    with _DRAIN_FAILURES_LOCK:
        return list(_DRAIN_FAILURES)


def reset_drain_failures() -> None:
    """Clear recorded drain evidence (test isolation)."""
    with _DRAIN_FAILURES_LOCK:
        _DRAIN_FAILURES.clear()


def _record_drain_failure(failure: DrainFailure) -> None:
    with _DRAIN_FAILURES_LOCK:
        _DRAIN_FAILURES.append(failure)


def drain_open_supervisors(timeout: float = 2.0) -> list[DrainFailure]:
    """Close all live supervisors at process exit, preserving failure evidence.

    Called by atexit to ensure readers/finalizers join before the interpreter
    tears down and DAL connections become unusable.

    A failure does not stop the drain (one stuck supervisor must not block the
    others) and is not discarded either: it is logged **and** recorded as a
    :class:`DrainFailure`. The failing supervisor stays registered, so it is
    retryable by a later explicit ``close``.

    Returns the failures recorded by *this* call.
    """
    with _OPEN_SUPERVISORS_LOCK:
        supervisors = list(_OPEN_SUPERVISORS)
    failures: list[DrainFailure] = []
    for supervisor in supervisors:
        try:
            supervisor.close(join_timeout=timeout, terminate_children=True)
        except SessionSupervisorCloseError as exc:
            failure = DrainFailure(
                supervisor=repr(supervisor),
                error=str(exc),
                timed_out_sessions=tuple(exc.timed_out_sessions),
                failed_manifests=tuple(exc.failed_manifests),
                stranded_launches=tuple(exc.stranded_launches),
                dal_close_pending=bool(getattr(supervisor, "dal_close_pending", False)),
            )
            failures.append(failure)
            _record_drain_failure(failure)
            LOG.warning(
                "atexit drain: supervisor close did not finish cleanly: %s",
                failure.as_dict(),
                exc_info=True,
            )
        except Exception as exc:  # noqa: BLE001 - one bad supervisor must not block the rest
            failure = DrainFailure(
                supervisor=repr(supervisor),
                error=f"{type(exc).__name__}: {exc}",
                dal_close_pending=bool(getattr(supervisor, "dal_close_pending", False)),
            )
            failures.append(failure)
            _record_drain_failure(failure)
            LOG.warning(
                "atexit drain: supervisor close failed: %s",
                failure.as_dict(),
                exc_info=True,
            )
    return failures


def _ensure_atexit_registered() -> None:
    """Register the atexit handler once per process."""
    global _ATEXIT_REGISTERED  # noqa: PLW0603
    if _ATEXIT_REGISTERED:
        return
    with _OPEN_SUPERVISORS_LOCK:
        if _ATEXIT_REGISTERED:
            return
        atexit.register(drain_open_supervisors)
        _ATEXIT_REGISTERED = True


# ---------------------------------------------------------------------------
# Adopted-PID identity (H-39 command-identity protection)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessObservation:
    """One point-in-time reading of a PID's full command line.

    ``observed`` distinguishes "we looked and the process is gone" (observed,
    not alive) from "we could not look" (not observed) -- the latter must fail
    closed rather than being treated as either alive or dead.
    """

    pid: int
    observed: bool
    alive: bool
    command: str = ""
    error: str = ""

    @property
    def tokens(self) -> list[str]:
        if not self.command:
            return []
        try:
            return shlex.split(self.command)
        except ValueError:
            return self.command.split()


def observe_process(
    pid: int, *, timeout: float = PROCESS_OBSERVATION_TIMEOUT_S
) -> ProcessObservation:
    """Read a PID's **full** command line via ``ps -ww -o command=``.

    Mirrors the house identity pattern already used by provider-exec orphan
    reconciliation. ``-o comm=`` is deliberately *not* used: it yields only the
    executable name, which for a bridge session is ``sandbox-exec``/``node`` and
    therefore carries no session provenance at all.

    Any failure to run or parse ``ps`` produces ``observed=False`` so callers
    can fail closed.
    """
    if pid <= 0:
        return ProcessObservation(pid=pid, observed=True, alive=False, error="nonpositive-pid")
    try:
        completed = subprocess.run(  # noqa: S603, S607 - fixed argv, no shell
            # GNU ps uses the output width even when stdout is not a terminal.
            # GitHub-hosted runners therefore truncated long bridge argv at the
            # default width and erased the session marker this check needs.
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProcessObservation(
            pid=pid,
            observed=False,
            alive=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    if completed.returncode != 0:
        # `ps -p` exits non-zero when no such process exists. That is a real
        # observation: the process is gone.
        return ProcessObservation(pid=pid, observed=True, alive=False)
    command = (completed.stdout or "").strip()
    if not command:
        # Exit 0 with no command line means we cannot characterise the process.
        return ProcessObservation(pid=pid, observed=False, alive=True, error="empty-command")
    return ProcessObservation(pid=pid, observed=True, alive=True, command=command)


def command_matches_session(command: str, *, session_ref: str) -> bool:
    """True when ``command`` carries this session's durable launch provenance.

    The bridge argv embeds the session's ``session_ref`` (``--session-id <ref>``
    on a fresh launch, ``--resume <ref>`` on a resume), so the ref is the
    strongest evidence available from outside the process. Matching is
    **token-exact** on the shell-split command line -- a substring test would
    match a stranger whose argv merely happens to contain the text.
    """
    ref = (session_ref or "").strip()
    if len(ref) < MIN_PROVENANCE_TOKEN_LEN:
        return False
    if not command:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for token in tokens:
        if token == ref:
            return True
        # `--session-id=<ref>` / `--resume=<ref>` joined forms.
        _, sep, value = token.partition("=")
        if sep and value == ref:
            return True
    return False


def command_matches_binary(command: str, *, binary: str) -> bool:
    """True when ``command`` invokes ``binary`` (basename, token-exact).

    Secondary corroboration for a session whose provenance token is absent from
    the observed argv. Never sufficient on its own to authorise a signal.
    """
    expected = Path(str(binary or "")).name
    if not expected or not command:
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return any(Path(token).name == expected for token in tokens)


class AdoptedPidVerdict(StrEnum):
    """Outcome of corroborating an adopted PID against launch provenance."""

    #: Full command line carries this session's durable provenance -> may signal.
    OWNED = "owned"
    #: Process exists but is demonstrably not this session -> must NOT signal.
    FOREIGN = "foreign"
    #: Process no longer exists -> nothing to signal.
    GONE = "gone"
    #: Could not observe, or provenance too weak to corroborate -> fail closed.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AdoptedPidDecision:
    """A verdict plus the evidence that produced it (recovery/audit trail)."""

    pid: int
    verdict: AdoptedPidVerdict
    session_id: str = ""
    session_ref: str = ""
    command: str = ""
    reason: str = ""

    @property
    def may_signal(self) -> bool:
        return self.verdict is AdoptedPidVerdict.OWNED

    def as_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "verdict": self.verdict.value,
            "session_id": self.session_id,
            "session_ref": self.session_ref,
            "command": self.command,
            "reason": self.reason,
        }


def verify_adopted_pid(
    pid: int,
    *,
    session_ref: str,
    session_id: str = "",
    claude_binary: str = "",
    observer: Callable[[int], ProcessObservation] = observe_process,
) -> AdoptedPidDecision:
    """Decide whether an adopted PID may be signalled, failing closed on doubt.

    Only :attr:`AdoptedPidVerdict.OWNED` authorises ``killpg``. This function is
    for PIDs read back from the durable sessions row -- PIDs the supervisor does
    **not** hold a :class:`subprocess.Popen` handle for. Managed handles are
    owned by construction and must never be routed here, or a real owned
    Node/Claude child could be leaked when observation is unavailable.
    """
    ref = (session_ref or "").strip()
    if pid <= 0:
        return AdoptedPidDecision(
            pid=pid,
            verdict=AdoptedPidVerdict.GONE,
            session_id=session_id,
            session_ref=ref,
            reason="nonpositive-pid",
        )
    if len(ref) < MIN_PROVENANCE_TOKEN_LEN:
        # No durable provenance to corroborate against: refuse rather than
        # fall back to a name heuristic that would kill a recycled PID.
        return AdoptedPidDecision(
            pid=pid,
            verdict=AdoptedPidVerdict.UNKNOWN,
            session_id=session_id,
            session_ref=ref,
            reason="provenance-too-weak",
        )

    observation = observer(pid)
    if not observation.observed:
        return AdoptedPidDecision(
            pid=pid,
            verdict=AdoptedPidVerdict.UNKNOWN,
            session_id=session_id,
            session_ref=ref,
            command=observation.command,
            reason=observation.error or "observation-failed",
        )
    if not observation.alive:
        return AdoptedPidDecision(
            pid=pid,
            verdict=AdoptedPidVerdict.GONE,
            session_id=session_id,
            session_ref=ref,
            reason="process-gone",
        )
    if command_matches_session(observation.command, session_ref=ref):
        return AdoptedPidDecision(
            pid=pid,
            verdict=AdoptedPidVerdict.OWNED,
            session_id=session_id,
            session_ref=ref,
            command=observation.command,
            reason="session-ref-in-argv",
        )
    if claude_binary and command_matches_binary(observation.command, binary=claude_binary):
        # A Claude process that is NOT carrying our ref belongs to some other
        # session. Demonstrably foreign, not merely unproven.
        return AdoptedPidDecision(
            pid=pid,
            verdict=AdoptedPidVerdict.FOREIGN,
            session_id=session_id,
            session_ref=ref,
            command=observation.command,
            reason="claude-process-without-our-ref",
        )
    return AdoptedPidDecision(
        pid=pid,
        verdict=AdoptedPidVerdict.FOREIGN,
        session_id=session_id,
        session_ref=ref,
        command=observation.command,
        reason="no-session-provenance-in-argv",
    )


@dataclass
class LaunchTicket:
    """One in-flight launch, from before child creation to reader start.

    ``close`` drains these before it snapshots background threads, so a child
    created but not yet registered cannot escape the shutdown. ``child`` is
    recorded the instant the process exists so a stranded launch can still be
    terminated.
    """

    token: int
    session_id: str
    child: Any | None = None
    registered: bool = False


__all__ = [
    "DEFAULT_JOIN_TIMEOUT_S",
    "MIN_PROVENANCE_TOKEN_LEN",
    "PROCESS_OBSERVATION_TIMEOUT_S",
    "SHUTDOWN_KILLED_BY",
    "AdoptedPidDecision",
    "AdoptedPidVerdict",
    "DrainFailure",
    "LaunchTicket",
    "ManifestObligation",
    "ProcessObservation",
    "SessionSupervisorCloseError",
    "_OPEN_SUPERVISORS",
    "_OPEN_SUPERVISORS_LOCK",
    "_ensure_atexit_registered",
    "command_matches_binary",
    "command_matches_session",
    "drain_failures",
    "drain_open_supervisors",
    "observe_process",
    "reset_drain_failures",
    "verify_adopted_pid",
]
