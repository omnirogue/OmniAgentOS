"""W0.5 provider-health gate for scheduled provider-dependent jobs.

A down provider produces a durable skip-with-receipt audit event. Five
consecutive failed consultations terminally park the job instead of letting a
scheduler retry forever. This is deliberately separate from spend caps: health
answers whether a provider is available; spend policy answers whether an
available paid call is affordable.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from omniagentos.contracts import Events, HealthStatus, default_db_path, new_id
from omniagentos.db.store import SqliteStore
from omniagentos.runtime_paths import resolve_var_root

MAX_CONSECUTIVE_FAILURES = 5
ACTION_PREFIX = "provider_health."
ACTION_AVAILABLE = f"{ACTION_PREFIX}available"
ACTION_SKIP = f"{ACTION_PREFIX}skip_with_receipt"
ACTION_TERMINAL = f"{ACTION_PREFIX}terminally_parked"

HealthCheck = Callable[[], HealthStatus | bool]
HealthAction = Literal["allow", "skip_with_receipt", "terminally_parked"]


@dataclass(frozen=True, slots=True)
class ProviderHealthDecision:
    action: HealthAction
    provider: str
    job_id: str
    consecutive_failures: int
    receipt_id: str
    detail: str

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


class ProviderHealthGateError(RuntimeError):
    """The health gate could not produce its required durable receipt."""


class ProviderHealthGate:
    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or default_db_path()
        self._store: SqliteStore | None = None
        self._lock = RLock()

    def _ledger(self) -> SqliteStore:
        with self._lock:
            if self._store is None:
                self._store = SqliteStore(self.db_path)
            return self._store

    @staticmethod
    def _target(job_id: str, provider: str) -> str:
        return f"{job_id}:{provider}"

    def _failure_history(
        self, store: SqliteStore, *, job_id: str, provider: str
    ) -> tuple[int, dict[str, Any] | None]:
        rows = store.audit_events_for_target(
            target_type="scheduled_provider_job",
            target_id=self._target(job_id, provider),
            action_prefix=ACTION_PREFIX,
            limit=MAX_CONSECUTIVE_FAILURES,
        )
        terminal = rows[0] if rows and rows[0]["action"] == ACTION_TERMINAL else None
        count = 0
        for row in rows:
            if row["action"] == ACTION_AVAILABLE:
                break
            if row["action"] in {ACTION_SKIP, ACTION_TERMINAL}:
                count += 1
        return count, terminal

    def consult(
        self,
        *,
        job_id: str,
        provider: str,
        health_check: HealthCheck,
    ) -> ProviderHealthDecision:
        """Consult provider health and persist the decision as an audit receipt."""

        try:
            store = self._ledger()
            previous, terminal = self._failure_history(store, job_id=job_id, provider=provider)
            if terminal is not None:
                payload = json.loads(str(terminal["payload_json"]))
                return ProviderHealthDecision(
                    action="terminally_parked",
                    provider=provider,
                    job_id=job_id,
                    consecutive_failures=int(
                        payload.get("consecutive_failures") or MAX_CONSECUTIVE_FAILURES
                    ),
                    receipt_id=str(payload["receipt_id"]),
                    detail=str(payload.get("detail") or "terminal provider-health park"),
                )
        except Exception as exc:
            raise ProviderHealthGateError(
                "provider health could not be read/receipted; scheduled call is refused: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        receipt_id = new_id("provider_health_receipt")
        try:
            measured = health_check()
            if isinstance(measured, HealthStatus):
                healthy = measured.healthy
                detail = measured.detail or ("healthy" if healthy else "unhealthy")
            elif isinstance(measured, bool):
                healthy = measured
                detail = "healthy" if healthy else "unhealthy"
            else:
                healthy = False
                detail = f"invalid health result: {type(measured).__name__}"
        except Exception as exc:  # noqa: BLE001 - inability to check is down, not up
            healthy = False
            detail = f"health check failed: {type(exc).__name__}: {exc}"

        try:
            failures = 0 if healthy else previous + 1
            if healthy:
                action = ACTION_AVAILABLE
                decision: HealthAction = "allow"
            elif failures >= MAX_CONSECUTIVE_FAILURES:
                action = ACTION_TERMINAL
                decision = "terminally_parked"
            else:
                action = ACTION_SKIP
                decision = "skip_with_receipt"
            store.insert_event(
                Events.AUDIT,
                actor="scheduler:provider-health-gate",
                action=action,
                target_type="scheduled_provider_job",
                target_id=self._target(job_id, provider),
                payload={
                    "schema": "omniagentos.provider-health-receipt.v0",
                    "receipt_id": receipt_id,
                    "job_id": job_id,
                    "provider": provider,
                    "decision": decision,
                    "consecutive_failures": failures,
                    "max_failures": MAX_CONSECUTIVE_FAILURES,
                    "detail": detail,
                },
            )
        except Exception as exc:
            raise ProviderHealthGateError(
                "provider health could not be read/receipted; scheduled call is refused: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        return ProviderHealthDecision(
            action=decision,
            provider=provider,
            job_id=job_id,
            consecutive_failures=failures,
            receipt_id=receipt_id,
            detail=detail,
        )

    def close(self) -> None:
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None


def snapshot_health(
    provider: str,
    *,
    path: str | Path | None = None,
) -> HealthStatus:
    """Read the provider-sentinel snapshot without probing a paid endpoint."""

    # Package anchor (env_keys=(), environ={}): must read where the sentinel
    # writes — file-anchored <repo>/var — not the launcher's runtime var
    # (review B1); same resolution as swarm/router's default.
    snapshot = (
        Path(path)
        if path
        else resolve_var_root(env_keys=(), environ={}, leaf=("provider-health.json",))
    )
    raw = json.loads(snapshot.read_text(encoding="utf-8"))
    results = raw.get("results") if isinstance(raw, dict) else None
    row: Any = results.get(provider) if isinstance(results, dict) else None
    if not isinstance(row, dict) or not isinstance(row.get("ok"), bool):
        return HealthStatus(healthy=False, detail=f"snapshot has no valid {provider!r} row")
    detail = str(row.get("error") or row.get("outcome") or "snapshot")
    return HealthStatus(healthy=bool(row["ok"]), detail=detail)


_DEFAULT_GATE: ProviderHealthGate | None = None
_DEFAULT_LOCK = RLock()


def default_provider_health_gate() -> ProviderHealthGate:
    global _DEFAULT_GATE
    with _DEFAULT_LOCK:
        if _DEFAULT_GATE is None:
            _DEFAULT_GATE = ProviderHealthGate()
        return _DEFAULT_GATE


__all__ = [
    "MAX_CONSECUTIVE_FAILURES",
    "ProviderHealthDecision",
    "ProviderHealthGate",
    "ProviderHealthGateError",
    "default_provider_health_gate",
    "snapshot_health",
]
