"""Production fan-in caller on the swarm summary path (M-18).

When a task ends with multiple completed attempts, run the full fan-in
pipeline (compiled context → adjudicate → verify → learning exit) so the
path is exercised on the default run-terminal surface, not only unit tests.

Does not edit L10 scheduler. Invoked from ``summary.write_summary``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

LOG = logging.getLogger(__name__)

__all__ = ["fanin_multi_attempt_tasks"]


def fanin_multi_attempt_tasks(
    *,
    dal: Any,
    run: Mapping[str, Any],
    metrics: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run fan-in for tasks that have 2+ ended attempts; return result stamps."""
    del metrics  # reserved for future score blending
    from omniagentos.fanin import Candidate, FanInMode, run_fanin_pipeline

    out: list[dict[str, Any]] = []
    root_id = str(run.get("board_task_id") or "")
    run_id = str(run.get("id") or "")
    try:
        tasks = list(dal.tasks_for_run(run_id) or [])
    except Exception:  # noqa: BLE001
        LOG.warning("fanin_multi_attempt_tasks list failed for %s", run_id, exc_info=True)
        return out

    for task in tasks:
        tid = str(task.get("id") or "")
        if not tid or tid == root_id:
            continue
        try:
            attempts = list(dal.list_attempts(tid) or [])
        except Exception:  # noqa: BLE001
            continue
        ended = [a for a in attempts if a.get("ended_at")]
        if len(ended) < 2:
            continue

        candidates: list[Candidate] = []
        for a in ended:
            aid = str(a.get("id") or "")
            end_reason = str(a.get("end_reason") or "")
            # Prefer completed/confirmed attempts; score others lower.
            score = 0.9 if end_reason in {"completed", "confirm", "confirmed"} else 0.3
            if end_reason in {"crashed", "killed", "failed", "auth_failed"}:
                score = 0.1
            candidates.append(
                Candidate(
                    id=aid,
                    content={
                        "attempt_id": aid,
                        "provider": a.get("provider"),
                        "model": a.get("model"),
                        "effort": a.get("effort"),
                        "end_reason": end_reason,
                    },
                    score=score,
                    confidence=score,
                )
            )

        try:
            result = run_fanin_pipeline(
                candidates,
                mode=FanInMode.ADJUDICATE,
                criteria={"min_score": 0.2},
                context_layers=[
                    ("task", f"select best attempt for task {tid}"),
                    ("run", f"swarm run {run_id}"),
                ],
                scope=f"swarm-summary:{tid}",
                decision_id=f"fin_{tid}",
            )
            stamp = result.to_dict()
            stamp["task_id"] = tid
            out.append(stamp)
        except Exception:  # noqa: BLE001 — summary path must not die
            LOG.warning("fanin pipeline failed for task %s", tid, exc_info=True)

    return out
