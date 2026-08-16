"""The append-only JSONL telemetry and audit trail for autonomy leases.

This module provides mechanisms to durably log lease-related events and decisions to monthly
partitioned JSONL ledger files. This serves as an audit trail of the execution lifecycle
of leased processes.

The suggested event vocabulary recorded in this ledger includes:
- "issued": Recorded when a lease is initially constructed and signed.
- "launched": Recorded when a sandboxed process is successfully spawned under the lease.
- "refused": Recorded when a launch is rejected due to lease validation failure.
- "revoked": Recorded when a lease is explicitly revoked or overridden.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore[assignment]

from omniagentos.lease.config import lease_ledger_dir
from omniagentos.lease.models import Lease, lease_record

LEASE_LEDGER_PREFIX: str = "leases"
MAX_RECORD_BYTES: int = 64 * 1024
LOCK_ATTEMPTS: int = 20
LOCK_RETRY_S: float = 0.05


def lease_ledger_path(ledger_dir: str | None = None, *, when: float | None = None) -> Path:
    """Resolve the Path to the monthly lease ledger file.

    The path format is '<ledger_dir>/leases-YYYYMM.jsonl', where YYYYMM represents the
    month partition based on the provided UTC timestamp.

    Args:
        ledger_dir: The directory where ledger files are stored. Defaults to
            lease_ledger_dir() if None is specified.
        when: Optional epoch timestamp. Defaults to the current time if None.

    Returns:
        The Path object pointing to the partition file.
    """
    if ledger_dir is None:
        ledger_dir = lease_ledger_dir()
    if when is None:
        when = time.time()
    month_str = datetime.fromtimestamp(when, UTC).strftime("%Y%m")
    return Path(ledger_dir) / f"{LEASE_LEDGER_PREFIX}-{month_str}.jsonl"


def lease_line(lease: Lease, *, event: str, mode: str, extra: dict[str, Any] | None = None) -> str:
    """Serialize the lease state and audit event into a single deterministic JSONL line.

    The HMAC signature and the signing key are NEVER written to disk — the ledger is
    an audit trail of DECISIONS, not a credential store, and a persisted signature over
    a persisted payload would turn the trail into an offline attack surface against the key.

    Args:
        lease: The Lease instance to record.
        event: The name of the event (e.g., 'issued', 'launched', 'refused').
        mode: The lease mode active at the time of the event (e.g., 'off', 'shadow', 'enforce').
        extra: Optional dictionary of additional metadata to merge into the record.

    Returns:
        The serialized JSONL string terminating with a newline character.
    """
    record = lease_record(lease)
    payload = {
        **record,
        "event": event,
        "mode": mode,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    if extra:
        payload.update(extra)
    line = json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"

    if len(line.encode("utf-8")) > MAX_RECORD_BYTES:
        # The ledger is telemetry, and an unbounded row on the launch path is a denial-of-service
        # against the thing it is supposed to be observing.
        reduced_payload = {
            "version": payload.get("version"),
            "lease_id": payload.get("lease_id"),
            "subject": payload.get("subject"),
            "issued_at": payload.get("issued_at"),
            "expires_at": payload.get("expires_at"),
            "generation": payload.get("generation"),
            "signed": payload.get("signed"),
            "event": payload.get("event"),
            "mode": payload.get("mode"),
            "recorded_at": payload.get("recorded_at"),
            "truncated": True,
        }
        line = json.dumps(reduced_payload, sort_keys=True, ensure_ascii=False) + "\n"

    return line


def append_lease_record(
    lease: Lease,
    *,
    event: str,
    mode: str,
    ledger_dir: str | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Durably append a lease audit record to the monthly JSONL log file.

    A ledger write failure must never take down a launch decision that has already
    been made; the decision itself is enforced in-process, and the row is telemetry.
    Therefore, this function is best-effort and will never raise an OSError on failure;
    instead, it logs a warning and returns an empty string.
    To prevent delaying the launch sequence, this write will drop the telemetry row
    under persistent lock contention rather than block the thread.

    Args:
        lease: The Lease instance to write.
        event: The name of the event (e.g., 'issued', 'launched', 'refused').
        mode: The lease mode active at the time of the event.
        ledger_dir: Optional custom ledger directory. Resolves to lease_ledger_dir() if None.
        extra: Optional additional metadata fields to merge into the JSONL row.

    Returns:
        The string path of the appended ledger file, or an empty string on failure.
    """
    fd: int | None = None
    locked: bool = False
    try:
        path = lease_ledger_path(ledger_dir)
        path.parent.mkdir(parents=True, exist_ok=True)

        parent_info = path.parent.lstat()
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            logging.getLogger(__name__).warning(
                "Parent directory is not a real directory: %s", path.parent
            )
            return ""

        line = lease_line(lease, event=event, mode=mode, extra=extra)

        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags, 0o600)

        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            logging.getLogger(__name__).warning("Lease ledger path is not a regular file: %s", path)
            os.close(fd)
            fd = None
            return ""

        if fcntl is not None:
            for attempt in range(LOCK_ATTEMPTS):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError:
                    if attempt < LOCK_ATTEMPTS - 1:
                        time.sleep(LOCK_RETRY_S)
            if not locked:
                logging.getLogger(__name__).warning(
                    "Failed to acquire lock on lease ledger path: %s", path
                )
                os.close(fd)
                fd = None
                return ""

        # Write the encoded line with a loop that handles short writes
        payload = line.encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if written <= 0:
                raise OSError("Lease ledger write made no progress")
            offset += written

        os.fsync(fd)
        return str(path)
    except (OSError, ValueError, TypeError) as error:
        logging.getLogger(__name__).warning("Failed to write lease record to ledger: %s", error)
        return ""
    finally:
        if locked and fcntl is not None and fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


__all__ = [
    "LEASE_LEDGER_PREFIX",
    "LOCK_ATTEMPTS",
    "LOCK_RETRY_S",
    "MAX_RECORD_BYTES",
    "append_lease_record",
    "lease_ledger_path",
    "lease_line",
]
