"""In-memory Store implementation shared by API tests.

It intentionally enforces the same guarded state writes as SQLite.  Keeping it
in tests prevents an alternate persistence implementation from leaking into the
application package.
"""

from __future__ import annotations

import json
from typing import Any

from omniagentos.contracts import (
    RUN_TRANSITIONS,
    TASK_TRANSITIONS,
    ApprovalState,
    RunState,
    TaskState,
    utc_now_iso,
)


class FakeStore:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.disciplines: dict[str, dict[str, Any]] = {
            "research-briefs": {
                "id": "research-briefs",
                "name": "Research briefs",
                "metric_contract": "{}",
                "status": "active",
                "created_at": "1970-01-01T00:00:00Z",
            },
            "code-changes": {
                "id": "code-changes",
                "name": "Bounded code changes",
                "metric_contract": "{}",
                "status": "active",
                "created_at": "1970-01-01T00:00:00Z",
            },
        }
        self.steps: dict[tuple[str, int], dict[str, Any]] = {}
        self.idempotency: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.approvals: dict[str, dict[str, Any]] = {}
        self.artifacts: dict[str, dict[str, Any]] = {}
        self.budgets: dict[str, dict[str, Any]] = {}
        self.heartbeats: dict[str, dict[str, Any]] = {}
        self.pause = {"id": 1, "paused": 0, "reason": "", "updated_at": "1970-01-01T00:00:00Z"}
        self._event_id = 0
        self._step_id = 0

    @staticmethod
    def _copy(row: dict[str, Any] | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def enqueue_run(self, row: dict[str, Any]) -> None:
        self.runs[row["id"]] = dict(row)

    def claim_next_run(self, worker_id: str) -> dict[str, Any] | None:
        candidates = [row for row in self.runs.values() if row["state"] == RunState.QUEUED.value]
        if not candidates:
            return None
        run = min(candidates, key=lambda row: row["queued_at"])
        run.update(
            {
                "state": RunState.RUNNING.value,
                "worker_id": worker_id,
                "started_at": run.get("started_at") or utc_now_iso(),
                "updated_at": utc_now_iso(),
            }
        )
        return self._copy(run)

    def reclaim_stale_runs(self, worker_id: str, stale_s: int) -> list[dict[str, Any]]:
        del worker_id, stale_s
        return []

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._copy(self.runs.get(run_id))

    def update_run(
        self, run_id: str, fields: dict[str, Any], expect_worker: str | None = None
    ) -> bool:
        row = self.runs.get(run_id)
        if row is None or (expect_worker is not None and row.get("worker_id") != expect_worker):
            return False
        if "state" in fields:
            current, target = RunState(row["state"]), RunState(fields["state"])
            if target != current and target not in RUN_TRANSITIONS[current]:
                return False
        row.update(fields)
        row["updated_at"] = utc_now_iso()
        return True

    def list_runs(self, filters: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.runs.values()
            if all(row.get(key) == value for key, value in filters.items())
        ]
        return [
            dict(row)
            for row in sorted(rows, key=lambda row: row.get("queued_at", ""), reverse=True)[:limit]
        ]

    def request_cancel(self, run_id: str) -> bool:
        row = self.runs.get(run_id)
        if row is None:
            return False
        row["cancel_requested"] = 1
        row["updated_at"] = utc_now_iso()
        return True

    def requeue_paused_runs(self) -> list[str]:
        changed: list[str] = []
        for row in self.runs.values():
            if row["state"] == RunState.PAUSED.value:
                row["state"] = RunState.QUEUED.value
                changed.append(row["id"])
        return changed

    def upsert_step(
        self, run_id: str, seq: int, fields: dict[str, Any], expect_worker: str | None = None
    ) -> bool:
        run = self.runs.get(run_id)
        if run is None or (expect_worker is not None and run.get("worker_id") != expect_worker):
            return False
        key = (run_id, seq)
        if key not in self.steps:
            self._step_id += 1
            self.steps[key] = {
                "id": self._step_id,
                "run_id": run_id,
                "seq": seq,
                "name": "",
                "action_class": "sandboxed_creation",
                "status": "pending",
                "checkpoint_json": "{}",
                "result_json": None,
                "error": None,
                "idempotency_key": None,
                "started_at": None,
                "finished_at": None,
            }
        self.steps[key].update(fields)
        return True

    def get_steps(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for (candidate, _), row in sorted(self.steps.items(), key=lambda item: item[0][1])
            if candidate == run_id
        ]

    def idem_insert(self, key: str, run_id: str, step_name: str) -> bool:
        if key in self.idempotency:
            return False
        self.idempotency[key] = {
            "key": key,
            "run_id": run_id,
            "step_name": step_name,
            "result_json": None,
            "created_at": utc_now_iso(),
            "completed_at": None,
        }
        return True

    def idem_get(self, key: str) -> dict[str, Any] | None:
        return self._copy(self.idempotency.get(key))

    def idem_complete(self, key: str, result_json: str) -> None:
        row = self.idempotency[key]
        row.update({"result_json": result_json, "completed_at": utc_now_iso()})

    def idem_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.idempotency.values() if row["run_id"] == run_id]

    def create_task(self, row: dict[str, Any]) -> None:
        self.tasks[row["id"]] = dict(row)

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        return self._copy(self.tasks.get(task_id))

    def update_task_state(self, task_id: str, target: str, expect: list[str] | None = None) -> bool:
        row = self.tasks.get(task_id)
        if row is None or (expect is not None and row["state"] not in expect):
            return False
        current, desired = TaskState(row["state"]), TaskState(target)
        if desired != current and desired not in TASK_TRANSITIONS[current]:
            return False
        row.update({"state": target, "updated_at": utc_now_iso()})
        return True

    def list_tasks(self, filters: dict[str, Any], limit: int = 100) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.tasks.values()
            if all(row.get(key) == value for key, value in filters.items())
        ]
        return [
            dict(row)
            for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)[:limit]
        ]

    def insert_event(
        self,
        type: str,
        actor: str,
        action: str,
        target_type: str = "",
        target_id: str = "",
        payload: dict[str, Any] | None = None,
        trace_id: str = "",
        execution_id: str = "",
    ) -> int:
        self._event_id += 1
        self.events.append(
            {
                "id": self._event_id,
                "ts": utc_now_iso(),
                "type": type,
                "actor": actor,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "payload_json": json.dumps(payload or {}),
                "trace_id": trace_id,
                "execution_id": execution_id or None,
            }
        )
        return self._event_id

    def get_events_after(
        self, after_id: int, types: list[str] | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.events
            if row["id"] > after_id and (types is None or row["type"] in types)
        ]
        return [dict(row) for row in rows[-limit:]]

    def get_events_for_run(self, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = [
            row for row in self.events if row["target_type"] == "run" and row["target_id"] == run_id
        ]
        return [dict(row) for row in rows[-limit:]]

    def get_events_for_target(
        self, target_type: str, target_id: str, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        rows = [
            row
            for row in self.events
            if row["target_type"] == target_type
            and row["target_id"] == target_id
            and row["id"] > after_id
        ]
        return [dict(row) for row in rows[:limit]]

    def latest_event_id(self) -> int:
        return self._event_id

    def create_approval(self, row: dict[str, Any]) -> None:
        self.approvals[row["id"]] = dict(row)

    def get_approval_for(self, run_id: str, step_seq: int | None) -> dict[str, Any] | None:
        return next(
            (
                dict(row)
                for row in self.approvals.values()
                if row["run_id"] == run_id and row.get("step_seq") == step_seq
            ),
            None,
        )

    def decide_approval(
        self, approval_id: str, state: str, decided_by: str, note: str | None = None
    ) -> bool:
        row = self.approvals.get(approval_id)
        if row is None or row["state"] != ApprovalState.PENDING.value:
            return False
        row.update(
            {
                "state": state,
                "decided_by": decided_by,
                "decision_note": note,
                "decided_at": utc_now_iso(),
            }
        )
        return True

    def void_pending_approvals(self, run_id: str, note: str) -> int:
        changed = 0
        for row in self.approvals.values():
            if row.get("run_id") == run_id and row["state"] == ApprovalState.PENDING.value:
                row.update(
                    {
                        "state": ApprovalState.EXPIRED.value,
                        "decision_note": note,
                        "decided_at": utc_now_iso(),
                    }
                )
                changed += 1
        return changed

    def list_approvals(self, state: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = [row for row in self.approvals.values() if state is None or row["state"] == state]
        return [
            dict(row)
            for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)[:limit]
        ]

    def list_approvals_for_project(
        self, project_id: str, state: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        project_task_ids = {
            task_id for task_id, task in self.tasks.items() if task.get("project_id") == project_id
        }
        owned_task_ids = {
            task_id for task_id, task in self.tasks.items() if task.get("project_id") is not None
        }
        rows = [
            row
            for row in self.approvals.values()
            if (state is None or row["state"] == state)
            and (
                row.get("task_id") in project_task_ids
                or (
                    row.get("session_id") is not None
                    and (not row.get("task_id") or row.get("task_id") not in owned_task_ids)
                )
            )
        ]
        return [
            dict(row)
            for row in sorted(rows, key=lambda row: row["created_at"], reverse=True)[:limit]
        ]

    def get_pause(self) -> dict[str, Any]:
        return dict(self.pause)

    def set_pause(self, paused: bool, reason: str = "") -> dict[str, Any]:
        self.pause.update({"paused": int(paused), "reason": reason, "updated_at": utc_now_iso()})
        return self.get_pause()

    def upsert_heartbeat(self, worker_id: str, pid: int, current_run_id: str | None) -> None:
        old = self.heartbeats.get(worker_id, {})
        self.heartbeats[worker_id] = {
            "worker_id": worker_id,
            "pid": pid,
            "started_at": old.get("started_at", utc_now_iso()),
            "last_beat_at": utc_now_iso(),
            "current_run_id": current_run_id,
        }

    def get_heartbeats(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.heartbeats.values()]

    def get_budget(self, budget_id: str) -> dict[str, Any] | None:
        return self._copy(self.budgets.get(budget_id))

    def upsert_budget_usage(
        self, budget_id: str, wall_ms: int, tokens: int, cost_usd: float
    ) -> None:
        row = self.budgets[budget_id]
        row.update(
            {
                "used_wall_ms": wall_ms,
                "used_tokens": tokens,
                "used_cost_usd": cost_usd,
                "updated_at": utc_now_iso(),
            }
        )

    def list_budgets(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.budgets.values()]

    def add_artifact(self, row: dict[str, Any]) -> None:
        self.artifacts[row["id"]] = dict(row)

    def get_artifacts(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.artifacts.values() if row["run_id"] == run_id]

    def list_disciplines(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.disciplines.values()]

    def create_discipline(self, row: dict[str, Any]) -> bool:
        if row["id"] in self.disciplines:
            return False
        self.disciplines[row["id"]] = dict(row)
        return True
