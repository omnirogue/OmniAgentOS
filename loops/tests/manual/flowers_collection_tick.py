#!/usr/bin/env python3
"""Fire ONE real tick of the flowers_collection loop and inspect what it filed.

NOT collected by pytest (the filename is not ``test_*``).

    OMNIAGENTOS_SEAM_E2E_SOURCE_DB=/Users/youruser/OmniAgentOS/var/runtime/state.sqlite3 \\
      var/loops/venv/bin/python loops/tests/manual/flowers_collection_tick.py

THIS SPENDS MONEY: one tick renders four Replicate images (~$0.40). Set
``FLOWERS_DESTINATION=/some/tmp/tree`` to file them somewhere other than the
operator's real delivery folder — the tick is the same, only the filing root
moves, and the loop is told through the routine row's params rather than by
this script writing anything itself.

The control plane is a COPY of the live database in a temp directory, every
pre-existing routine is stood down first, and ``OMNIAGENTOS_VAR_DIR`` points at
a throwaway tree — so the only thing this touches outside ``/tmp`` is the
artifacts the loop itself files into the destination.

WHAT IT PROVES
--------------

That the LOOP writes the directory its gate reads. It used to prove the
opposite: this script filed the artifacts itself, in an inline block after the
tick, which meant production ticks never wrote that directory at all and the
gate certified files no run had produced. Filing now lives in the instance's
effect; if the artifacts are not there when this script looks, the loop did
not file them and this script says so.

Every path this script checks is derived from the INSTANCE's own naming
functions (``flowers_day``, ``output_dir_name``, ``artifact_name``). It used to
re-derive them here with ``date.today()``, which is a different clock from the
loop's UTC day and would have reported a correct tick as a failure across the
local-midnight boundary.
"""

from __future__ import annotations

import hashlib
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

#: A trigger that is due now. Production uses a cron expression.
MANUAL_CRON = "*/5 * * * *"

#: Where the loop files the collection. Empty means the operator's declared
#: tree; the loop reads it from the routine row's params, so this script never
#: writes an artifact itself.
DESTINATION = os.environ.get("FLOWERS_DESTINATION", "").strip()


def _say(step: str, detail: str = "") -> None:
    print(f"[{step}] {detail}".rstrip(), flush=True)


def _routine_row() -> dict:
    from omniagentos_loops.registry import loop_routine_row

    params: dict = {
        "max_spend_usd": 1.00,
        "flowers": ["rose", "tulip", "sunflower", "blue_rose"],
    }
    if DESTINATION:
        params["destination"] = DESTINATION

    return loop_routine_row(
        name="manual-flowers-tick",
        template="poll_classify_act_verify",
        instance_id="flowers_collection",
        instance_module="omniagentos_loops.instances.flowers_collection",
        cron=MANUAL_CRON,
        description="Generate a beautiful collection of four flowers using Replicate",
        gate_command="pytest tests/test_flowers_gate.py",
        params=params,
    )


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="flowers-tick-"))
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

    from omniagentos_loops.instances import flowers_collection as instance

    # Check that artifacts were created in var/loops/artifacts/
    artifact_dir = var_dir / "loops" / "artifacts" / instance.INSTANCE_ID
    if not artifact_dir.is_dir():
        _say("FAIL", f"artifact directory not found at {artifact_dir}")
        return 1

    # The loop's OWN naming, not this script's guess at it.
    day = instance.flowers_day()
    flowers = list(instance.FLOWER_ORDER)

    for flower in flowers:
        artifact = artifact_dir / instance.artifact_name(flower, day)
        if not artifact.is_file():
            _say("FAIL", f"{flower} artifact not found at {artifact}")
            return 1
        content = artifact.read_bytes()
        if len(content) == 0:
            _say("FAIL", f"{flower} artifact is empty")
            return 1
        _say("artifact", f"{artifact} ({len(content)} bytes)")

    # The LOOP files these, not this script. If they are missing, the gate would be
    # reading somebody else's files.
    root = Path(DESTINATION).expanduser() if DESTINATION else instance.operator_output_root()
    output_dir = root / instance.output_dir_name(day)
    if not output_dir.is_dir():
        _say("FAIL", f"the loop did not create output directory {output_dir}")
        return 1

    filed_artifacts = []
    for flower in flowers:
        filed = output_dir / instance.filed_name(flower)
        if not filed.is_file():
            _say("FAIL", f"the loop did not file {flower} artifact to {filed}")
            return 1
        artifact_in_var = artifact_dir / instance.artifact_name(flower, day)
        if filed.read_bytes() != artifact_in_var.read_bytes():
            _say("FAIL", f"{filed} differs from {artifact_in_var}")
            return 1
        filed_artifacts.append(filed)
        _say("filed", f"{filed} ({filed.stat().st_size} bytes)")

    # Verify magic bytes
    for flower, filed in zip(flowers, filed_artifacts, strict=True):
        content = filed.read_bytes()
        is_png = content.startswith(b'\x89PNG\r\n\x1a\n')
        is_jpeg = content.startswith(b'\xff\xd8\xff')
        if not (is_png or is_jpeg):
            _say("FAIL", f"{flower} artifact has invalid magic bytes: {content[:8].hex()}")
            return 1
        _say("magic", f"{flower}: {'PNG' if is_png else 'JPEG'}")

    # Verify all four are distinct (different hashes)
    hashes = {hashlib.sha256(filed.read_bytes()).hexdigest() for filed in filed_artifacts}
    if len(hashes) != len(flowers):
        _say("FAIL", f"expected {len(flowers)} distinct flowers, but found {len(hashes)} hashes")
        return 1
    _say("distinct", f"all {len(flowers)} flowers are distinct")

    if PRODUCTION_DB.is_file() and PRODUCTION_DB.stat().st_mtime_ns != before:
        _say("ERROR", "the production database was modified!")
        return 1

    store.close()
    print()
    print("SUCCESS: the loop generated and filed four distinct flowers")
    print(f"Output directory: {output_dir}")
    print(f"Work directory: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
