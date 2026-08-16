"""Durable, bounded terminal evidence for direct longhaul CLI attempts.

The longhaul daemon cannot make ``Popen.communicate()`` and a later SQLite
write atomic.  This module supplies a small trusted wrapper process that owns
the provider child, drains both output pipes into bounded tails, and publishes
an identity-bound JSON record before the wrapper exits.  If the daemon dies,
the wrapper remains in the attempt's process group, finishes the provider
process, fsyncs the record, and lets a fresh daemon replay the same terminal
classification.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, BinaryIO

LOG = logging.getLogger(__name__)

TERMINAL_EVIDENCE_VERSION = 1
TERMINAL_CAPTURE_LIMIT_BYTES = 64 * 1024
# Each captured byte can become a three-byte UTF-8 replacement character during
# sanitization, and there are two independent streams.
TERMINAL_RECORD_MAX_BYTES = 6 * TERMINAL_CAPTURE_LIMIT_BYTES + 16 * 1024

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


class TerminalEvidenceError(RuntimeError):
    """A terminal record is absent, corrupt, oversized, or identity-mismatched."""


class _TailBuffer:
    """Keep only the last bounded bytes while always draining the source pipe."""

    def __init__(self, limit: int = TERMINAL_CAPTURE_LIMIT_BYTES) -> None:
        self._limit = limit
        self._value = bytearray()
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._value.extend(chunk)
        overflow = len(self._value) - self._limit
        if overflow > 0:
            del self._value[:overflow]
            self.truncated = True

    def text(self) -> str:
        decoded = bytes(self._value).decode("utf-8", errors="replace")
        # Preserve useful line structure but neutralize control characters that
        # should never influence logs, JSON consumers, or terminal rendering.
        return "".join(
            char if char in "\n\r\t" or ord(char) >= 32 else "\N{REPLACEMENT CHARACTER}"
            for char in decoded
        )


def _validated_token(value: str, label: str) -> str:
    if not _TOKEN_RE.fullmatch(value):
        raise TerminalEvidenceError(f"invalid {label} in terminal evidence identity")
    return value


def evidence_root(db_path: str) -> Path:
    """Return the trusted evidence directory adjacent to the durable database."""

    if db_path == ":memory:":
        configured = os.environ.get("OMNIAGENTOS_VAR_DIR") or os.environ.get("OMNIAGENTOS_VAR")
        base = Path(configured).expanduser() if configured else Path.cwd() / "var"
    else:
        base = Path(db_path).expanduser().resolve(strict=False).parent
    return base / "longhaul-terminal-evidence"


def prepare_evidence_root(db_path: str) -> Path:
    """Create the private evidence directory without following a directory symlink."""

    root = evidence_root(db_path)
    try:
        info = root.lstat()
    except FileNotFoundError:
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            # Another direct-CLI attempt created the shared private directory
            # between lstat and mkdir. Validate what won below.
            pass
        info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TerminalEvidenceError("terminal evidence root is not a real directory")
    return root


def terminal_record_path(db_path: str, attempt_id: str, launch_nonce: str) -> Path:
    """Derive a record path solely from trusted database and launch identity."""

    attempt = _validated_token(attempt_id, "attempt id")
    nonce = _validated_token(launch_nonce, "launch nonce")
    return evidence_root(db_path) / f"{attempt}.{nonce}.json"


def launch_record_path(db_path: str, attempt_id: str, launch_nonce: str) -> Path:
    """The wrapper-start record used to close the Popen→PID-commit crash gap."""

    attempt = _validated_token(attempt_id, "attempt id")
    nonce = _validated_token(launch_nonce, "launch nonce")
    return evidence_root(db_path) / f"{attempt}.{nonce}.started.json"


def launch_ack_path(db_path: str, attempt_id: str, launch_nonce: str) -> Path:
    """The daemon authorization record granting permission to start the provider."""

    attempt = _validated_token(attempt_id, "attempt id")
    nonce = _validated_token(launch_nonce, "launch nonce")
    return evidence_root(db_path) / f"{attempt}.{nonce}.ack.json"


def tombstone_record_path(db_path: str, attempt_id: str, launch_nonce: str) -> Path:
    """The cancellation record forbidding provider launch for this nonce."""

    attempt = _validated_token(attempt_id, "attempt id")
    nonce = _validated_token(launch_nonce, "launch nonce")
    return evidence_root(db_path) / f"{attempt}.{nonce}.canceled.json"


def attempt_tombstone_path(db_path: str, attempt_id: str) -> Path:
    """Attempt-wide tombstone forbidding any provider launch for this attempt."""

    attempt = _validated_token(attempt_id, "attempt id")
    return evidence_root(db_path) / f"{attempt}.canceled.json"


def _canonical_payload(record: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in record.items() if key != "sha256"}
    return json.dumps(
        payload,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")


def _with_digest(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["sha256"] = hashlib.sha256(_canonical_payload(result)).hexdigest()
    return result


def _write_all(fd: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        if written <= 0:
            raise OSError("terminal evidence write made no progress")
        offset += written


def publish_terminal_record(path: Path, record: dict[str, Any]) -> None:
    """Fsync and atomically publish one immutable terminal record."""

    complete = _with_digest(record)
    # Add the digest only after hashing the digest-free canonical object.
    payload = json.dumps(
        complete,
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")
    if len(payload) > TERMINAL_RECORD_MAX_BYTES:
        raise TerminalEvidenceError("terminal evidence record exceeded its bound")

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_info = path.parent.lstat()
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise TerminalEvidenceError("terminal evidence parent is not a real directory")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary, flags | nofollow, 0o600)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        try:
            # link() is an atomic no-overwrite publication on the same
            # filesystem. Unlike replace(), an attacker cannot win a race by
            # pre-creating the final path and having us overwrite it.
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError as exc:
            raise TerminalEvidenceError("terminal evidence record already exists") from exc
        temporary.unlink()
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_bounded_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise TerminalEvidenceError("terminal evidence record is missing") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise TerminalEvidenceError("terminal evidence record is not a regular file")
        if info.st_size <= 0 or info.st_size > TERMINAL_RECORD_MAX_BYTES:
            raise TerminalEvidenceError("terminal evidence record has an invalid size")
        chunks: list[bytes] = []
        remaining = TERMINAL_RECORD_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(16 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > TERMINAL_RECORD_MAX_BYTES:
            raise TerminalEvidenceError("terminal evidence record exceeded its read bound")
        return payload
    finally:
        os.close(fd)


def load_terminal_record(
    db_path: str,
    *,
    attempt_id: str,
    harness: str,
    provider: str,
    launch_nonce: str,
    expected_wrapper_pid: int | None,
) -> dict[str, Any]:
    """Load and verify one record against the durable attempt launch identity."""

    path = terminal_record_path(db_path, attempt_id, launch_nonce)
    payload = _read_bounded_regular_file(path)
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalEvidenceError("terminal evidence record is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise TerminalEvidenceError("terminal evidence record is not an object")
    digest = decoded.get("sha256")
    if not isinstance(digest, str) or not hmac.compare_digest(
        digest, hashlib.sha256(_canonical_payload(decoded)).hexdigest()
    ):
        raise TerminalEvidenceError("terminal evidence digest mismatch")

    expected = {
        "version": TERMINAL_EVIDENCE_VERSION,
        "attempt_id": attempt_id,
        "harness": harness,
        "provider": provider,
        "launch_nonce": launch_nonce,
    }
    for key, value in expected.items():
        if decoded.get(key) != value:
            raise TerminalEvidenceError(f"terminal evidence {key} mismatch")
    wrapper_pid = decoded.get("wrapper_pid")
    if isinstance(wrapper_pid, bool) or not isinstance(wrapper_pid, int) or wrapper_pid <= 0:
        raise TerminalEvidenceError("terminal evidence wrapper pid is invalid")
    if expected_wrapper_pid is not None and wrapper_pid != expected_wrapper_pid:
        raise TerminalEvidenceError("terminal evidence wrapper pid mismatch")
    returncode = decoded.get("returncode")
    if isinstance(returncode, bool) or not isinstance(returncode, int):
        raise TerminalEvidenceError("terminal evidence return code is invalid")
    for key in ("stdout", "stderr"):
        value = decoded.get(key)
        if not isinstance(value, str):
            raise TerminalEvidenceError(f"terminal evidence {key} is invalid")
        if len(value.encode("utf-8")) > TERMINAL_CAPTURE_LIMIT_BYTES * 3:
            # A UTF-8 replacement expansion can exceed the raw-byte tail, but
            # never by more than three bytes per captured byte.
            raise TerminalEvidenceError(f"terminal evidence {key} exceeded its bound")
    return decoded


def load_launch_record(
    db_path: str,
    *,
    attempt_id: str,
    harness: str,
    provider: str,
    launch_nonce: str,
) -> dict[str, Any]:
    """Verify the wrapper identity it fsynced before starting the provider child."""

    path = launch_record_path(db_path, attempt_id, launch_nonce)
    payload = _read_bounded_regular_file(path)
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TerminalEvidenceError("terminal launch record is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise TerminalEvidenceError("terminal launch record is not an object")
    digest = decoded.get("sha256")
    if not isinstance(digest, str) or not hmac.compare_digest(
        digest, hashlib.sha256(_canonical_payload(decoded)).hexdigest()
    ):
        raise TerminalEvidenceError("terminal launch record digest mismatch")
    expected = {
        "version": TERMINAL_EVIDENCE_VERSION,
        "attempt_id": attempt_id,
        "harness": harness,
        "provider": provider,
        "launch_nonce": launch_nonce,
        "state": "started",
    }
    for key, value in expected.items():
        if decoded.get(key) != value:
            raise TerminalEvidenceError(f"terminal launch {key} mismatch")
    wrapper_pid = decoded.get("wrapper_pid")
    if isinstance(wrapper_pid, bool) or not isinstance(wrapper_pid, int) or wrapper_pid <= 0:
        raise TerminalEvidenceError("terminal launch wrapper pid is invalid")
    return decoded


def publish_launch_ack(
    db_path: str,
    *,
    attempt_id: str,
    harness: str,
    provider: str,
    launch_nonce: str,
) -> Path:
    """Fsync and publish daemon authorization for wrapper provider execution."""

    path = launch_ack_path(db_path, attempt_id, launch_nonce)
    try:
        publish_terminal_record(
            path,
            {
                "version": TERMINAL_EVIDENCE_VERSION,
                "attempt_id": _validated_token(attempt_id, "attempt id"),
                "harness": _validated_token(harness, "harness"),
                "provider": _validated_token(provider, "provider"),
                "launch_nonce": _validated_token(launch_nonce, "launch nonce"),
                "state": "ack",
                "daemon_pid": os.getpid(),
            },
        )
    except TerminalEvidenceError as exc:
        if "already exists" not in str(exc):
            raise
    return path


def load_launch_ack(
    db_path: str,
    *,
    attempt_id: str,
    harness: str,
    provider: str,
    launch_nonce: str,
) -> dict[str, Any] | None:
    """Verify daemon launch authorization record."""

    path = launch_ack_path(db_path, attempt_id, launch_nonce)
    try:
        payload = _read_bounded_regular_file(path)
    except TerminalEvidenceError:
        return None
    try:
        decoded = json.loads(payload)
    except Exception:
        return None
    if not isinstance(decoded, dict):
        return None
    digest = decoded.get("sha256")
    if not isinstance(digest, str) or not hmac.compare_digest(
        digest, hashlib.sha256(_canonical_payload(decoded)).hexdigest()
    ):
        return None
    if decoded.get("state") != "ack":
        return None
    if decoded.get("attempt_id") != attempt_id or decoded.get("launch_nonce") != launch_nonce:
        return None
    return decoded


def publish_tombstone(
    db_path: str,
    *,
    attempt_id: str,
    harness: str | None = None,
    provider: str | None = None,
    launch_nonce: str | None = None,
    reason: str = "canceled",
) -> None:
    """Publish durable launch cancellation record(s) forbidding provider execution."""

    validated_attempt = _validated_token(attempt_id, "attempt id")
    payload: dict[str, Any] = {
        "version": TERMINAL_EVIDENCE_VERSION,
        "attempt_id": validated_attempt,
        "state": "canceled",
        "reason": reason[:500],
        "daemon_pid": os.getpid(),
    }
    if launch_nonce and harness and provider:
        payload["harness"] = _validated_token(harness, "harness")
        payload["provider"] = _validated_token(provider, "provider")
        payload["launch_nonce"] = _validated_token(launch_nonce, "launch nonce")
        nonce_path = tombstone_record_path(db_path, validated_attempt, payload["launch_nonce"])
        try:
            publish_terminal_record(nonce_path, payload)
        except TerminalEvidenceError as exc:
            if "already exists" not in str(exc):
                LOG.warning("failed to publish nonce tombstone for %s: %s", validated_attempt, exc)

    attempt_path = attempt_tombstone_path(db_path, validated_attempt)
    try:
        publish_terminal_record(attempt_path, payload)
    except TerminalEvidenceError as exc:
        if "already exists" not in str(exc):
            LOG.warning("failed to publish attempt tombstone for %s: %s", validated_attempt, exc)


def is_tombstoned(
    db_path: str,
    *,
    attempt_id: str,
    launch_nonce: str | None = None,
) -> bool:
    """Return True if either the specific nonce or the whole attempt is tombstoned."""

    paths = [attempt_tombstone_path(db_path, attempt_id)]
    if launch_nonce:
        paths.append(tombstone_record_path(db_path, attempt_id, launch_nonce))
    for path in paths:
        if path.exists():
            return True
    return False


def remove_terminal_records(db_path: str, attempt_id: str, launch_nonce: str) -> None:
    """Best-effort cleanup after the attempt close CAS has durably won."""

    for path_function in (
        terminal_record_path,
        launch_record_path,
        launch_ack_path,
        tombstone_record_path,
    ):
        try:
            path_function(db_path, attempt_id, launch_nonce).unlink()
        except (FileNotFoundError, OSError, TerminalEvidenceError):
            pass

    try:
        attempt_tombstone_path(db_path, attempt_id).unlink()
    except (FileNotFoundError, OSError, TerminalEvidenceError):
        pass


def _drain(stream: BinaryIO, target: _TailBuffer) -> None:
    try:
        while True:
            chunk = stream.read(16 * 1024)
            if not chunk:
                return
            target.append(chunk)
    finally:
        stream.close()


def _run_child(
    command: Sequence[str], stdin_payload: bytes
) -> tuple[int, _TailBuffer, _TailBuffer]:
    stdout_tail = _TailBuffer()
    stderr_tail = _TailBuffer()
    try:
        child = subprocess.Popen(
            list(command),
            stdin=subprocess.PIPE if stdin_payload else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except Exception as exc:  # noqa: BLE001 - converted to durable terminal evidence.
        stderr_tail.append(f"provider launch failed: {exc}".encode("utf-8", errors="replace"))
        return 127, stdout_tail, stderr_tail

    assert child.stdout is not None
    assert child.stderr is not None
    drains = [
        threading.Thread(target=_drain, args=(child.stdout, stdout_tail), daemon=True),
        threading.Thread(target=_drain, args=(child.stderr, stderr_tail), daemon=True),
    ]
    for thread in drains:
        thread.start()
    if child.stdin is not None:
        try:
            child.stdin.write(stdin_payload)
            child.stdin.flush()
        except BrokenPipeError:
            pass
        finally:
            child.stdin.close()
    returncode = child.wait()
    for thread in drains:
        thread.join()
    return returncode, stdout_tail, stderr_tail


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--record-path", required=True)
    parser.add_argument("--launch-record-path", required=True)
    parser.add_argument("--attempt-id", required=True)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--provider", required=True)
    parser.add_argument("--launch-nonce", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("terminal evidence wrapper requires a provider command")

    attempt_id = _validated_token(args.attempt_id, "attempt id")
    harness = _validated_token(args.harness, "harness")
    provider = _validated_token(args.provider, "provider")
    launch_nonce = _validated_token(args.launch_nonce, "launch nonce")
    publish_terminal_record(
        Path(args.launch_record_path),
        {
            "version": TERMINAL_EVIDENCE_VERSION,
            "attempt_id": attempt_id,
            "harness": harness,
            "provider": provider,
            "launch_nonce": launch_nonce,
            "state": "started",
            "wrapper_pid": os.getpid(),
        },
    )
    stdin_payload = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
    if len(stdin_payload) > 4 * 1024 * 1024:
        raise SystemExit("terminal evidence wrapper stdin exceeded 4 MiB")

    ack_path = Path(args.launch_record_path).with_name(f"{attempt_id}.{launch_nonce}.ack.json")
    tombstone_path = Path(args.launch_record_path).with_name(
        f"{attempt_id}.{launch_nonce}.canceled.json"
    )
    attempt_tombstone = Path(args.launch_record_path).with_name(f"{attempt_id}.canceled.json")

    start_wait = time.monotonic()
    authorized = False
    canceled = False

    while time.monotonic() - start_wait < 30.0:
        if tombstone_path.exists() or attempt_tombstone.exists():
            canceled = True
            break
        if ack_path.exists():
            try:
                payload = json.loads(ack_path.read_text(encoding="utf-8"))
                if payload.get("state") == "ack" and payload.get("launch_nonce") == launch_nonce:
                    authorized = True
                    break
            except Exception:  # noqa: S110 - race or partial write; retry until valid or timeout
                pass
        time.sleep(0.002)

    if authorized and not canceled:
        returncode, stdout_tail, stderr_tail = _run_child(command, stdin_payload)
    else:
        returncode = 125
        stdout_tail = _TailBuffer()
        stderr_tail = _TailBuffer()
        stderr_tail.append(b"provider launch canceled or unacknowledged by daemon\n")

    publish_terminal_record(
        Path(args.record_path),
        {
            "version": TERMINAL_EVIDENCE_VERSION,
            "attempt_id": attempt_id,
            "harness": harness,
            "provider": provider,
            "launch_nonce": launch_nonce,
            "wrapper_pid": os.getpid(),
            "returncode": returncode,
            "stdout": stdout_tail.text(),
            "stderr": stderr_tail.text(),
            "stdout_truncated": stdout_tail.truncated,
            "stderr_truncated": stderr_tail.truncated,
            "launch_authorized": authorized and not canceled,
        },
    )
    return returncode if 0 <= returncode <= 255 else 1


if __name__ == "__main__":
    raise SystemExit(_main())
