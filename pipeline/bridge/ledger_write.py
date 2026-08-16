#!/usr/bin/env python3
"""The single transport for appending events to the loop-queue ledger.

This module deliberately does not validate the event vocabulary. Several
producers use extension events that are not yet represented by the advisory
JSON schema; centralising transport must not silently change that policy.

Every cooperating writer locks ``<queue>/locks/ledger.lock`` *before* opening
``ledger.jsonl``. The lock lives at a stable path and is never rotated with the
data file, so future maintenance can participate in the same protocol.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
import stat
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AppendResult:
    """A successfully completed, durably flushed append."""

    bytes_written: int
    durable: bool = True


class LedgerAppendError(OSError):
    """An append failed, with enough state for callers to avoid bad rollback.

    ``bytes_written == 0`` means this invocation added no ledger bytes.
    Otherwise the outcome is at least partially committed and callers must not
    roll back related artifacts as though the append never happened.
    """

    def __init__(
        self,
        message: str,
        *,
        phase: str,
        bytes_written: int = 0,
        complete: bool = False,
        durable: bool = False,
    ) -> None:
        super().__init__(message)
        self.phase = phase
        self.bytes_written = bytes_written
        self.complete = complete
        self.durable = durable


class LedgerTailUnhealthy(LedgerAppendError):
    """The existing ledger ends in an incomplete record; no bytes were added."""


_STATE_ATTR = "_omni_ledger_writer_thread_state_v1"


def _new_thread_state() -> tuple[threading.Lock, dict[str, threading.Lock]]:
    return threading.Lock(), {}


if not hasattr(fcntl, _STATE_ATTR):
    # Some legacy entry points import siblings as top-level modules while
    # package callers use ``bridge.*``. Keep process-local coordination on the
    # singleton fcntl module so both import identities share one registry.
    setattr(fcntl, _STATE_ATTR, _new_thread_state())


def _reset_thread_locks_after_fork() -> None:
    # A child must not inherit a Python lock whose owning thread no longer
    # exists. The kernel flock is independently reacquired below.
    setattr(fcntl, _STATE_ATTR, _new_thread_state())


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_thread_locks_after_fork)


def _thread_lock(path: Path) -> threading.Lock:
    # Resolve aliases so two threads that spell one queue through different
    # symlinks cannot obtain distinct Python locks for the same lock inode.
    key = os.fspath(path.resolve(strict=False))
    guard, locks = getattr(fcntl, _STATE_ATTR)
    with guard:
        return locks.setdefault(key, threading.Lock())


def _open_flags(base: int) -> int:
    return base | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _require_regular(fd: int, path: Path, *, phase: str) -> None:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise LedgerAppendError(f"refusing non-regular ledger transport path: {path}", phase=phase)


def _write_all(fd: int, payload: bytes) -> int:
    """Write the complete payload, retrying interruptions and short writes."""

    written = 0
    view = memoryview(payload)
    while written < len(payload):
        try:
            count = os.write(fd, view[written:])
        except InterruptedError:
            continue
        except OSError as exc:
            if exc.errno == errno.EINTR:
                continue
            raise LedgerAppendError(
                f"ledger write failed after {written} of {len(payload)} bytes: {exc}",
                phase="write",
                bytes_written=written,
            ) from exc
        if count <= 0:
            raise LedgerAppendError(
                f"ledger write made no progress after {written} of {len(payload)} bytes",
                phase="write",
                bytes_written=written,
            )
        written += count
    return written


def _close(fd: int, *, phase: str, written: int, complete: bool, durable: bool) -> None:
    try:
        os.close(fd)
    except OSError as exc:
        raise LedgerAppendError(
            f"ledger {phase} descriptor close failed: {exc}",
            phase=f"{phase}_close",
            bytes_written=written,
            complete=complete,
            durable=durable,
        ) from exc


def _append_payload(
    queue: Path, payload: bytes | Callable[[], bytes]
) -> AppendResult:
    queue = Path(queue)
    if not queue.is_dir():
        raise LedgerAppendError(f"ledger queue is not a directory: {queue}", phase="queue")

    locks_dir = queue / "locks"
    try:
        locks_dir.mkdir(mode=0o755, exist_ok=True)
    except OSError as exc:
        raise LedgerAppendError(
            f"cannot create ledger lock directory {locks_dir}: {exc}", phase="lock_open"
        ) from exc

    lock_path = locks_dir / "ledger.lock"
    local_lock = _thread_lock(lock_path)
    with local_lock:
        lock_fd = -1
        ledger_fd = -1
        written = 0
        complete = False
        durable = False
        active_error: BaseException | None = None
        try:
            try:
                lock_fd = os.open(lock_path, _open_flags(os.O_RDWR | os.O_CREAT), 0o644)
                _require_regular(lock_fd, lock_path, phase="lock_open")
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except LedgerAppendError:
                raise
            except OSError as exc:
                raise LedgerAppendError(
                    f"cannot acquire ledger lock {lock_path}: {exc}", phase="lock"
                ) from exc

            if callable(payload):
                # Deferred build: the caller's payload builder runs only once
                # the exclusive lock is held, so anything it computes (the `ts`
                # append stamp above all) is ordered WITH the append itself —
                # a writer that loses the lock race also stamps later.
                payload = payload()

            ledger_path = queue / "ledger.jsonl"
            existed = ledger_path.exists()
            try:
                ledger_fd = os.open(
                    ledger_path,
                    _open_flags(os.O_RDWR | os.O_CREAT | os.O_APPEND),
                    0o644,
                )
                _require_regular(ledger_fd, ledger_path, phase="ledger_open")
            except LedgerAppendError:
                raise
            except OSError as exc:
                raise LedgerAppendError(
                    f"cannot open ledger {ledger_path}: {exc}", phase="ledger_open"
                ) from exc

            size = os.fstat(ledger_fd).st_size
            if size:
                try:
                    tail = os.pread(ledger_fd, 1, size - 1)
                except OSError as exc:
                    raise LedgerAppendError(
                        f"cannot inspect ledger tail {ledger_path}: {exc}", phase="tail"
                    ) from exc
                if tail != b"\n":
                    raise LedgerTailUnhealthy(
                        f"refusing append: {ledger_path} has an unterminated tail",
                        phase="tail",
                    )

            written = _write_all(ledger_fd, payload)
            complete = True
            try:
                os.fsync(ledger_fd)
            except OSError as exc:
                raise LedgerAppendError(
                    f"ledger fsync failed after the complete event was written: {exc}",
                    phase="fsync",
                    bytes_written=written,
                    complete=True,
                ) from exc

            if not existed:
                directory_fd = -1
                directory_error: BaseException | None = None
                try:
                    directory_fd = os.open(queue, _open_flags(os.O_RDONLY))
                    os.fsync(directory_fd)
                    durable = True
                except OSError as exc:
                    directory_error = LedgerAppendError(
                        f"ledger directory fsync failed after the event was written: {exc}",
                        phase="directory_fsync",
                        bytes_written=written,
                        complete=True,
                    )
                    raise directory_error from exc
                except BaseException as exc:
                    directory_error = exc
                    raise
                finally:
                    if directory_fd >= 0:
                        try:
                            _close(
                                directory_fd,
                                phase="directory",
                                written=written,
                                complete=True,
                                durable=durable,
                            )
                        except LedgerAppendError:
                            if directory_error is None:
                                raise
            else:
                durable = True
            return AppendResult(bytes_written=written)
        except BaseException as exc:
            active_error = exc
            raise
        finally:
            cleanup_error: LedgerAppendError | None = None
            if ledger_fd >= 0:
                try:
                    _close(
                        ledger_fd,
                        phase="ledger",
                        written=written,
                        complete=complete,
                        durable=durable,
                    )
                except LedgerAppendError as exc:
                    if active_error is None:
                        cleanup_error = exc
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                except OSError as exc:
                    if active_error is None and cleanup_error is None:
                        cleanup_error = LedgerAppendError(
                            f"ledger lock release failed: {exc}",
                            phase="lock_unlock",
                            bytes_written=written,
                            complete=complete,
                            durable=durable,
                        )
                try:
                    _close(
                        lock_fd,
                        phase="lock",
                        written=written,
                        complete=complete,
                        durable=durable,
                    )
                except LedgerAppendError as exc:
                    if active_error is None and cleanup_error is None:
                        cleanup_error = exc
            if cleanup_error is not None:
                raise cleanup_error


def _now_iso() -> str:
    """The append clock, in the ``...Z`` form every reader already parses."""

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plain(value: Any) -> Any:
    """Recursively materialize dict/list containers from their REAL storage.

    A dict or list subclass can lie through overridden accessor/iteration
    methods while json.dumps serializes (dicts) its real storage or (lists)
    its lying iteration — either way the preservation logic and the written
    bytes can disagree at ANY nesting depth (Sol FINDING-6, Grok FINDING-7/8:
    each round found the same class one level deeper). Closing the CLASS:
    unbound base slots (`dict.keys`/`dict.__getitem__`, `list.copy`) read the
    real storage regardless of overrides — verified empirically on CPython
    3.12 — applied recursively to both container kinds. Non-JSON containers
    (tuples, sets, custom objects) are out of the event contract and keep
    failing at json.dumps exactly as before.
    """
    if isinstance(value, dict):
        return {k: _plain(dict.__getitem__(value, k)) for k in dict.keys(value)}
    if isinstance(value, list):
        return [_plain(v) for v in list.copy(value)]
    return value


def stamp_ts(event: dict[str, Any]) -> dict[str, Any]:
    """Return ``event`` with an authoritative ``ts``, set from the append clock.

    ``ts`` is schema-required, but nothing on the write path ever supplied it:
    the transport serialized whatever the caller sent. Measured on the live
    ledger before this change, 584 of 12341 events (4.7%) carried no ``ts`` at
    all, and the ``:00``-seconds share was 10% against the ~1.7% chance rate --
    the signature of LLM loop sessions hand-writing plausible times. Ten events
    were stamped in the FUTURE, which is what impairs the hang-recycler: it
    cannot date an event that has not happened yet, so it fails closed and stops
    verifying loop liveness.

    Readers treat a missing or unparseable ``ts`` as favourable absence, so the
    fix belongs here, at the single transport, rather than in each reader.

    A caller-supplied value is preserved in the append-only ``ts_claims``
    LIST rather than dropped, WHATEVER its type: ``None``, an epoch float, an
    empty string and a wrong string are all evidence about the writer, and
    silently erasing any of them would be a favourable-absence violation
    frozen into an append-only log (Sol FINDING-1, PR #409 review).

    The list is structural, not a renaming chain: three consecutive review
    rounds each found the previous scalar scheme (``ts_claimed``, then
    ``ts_claimed_prior``) clobberable one nesting level deeper, because any
    FIXED set of scalar keys can collide with a caller that already used
    them. A caller-supplied ``ts_claims`` list is therefore EXTENDED, never
    replaced, so arbitrary claim depth survives by construction. Only a value
    of exact built-in type ``str`` (``type(x) is str`` -- a subclass can lie
    through ``__eq__``, Sol FINDING-5) that equals the fresh stamp is
    omitted; it adds no information. The append clock always wins, because
    for an append-only log the time of the append IS the truth -- no caller
    is better placed to know it.
    """

    # Normalize to PLAIN containers at every depth (see _plain): after this
    # line the preservation logic below and the serializer always see
    # identical data, whatever accessor/iteration lies the caller's dict or
    # list subclasses tell, at any nesting level.
    event = _plain(event)
    stamped = {"ts": _now_iso(),
               **{k: v for k, v in event.items() if k not in ("ts", "ts_claims")}}
    prior = event.get("ts_claims")
    # `event` is already recursively plain here, so this is a plain list (or
    # a preserved scalar) — no subclass games can survive _plain above.
    claims = list(prior) if isinstance(prior, list) else (
        [prior] if "ts_claims" in event else [])
    if "ts" in event and not (
            type(event["ts"]) is str and event["ts"] == stamped["ts"]):
        claims.append(event["ts"])
    if claims or "ts_claims" in event:
        # An explicitly-supplied empty list is itself a claim statement and
        # must not vanish from the written record.
        stamped["ts_claims"] = claims
    return stamped


def append_event(queue: Path, event: dict[str, Any]) -> AppendResult:
    """Serialize and durably append one JSON object plus its newline.

    The ``ts`` stamp is taken INSIDE the ledger lock (the builder below runs
    after ``flock`` succeeds), so stamp order and physical append order can
    never disagree: a writer that loses the lock race also stamps later (Sol
    FINDING-2, PR #409 review). Readers that treat ``ts`` as append time --
    the hang-recycler's event-age basis, integrity, the backlog census --
    depend on that monotonicity. A non-JSON-serializable event still raises
    ``TypeError`` as before, now from under the lock; the transport's cleanup
    path releases the lock on any exception.
    """

    if not isinstance(event, dict):
        raise TypeError("ledger events must be JSON objects")

    def _build_under_lock() -> bytes:
        stamped = stamp_ts(event)
        return (json.dumps(stamped, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    return _append_payload(queue, _build_under_lock)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    append_parser = subparsers.add_parser("append", help="append one JSON object from stdin")
    append_parser.add_argument("--queue", required=True, type=Path)
    args = parser.parse_args(argv)

    if not args.queue.is_absolute():
        parser.error("--queue must be an absolute path")
    try:
        event = json.loads(sys.stdin.read())
        if not isinstance(event, dict):
            raise TypeError("stdin must contain exactly one JSON object")
        append_event(args.queue, event)
    except LedgerAppendError as exc:
        print(f"ledger append failed during {exc.phase}: {exc}", file=sys.stderr)
        return 3 if exc.bytes_written else 2
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(f"invalid ledger event: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
