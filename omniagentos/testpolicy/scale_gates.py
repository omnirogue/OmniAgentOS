"""Bounded scale and backpressure gates (M-23).

In-process, token-free certification that capacity admits succeed up to a
configured limit and further admits are rejected (backpressure), including
100+ session and 10-project contention shapes.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from omniagentos.testpolicy.policy_load import load_policy_dict


@dataclass(frozen=True)
class BackpressureDecision:
    admitted: bool
    reason: str
    active: int
    capacity: int


@dataclass
class SessionPool:
    """Minimal session-capacity pool with backpressure."""

    capacity: int
    _active: int = 0
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._active = 0

    def try_admit(self, session_id: str) -> BackpressureDecision:
        del session_id  # identity only matters to callers
        with self._lock:
            if self._active >= self.capacity:
                return BackpressureDecision(
                    admitted=False,
                    reason="backpressure_capacity",
                    active=self._active,
                    capacity=self.capacity,
                )
            self._active += 1
            return BackpressureDecision(
                admitted=True,
                reason="admitted",
                active=self._active,
                capacity=self.capacity,
            )

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1

    @property
    def active(self) -> int:
        with self._lock:
            return self._active


@dataclass
class ProjectContentionGate:
    """One writer slot per project — excess writers are rejected (backpressure)."""

    writer_slots: int = 1
    _held: dict[str, int] | None = None
    _lock: threading.Lock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._held = {}
        self._lock = threading.Lock()

    def try_acquire(self, project_id: str) -> BackpressureDecision:
        with self._lock:
            assert self._held is not None
            current = self._held.get(project_id, 0)
            if current >= self.writer_slots:
                return BackpressureDecision(
                    admitted=False,
                    reason="project_writer_backpressure",
                    active=current,
                    capacity=self.writer_slots,
                )
            self._held[project_id] = current + 1
            return BackpressureDecision(
                admitted=True,
                reason="project_writer_acquired",
                active=current + 1,
                capacity=self.writer_slots,
            )

    def release(self, project_id: str) -> None:
        with self._lock:
            assert self._held is not None
            current = self._held.get(project_id, 0)
            if current <= 1:
                self._held.pop(project_id, None)
            else:
                self._held[project_id] = current - 1


@dataclass(frozen=True)
class ScaleGateResult:
    ok: bool
    sessions_requested: int
    sessions_admitted: int
    sessions_rejected: int
    projects: int
    project_exclusive_ok: bool
    elapsed_seconds: float
    reasons: tuple[str, ...]
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "sessions_requested": self.sessions_requested,
            "sessions_admitted": self.sessions_admitted,
            "sessions_rejected": self.sessions_rejected,
            "projects": self.projects,
            "project_exclusive_ok": self.project_exclusive_ok,
            "elapsed_seconds": self.elapsed_seconds,
            "reasons": list(self.reasons),
            "details": self.details,
        }


def run_bounded_scale_gate(
    *,
    min_sessions: int | None = None,
    min_projects: int | None = None,
    admit_capacity: int | None = None,
    project_writer_slots: int | None = None,
    max_gate_seconds: float | None = None,
) -> ScaleGateResult:
    """Run the focused scale/backpressure gate and return a structured result."""
    cfg = load_policy_dict().get("scale") or {}
    sessions_n = int(min_sessions if min_sessions is not None else cfg.get("min_sessions", 100))
    projects_n = int(min_projects if min_projects is not None else cfg.get("min_projects", 10))
    capacity = int(admit_capacity if admit_capacity is not None else cfg.get("admit_capacity", 32))
    writer_slots = int(
        project_writer_slots
        if project_writer_slots is not None
        else cfg.get("project_writer_slots", 1)
    )
    deadline = float(
        max_gate_seconds if max_gate_seconds is not None else cfg.get("max_gate_seconds", 30)
    )
    reject_over = bool(cfg.get("reject_when_over_capacity", True))

    started = time.monotonic()
    reasons: list[str] = []
    pool = SessionPool(capacity=capacity)

    # Phase 1: fill to capacity — all must admit.
    admitted_fill = 0
    for i in range(capacity):
        decision = pool.try_admit(f"ses_fill_{i}")
        if not decision.admitted:
            reasons.append(f"capacity fill failed at i={i}: {decision.reason}")
            break
        admitted_fill += 1
    if admitted_fill != capacity:
        reasons.append(f"expected {capacity} admits during fill, got {admitted_fill}")

    # Phase 2: over-capacity requests must be rejected (backpressure).
    rejected = 0
    overflow = max(sessions_n - capacity, capacity)  # ensure overflow pressure
    for i in range(overflow):
        decision = pool.try_admit(f"ses_over_{i}")
        if decision.admitted:
            if reject_over:
                reasons.append(f"over-capacity admit unexpectedly granted at i={i}")
            pool.release()
        else:
            rejected += 1
            if decision.reason != "backpressure_capacity":
                reasons.append(f"unexpected reject reason: {decision.reason}")

    if reject_over and rejected < overflow:
        reasons.append(f"expected {overflow} backpressure rejects, got {rejected}")

    # Release fill admits.
    for _ in range(admitted_fill):
        pool.release()

    # Phase 3: 100+ session churn under capacity (admit/release), proving scale shape.
    churn_admitted = 0
    churn_rejected = 0
    workers = min(32, max(8, capacity))
    barrier = threading.Barrier(workers)

    def churn_worker(worker_id: int) -> tuple[int, int]:
        local_ok = 0
        local_rej = 0
        barrier.wait(timeout=5)
        # Each worker drives enough attempts so total >= min_sessions.
        attempts = max(1, (sessions_n + workers - 1) // workers)
        for j in range(attempts):
            decision = pool.try_admit(f"ses_w{worker_id}_{j}")
            if decision.admitted:
                local_ok += 1
                # Simulate brief session work then release so others can admit.
                pool.release()
            else:
                local_rej += 1
        return local_ok, local_rej

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(churn_worker, i) for i in range(workers)]
        for fut in as_completed(futures):
            ok_n, rej_n = fut.result(timeout=deadline)
            churn_admitted += ok_n
            churn_rejected += rej_n

    if churn_admitted + churn_rejected < sessions_n:
        reasons.append(
            f"session attempts {churn_admitted + churn_rejected} < min_sessions={sessions_n}"
        )
    if churn_admitted <= 0:
        reasons.append("no sessions admitted during churn")

    # Phase 4: 10-project exclusive writer contention.
    # Holders acquire and wait; contenders must see backpressure while held.
    projects = [f"proj_{i}" for i in range(projects_n)]
    project_gate = ProjectContentionGate(writer_slots=writer_slots)
    project_exclusive_ok = True
    project_rejects = 0
    project_admits = 0
    release_holders = threading.Event()
    holders_ready = threading.Barrier(projects_n + 1)

    def holder(project_id: str) -> BackpressureDecision:
        decision = project_gate.try_acquire(project_id)
        if decision.admitted:
            try:
                holders_ready.wait(timeout=5)
                release_holders.wait(timeout=5)
            finally:
                project_gate.release(project_id)
        else:
            try:
                holders_ready.wait(timeout=5)
            except Exception:
                pass
        return decision

    def contender(project_id: str) -> BackpressureDecision:
        decision = project_gate.try_acquire(project_id)
        if decision.admitted:
            project_gate.release(project_id)
        return decision

    with ThreadPoolExecutor(max_workers=projects_n * 2) as executor:
        holder_futs = [executor.submit(holder, pid) for pid in projects]
        try:
            holders_ready.wait(timeout=5)
        except Exception as exc:  # noqa: BLE001 — surface as gate failure
            project_exclusive_ok = False
            reasons.append(f"project holders failed to synchronize: {exc}")
        contender_futs = [executor.submit(contender, pid) for pid in projects]
        contender_results = [f.result(timeout=5) for f in contender_futs]
        release_holders.set()
        holder_results = [f.result(timeout=5) for f in holder_futs]

    for decision in holder_results:
        if decision.admitted:
            project_admits += 1
        else:
            project_exclusive_ok = False
            reasons.append("project holder failed to acquire exclusive slot")
    for decision in contender_results:
        if decision.admitted:
            project_exclusive_ok = False
            reasons.append("second writer admitted while first held project slot")
        else:
            project_rejects += 1

    if project_rejects < projects_n:
        project_exclusive_ok = False
        reasons.append(f"expected {projects_n} project backpressure rejects, got {project_rejects}")

    elapsed = time.monotonic() - started
    if elapsed > deadline:
        reasons.append(f"gate exceeded max_gate_seconds={deadline}: {elapsed:.2f}s")

    sessions_requested = capacity + overflow + churn_admitted + churn_rejected
    ok = not reasons
    return ScaleGateResult(
        ok=ok,
        sessions_requested=sessions_requested,
        sessions_admitted=admitted_fill + churn_admitted,
        sessions_rejected=rejected + churn_rejected,
        projects=projects_n,
        project_exclusive_ok=project_exclusive_ok,
        elapsed_seconds=elapsed,
        reasons=tuple(reasons),
        details={
            "capacity": capacity,
            "fill_admitted": admitted_fill,
            "overflow_rejected": rejected,
            "churn_admitted": churn_admitted,
            "churn_rejected": churn_rejected,
            "project_admits": project_admits,
            "project_rejects": project_rejects,
            "writer_slots": writer_slots,
        },
    )
