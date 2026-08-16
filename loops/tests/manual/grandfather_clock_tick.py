#!/usr/bin/env python3
"""Fire ONE real tick of the grandfather clock loop and inspect what it filed.

NOT collected by pytest (the filename is not ``test_*``).

    OMNIAGENTOS_SEAM_E2E_SOURCE_DB=/Users/youruser/OmniAgentOS/var/runtime/state.sqlite3 \\
      .venv/bin/python loops/tests/manual/grandfather_clock_tick.py

The control plane is a COPY of the live database in a temp directory, every
pre-existing routine is stood down first, and ``OMNIAGENTOS_VAR_DIR`` points at
a throwaway tree — so the only thing this touches outside ``/tmp`` is the
artifact the loop itself files into the operator's output directory.

WHAT IT PROVES
--------------

That the LOOP writes the directory its gate reads. It used to prove the
opposite: this script filed the artifact itself, in an inline block after the
tick, which meant production ticks never wrote that directory at all and the
gate certified a file no run had produced. Filing now lives in the instance's
``publish``; if the artifact is not there when this script looks, the loop did
not file it and this script says so.

The routine row comes from :func:`omniagentos_loops.registry.loop_routine_row`,
the same builder the e2e and every other loop row uses. The ONLY value this
script chooses for itself is the cron expression — a manual run needs a trigger
that is due within the minute, where production wants ``0 6 * * *`` — and it is
passed as an argument rather than by rebuilding the row, so budget, timeout,
gate command, harness and purpose cannot drift between this proof and
production. They did: this script used to declare ``hard_cap 10.0``,
``timeout_s 600`` where the helper says ``5.0`` and ``60``.
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
# This script runs on the PRODUCTION venv (it drives the scheduler); the worker
# it spawns runs on the loops venv. Only the row builder and the instance's
# filing convention are needed here, and both are pure stdlib on import.
sys.path.insert(0, str(REPO))
sys.path.insert(1, str(REPO / "loops"))

#: Copied (never written), like the e2e's. Override when the checkout under test
#: is a worktree whose ``var/`` is empty.
PRODUCTION_DB = Path(
    os.environ.get("OMNIAGENTOS_SEAM_E2E_SOURCE_DB")
    or (REPO / "var" / "runtime" / "state.sqlite3")
)

#: A trigger that is due now. Production uses ``0 6 * * *``.
MANUAL_CRON = "*/5 * * * *"


def _say(step: str, detail: str = "") -> None:
    print(f"[{step}] {detail}".rstrip(), flush=True)


def _routine_row() -> dict:
    from omniagentos_loops.registry import loop_routine_row

    return loop_routine_row(
        name="manual-clock-tick",
        template="generate_evaluate_improve",
        instance_id="grandfather_clock_html",
        instance_module="omniagentos_loops.instances.grandfather_clock_html",
        cron=MANUAL_CRON,
        description="Generate a grandfather clock HTML showing Eastern time",
        gate_command="pytest tests/test_grandfather_clock_gate.py",
        params={"score_threshold": 1.0, "max_rounds": 1},
    )


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="clock-tick-"))
    var_dir = work / "var"
    var_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(work / "state.sqlite3")

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
    os.environ.pop("OMNIAGENTOS_GATE_WORKSPACE", None)

    from omniagentos.db.migrate import migrate
    from omniagentos.db.store import SqliteStore
    from omniagentos.policy import load_policy
    from omniagentos.scheduler.routines_tick import tick
    from omniagentos.scheduler.store import RoutinesStore

    migrate(db_path)
    store = SqliteStore(db_path)

    # Stand down all pre-existing routines so this tick is isolated
    stood_down = store._connection.execute(
        "UPDATE routines SET status = 'disabled' WHERE status = 'active'"
    ).rowcount
    store._connection.commit()
    _say("isolation", f"stood down {stood_down} pre-existing routine(s) in the COPY")

    routines = RoutinesStore(store)
    row = _routine_row()
    try:
        routines.create_routine(row)
        _say("routine", f"seeded {row['name']} status={row['status']} cron={MANUAL_CRON}")
    except Exception as exc:
        _say("routine", f"error creating routine: {exc}")
        return 1

    _say("tick", "firing routines_tick.tick...")
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    now = now.replace(minute=now.minute - (now.minute % 5))

    try:
        summary = tick(store, load_policy(), now=now)
        _say("tick", json.dumps(summary, default=str)[:600])
    except Exception as exc:
        _say("tick", f"error during tick: {exc}")
        import traceback

        traceback.print_exc()
        return 1

    from omniagentos_loops.instances.grandfather_clock_html import (
        ARTIFACT_NAME,
        INSTANCE_ID,
        clock_day,
        operator_output_root,
        output_dir_name,
        read_stamp,
    )

    artifact = var_dir / "loops" / "artifacts" / INSTANCE_ID / ARTIFACT_NAME
    if not artifact.is_file():
        _say("FAIL", f"clock artifact not found at {artifact}")
        return 1
    payload = artifact.read_text(encoding="utf-8")
    _say("artifact", f"{artifact} ({len(payload)} bytes)")

    # The LOOP files this, not this script. If it is missing, the gate would be
    # reading somebody else's file.
    filed = operator_output_root() / output_dir_name(clock_day()) / ARTIFACT_NAME
    if not filed.is_file():
        _say("FAIL", f"the loop did not file its artifact to {filed}")
        return 1
    if filed.read_text(encoding="utf-8") != payload:
        _say("FAIL", f"{filed} differs from {artifact}")
        return 1
    stamp = read_stamp(payload)
    _say("filed", f"{filed} stamped {stamp.get('loop-published-at')!r}")

    for needle, why in (
        ("America/New_York", "IANA zone missing"),
        ("toLocaleString", "no Intl formatting"),
        ("timeZone", "no timeZone parameter"),
    ):
        if needle not in payload:
            _say("FAIL", why)
            return 1
    for forbidden in ("-04:00", "-05:00"):
        if forbidden in payload:
            _say("FAIL", f"clock contains hardcoded offset {forbidden}")
            return 1
    _say("verify", "clock structure verified")

    if PRODUCTION_DB.is_file() and PRODUCTION_DB.stat().st_mtime_ns != before:
        _say("ERROR", "the production database was modified!")
        return 1

    store.close()
    print()
    print("SUCCESS: the loop generated and filed the clock")
    print(f"Artifact: {filed}")
    print(f"Work directory: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
