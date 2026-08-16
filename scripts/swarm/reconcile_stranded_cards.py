#!/usr/bin/env python3
"""Settle board cards left "live" by a swarm run that has already stopped.

Backfill for the bug fixed in ``SwarmDal.close_out_run_cards``: only
``_complete_run`` ever closed a run's root card, so every ``failed`` and
``cancelled`` run left its root card (``Swarm: <goal>``) plus every unfinished
member card pinned at ``in_progress``/``open`` forever. The board then showed a
pile of permanently "Running" work with no progress and no way to tell a wedged
run from a live one.

New terminal transitions self-heal (``set_run_status`` closes cards out at the
commit chokepoint); this script fixes the rows that were stranded BEFORE that
landed. It is idempotent — cards already terminal or archived are untouched —
so it is safe to re-run, and safe to run against a live database (SQLite WAL).

Usage::

    python -m scripts.swarm.reconcile_stranded_cards            # dry run
    python -m scripts.swarm.reconcile_stranded_cards --apply    # write
"""

from __future__ import annotations

import argparse
import sys

from omniagentos.contracts import default_db_path
from omniagentos.swarm.contracts import TERMINAL_RUN_STATUSES
from omniagentos.swarm.dal import SwarmDal

_LIVE_TASK_STATUSES = ("pending", "open", "claimed", "in_progress")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    parser.add_argument("--db", default=None, help="database path (default: the resolved app db)")
    args = parser.parse_args(argv)

    dal = SwarmDal(args.db or default_db_path())
    terminal = {str(status) for status in TERMINAL_RUN_STATUSES}
    stranded_total = 0
    try:
        for run in dal.list_runs():
            status = str(run["status"])
            if status not in terminal:
                continue
            run_id = str(run["id"])
            live = [
                task
                for task in dal.tasks_for_run(run_id)
                if str(task["status"]) in _LIVE_TASK_STATUSES and task["archived_at"] is None
            ]
            if not live:
                continue
            stranded_total += len(live)
            landing = "blocked" if status == "failed" else "cancelled"
            print(f"{run_id}  run={status:<9} -> {len(live)} card(s) -> {landing}")
            for task in live:
                print(f"    {task['id']}  {task['status']:<12} {str(task['title'])[:70]}")
            if args.apply:
                dal.close_out_run_cards(run_id, run_status=status)
    finally:
        dal.close()

    if not stranded_total:
        print("No stranded cards — every terminal run's board cards are settled.")
    elif args.apply:
        print(f"\nSettled {stranded_total} stranded card(s).")
    else:
        print(f"\n{stranded_total} stranded card(s). Re-run with --apply to settle them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
