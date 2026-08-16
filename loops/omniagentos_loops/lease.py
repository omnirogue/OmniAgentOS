"""Per-instance advisory lease: at most ONE worker per loop instance at a time.

Without it, two ticks that overlap (a slow tick still running when launchd fires
the next one, an operator running a loop by hand) both read the same checkpoint
snapshot and race. The observable damage is not theoretical:

* both see "no pending interrupt" and both start a tick;
* both reach the same effect, and the receipt's check-then-claim becomes a race
  — one wins and executes, the loser raises ``EffectStateUnknown`` and reports a
  spurious ABORT, which counts against the routine acceptance floor;
* both may write an approvals row for the same effect (the deterministic id
  makes that an INSERT collision rather than a duplicate, but the loser's
  ``create_approval`` raises).

The lease is ``fcntl.flock`` on a STABLE per-instance file, held open for the
whole tick. That choice replaced an O_EXCL-lockfile scheme that reclaimed a
lease it judged abandoned by ``unlink`` + re-``create``, which had a real
mutual-exclusion race: two workers could both read the same stale holder, then
interleave as

    A unlink -> A create -> B unlink (deletes A's fresh file) -> B create

and BOTH return holding the lease. Reproduced deterministically before this
rewrite. ``flock`` has no such window: the kernel arbitrates, exactly one
``LOCK_EX | LOCK_NB`` succeeds, and the loser gets ``EWOULDBLOCK``.

Consequences of the choice, all deliberate:

* **Single host.** ``flock`` does not arbitrate across machines and is unreliable
  on network filesystems. This runtime is launchd-on-one-Mac by construction
  (loops root lives under ``var/``), so that is the correct trade; a multi-host
  loop fleet would need a database lease instead.
* **Crash safety is free.** The kernel drops every ``flock`` when the holding
  process exits, however it exits. ``kill -9`` releases the lease; no PID
  liveness probe and no reclaim path is involved, which is precisely what
  removes the race.
* **PID and timestamp are DIAGNOSTICS ONLY.** They are written inside the file
  for an operator reading ``var/loops/*.lease``, and reported on
  :class:`LeaseHeld`. Nothing in acquisition reads them — an age threshold that
  can *take* a lease is the reclaim race in another costume. A lease held past
  :data:`DEFAULT_MAX_AGE_S` is reported as ``stale_looking`` so a human can look,
  and is still not stolen: the holder is a live process, and the scheduler's own
  tick timeout (``loop_jobs.MAX_TIMEOUT_S``) already kills a runaway worker,
  which releases the lock.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from omniagentos_loops.paths import loops_root, require_safe_name

#: A lease held longer than this is *reported* as suspicious in the holder
#: diagnostics. It is NOT an acquisition input: see the module docstring.
#: Comfortably longer than loop_jobs' maximum tick timeout so a legitimately
#: slow tick is never even flagged.
DEFAULT_MAX_AGE_S = 3600.0

#: The diagnostics record is written space-padded to a fixed size and never
#: truncated, so a peer that reads the file while the winner is stamping it sees
#: either the previous record or the new one — never a half-truncated file that
#: parses as "no holder". The lock does not depend on this; the operator's
#: ability to answer "who has it?" does.
_RECORD_BYTES = 512


class LeaseHeld(RuntimeError):
    """Another live worker holds this instance's lease."""

    def __init__(self, holder: dict[str, object]) -> None:
        super().__init__(f"loop instance is already running: {holder}")
        self.holder = holder


@dataclass(frozen=True)
class Lease:
    """A held lease. ``fd`` keeps the lock alive and MUST stay open."""

    path: Path
    pid: int
    acquired_at: float
    fd: int = -1


def lease_path(template: str, instance_id: str) -> Path:
    require_safe_name(template, kind="template")
    require_safe_name(instance_id, kind="instance")
    root = loops_root()
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{template}.{instance_id}.lease"


def _read(path: Path) -> dict[str, object]:
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def _holder_diagnostics(path: Path, *, max_age_s: float) -> dict[str, object]:
    """What the LOSER reports. Never consulted to decide anything."""
    holder = _read(path)
    raw = holder.get("acquired_at")
    started = float(raw) if isinstance(raw, int | float) else 0.0
    if started:
        age = max(0.0, time.time() - started)
        holder["age_s"] = round(age, 1)
        holder["stale_looking"] = age > max_age_s
    return holder


def acquire(template: str, instance_id: str, *, max_age_s: float = DEFAULT_MAX_AGE_S) -> Lease:
    """Take the lease, or raise :class:`LeaseHeld`. Never blocks.

    *max_age_s* only annotates the holder reported on :class:`LeaseHeld`; it
    cannot cause a lease to be taken from a live holder.
    """
    path = lease_path(template, instance_id)
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        # The whole lease, in one uninterruptible kernel decision.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES, errno.EDEADLK):
            raise LeaseHeld(_holder_diagnostics(path, max_age_s=max_age_s)) from None
        raise

    acquired_at = time.time()
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "acquired_at": acquired_at,
            "instance": instance_id,
            "template": template,
        },
        sort_keys=True,
    )
    record = payload.encode("utf-8")
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        if len(record) <= _RECORD_BYTES:
            os.write(fd, record.ljust(_RECORD_BYTES))
        else:  # pragma: no cover - names are length-capped by require_safe_name
            os.write(fd, record)
            os.ftruncate(fd, len(record))
        os.fsync(fd)
    except OSError:  # pragma: no cover - the LOCK is the lease; the note is a note
        pass
    return Lease(path=path, pid=os.getpid(), acquired_at=acquired_at, fd=fd)


def release(lease: Lease) -> None:
    """Drop the lock and close the descriptor. The file itself STAYS.

    Unlinking would reintroduce the race the rewrite removed: a peer blocked on
    the lock holds a descriptor onto this inode, so a fresh ``open`` after an
    unlink creates a DIFFERENT inode that nobody's lock covers.
    """
    if lease.fd < 0:
        return
    try:
        fcntl.flock(lease.fd, fcntl.LOCK_UN)
    except OSError:  # pragma: no cover - closing releases it regardless
        pass
    try:
        os.close(lease.fd)
    except OSError:  # pragma: no cover
        pass


@contextmanager
def held(
    template: str, instance_id: str, *, max_age_s: float = DEFAULT_MAX_AGE_S
) -> Iterator[Lease]:
    lease = acquire(template, instance_id, max_age_s=max_age_s)
    try:
        yield lease
    finally:
        release(lease)


__all__ = [
    "DEFAULT_MAX_AGE_S",
    "Lease",
    "LeaseHeld",
    "acquire",
    "held",
    "lease_path",
    "release",
]
