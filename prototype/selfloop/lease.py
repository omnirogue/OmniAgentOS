"""Per-instance mutual exclusion: at most ONE worker per loop instance at a time.

Without a lease, two ticks that overlap — a slow tick still running when the
scheduler fires the next one, or an operator running a loop by hand while cron
runs it too — both read the same checkpoint snapshot and race. The damage is not
theoretical; it was observed in the system this package was extracted from:

* both processes see "no pending interrupt" and both start a tick;
* both reach the same effect, so the receipt's claim becomes a race — one wins
  and executes, the loser fails closed with ``EffectStateUnknown`` and reports a
  spurious abort, which then counts against the acceptance floor;
* both try to mint the same approval row, and the loser's create loses.

Three backends ship, and choosing between them is a real decision rather than a
preference:

* :class:`FlockLease` — POSIX ``fcntl.flock`` on a stable per-instance file. The
  default on macOS and Linux. Single host only.
* :class:`SqliteLease` — a ``BEGIN IMMEDIATE`` transaction held for the whole
  tick. Portable, including to Windows, and the answer when ``fcntl`` does not
  exist.
* :class:`InProcessLease` — a ``threading.Lock``. Gives **zero** protection
  between OS processes and therefore refuses to be constructed by accident.

Why ``flock`` and not a lockfile
--------------------------------

The predecessor used an ``O_EXCL`` lockfile and reclaimed a lease it judged
abandoned by ``unlink`` + re-``create``. That has a real mutual-exclusion race:
two workers both read the same stale holder, then interleave as

    A unlink -> A create -> B unlink (deletes A's fresh file) -> B create

and BOTH return holding the lease. It was reproduced deterministically before
the rewrite. ``flock`` has no such window: the kernel arbitrates, exactly one
``LOCK_EX | LOCK_NB`` succeeds, and the loser gets ``EWOULDBLOCK``.

The decision a future maintainer will try to undo
-------------------------------------------------

**There is no pid-liveness probe and no age-based reclaim anywhere in this
file, and adding one would be a regression, not a fix.**

It looks like an omission. It is the single most load-bearing choice here. An
age threshold that can *take* a lease is the unlink/recreate race in another
costume: two processes both observe the lease as older than the threshold, both
decide it is abandoned, both take it, and both run — which is precisely the
double execution the lease exists to prevent. A pid probe is the same defect
with a smaller window, because a pid can be reused and because the answer is
stale the instant it is read.

``age_s`` and ``stale_looking`` exist and are computed. They are **diagnostics
for a human**, reported on :class:`~selfloop.contracts.LeaseHeld` so an operator
can tell "a peer is working" from "something has been holding this for nine
hours, go look". Nothing in any acquisition path reads them.

Crash safety comes free instead of from a reclaim path: the kernel drops every
``flock`` when the holding process exits, however it exits, so ``kill -9``
releases the lease and there is nothing to reclaim. SQLite's write lock behaves
the same way — the OS drops the byte-range locks when the process dies.
"""

from __future__ import annotations

import errno
import json
import os
import re
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from selfloop.contracts import LeaseHeld, LoopError

#: A lease held longer than this is *reported* as suspicious in the holder
#: diagnostics. It is NOT an acquisition input — see the module docstring. The
#: default is deliberately far longer than any sane tick, so a legitimately slow
#: tick is never even flagged, let alone interfered with.
DEFAULT_MAX_AGE_S = 3600.0

#: The diagnostics record is written space-padded to this fixed size and the file
#: is never truncated, so a peer that reads while the winner is stamping sees
#: either the previous record or the new one — never a half-written file that
#: parses as "no holder". The LOCK does not depend on this; an operator's ability
#: to answer "who has it?" does.
_RECORD_BYTES = 512

#: Lease names become filenames, so they are validated rather than sanitised. A
#: sanitiser maps two different instance ids onto one lease file, which silently
#: makes two unrelated loops exclude each other; a validator makes the caller fix
#: the name. Must start alphanumeric, so ``".."`` and hidden files are refused.
_SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")


def require_safe_lease_name(name: str) -> str:
    """Return *name* if it is safe to use as a lease filename, else raise.

    Refused rather than escaped. Escaping is what lets ``a/b`` and ``a_b`` land
    on the same file, and a lease file shared by two instances is worse than no
    lease at all: it serialises two loops that have nothing to do with each
    other while giving each of them a false sense of exclusion.
    """
    if not isinstance(name, str) or not _SAFE_NAME.match(name):
        raise ValueError(
            f"unsafe lease name {name!r}: a lease name becomes a filename, so it must "
            "start with a letter or digit and contain only letters, digits, '.', '_' "
            "and '-' (max 100 characters). Names are refused rather than sanitised, "
            "because sanitising can map two instances onto one lease file."
        )
    return name


def flock_available() -> bool:
    """True when this interpreter has POSIX ``fcntl.flock``.

    Exposed so the CLI can refuse multi-process operation on a host where the
    only available lease is :class:`InProcessLease`, instead of degrading to a
    lock that protects nothing and saying nothing about it.
    """
    try:
        import fcntl
    except ImportError:
        return False
    return hasattr(fcntl, "flock")


def _load_fcntl() -> Any:
    """Import ``fcntl`` on demand.

    Deliberately lazy: ``fcntl`` is POSIX-only, and a module-level import would
    make ``import selfloop`` fail outright on Windows — for a package whose whole
    claim is that importing it does nothing. A Windows user gets a legible error
    here, at the point they ask for a POSIX lease, and a working alternative in
    :class:`SqliteLease`.
    """
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - exercised only off POSIX
        raise LoopError(
            "fcntl is not available on this platform, so FlockLease cannot be used. "
            "Use SqliteLease (portable, multi-process) instead; InProcessLease "
            "protects nothing between processes and must be chosen explicitly."
        ) from exc
    return fcntl


def _hostname() -> str:
    """Best-effort node name for the diagnostics record; ``""`` if unavailable."""
    try:
        return os.uname().nodename
    except AttributeError:  # pragma: no cover - POSIX-only path
        return ""


def _read_record(path: Path) -> dict[str, Any]:
    """Parse the diagnostics record, or ``{}`` for anything unreadable.

    Every failure mode collapses to "we do not know who holds it", which is the
    honest answer and is never used to decide anything. A malformed record must
    not be able to crash the loser's error path — the loser is already failing.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _holder_diagnostics(path: Path, *, max_age_s: float) -> dict[str, Any]:
    """What the LOSER reports on :class:`LeaseHeld`. Never consulted to decide.

    ``age_s`` is computed against the wall clock and not against
    :meth:`~selfloop.ports.Clock.elapsed`, which everywhere else in this package
    is the only legal source for a freshness check. The exception is deliberate
    and is safe for exactly one reason: this number is not an input to anything.
    A monotonic reading taken in another process has no shared origin with this
    one, so it cannot be subtracted; a wall-clock delta at least means something
    to the human reading it.
    """
    holder = _read_record(path)
    raw_started = holder.get("acquired_at")
    started = float(raw_started) if isinstance(raw_started, int | float) else 0.0
    if started:
        age = max(0.0, time.time() - started)
        holder["age_s"] = round(age, 1)
        # Reported so a human can go and look. NOT a permission to take it.
        holder["stale_looking"] = age > max_age_s
    recorded_host = holder.get("host")
    if isinstance(recorded_host, str) and recorded_host:
        # flock does not arbitrate across machines and is unreliable on network
        # filesystems. A holder on a different node means this lease was never
        # protecting anything, and that is worth saying out loud.
        holder["same_host"] = recorded_host == _hostname()
    return holder


class _ReleaseOnExit(AbstractContextManager[None]):
    """Context manager over a resource that was ALREADY acquired.

    Every lease here acquires eagerly, inside ``hold()``, and returns one of
    these. That is what makes ``hold()`` itself raise :class:`LeaseHeld` — the
    port's contract is "acquire or raise *now*", and a ``@contextmanager``
    generator would defer both to ``__enter__``, so ``lease.hold(name)`` on a
    contended instance would return a perfectly innocent-looking object.

    The cost of eager acquisition: a caller who calls ``hold()`` and never enters
    the ``with`` block holds the lease until the process exits. Use it in a
    ``with`` statement, which is the only shape any caller in this package uses.
    """

    __slots__ = ("_release", "_released")

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._released = False

    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Returns None, so an exception raised inside the block propagates. A
        # lease that swallowed the tick's error would report a clean run over a
        # failure, which is the one thing this package refuses to do anywhere.
        self.release()

    def release(self) -> None:
        """Release once. Idempotent, because a double release of a reused fd is
        a lock somebody else is holding."""
        if self._released:
            return
        self._released = True
        self._release()


@dataclass(frozen=True)
class FlockLease:
    """POSIX ``flock`` on a stable per-instance file, held for the whole tick.

    The lock IS the lease. The file's contents are a note for an operator, and
    the file itself is never unlinked — see :meth:`_release` for why that
    matters, because it is the second-most counter-intuitive line in this file.

    Consequences of choosing ``flock``, all deliberate:

    * **Single host.** ``flock`` does not arbitrate across machines and is
      unreliable over NFS/SMB. For one machine running a scheduler this is the
      right trade; a multi-host fleet wants :class:`SqliteLease` on shared
      storage that actually locks, or a real distributed lock.
    * **Crash safety is free.** The kernel drops the lock when the process exits,
      by any means. ``kill -9`` releases the lease, which is exactly what removes
      the need for a reclaim path — and removing the reclaim path is what removes
      the double-acquire race.
    * **Nothing is reclaimed, ever.** See the module docstring.
    """

    directory: Path
    max_age_s: float = DEFAULT_MAX_AGE_S

    def __post_init__(self) -> None:
        object.__setattr__(self, "directory", Path(self.directory))
        if self.max_age_s <= 0:
            raise ValueError(
                f"max_age_s must be positive (got {self.max_age_s!r}); it annotates the "
                "holder diagnostics and cannot cause a lease to be taken"
            )
        # Fail here rather than at the first tick. A FlockLease on a host without
        # fcntl can never work, and finding that out at 03:00 from a scheduler
        # log is strictly worse than finding it out when the context is built.
        _load_fcntl()

    def path_for(self, name: str) -> Path:
        """The stable lease file for *name*. Same name, same inode, forever."""
        return self.directory / f"{require_safe_lease_name(name)}.lease"

    def hold(self, name: str) -> AbstractContextManager[None]:
        """Acquire now, or raise :class:`LeaseHeld` now. Never blocks.

        Blocking would build a queue of processes all intending to run the same
        tick, which is the double execution this exists to prevent with a delay
        in front of it.
        """
        fcntl = _load_fcntl()
        path = self.path_for(name)
        os.makedirs(self.directory, exist_ok=True)
        fd = os.open(os.fspath(path), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            # The whole lease, in one uninterruptible kernel decision.
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES, errno.EDEADLK):
                holder = _holder_diagnostics(path, max_age_s=self.max_age_s)
                raise LeaseHeld(
                    f"lease {name!r} is held by another process: {holder or 'holder unknown'}",
                    holder=holder,
                ) from None
            raise
        self._stamp(fd, name)
        return _ReleaseOnExit(lambda: self._release(fd))

    def _stamp(self, fd: int, name: str) -> None:
        """Write the fixed-size diagnostics record onto the locked descriptor.

        Written with ``os.write`` + ``os.fsync`` on the RAW fd rather than
        through a buffered file object, and this is not fussiness. A buffered
        write can be split by the runtime into more than one syscall, so a
        concurrent reader can observe a partial record; if that partial happens
        to be empty or unparseable, the reader concludes "no holder" about a
        lease that is very much held. One ``write`` of a fixed 512 bytes, with no
        truncation, means a reader sees either the old record or the new one.

        A failure here is swallowed on purpose: the LOCK is the lease and it is
        already held. Losing the operator's note is a cosmetic loss; refusing the
        tick over it would turn a cosmetic loss into a missed run.
        """
        record = json.dumps(
            {
                "backend": "flock",
                "pid": os.getpid(),
                "acquired_at": time.time(),
                "name": name,
                "host": _hostname(),
            },
            sort_keys=True,
        ).encode("utf-8")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if len(record) <= _RECORD_BYTES:
                os.write(fd, record.ljust(_RECORD_BYTES))
            else:  # pragma: no cover - names are length-capped by require_safe_lease_name
                os.write(fd, record)
                os.ftruncate(fd, len(record))
            os.fsync(fd)
        except OSError:  # pragma: no cover - the lock is the lease; the note is a note
            pass

    def _release(self, fd: int) -> None:
        """Unlock and close. **The file itself stays.**

        Unlinking would reintroduce the race the whole design removed: a peer
        blocked on this lock holds a descriptor onto *this inode*, so a later
        ``open`` after an unlink creates a DIFFERENT inode that nobody's lock
        covers, and two processes end up holding two "leases" on the same
        instance. An empty lease file costs one inode; the alternative costs
        correctness.
        """
        try:
            fcntl = _load_fcntl()
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:  # pragma: no cover - closing releases it regardless
            pass
        finally:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover
                pass


@dataclass(frozen=True)
class SqliteLease:
    """The portable multi-process lease: ``BEGIN IMMEDIATE`` held for the tick.

    SQLite takes a real write lock on the database for the life of an immediate
    transaction, arbitrated by the OS, on every platform SQLite runs on. Holding
    that transaction open for the whole tick makes it a lease with the same two
    properties ``flock`` has: exactly one winner, and automatic release when the
    holder dies, because the OS drops the file locks of a dead process.

    One database file per lease name, which is why this class takes a directory
    rather than a database path. A shared database would serialise every
    instance against every other instance, since ``BEGIN IMMEDIATE`` locks the
    database and not a row.

    **This lease publishes no holder identity, and that is a refusal rather than
    an omission.** The transaction IS the lease, so anything written inside it is
    invisible to other connections until it commits — and committing would
    release the lease. Writing the holder record *before* the transaction, or
    committing it and re-acquiring, both create a window in which the published
    holder is not the actual holder. A misleading diagnostic is worse than an
    absent one, especially for a field an operator might be tempted to act on. If
    you need to know which pid holds a lease, use :class:`FlockLease`.

    The database file is deliberately schemaless and stays empty: there is
    nothing to store, because the lock is the entire content.
    """

    directory: Path
    #: Kept for interface symmetry with :class:`FlockLease` and reported on
    #: :class:`LeaseHeld`. Like every other age field in this module it is never
    #: an acquisition input.
    max_age_s: float = DEFAULT_MAX_AGE_S

    def __post_init__(self) -> None:
        object.__setattr__(self, "directory", Path(self.directory))
        if self.max_age_s <= 0:
            raise ValueError(f"max_age_s must be positive (got {self.max_age_s!r})")

    def path_for(self, name: str) -> Path:
        """The stable lease database for *name*."""
        return self.directory / f"{require_safe_lease_name(name)}.lease.db"

    def hold(self, name: str) -> AbstractContextManager[None]:
        """Acquire now, or raise :class:`LeaseHeld` now. Never blocks.

        ``busy_timeout`` is pinned to zero and the connect timeout to zero, so a
        contended database returns ``SQLITE_BUSY`` immediately instead of
        retrying for five seconds. Waiting here is the queue-of-duplicate-ticks
        failure, and a lease that waits is a lease that eventually lets two
        processes run the same tick back to back.
        """
        import sqlite3

        path = self.path_for(name)
        os.makedirs(self.directory, exist_ok=True)
        conn = sqlite3.connect(os.fspath(path), timeout=0.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 0")
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            conn.close()
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                holder = {"backend": "sqlite", "database": os.fspath(path), "name": name}
                raise LeaseHeld(
                    f"lease {name!r} is held by another process holding the write "
                    f"transaction on {path}",
                    holder=holder,
                ) from None
            raise
        except BaseException:
            conn.close()
            raise
        return _ReleaseOnExit(lambda: self._release(conn))

    @staticmethod
    def _release(conn: Any) -> None:
        """Roll back — releasing the write lock — and close.

        ``ROLLBACK`` rather than ``COMMIT`` because the transaction was never
        used to write anything: its entire purpose was to hold the lock. A commit
        would work identically and would invite a future reader to think there
        was state worth committing.
        """
        import sqlite3

        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:  # pragma: no cover - closing releases it regardless
            pass
        finally:
            conn.close()


@dataclass
class InProcessLease:
    """A ``threading.Lock`` per name. **Zero protection between OS processes.**

    Say that again, because the name does not: this lease excludes two threads
    inside ONE interpreter and nothing else. Two ``python -m selfloop`` processes
    started a second apart will both acquire it, both read the same checkpoint,
    and both run the tick. Every failure the module docstring describes is fully
    available with this lease installed.

    It therefore refuses to be constructed by accident. You must pass
    ``accept_single_process_only=True``, which exists so that the choice appears
    in the caller's source where a reviewer can see it, rather than being
    inherited from a default nobody typed. A silent degradation to this lease on
    a host without ``fcntl`` is exactly the failure mode this argument closes:
    the loop keeps running, reports nothing unusual, and has quietly stopped
    being safe.

    Legitimate uses: in-process tests, the quickstart, and a single-process
    embedding where you can prove no second process exists.
    """

    accept_single_process_only: bool = False
    _locks: dict[str, threading.Lock] = field(default_factory=dict, repr=False)
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        if not self.accept_single_process_only:
            raise ValueError(
                "InProcessLease protects nothing between OS processes: two "
                "interpreters both acquire it and both run the same tick. Construct "
                "it as InProcessLease(accept_single_process_only=True) to state that "
                "you have established there is only one process, or use FlockLease "
                "(POSIX) or SqliteLease (portable) instead."
            )

    def hold(self, name: str) -> AbstractContextManager[None]:
        """Acquire now, or raise :class:`LeaseHeld` now. Never blocks."""
        require_safe_lease_name(name)
        with self._guard:
            lock = self._locks.setdefault(name, threading.Lock())
        if not lock.acquire(blocking=False):
            holder = {
                "backend": "in_process",
                "pid": os.getpid(),
                "name": name,
                "thread": threading.current_thread().name,
                "note": "in-process only; another PROCESS holding this lease is invisible here",
            }
            raise LeaseHeld(
                f"lease {name!r} is held by another thread in this process: {holder}",
                holder=holder,
            )
        return _ReleaseOnExit(lock.release)


__all__ = [
    "DEFAULT_MAX_AGE_S",
    "FlockLease",
    "InProcessLease",
    "SqliteLease",
    "flock_available",
    "require_safe_lease_name",
]
