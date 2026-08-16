#!/usr/bin/env python3
"""Bounded backlog consumer -- drains the detoxed board queue, receipting every try.

Selects up to ``--max-tasks`` OPEN ``board_tasks`` rows (oldest-first) from
``pipeline.bridge.backlog_drain_census.select_drainable_board_tasks`` -- the
census read-model, reused rather than re-queried, so this consumer only ever
sees the post-detox queue (the reflection-alert duplicate mass and its
successors are already retired to ``cancelled`` by
``scripts/reliability/collapse-alert-backlog.py --apply`` before this script
has anything to select).

For each selected task it runs one end-to-end attempt --
selection -> spawn -> verify -> merge-or-named-refusal -- through a
pluggable ``executor`` callable and emits ONE receipt per task, no matter
the outcome. **No spawn/gate/merge backend is wired in this build**: that
capability lives in ``pipeline/bridge/spawn_builders.py`` +
``pipeline/bridge/gate_loop.py`` + ``pipeline/bridge/train_assembler.py``,
none of which are owned paths here, and wiring a fake success into this
script would be exactly the kind of favourable-absence defect this repo's
gates exist to catch. So the DEFAULT executor names an honest refusal
(``NO_EXECUTOR_REFUSAL``) for every task rather than fabricating a pass --
a receipted no-op, not a fake drain. A caller with a real backend supplies
its own ``executor`` to :func:`run` (or imports this module and calls
``run(..., executor=<callable>)`` directly); the CLI entrypoint always uses
the default.

Bounded two ways, both checked BEFORE a task is started (never mid-task):
a hard count (``--max-tasks``) and a wall-clock deadline
(``--max-seconds``) -- killable in the sense that a caller watching the
deadline (or sending SIGTERM) always finds this loop between tasks, never
blocked inside one.

    python scripts/reliability/backlog-consumer.py --max-tasks 10
    python scripts/reliability/backlog-consumer.py --max-tasks 10 --receipt-path var/reliability/backlog-consumer-receipt.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "pipeline"))

from bridge.backlog_drain_census import select_drainable_board_tasks  # noqa: E402

from omniagentos.contracts import default_db_path, utc_now_iso  # noqa: E402

#: Named refusal every task hits by default -- no execution backend wired.
NO_EXECUTOR_REFUSAL = "no execution backend configured (dry consumer)"

#: Named refusal for a task that never got a turn before the wall-clock cap.
WALL_CLOCK_REFUSAL = "wall-clock cap reached before this task started"

Executor = Callable[[dict[str, Any]], dict[str, Any]]


def _default_executor(task: dict[str, Any]) -> dict[str, Any]:
    """No spawn/verify/merge backend wired -- a named refusal, never a fake pass."""
    return {"stage": "selection", "outcome": "refused", "reason": NO_EXECUTOR_REFUSAL}


def run(
    db_path: str,
    *,
    max_tasks: int,
    max_seconds: float,
    executor: Executor = _default_executor,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Select up to ``max_tasks`` open board_tasks and receipt an attempt for each.

    Returns a list of at most ``max_tasks`` receipts, each shaped::

        {
          "task_id": str,
          "title": str,
          "selected_at": iso-8601,
          "stage": "selection" | "spawn" | "verify" | "merge",
          "outcome": "merged" | "refused",
          "reason": str | None,
        }

    ``max_tasks`` and ``max_seconds`` are BOTH hard bounds: the selection
    query itself is capped at ``max_tasks`` rows (so a caller can never fan
    out past what it asked for even if the executor never returns), and the
    wall-clock deadline is checked before every task starts -- a task
    already mid-flight when time runs out still gets to finish and receipt,
    but no NEW task starts past the deadline; a task denied a turn purely
    for lack of time gets its own named refusal rather than being silently
    dropped from the receipt list.
    """
    if max_tasks < 0:
        raise ValueError("max_tasks must be >= 0")
    if max_seconds < 0:
        raise ValueError("max_seconds must be >= 0")

    deadline = time.monotonic() + max_seconds
    tasks = select_drainable_board_tasks(db_path, limit=max_tasks, now=now)

    receipts: list[dict[str, Any]] = []
    for task in tasks:
        if time.monotonic() >= deadline:
            receipts.append(
                {
                    "task_id": task["id"],
                    "title": task["title"],
                    "selected_at": utc_now_iso(),
                    "stage": "selection",
                    "outcome": "refused",
                    "reason": WALL_CLOCK_REFUSAL,
                }
            )
            continue

        selected_at = utc_now_iso()
        try:
            result = executor(task)
        except Exception as exc:  # defensive: an executor error is a receipted refusal
            result = {
                "stage": "execution",
                "outcome": "refused",
                "reason": f"executor error: {exc}",
            }
        outcome = str(result.get("outcome") or "refused")
        receipts.append(
            {
                "task_id": task["id"],
                "title": task["title"],
                "selected_at": selected_at,
                "stage": result.get("stage", "selection"),
                "outcome": outcome,
                "reason": result.get("reason"),
            }
        )
    return receipts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=default_db_path(), help="database path")
    parser.add_argument("--max-tasks", type=int, default=10, help="hard task cap per run")
    parser.add_argument(
        "--max-seconds", type=float, default=600.0, help="wall-clock cap per run, in seconds"
    )
    parser.add_argument(
        "--receipt-path",
        default=None,
        help="optional path to also write this run's receipts as JSON",
    )
    args = parser.parse_args()
    receipts = run(args.db, max_tasks=args.max_tasks, max_seconds=args.max_seconds)
    print(json.dumps(receipts, indent=2, sort_keys=True))
    if args.receipt_path:
        path = Path(args.receipt_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipts, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
