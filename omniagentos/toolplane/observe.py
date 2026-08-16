"""Safe, structured observations for toolplane calls.

Observations are metadata-only records of tool invocations. They NEVER include:
- Tool arguments or input data
- Output content, paths to user data, or credentials
- Any material that could leak sensitive context

Observations DO include:
- Correlation fields: run_id, session_id, holder_generation, correlation_id
- Status: attempted, denied, failed, success
- Timing: ts (ISO timestamp), duration_ms
- Provenance: version, source (e.g. "toolplane", "session-hook")

L17 compliance:
- Scrubbed: every record passes through ``scrub_value`` before it is returned or
  written, so a credential that leaks into an error string never reaches disk.
- Durable emission: a write that fails is spooled to disk, not just to memory,
  so pending retry state survives process restart (M-28).
- Bounded retry: each spooled record carries an ``attempts`` counter; once it
  exceeds ``max_attempts`` the record is moved to a dead-letter directory rather
  than being retried forever or silently dropped.
- Idempotency: correlation_id names the durable file, so re-emitting the same
  observation (including after a restart) never produces a duplicate.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .manifest import CapabilityManifest
from .scrub import scrub_value

# Observation schema version for forward compatibility
OBSERVE_VERSION = "1"

# Denied error codes that indicate policy denial vs operational failure
DENIAL_ERRORS = frozenset(
    {
        "out_of_scope",
        "not_allowed",
        "secret_path",
        "broker_denied",
        "unknown_capability",
    }
)

# Subdirectory names under the ledger directory.
OBSERVATIONS_SUBDIR = "toolplane-observations"
SPOOL_SUBDIR = "toolplane-observations-pending"
DEADLETTER_SUBDIR = "toolplane-observations-failed"

# Retries per spooled observation before it is moved to the dead-letter directory.
DEFAULT_MAX_ATTEMPTS = 5

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.+-]")


def _safe_component(value: str, *, fallback: str) -> str:
    """Reduce a value to a filesystem-safe path component."""
    cleaned = _UNSAFE_NAME.sub("-", value).strip("-")
    return cleaned[:80] if cleaned else fallback


def _generate_correlation_id() -> str:
    """Generate a unique correlation ID for observation deduplication."""
    return uuid.uuid4().hex[:16]


def observe_call(
    tool: str,
    manifest: CapabilityManifest,
    result: dict[str, Any] | None,
    duration_ms: int,
    *,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Build observation metadata; never include args, output, paths, or credentials.

    Args:
        tool: The tool name invoked
        manifest: The capability manifest for this invocation
        result: The tool result (None = attempted but not executed)
        duration_ms: Execution duration in milliseconds
        correlation_id: Optional ID for deduplication (generated if not provided)

    Returns:
        Scrubbed observation record with trusted provenance and correlation fields.
    """
    if result is None:
        status = "attempted"
        ok = False
        error = None
    elif result.get("ok"):
        status = "success"
        ok = True
        error = None
    else:
        error = result.get("error")
        if error in DENIAL_ERRORS:
            status = "denied"
        else:
            status = "failed"
        ok = False

    observation = {
        "version": OBSERVE_VERSION,
        "ts": datetime.now(UTC).isoformat(),
        "tool": tool,
        "run_id": manifest.run_id,
        "session_id": manifest.session_id,
        "holder_generation": manifest.holder_generation,
        "correlation_id": correlation_id or _generate_correlation_id(),
        "status": status,
        "ok": ok,
        "error": error,
        "duration_ms": duration_ms,
        "source": "toolplane",
    }
    scrubbed = scrub_value(observation)
    return scrubbed if isinstance(scrubbed, dict) else observation


class ObservationSink:
    """Durable emission sink for observations with restart-safe bounded retry.

    Observations are written atomically to ``{ledger_dir}/toolplane-observations``.
    A write that fails is spooled to ``{ledger_dir}/toolplane-observations-pending``
    so the retry backlog is on disk, not in process memory: a fresh sink in a fresh
    process recovers the same backlog. Only if the spool itself is unwritable does
    the sink fall back to an in-memory queue.

    Retries are bounded. Each spooled record carries an ``attempts`` counter; when
    it reaches ``max_attempts`` the record is moved to
    ``{ledger_dir}/toolplane-observations-failed`` so the failure stays visible
    instead of being retried forever or dropped.
    """

    def __init__(
        self,
        ledger_dir: str | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        """Initialize sink with ledger directory.

        Args:
            ledger_dir: Directory for observation records. Defaults to
                        OMNIAGENTOS_LEDGER_DIR or var/ledger.
            max_attempts: Retries per observation before dead-lettering.
        """
        if ledger_dir is None:
            ledger_dir = os.environ.get("OMNIAGENTOS_LEDGER_DIR", "var/ledger")
        self.ledger_dir = ledger_dir
        self.max_attempts = max(1, int(max_attempts))
        # Last-resort queue used only when the on-disk spool is unwritable.
        self._pending: list[dict[str, Any]] = []

    # -- paths ---------------------------------------------------------------

    @property
    def observations_dir(self) -> str:
        return os.path.join(self.ledger_dir, OBSERVATIONS_SUBDIR)

    @property
    def spool_dir(self) -> str:
        return os.path.join(self.ledger_dir, SPOOL_SUBDIR)

    @property
    def deadletter_dir(self) -> str:
        return os.path.join(self.ledger_dir, DEADLETTER_SUBDIR)

    def _record_name(self, observation: dict[str, Any]) -> str:
        """Idempotent filename for an observation."""
        correlation_id = _safe_component(
            str(observation.get("correlation_id") or ""), fallback=_generate_correlation_id()
        )
        ts = _safe_component(
            str(observation.get("ts") or datetime.now(UTC).isoformat()).replace(":", "-"),
            fallback="unknown-ts",
        )
        return f"{ts}_{correlation_id}.json"

    def _spool_name(self, observation: dict[str, Any]) -> str:
        """Spool filename keyed only by correlation_id so retries overwrite in place."""
        correlation_id = _safe_component(
            str(observation.get("correlation_id") or ""), fallback=_generate_correlation_id()
        )
        return f"{correlation_id}.json"

    # -- durable write helpers ----------------------------------------------

    @staticmethod
    def _atomic_write(path: str, payload: dict[str, Any]) -> bool:
        """Write JSON atomically via temp file + fsync + rename."""
        temp_path = f"{path}.{os.getpid()}.tmp"
        try:
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"), sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.rename(temp_path, path)
            return True
        except (OSError, TypeError, ValueError):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            return False

    def _write_observation(self, observation: dict[str, Any]) -> bool:
        """Attempt the durable write of a final observation record."""
        try:
            os.makedirs(self.observations_dir, exist_ok=True)
        except OSError:
            return False

        filepath = os.path.join(self.observations_dir, self._record_name(observation))
        # Idempotent: an already-recorded observation is a success, not a rewrite.
        if os.path.exists(filepath):
            return True
        return self._atomic_write(filepath, observation)

    def _spool(self, observation: dict[str, Any], attempts: int) -> bool:
        """Persist a failed observation to the on-disk retry spool."""
        try:
            os.makedirs(self.spool_dir, exist_ok=True)
        except OSError:
            return False
        path = os.path.join(self.spool_dir, self._spool_name(observation))
        return self._atomic_write(path, {"attempts": attempts, "observation": observation})

    def _deadletter(self, observation: dict[str, Any], attempts: int) -> None:
        """Move an exhausted observation to the dead-letter directory."""
        try:
            os.makedirs(self.deadletter_dir, exist_ok=True)
        except OSError:
            return
        path = os.path.join(self.deadletter_dir, self._spool_name(observation))
        self._atomic_write(path, {"attempts": attempts, "observation": observation})

    def _unspool(self, observation: dict[str, Any]) -> None:
        """Remove a spool entry once its observation is durably recorded."""
        try:
            os.unlink(os.path.join(self.spool_dir, self._spool_name(observation)))
        except OSError:
            pass

    # -- public API ----------------------------------------------------------

    def emit(self, observation: dict[str, Any]) -> bool:
        """Emit an observation durably.

        Args:
            observation: Observation record from observe_call

        Returns:
            True if durably recorded, False if queued for retry.
        """
        scrubbed = scrub_value(observation)
        if not isinstance(scrubbed, dict):  # pragma: no cover - defensive
            return False

        if self._write_observation(scrubbed):
            self._unspool(scrubbed)
            # Drain any pending spool entries after a successful emission.
            if not getattr(self, "_in_retry", False):
                try:
                    self._in_retry = True
                    self.retry_pending()
                except Exception:  # pragma: no cover - retry is best-effort
                    pass
                finally:
                    self._in_retry = False
            return True

        # Failed: keep the backlog on disk so it survives a restart.
        if not self._spool(scrubbed, attempts=1):
            self._pending.append(scrubbed)
        return False

    def _load_spool(self) -> list[tuple[str, dict[str, Any], int]]:
        """Read spooled entries as (path, observation, attempts)."""
        try:
            names = sorted(os.listdir(self.spool_dir))
        except OSError:
            return []

        entries: list[tuple[str, dict[str, Any], int]] = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.spool_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)
            except (OSError, json.JSONDecodeError):
                self._quarantine_spool_file(path, name)
                continue
            observation = payload.get("observation") if isinstance(payload, dict) else None
            if not isinstance(observation, dict):
                self._quarantine_spool_file(path, name)
                continue
            attempts = payload.get("attempts")
            entries.append((path, observation, int(attempts) if isinstance(attempts, int) else 1))
        return entries

    def _quarantine_spool_file(self, path: str, name: str) -> None:
        """Move a corrupt spool file to dead-letter so it stops blocking the queue."""
        try:
            os.makedirs(self.deadletter_dir, exist_ok=True)
            os.replace(path, os.path.join(self.deadletter_dir, f"corrupt_{name}"))
        except OSError:
            pass

    def recover_pending(self) -> int:
        """Report the on-disk retry backlog left by a previous process.

        The spool is the source of truth, so recovery needs no in-memory carry-over:
        a sink constructed in a new process sees the same backlog. This method exists
        so callers can observe the backlog size without draining it.
        """
        return len(self._load_spool())

    def retry_pending(self) -> int:
        """Retry the durable spool and any in-memory fallback queue.

        Returns:
            Number of observations still pending after this pass. Dead-lettered
            observations are not counted as pending.
        """
        for path, observation, attempts in self._load_spool():
            if self._write_observation(observation):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            next_attempts = attempts + 1
            if next_attempts > self.max_attempts:
                self._deadletter(observation, attempts)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            self._spool(observation, attempts=next_attempts)

        memory_queue = self._pending
        self._pending = []
        for observation in memory_queue:
            self.emit(observation)

        return self.pending_count()

    def pending_count(self) -> int:
        """Count observations awaiting retry, on disk plus in memory."""
        return len(self._load_spool()) + len(self._pending)

    def deadletter_count(self) -> int:
        """Count observations that exhausted their retry budget."""
        try:
            return len([n for n in os.listdir(self.deadletter_dir) if n.endswith(".json")])
        except OSError:
            return 0


# Global sink instance for durable emission
_default_sink: ObservationSink | None = None


def get_sink() -> ObservationSink:
    """Get the default observation sink (created on first access)."""
    global _default_sink
    if _default_sink is None:
        _default_sink = ObservationSink()
    return _default_sink


def emit_observation(observation: dict[str, Any]) -> bool:
    """Emit an observation using the default sink.

    Args:
        observation: Observation record from observe_call

    Returns:
        True if durably recorded, False if queued for retry.
    """
    return get_sink().emit(observation)


def drain_spool() -> int:
    """Drain the toolplane observation retry spool using the default sink.

    Returns:
        Number of observations still pending after this drain pass.
    """
    return get_sink().retry_pending()


def pending_count() -> int:
    """Count toolplane observations currently pending in the spool."""
    return get_sink().pending_count()


def deadletter_count() -> int:
    """Count toolplane observations moved to the dead-letter queue."""
    return get_sink().deadletter_count()


__all__ = [
    "DEADLETTER_SUBDIR",
    "DEFAULT_MAX_ATTEMPTS",
    "DENIAL_ERRORS",
    "OBSERVATIONS_SUBDIR",
    "OBSERVE_VERSION",
    "SPOOL_SUBDIR",
    "ObservationSink",
    "deadletter_count",
    "drain_spool",
    "emit_observation",
    "get_sink",
    "observe_call",
    "pending_count",
]
