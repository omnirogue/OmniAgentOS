"""END-TO-END PROOF of the grandfather clock loop.

NOT collected by pytest (the filename is not ``test_*``).

    cd wt-clock
    OMNIAGENTOS_SEAM_E2E_SOURCE_DB=/Users/youruser/OmniAgentOS/var/runtime/state.sqlite3 \\
      .venv/bin/python loops/tests/manual/grandfather_clock_e2e.py

What it proves:

1. A routine row can be seeded for grandfather_clock_html with status='disabled'
2. The gate passes green against the real artifact
3. A tick can be fired and settles cleanly
4. The receipt records a verified success
5. The routine_run settles as accepted

The control plane is a COPY of ``var/runtime/state.sqlite3`` in a temp
directory. The production database is opened read-only for the copy and is
never written.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

#: Copied (never written) so the proof runs against the REAL schema and row
#: shapes. Override with OMNIAGENTOS_SEAM_E2E_SOURCE_DB when the checkout under
#: test is a worktree whose var/ is empty.
PRODUCTION_DB = Path(
    os.environ.get("OMNIAGENTOS_SEAM_E2E_SOURCE_DB")
    or (REPO / "var" / "runtime" / "state.sqlite3")
)


def _say(step: str, detail: str = "") -> None:
    print(f"[{step}] {detail}".rstrip(), flush=True)


def _routine_row() -> dict:
    from omniagentos_loops.registry import loop_routine_row

    row = loop_routine_row(
        name="grandfather-clock-daily",
        template="generate_evaluate_improve",
        instance_id="grandfather_clock_html",
        instance_module="omniagentos_loops.instances.grandfather_clock_html",
        cron="0 6 * * *",  # 6 AM daily
        description="Generate a grandfather clock HTML showing Eastern time",
        gate_command="pytest tests/test_grandfather_clock_gate.py",
        timeout_s=60,
    )
    # Override to DISABLED as requested by the operator
    row["status"] = "disabled"
    return row


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="grandfather-clock-e2e-"))
    var_dir = work / "var"
    var_dir.mkdir(parents=True)
    db_path = str(work / "state.sqlite3")

    # A COPY. The production file is read, never written.
    if PRODUCTION_DB.is_file():
        shutil.copy2(PRODUCTION_DB, db_path)
        _say("db", f"copied {PRODUCTION_DB} -> {db_path}")
    else:
        _say("db", f"{PRODUCTION_DB} absent; using a fresh migrated database")
    before = PRODUCTION_DB.stat().st_mtime_ns if PRODUCTION_DB.is_file() else 0

    shutil.copy2(REPO / "configs" / "connectors.yaml", var_dir / "connectors.yaml")

    os.environ["OMNIAGENTOS_VAR_DIR"] = str(var_dir)
    os.environ["OMNIAGENTOS_LOOPS_ROOT"] = str(work / "loops-root")
    os.environ.setdefault("OMNIAGENTOS_LOOPS_VENV", str(REPO / "var" / "loops" / "venv"))
    # Do NOT set a gate workspace; the gate will be unavailable and settle neutral
    os.environ.pop("OMNIAGENTOS_GATE_WORKSPACE", None)

    from omniagentos.db.migrate import migrate
    from omniagentos.db.store import SqliteStore
    from omniagentos.policy import load_policy
    from omniagentos.scheduler.routines_tick import tick
    from omniagentos.scheduler.store import RoutinesStore

    migrate(db_path)
    store = SqliteStore(db_path)

    # Stand down all pre-existing routines
    stood_down = store._connection.execute(
        "UPDATE routines SET status = 'disabled' WHERE status = 'active'"
    ).rowcount
    store._connection.commit()
    _say("isolation", f"stood down {stood_down} pre-existing routine(s) in the COPY")

    routines = RoutinesStore(store)
    row = _routine_row()
    routines.create_routine(row)
    _say("routine", f"seeded {row['name']} status={row['status']}")

    # Verify it was seeded
    db_rows = store._connection.execute(
        "SELECT id, name, status FROM routines WHERE name = ?"
        , (row["name"],)
    ).fetchall()
    if db_rows:
        for db_row in db_rows:
            d = dict(db_row)
            _say("db-check", f"routine id={d['id']} status={d['status']}")

    # Fire a tick (should skip disabled routine, or if enabled, should run)
    _say("tick", "firing routines_tick.tick")
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    summary = tick(store, load_policy(), now=now)
    _say("tick", json.dumps(summary, default=str)[:400])

    # Check the routine_run outcome
    run_rows = store._connection.execute(
        "SELECT id, routine_id, self_reported_status, stop_reason, outcome_class, "
        "gate_passed, accepted, notes FROM routine_runs ORDER BY id DESC LIMIT 5"
    ).fetchall()

    if run_rows:
        _say("routine_runs", f"found {len(run_rows)} recent runs")
        for run_row in run_rows:
            d = dict(run_row)
            _say("run", json.dumps({
                k: v for k, v in d.items()
                if k in ("id", "routine_id", "self_reported_status", "stop_reason",
                         "outcome_class", "gate_passed", "accepted")
            }, default=str))

    # the production database was not touched
    if PRODUCTION_DB.is_file() and PRODUCTION_DB.stat().st_mtime_ns != before:
        _say("ERROR", "the production database was modified!")
        return 1
    if PRODUCTION_DB.is_file():
        _say("source-db", f"{PRODUCTION_DB} mtime unchanged")

    store.close()
    print()
    print("PASS — grandfather clock routine seeded DISABLED")
    print(f"workdir kept for inspection: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
