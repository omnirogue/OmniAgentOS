"""PostMergeObserver — did the claimed benefit actually materialise? (PLAN §13).

An improvement is merged on the strength of a *claim*. This module records what
actually happened afterwards, at fixed 24h / 7d / 30d checkpoints, so a claim
that never paid off is visible instead of assumed.

L17 compliance:
- No fabricated success. An observation is written only when a real measurement
  exists. A window that is not yet due, or that has no measurement, is reported
  as *deferred* and NOTHING is persisted -- so the checkpoint stays open for a
  later real measurement instead of being consumed by a placeholder.
- Validated windows. Only 24h, 7d and 30d are accepted; anything else is a
  permanent (non-retryable) error rather than a silently stored typo.
- Idempotent. Each (improvement_id, window) pair is observed at most once.
- Durable bounded retry. A failed write is spooled to disk so the backlog
  survives process restart, and each entry carries an attempt counter so it is
  eventually dead-lettered rather than retried forever or dropped.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# The only valid observation windows, and how far after apply each one falls due.
WINDOW_DELTAS: dict[str, timedelta] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}
# Bounded window limits: (open_after, closes_at).
# A checkpoint is only open until the next window falls due, preventing post-window backfill.
WINDOW_BOUNDS: dict[str, tuple[timedelta, timedelta]] = {
    "24h": (timedelta(hours=24), timedelta(days=7)),
    "7d": (timedelta(days=7), timedelta(days=30)),
    "30d": (timedelta(days=30), timedelta(days=60)),
}
WINDOWS: tuple[str, ...] = ("24h", "7d", "30d")

# Values that mean "no measurement yet". These may never be persisted as an
# observed result -- doing so would fabricate a data point and permanently
# consume the (improvement_id, window) idempotency key.
_NON_MEASUREMENTS = frozenset({"", "pending", "unknown", "n/a", "none", "tbd"})

OBSERVATIONS_SUBDIR = "csi-observations"
SPOOL_SUBDIR = "csi-observations-pending"
DEADLETTER_SUBDIR = "csi-observations-failed"

DEFAULT_MAX_ATTEMPTS = 5

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.+-]")


@dataclass
class ObservationResult:
    """A recorded post-merge benefit observation backed by a real measurement."""

    improvement_id: str
    window: str  # 24h | 7d | 30d
    claimed_metric: str
    baseline: str
    observed: str
    materialised: bool | None  # None = measured but inconclusive
    notes: str = ""
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    ts: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class ObservationError(Exception):
    """Base exception for observation failures.

    ``retryable=False`` marks a permanent error (bad input, corrupt record) that
    must not be requeued; ``retryable=True`` marks a transient I/O failure.
    """

    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


def validate_window(window: str) -> str:
    """Return the window if it is one of 24h/7d/30d, else raise permanently."""
    if window not in WINDOW_DELTAS:
        raise ObservationError(
            f"invalid observation window {window!r}; expected one of {', '.join(WINDOWS)}",
            retryable=False,
        )
    return window


def _parse_ts(value: str) -> datetime:
    """Parse an ISO timestamp, treating a naive value as UTC."""
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ObservationError(f"unparseable timestamp {value!r}", retryable=False) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _is_measurement(observed: Any) -> bool:
    """True only when ``observed`` is a real, non-placeholder measurement."""
    return isinstance(observed, str) and observed.strip().lower() not in _NON_MEASUREMENTS


def _safe_component(value: str, *, fallback: str) -> str:
    cleaned = _UNSAFE_NAME.sub("-", value).strip("-")
    return cleaned[:80] if cleaned else fallback


class PostMergeObserver:
    """Record observation checkpoints with idempotency and durable bounded retry."""

    def __init__(
        self,
        ledger_dir: str | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        """Initialize observer with ledger directory.

        Args:
            ledger_dir: Directory for observation storage. Defaults to
                        OMNIAGENTOS_LEDGER_DIR or var/ledger.
            max_attempts: Retries per observation before dead-lettering.
        """
        if ledger_dir is None:
            ledger_dir = os.environ.get("OMNIAGENTOS_LEDGER_DIR", "var/ledger")
        self.ledger_dir = ledger_dir
        self.max_attempts = max(1, int(max_attempts))

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

    def _obs_key(self, improvement_id: str, window: str) -> str:
        """Idempotency key for an observation."""
        return _safe_component(f"{improvement_id}_{window}", fallback="unknown-observation")

    def _obs_path(self, improvement_id: str, window: str) -> str:
        return os.path.join(self.observations_dir, f"{self._obs_key(improvement_id, window)}.json")

    def is_observed(self, improvement_id: str, window: str) -> bool:
        """True if this (improvement_id, window) checkpoint is already recorded."""
        return os.path.exists(self._obs_path(improvement_id, window))

    # -- window planning -----------------------------------------------------

    def plan_windows(self, applied_at: datetime | None = None) -> list[dict[str, Any]]:
        """Plan the 24h/7d/30d observation checkpoints for a merged improvement."""
        start = applied_at or datetime.now(UTC)
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        return [
            {"window": window, "due_at": (start + WINDOW_DELTAS[window]).isoformat()}
            for window in WINDOWS
        ]

    def due_windows(self, applied_at: str | datetime, now: datetime | None = None) -> list[str]:
        """Return the windows whose checkpoint is currently open/due as of ``now``.

        A checkpoint is open only until the next window falls due (24h closes at 7d,
        7d closes at 30d, 30d closes at 60d). Missed earlier checkpoints are closed
        and will not be returned, preventing post-window terminal backfill.
        """
        start = _parse_ts(applied_at) if isinstance(applied_at, str) else applied_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)

        open_windows: list[str] = []
        for w in WINDOWS:
            open_after, closes_at = WINDOW_BOUNDS[w]
            if start + open_after <= current < start + closes_at:
                open_windows.append(w)
        return open_windows

    # -- observation ---------------------------------------------------------

    def observe(
        self,
        *,
        improvement_id: str,
        window: str,
        baseline: str,
        claimed_metric: str,
        observed: str,
        materialised: bool | None,
        notes: str = "",
    ) -> ObservationResult:
        """Record a benefit observation backed by a real measurement.

        Args:
            improvement_id: The improvement being observed
            window: Observation window; must be 24h, 7d or 30d
            baseline: Baseline metric value before the improvement
            claimed_metric: What benefit was claimed
            observed: The actual measured outcome. A placeholder such as
                      "pending" is rejected -- absence of data is not a result.
            materialised: True/False when the measurement decides it, None when
                          the measurement exists but is genuinely inconclusive.
            notes: Additional notes

        Returns:
            The recorded ObservationResult, or the existing one if this
            checkpoint was already observed.

        Raises:
            ObservationError: On an invalid window, a missing measurement
                (both permanent), or a failed write (retryable).
        """
        validate_window(window)
        if not improvement_id:
            raise ObservationError("improvement_id is required", retryable=False)
        if not _is_measurement(observed):
            raise ObservationError(
                f"refusing to record observation for {improvement_id}/{window}: "
                f"{observed!r} is not a measurement",
                retryable=False,
            )

        # Idempotency: an already-recorded checkpoint is returned, not rewritten.
        if self.is_observed(improvement_id, window):
            return self._load_observation(improvement_id, window)

        result = ObservationResult(
            improvement_id=improvement_id,
            window=window,
            claimed_metric=claimed_metric,
            baseline=baseline,
            observed=observed,
            materialised=materialised,
            notes=notes,
        )
        self._persist_observation(result)
        return result

    # -- durable storage -----------------------------------------------------

    @staticmethod
    def _atomic_write(path: str, payload: dict[str, Any]) -> bool:
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

    def _try_write(self, result: ObservationResult) -> bool:
        try:
            os.makedirs(self.observations_dir, exist_ok=True)
        except OSError:
            return False
        path = self._obs_path(result.improvement_id, result.window)
        if os.path.exists(path):
            return True
        return self._atomic_write(path, asdict(result))

    def _persist_observation(self, result: ObservationResult) -> None:
        """Persist durably; on failure spool to disk and raise retryable."""
        if self._try_write(result):
            self._unspool(result.improvement_id, result.window)
            return
        self._spool(result, attempts=1)
        raise ObservationError(
            f"cannot write observation {result.improvement_id}/{result.window}", retryable=True
        )

    def _load_observation(self, improvement_id: str, window: str) -> ObservationResult:
        """Load an existing observation from storage."""
        path = self._obs_path(improvement_id, window)
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return ObservationResult(**data)
        except (OSError, json.JSONDecodeError, TypeError) as e:
            raise ObservationError(f"cannot load observation: {e}", retryable=False) from e

    def _spool_path(self, improvement_id: str, window: str) -> str:
        return os.path.join(self.spool_dir, f"{self._obs_key(improvement_id, window)}.json")

    def _spool(self, result: ObservationResult, attempts: int) -> bool:
        """Persist a failed observation to the on-disk retry spool."""
        try:
            os.makedirs(self.spool_dir, exist_ok=True)
        except OSError:
            return False
        return self._atomic_write(
            self._spool_path(result.improvement_id, result.window),
            {"attempts": attempts, "observation": asdict(result)},
        )

    def _unspool(self, improvement_id: str, window: str) -> None:
        try:
            os.unlink(self._spool_path(improvement_id, window))
        except OSError:
            pass

    def _deadletter(self, result: ObservationResult, attempts: int, name: str) -> None:
        try:
            os.makedirs(self.deadletter_dir, exist_ok=True)
        except OSError:
            return
        self._atomic_write(
            os.path.join(self.deadletter_dir, name),
            {"attempts": attempts, "observation": asdict(result)},
        )

    def _load_spool(self) -> list[tuple[str, ObservationResult, int]]:
        """Read spooled entries as (path, result, attempts)."""
        try:
            names = sorted(os.listdir(self.spool_dir))
        except OSError:
            return []

        entries: list[tuple[str, ObservationResult, int]] = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.spool_dir, name)
            try:
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)
                result = ObservationResult(**payload["observation"])
            except (OSError, json.JSONDecodeError, KeyError, TypeError):
                self._quarantine(path, name)
                continue
            attempts = payload.get("attempts")
            entries.append((path, result, int(attempts) if isinstance(attempts, int) else 1))
        return entries

    def _quarantine(self, path: str, name: str) -> None:
        """Move a corrupt spool entry aside so it stops blocking the queue."""
        try:
            os.makedirs(self.deadletter_dir, exist_ok=True)
            os.replace(path, os.path.join(self.deadletter_dir, f"corrupt_{name}"))
        except OSError:
            pass

    def recover_pending(self) -> int:
        """Report the on-disk retry backlog left by a previous process.

        The spool is the source of truth, so a fresh observer in a fresh process
        sees exactly the backlog its predecessor left behind.
        """
        return len(self._load_spool())

    def retry_pending(self) -> tuple[int, int]:
        """Retry the durable spool, bounded by ``max_attempts``.

        Returns:
            (succeeded, still_pending). Dead-lettered entries count as neither.
        """
        succeeded = 0
        for path, result, attempts in self._load_spool():
            if self._try_write(result):
                succeeded += 1
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            next_attempts = attempts + 1
            if next_attempts > self.max_attempts:
                self._deadletter(result, attempts, os.path.basename(path))
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            self._spool(result, attempts=next_attempts)
        return succeeded, self.get_pending_count()

    def get_pending_count(self) -> int:
        """Count observations awaiting retry on disk."""
        return len(self._load_spool())

    def deadletter_count(self) -> int:
        """Count observations that exhausted their retry budget."""
        try:
            return len([n for n in os.listdir(self.deadletter_dir) if n.endswith(".json")])
        except OSError:
            return 0


def sweep_post_merge(
    records: Iterable[Mapping[str, Any]],
    *,
    now: datetime | None = None,
    observer: PostMergeObserver | None = None,
) -> dict[str, Any]:
    """Observe every due checkpoint for a batch of merged improvements.

    Each record is a plain mapping so this module stays free of any dependency
    on the reliability store; the caller assembles the measurement.

    Required per record: ``improvement_id``, ``applied_at``. Optional:
    ``claimed_metric``, ``baseline``, ``observed``, ``materialised``, ``notes``.

    A window is only observed when it has come due AND the record carries a real
    measurement. Otherwise it is counted as ``deferred`` and nothing is written,
    leaving the checkpoint open for a later real measurement.

    Returns:
        {"observed": int, "deferred": int, "already": int, "errors": [...]}
    """
    obs = observer or get_observer()
    current = now or datetime.now(UTC)

    observed_count = 0
    deferred = 0
    already = 0
    errors: list[dict[str, str]] = []

    for record in records:
        improvement_id = str(record.get("improvement_id") or "")
        applied_at = record.get("applied_at")
        if not improvement_id or not isinstance(applied_at, str) or not applied_at:
            # Not yet merged, or unidentifiable: nothing honest to observe.
            deferred += 1
            continue

        try:
            due = obs.due_windows(applied_at, current)
        except ObservationError as exc:
            errors.append({"improvement_id": improvement_id, "error": str(exc)})
            continue

        measurement = record.get("observed")
        for window in WINDOWS:
            if obs.is_observed(improvement_id, window):
                already += 1
                continue
            if window not in due:
                deferred += 1
                continue
            if not _is_measurement(measurement):
                # Due, but no data. Recording a placeholder here would consume
                # the checkpoint and fabricate a result; leave it open instead.
                deferred += 1
                continue
            try:
                obs.observe(
                    improvement_id=improvement_id,
                    window=window,
                    baseline=str(record.get("baseline") or "unrecorded"),
                    claimed_metric=str(record.get("claimed_metric") or "unrecorded"),
                    observed=str(measurement),
                    materialised=record.get("materialised"),
                    notes=str(record.get("notes") or ""),
                )
                observed_count += 1
            except ObservationError as exc:
                errors.append(
                    {"improvement_id": improvement_id, "window": window, "error": str(exc)}
                )

    return {
        "observed": observed_count,
        "deferred": deferred,
        "already": already,
        "errors": errors,
    }


# Global observer instance
_default_observer: PostMergeObserver | None = None


def get_observer() -> PostMergeObserver:
    """Get the default post-merge observer (created on first access)."""
    global _default_observer
    if _default_observer is None:
        _default_observer = PostMergeObserver()
    return _default_observer


__all__ = [
    "DEADLETTER_SUBDIR",
    "DEFAULT_MAX_ATTEMPTS",
    "OBSERVATIONS_SUBDIR",
    "SPOOL_SUBDIR",
    "WINDOWS",
    "WINDOW_BOUNDS",
    "WINDOW_DELTAS",
    "ObservationError",
    "ObservationResult",
    "PostMergeObserver",
    "get_observer",
    "sweep_post_merge",
    "validate_window",
]
