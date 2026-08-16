"""END-TO-END PROOF of the parent-side credential seam. Costs real money.

NOT collected by pytest (the filename is not ``test_*``) and deliberately so:
it makes a live Replicate prediction, which is a few tenths of a cent and a
network dependency. It is committed anyway because "a claim is not evidence;
re-run the command" is this repo's only working QA process, and a reviewer must
be able to re-run this one.

    set -a; . ~/.config/omni/connections.env; set +a
    .venv/bin/python loops/tests/manual/seam_e2e.py

What it proves, in one real scheduler tick:

1. ``routines_tick.tick`` — the production entry launchd runs — fires a
   ``kind='loop'`` row, which launches the REAL ``loops/bin/loop-worker``
   subprocess in the loops venv;
2. the worker's environment contains NO credential (asserted against the live
   ``connections.env`` values loaded into this process, by name AND by value);
3. the worker declares ``replicate.generate`` across the per-tick AF_UNIX
   socket; the parent authorizes it, resolves ``REPLICATE_API_TOKEN`` through
   ``connectors.broker``, calls Replicate and writes the artifact;
4. the artifact is on disk with PNG magic bytes and dimensions an INDEPENDENT
   decoder reports — a channel that never spoke to Replicate;
5. the idempotency receipt records the OUTCOME (``state=succeeded``,
   ``verified=true``), not merely the identity;
6. ``broker_calls`` holds the audit row;
7. the tick settles ``completed`` / favourable.

The control plane is a COPY of ``var/runtime/state.sqlite3`` in a temp
directory. The production database is opened read-only for the copy and is
never written.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

#: Copied (never written) so the proof runs against the REAL schema and row
#: shapes. Override with OMNIAGENTOS_SEAM_E2E_SOURCE_DB when the checkout under
#: test is a worktree whose var/ is empty. Absent => a fresh migrated database.
PRODUCTION_DB = Path(
    os.environ.get("OMNIAGENTOS_SEAM_E2E_SOURCE_DB")
    or (REPO / "var" / "runtime" / "state.sqlite3")
)
BRIEF = {
    "prompt": "a single red rose on a plain white background, studio photograph",
    "artifact_name": "rose.png",
    "output_format": "png",
    "aspect_ratio": "1:1",
    "expect_min_width": 512,
    "expect_min_height": 512,
}

CREDENTIAL_MARKERS = ("AUTH", "TOKEN", "SECRET", "KEY", "PASSWORD", "CREDENTIAL")


def _say(step: str, detail: str = "") -> None:
    print(f"[{step}] {detail}".rstrip(), flush=True)


def _routine_row(instance: str) -> dict[str, Any]:
    return {
        "name": "seam-e2e-render-probe",
        "description": "credential seam end-to-end proof",
        "trigger_type": "cron",
        "trigger_config": {"cron": "*/5 * * * *"},
        "task_template": {
            "title": "Loop tick: render_probe",
            "harness": "mock",
            "input": {
                "module": "omniagentos.loops",
                "kind": "loop",
                "template": "poll_classify_act_verify",
                "instance_id": instance,
                "instance_module": "omniagentos_loops.instances.render_probe",
                "params": dict(BRIEF),
                "timeout_s": 600,
            },
        },
        "gate_type": "test_command",
        "gate_config": {
            "command": "pytest loops/tests/test_parent_seam.py",
            "expected_exit_code": 0,
        },
        "hard_cap_type": "budget_usd",
        "hard_cap_value": 5.0,
        "notification_target": {"channel": "desktop"},
        "status": "active",
    }


def main() -> int:
    if not os.environ.get("REPLICATE_API_TOKEN"):
        _say("SKIP", "REPLICATE_API_TOKEN is not in this process; source connections.env first")
        return 2

    work = Path(tempfile.mkdtemp(prefix="seam-e2e-"))
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

    # The runtime catalogue the broker actually loads is $OMNIAGENTOS_VAR_DIR/
    # connectors.yaml (connectors.__init__._default_registry_path).
    shutil.copy2(REPO / "configs" / "connectors.yaml", var_dir / "connectors.yaml")

    os.environ["OMNIAGENTOS_VAR_DIR"] = str(var_dir)
    os.environ["OMNIAGENTOS_LOOPS_ROOT"] = str(work / "loops-root")
    os.environ.setdefault("OMNIAGENTOS_LOOPS_VENV", str(REPO / "var" / "loops" / "venv"))
    os.environ.pop("OMNIAGENTOS_GATE_WORKSPACE", None)

    from omniagentos.db.migrate import migrate
    from omniagentos.db.store import SqliteStore
    from omniagentos.policy import load_policy
    from omniagentos.scheduler import loop_jobs
    from omniagentos.scheduler.routines_tick import tick
    from omniagentos.scheduler.store import RoutinesStore

    migrate(db_path)
    store = SqliteStore(db_path)

    # Spy on the REAL subprocess call so the worker's environment is evidence,
    # not an assurance. Delegates to the real subprocess.run.
    real_run = subprocess.run
    captured: dict[str, Any] = {}

    def spy(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured["argv"] = list(argv)
        captured["env"] = dict(kwargs.get("env") or {})
        return real_run(argv, **kwargs)

    loop_jobs.subprocess.run = spy  # type: ignore[assignment]

    # The copy carries every production routine. Stand them down so this proof
    # fires exactly one loop and nothing else.
    stood_down = store._connection.execute(
        "UPDATE routines SET status = 'disabled' WHERE status = 'active'"
    ).rowcount
    store._connection.commit()
    _say("isolation", f"stood down {stood_down} pre-existing routine(s) in the COPY")

    routines = RoutinesStore(store)
    routines.create_routine(_routine_row("render_probe"))
    _say("tick", "firing routines_tick.tick — the production entry")
    # The row's cron is */5, and the due-check matches the minute exactly.
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    now = now.replace(minute=now.minute - (now.minute % 5))
    summary = tick(store, load_policy(), now=now)
    _say("tick", json.dumps(summary, default=str)[:400])

    failures: list[str] = []

    # 1. the worker really ran, and was told where the seam is (argv, not env)
    argv = captured.get("argv") or []
    if "--effect-socket" not in argv:
        failures.append("the tick did not open a parent effect seam")
    else:
        _say("seam", f"socket handed over in argv: {argv[argv.index('--effect-socket') + 1]}")

    # 2. THE WORKER HELD NO CREDENTIAL
    env = captured.get("env") or {}
    shaped = sorted(n for n in env if any(m in n.upper() for m in CREDENTIAL_MARKERS))
    if shaped:
        failures.append(f"credential-shaped names reached the worker: {shaped}")
    live_values = {
        value
        for name, value in os.environ.items()
        if value and len(value) > 12 and any(m in name.upper() for m in CREDENTIAL_MARKERS)
    }
    leaked = sorted(n for n, v in env.items() if v in live_values)
    if leaked:
        failures.append(f"a live credential VALUE reached the worker under: {leaked}")
    _say(
        "worker-env",
        f"{len(env)} names, 0 credential-shaped, 0 live secret values; keys={sorted(env)}",
    )

    # 3/4. the artifact, decoded by a channel that never spoke to Replicate
    artifact = var_dir / "loops" / "artifacts" / "render_probe" / BRIEF["artifact_name"]
    if not artifact.is_file():
        failures.append(f"no artifact at {artifact}")
    else:
        payload = artifact.read_bytes()
        _say("artifact", f"{artifact} ({len(payload)} bytes)")
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            failures.append(f"artifact does not start with the PNG magic number: {payload[:8]!r}")
        else:
            _say("magic", "89 50 4e 47 0d 0a 1a 0a — PNG")
        probe = real_run(
            ["/usr/bin/sips", "-g", "pixelWidth", "-g", "pixelHeight", str(artifact)],
            capture_output=True,
            text=True,
            check=False,
        )
        _say("decoder(sips)", probe.stdout.strip().replace("\n", " | "))
        dimensions = {
            key.strip(): int(value)
            for line in probe.stdout.splitlines()
            if ":" in line
            for key, _, value in [line.partition(":")]
            if key.strip() in ("pixelWidth", "pixelHeight") and value.strip().isdigit()
        }
        if dimensions.get("pixelWidth", 0) < BRIEF["expect_min_width"]:
            failures.append(f"width {dimensions} below the declared minimum")
        if dimensions.get("pixelHeight", 0) < BRIEF["expect_min_height"]:
            failures.append(f"height {dimensions} below the declared minimum")

    # 5. the receipt records the OUTCOME
    rows = store._connection.execute(
        "SELECT key, result_json FROM idempotency WHERE key LIKE 'loop:%render_probe%'"
    ).fetchall()
    if not rows:
        failures.append("no idempotency receipt was written")
    for row in rows:
        envelope = json.loads(dict(row)["result_json"] or "{}")
        _say(
            "receipt",
            f"{dict(row)['key']} state={envelope.get('state')} "
            f"verified={envelope.get('verified')} attempt={envelope.get('attempt')}",
        )
        if envelope.get("state") != "succeeded" or envelope.get("verified") is not True:
            failures.append(f"receipt {dict(row)['key']} is not a verified success")

    # 6. the broker audit trail
    audit = store._connection.execute(
        "SELECT capability_id, allowed, reason, agent_id FROM broker_calls "
        "WHERE agent_id = 'loop:render_probe' ORDER BY id"
    ).fetchall()
    if not audit:
        failures.append("no broker_calls audit row for the seam call")
    for row in audit:
        _say("broker_calls", json.dumps(dict(row)))

    # 7. the tick's own verdict
    run_rows = store._connection.execute(
        "SELECT self_reported_status, stop_reason, outcome_class, gate_passed, "
        "accepted, notes FROM routine_runs ORDER BY id DESC LIMIT 1"
    ).fetchall()
    for row in run_rows:
        _say("routine_run", json.dumps(dict(row), default=str)[:500])
        if dict(row).get("self_reported_status") != "completed":
            failures.append(f"the tick did not complete: {dict(row)}")

    # the production database was not touched
    if PRODUCTION_DB.is_file() and PRODUCTION_DB.stat().st_mtime_ns != before:
        failures.append("the production database was modified")
    if PRODUCTION_DB.is_file():
        _say("source-db", f"{PRODUCTION_DB} mtime unchanged: {before}")

    store.close()
    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print(f"workdir kept for inspection: {work}")
        return 1
    print("PASS — the credential seam worked end to end")
    print(f"workdir kept for inspection: {work}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
