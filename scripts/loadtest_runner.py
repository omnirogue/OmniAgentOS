#!/usr/bin/env python3
"""Scale + isolation load test for the OmniAgentOS runner (token-free, mock adapter only).

Proves two things with real numbers, not estimates:
  1. The runner scales horizontally: a pool of N worker PROCESSES (real
     ``python -m omniagentos.runner`` subprocesses, exactly as launchd runs them)
     drains a queue of mock runs, and we measure peak concurrency + throughput.
  2. Isolation holds: no two workers ever execute the same run. This is proven
     two ways -- (a) authoritative: each run's complete event history has
     EXACTLY ONE "running" claim event, ever, attributed to exactly one worker
     (not a sample -- the full permanent record); (b) corroborating: live
     heartbeat sampling during the drain never shows two workers holding the
     same run_id at the same sampled instant.

Safety (READ BEFORE CHANGING):
  - This script NEVER opens or writes ~/OmniAgentOS/var/omniagentos.db. It takes
    a consistent online backup (sqlite3 Connection.backup(), not a raw `cp`,
    because the live db is WAL-mode and actively being written by the real
    runner right now) into an isolated copy, then works ONLY against the copy.
  - The copy is purged of every runner-relevant row (runs/steps/approvals/
    artifacts/idempotency/heartbeats/events/tasks/projects) before any worker
    process is launched. This matters: the live system has a real runner
    running against real harnesses (cli-claude, cli-codex, ...). If a stale
    QUEUED/RUNNING row from the live snapshot were left in the copy, our test
    workers -- which use the REAL RunnerDependencies.load() adapter registry,
    exactly like production -- would claim it and could shell out to a real,
    token-costing CLI adapter. Purging the runs table before seeding closes
    that hole. Disciplines/pause/schema_migrations and every other subsystem's
    tables are left untouched (harmless either way; the runner's broad,
    non-id-scoped scans only ever read `runs` + `heartbeats`).
  - Every worker subprocess gets an isolated ledger dir, vault dir, and
    workspace dir (env-var redirected) so finalization file writes never touch
    the real repo's ledger/ or vault/. The knowledge subsystem (which talks to
    a real local Postgres in production -- see the runner launchd plist) is
    left at its default-OFF; the memory subsystem is explicitly disabled
    (OMNIAGENTOS_MEMORY=0) to keep the test to exactly the runner+store+mock
    surface being scaled. Harness is "mock" for every run: zero network calls,
    zero tokens, zero cost, per omniagentos/mock_adapter.py.
  - The pause row is force-set to unpaused on the copy regardless of what the
    live value was, so a paused live system can't silently stall this test.

Usage:
  .venv/bin/python scripts/loadtest_runner.py --workers 16 --db /tmp/loadtest.db
  .venv/bin/python scripts/loadtest_runner.py --workers 24 --db /tmp/loadtest-24.db
  .venv/bin/python scripts/loadtest_runner.py --workers 32 --db /tmp/loadtest-32.db
"""

from __future__ import annotations

import argparse
import json
import random
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
LIVE_DB = REPO_ROOT / "var" / "omniagentos.db"

sys.path.insert(0, str(REPO_ROOT))


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Step 1: safe, consistent, isolated copy of the live db
# ---------------------------------------------------------------------------


def backup_live_db(source: Path, dest: Path) -> None:
    """Consistent online backup of a live WAL-mode db into *dest*.

    Uses sqlite3's Connection.backup() (a proper page-level online backup)
    rather than `cp`, because the source is actively written by the real
    runner right now -- a raw file copy of a WAL-mode db mid-write risks an
    inconsistent snapshot. The source connection is opened read-only via a
    file: URI so this script never acquires a write lock on the live db.
    """
    if not source.is_file():
        raise FileNotFoundError(f"live db not found at {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        stale = dest.with_name(dest.name + suffix)
        if stale.exists():
            stale.unlink()
    src_uri = f"file:{urllib.parse.quote(str(source))}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    dest_conn = sqlite3.connect(str(dest))
    try:
        src_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        src_conn.close()
    log(f"backed up {source} -> {dest} ({dest.stat().st_size} bytes) via online sqlite backup")


# Tables the runner's *broad* (non-id-scoped) tick() scans can read:
# claim_next_run / the finalization scan / reclaim_stale_runs all read `runs`
# without filtering by our synthetic ids, and reclaim_stale_runs joins
# `heartbeats`. Everything downstream of a run (steps/approvals/artifacts/
# idempotency) is purged with it. tasks/projects are purged too so the script
# is safely re-runnable against the same --db path (project names are UNIQUE).
_PURGE_TABLES = (
    "steps",
    "approvals",
    "artifacts",
    "idempotency",
    "runs",
    "project_permission_grants",
    "tasks",
    "projects",
    "heartbeats",
    "events",
    "budgets",
)


def purge_runner_state(dest: Path) -> dict[str, int]:
    """Wipe every runner-relevant row from the isolated copy before seeding.

    This is the safety-critical step: it guarantees no pre-existing queued/
    running row from the live snapshot can be claimed and executed by our
    mock-only test workers (which otherwise resolve REAL adapters for
    non-mock harnesses, exactly like production).
    """
    conn = sqlite3.connect(str(dest))
    counts: dict[str, int] = {}
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in _PURGE_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                counts[table] = 0
                continue
            before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            conn.execute(f"DELETE FROM {table}")
            counts[table] = before
        # Defensively force-unpause: a paused live snapshot must never stall
        # this test's workers (they'd tick forever claiming nothing).
        conn.execute(
            "UPDATE pause SET paused = 0, reason = 'loadtest: forced unpaused', "
            "updated_at = ? WHERE id = 1",
            (_now(),),
        )
        conn.commit()
    finally:
        conn.close()
    log(
        f"purged pre-existing runner state from copy: { {k: v for k, v in counts.items() if v} or 'nothing to purge' }"
    )
    return counts


def verify_no_live_leftovers(dest: Path) -> None:
    """Hard assertion: after purge, zero rows remain in the tables that a
    worker's broad, non-id-scoped scans could pick up. Refuses to continue
    (raises) rather than silently proceeding if this is ever untrue."""
    conn = sqlite3.connect(str(dest))
    try:
        remaining_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        remaining_hb = conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()[0]
    finally:
        conn.close()
    if remaining_runs or remaining_hb:
        raise RuntimeError(
            f"REFUSING to launch workers: purge left runs={remaining_runs} "
            f"heartbeats={remaining_hb} in {dest} (expected 0 of each)"
        )


# ---------------------------------------------------------------------------
# Step 2: seed projects + mock runs on the isolated copy
# ---------------------------------------------------------------------------


def seed(
    dest: Path,
    *,
    label: str,
    num_projects: int,
    num_runs: int,
    delay_ms_range: tuple[int, int],
    rng_seed: int,
) -> dict[str, Any]:
    from omniagentos.api.services import create_run_service, create_task_service
    from omniagentos.db.store import SqliteStore
    from omniagentos.policy import load_policy
    from omniagentos.projects.store import ProjectStore

    store = SqliteStore(str(dest))
    policy_cfg = load_policy()
    proj_store = ProjectStore(store)

    project_ids: list[str] = []
    for i in range(num_projects):
        row = proj_store.create_project({"name": f"loadtest-{label}-project-{i:02d}"})
        project_ids.append(row["id"])

    rng = random.Random(rng_seed)
    run_ids: list[str] = []
    run_meta: dict[str, dict[str, Any]] = {}
    t0 = time.monotonic()
    for i in range(num_runs):
        project_id = project_ids[i % num_projects]
        task = create_task_service(
            store,
            policy_cfg,
            title=f"loadtest-{label}-task-{i:04d}",
            project_id=project_id,
        )
        delay_ms = rng.randint(*delay_ms_range)
        plan = [
            {
                "name": "agent",
                "kind": "agent",
                "action_class": "sandboxed_creation",
                "params": {
                    "prompt": f"loadtest {label} run {i:04d}",
                    "mock": {"reply": "loadtest-ok", "delay_ms": delay_ms},
                },
            }
        ]
        run = create_run_service(
            store,
            policy_cfg,
            task_id=task["id"],
            harness="mock",
            plan=plan,
        )
        run_ids.append(run["id"])
        run_meta[run["id"]] = {
            "task_id": task["id"],
            "project_id": project_id,
            "delay_ms": delay_ms,
        }
    seed_elapsed = time.monotonic() - t0
    log(
        f"seeded {len(project_ids)} projects, {len(run_ids)} tasks+runs "
        f"(harness=mock) in {seed_elapsed:.2f}s"
    )
    return {"project_ids": project_ids, "run_ids": run_ids, "run_meta": run_meta}


# ---------------------------------------------------------------------------
# Step 3: launch the worker pool
# ---------------------------------------------------------------------------


def launch_workers(
    *,
    n: int,
    label: str,
    db_path: Path,
    ledger_dir: Path,
    vault_dir: Path,
    workspace_dir: Path,
    log_dir: Path,
    poll_ms: int,
) -> list[dict[str, Any]]:
    import os

    log_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["OMNIAGENTOS_DB"] = str(db_path)
    env["OMNIAGENTOS_LEDGER_DIR"] = str(ledger_dir)
    env["OMNIAGENTOS_VAULT_DIR"] = str(vault_dir)
    env["OMNIAGENTOS_WORKSPACE_DIR"] = str(workspace_dir)
    env["OMNIAGENTOS_MEMORY"] = "0"  # isolate to the runner/store/mock surface being scaled
    for stray in (
        "OMNIAGENTOS_KNOWLEDGE",
        "OMNIAGENTOS_KNOWLEDGE_PG_DSN",
        "OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN",
        "OMNIAGENTOS_VAULT_AUTOCOMMIT",
    ):
        env.pop(stray, None)  # never inherit the live runner's Postgres/autocommit config

    workers: list[dict[str, Any]] = []
    for i in range(1, n + 1):
        worker_id = f"loadtest-{label}-{i}"
        log_path = log_dir / f"{worker_id}.log"
        handle = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [
                str(VENV_PYTHON),
                "-m",
                "omniagentos.runner",
                "--worker-id",
                worker_id,
                "--poll-ms",
                str(poll_ms),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        workers.append(
            {
                "worker_id": worker_id,
                "proc": proc,
                "pid": proc.pid,
                "log": log_path,
                "handle": handle,
            }
        )
    log(f"launched {n} worker processes: pids {[w['pid'] for w in workers]}")
    return workers


def stop_workers(workers: list[dict[str, Any]], *, grace_s: float = 8.0) -> None:
    for w in workers:
        try:
            w["proc"].send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_s
    for w in workers:
        remaining = max(0.1, deadline - time.monotonic())
        try:
            w["proc"].wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            log(
                f"worker {w['worker_id']} (pid {w['pid']}) did not exit on SIGTERM, sending SIGKILL"
            )
            w["proc"].kill()
            try:
                w["proc"].wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        finally:
            try:
                w["handle"].close()
            except Exception:
                pass
    still_alive = [w["pid"] for w in workers if w["proc"].poll() is None]
    if still_alive:
        raise RuntimeError(f"worker processes still alive after teardown: {still_alive}")
    log(f"all {len(workers)} worker processes confirmed terminated")


# ---------------------------------------------------------------------------
# Step 4: drain + concurrency sampling
# ---------------------------------------------------------------------------

_TERMINAL_STATES = {"completed", "failed", "cancelled"}


def poll_until_drained(
    *,
    db_path: Path,
    run_ids: list[str],
    label: str,
    timeout_s: float,
    sample_interval_s: float,
) -> dict[str, Any]:
    run_id_set = set(run_ids)
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    placeholders = ",".join("?" for _ in run_ids)

    started = time.monotonic()
    peak_concurrency = 0
    concurrency_samples: list[tuple[float, int]] = []
    run_worker_sightings: dict[str, set[str]] = defaultdict(set)
    concurrency_violations: list[dict[str, Any]] = []
    state_counts: dict[str, int] = {}
    last_progress_log = 0.0

    while True:
        now = time.monotonic()
        elapsed = now - started

        hb_rows = conn.execute(
            "SELECT worker_id, current_run_id FROM heartbeats WHERE worker_id LIKE ?",
            (f"loadtest-{label}-%",),
        ).fetchall()
        holders: dict[str, list[str]] = defaultdict(list)
        for row in hb_rows:
            rid = row["current_run_id"]
            if rid and rid in run_id_set:
                holders[rid].append(row["worker_id"])
                run_worker_sightings[rid].add(row["worker_id"])
        active = sum(1 for workers in holders.values() if workers)
        peak_concurrency = max(peak_concurrency, active)
        concurrency_samples.append((elapsed, active))
        for rid, workers in holders.items():
            if len(workers) > 1:
                concurrency_violations.append(
                    {"t": elapsed, "run_id": rid, "workers": list(workers)}
                )

        cur = conn.execute(
            f"SELECT state, COUNT(*) c FROM runs WHERE id IN ({placeholders}) GROUP BY state",
            run_ids,
        )
        state_counts = {r["state"]: r["c"] for r in cur.fetchall()}
        done = sum(v for k, v in state_counts.items() if k in _TERMINAL_STATES)

        if elapsed - last_progress_log >= 2.0:
            log(
                f"[{label}] t={elapsed:5.1f}s active_workers={active} "
                f"done={done}/{len(run_ids)} states={state_counts}"
            )
            last_progress_log = elapsed

        if done >= len(run_ids):
            break
        if elapsed > timeout_s:
            conn.close()
            raise TimeoutError(
                f"[{label}] did not drain within {timeout_s}s; "
                f"done={done}/{len(run_ids)} states={state_counts}"
            )
        time.sleep(sample_interval_s)

    elapsed = time.monotonic() - started
    conn.close()
    return {
        "elapsed_s": elapsed,
        "peak_concurrency": peak_concurrency,
        "num_samples": len(concurrency_samples),
        "concurrency_samples": concurrency_samples,
        "run_worker_sightings": {k: sorted(v) for k, v in run_worker_sightings.items()},
        "concurrency_violations": concurrency_violations,
        "final_state_counts": state_counts,
    }


# ---------------------------------------------------------------------------
# Step 5: isolation proof (authoritative, from the complete event log)
# ---------------------------------------------------------------------------


def isolation_proof(db_path: Path, run_ids: list[str]) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in run_ids)

    claim_rows = conn.execute(
        f"SELECT target_id, actor, COUNT(*) c FROM events "
        f"WHERE type = 'run.updated' AND action = 'running' AND target_id IN ({placeholders}) "
        f"GROUP BY target_id, actor",
        run_ids,
    ).fetchall()
    claims_by_run: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for row in claim_rows:
        claims_by_run[row["target_id"]].append((row["actor"], row["c"]))

    multi_claim_runs = {rid: actors for rid, actors in claims_by_run.items() if len(actors) > 1}
    repeated_claim_runs = {
        rid: actors
        for rid, actors in claims_by_run.items()
        if len(actors) == 1 and actors[0][1] > 1
    }
    missing_claim_runs = sorted(set(run_ids) - set(claims_by_run.keys()))

    run_rows = conn.execute(
        f"SELECT id, worker_id, state FROM runs WHERE id IN ({placeholders})", run_ids
    ).fetchall()
    run_worker_final = {r["id"]: r["worker_id"] for r in run_rows}
    final_state = {r["id"]: r["state"] for r in run_rows}

    actor_mismatches = []
    for rid, actors in claims_by_run.items():
        if len(actors) != 1:
            continue
        claimed_worker = actors[0][0].removeprefix("runner:")
        final_worker = run_worker_final.get(rid)
        if claimed_worker != final_worker:
            actor_mismatches.append(
                {
                    "run_id": rid,
                    "claim_actor_worker": claimed_worker,
                    "final_runs_worker_id": final_worker,
                }
            )

    conn.close()

    verdict = (
        "PASS"
        if not (multi_claim_runs or repeated_claim_runs or missing_claim_runs or actor_mismatches)
        else "FAIL"
    )

    return {
        "total_runs_checked": len(run_ids),
        "runs_with_exactly_one_claim_event": sum(
            1 for a in claims_by_run.values() if len(a) == 1 and a[0][1] == 1
        ),
        "multi_claim_runs": multi_claim_runs,  # >1 DISTINCT WORKER ever claimed this run -> double-execution
        "repeated_claim_runs": repeated_claim_runs,  # same worker claimed >1x (e.g. a reclaim) -> investigate
        "missing_claim_event_runs": missing_claim_runs,  # claimed but no running-event recorded -> investigate
        "worker_id_actor_mismatches": actor_mismatches,  # final owner != sole claimant -> investigate
        "distinct_runs_completed": sum(1 for s in final_state.values() if s == "completed"),
        "distinct_runs_non_completed": {
            rid: s for rid, s in final_state.items() if s != "completed"
        },
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Step 6: run-level stats (throughput inputs, load balance, mock cost)
# ---------------------------------------------------------------------------


def collect_run_stats(db_path: Path, run_ids: list[str]) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in run_ids)
    rows = conn.execute(
        f"SELECT id, worker_id, state, wall_ms, queued_at, started_at, finished_at "
        f"FROM runs WHERE id IN ({placeholders})",
        run_ids,
    ).fetchall()
    conn.close()

    worker_dist = Counter(r["worker_id"] for r in rows)
    wall_ms_values = [int(r["wall_ms"] or 0) for r in rows]
    wall_ms_values_sorted = sorted(wall_ms_values)
    n = len(wall_ms_values_sorted)

    def pct(p: float) -> float:
        if not n:
            return 0.0
        idx = min(n - 1, int(p * n))
        return wall_ms_values_sorted[idx]

    return {
        "worker_distribution": dict(worker_dist),
        "num_workers_that_completed_at_least_one_run": len(worker_dist),
        "wall_ms_sum": sum(wall_ms_values),
        "wall_ms_mean": (sum(wall_ms_values) / n) if n else 0.0,
        "wall_ms_min": min(wall_ms_values) if wall_ms_values else 0,
        "wall_ms_max": max(wall_ms_values) if wall_ms_values else 0,
        "wall_ms_p50": pct(0.50),
        "wall_ms_p95": pct(0.95),
    }


def scan_worker_logs_for_errors(workers: list[dict[str, Any]]) -> dict[str, Any]:
    hits: dict[str, list[str]] = {}
    for w in workers:
        try:
            text = w["log"].read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        bad_lines = [
            line
            for line in text.splitlines()
            if ("ERROR" in line or "CRITICAL" in line or "Traceback" in line)
        ]
        if bad_lines:
            hits[w["worker_id"]] = bad_lines[:20]
    return hits


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", required=True, help="Path to the isolated db copy this run will use"
    )
    parser.add_argument(
        "--source-db", default=str(LIVE_DB), help="Live db to back up FROM (read-only)"
    )
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--num-runs", type=int, default=300)
    parser.add_argument("--num-projects", type=int, default=12)
    parser.add_argument("--poll-ms", type=int, default=100, help="Worker --poll-ms")
    parser.add_argument(
        "--sample-interval-ms", type=int, default=100, help="Main-loop heartbeat sampling interval"
    )
    parser.add_argument("--delay-min-ms", type=int, default=120)
    parser.add_argument("--delay-max-ms", type=int, default=280)
    parser.add_argument("--timeout-s", type=float, default=180.0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--label", default=None, help="Defaults to w<workers>")
    parser.add_argument("--results-json", default=None)
    parser.add_argument(
        "--artifacts-dir", default=None, help="Base dir for isolated ledger/vault/workspace/logs"
    )
    args = parser.parse_args()

    label = args.label or f"w{args.workers}"
    db_path = Path(args.db).expanduser().resolve()
    source_db = Path(args.source_db).expanduser().resolve()
    artifacts_dir = (
        Path(args.artifacts_dir).expanduser().resolve()
        if args.artifacts_dir
        else Path(f"/tmp/loadtest-artifacts-{label}")
    )
    ledger_dir = artifacts_dir / "ledger"
    vault_dir = artifacts_dir / "vault"
    workspace_dir = artifacts_dir / "runs"
    log_dir = artifacts_dir / "worker-logs"
    results_path = (
        Path(args.results_json).expanduser().resolve()
        if args.results_json
        else Path(f"/tmp/loadtest-results-{label}.json")
    )

    if source_db.resolve() == db_path.resolve():
        raise SystemExit("REFUSING: --db must not equal --source-db (the live db)")

    log(
        f"=== OmniAgentOS runner load test: label={label} workers={args.workers} num_runs={args.num_runs} ==="
    )
    log(f"live source (read-only backup source): {source_db}")
    log(f"isolated copy (everything else operates ONLY on this file): {db_path}")

    backup_live_db(source_db, db_path)
    purge_runner_state(db_path)
    verify_no_live_leftovers(db_path)

    seeded = seed(
        db_path,
        label=label,
        num_projects=args.num_projects,
        num_runs=args.num_runs,
        delay_ms_range=(args.delay_min_ms, args.delay_max_ms),
        rng_seed=args.seed,
    )
    run_ids = seeded["run_ids"]

    workers: list[dict[str, Any]] = []
    drain: dict[str, Any] | None = None
    error: str | None = None
    wall_started = time.time()
    try:
        workers = launch_workers(
            n=args.workers,
            label=label,
            db_path=db_path,
            ledger_dir=ledger_dir,
            vault_dir=vault_dir,
            workspace_dir=workspace_dir,
            log_dir=log_dir,
            poll_ms=args.poll_ms,
        )
        drain = poll_until_drained(
            db_path=db_path,
            run_ids=run_ids,
            label=label,
            timeout_s=args.timeout_s,
            sample_interval_s=args.sample_interval_ms / 1000,
        )
    except Exception as exc:  # noqa: BLE001 - we must still tear down and report
        error = f"{type(exc).__name__}: {exc}"
        log(f"ERROR during drain: {error}")
    finally:
        log("tearing down worker pool...")
        stop_workers(workers)

    log_errors = scan_worker_logs_for_errors(workers)

    result: dict[str, Any] = {
        "label": label,
        "workers": args.workers,
        "num_runs": args.num_runs,
        "num_projects": args.num_projects,
        "poll_ms": args.poll_ms,
        "delay_ms_range": [args.delay_min_ms, args.delay_max_ms],
        "db_path": str(db_path),
        "artifacts_dir": str(artifacts_dir),
        "wall_clock_started_at": datetime.fromtimestamp(wall_started, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "worker_pids": [w["pid"] for w in workers],
        "error": error,
        "worker_log_errors": log_errors,
    }

    if drain is not None:
        elapsed = drain["elapsed_s"]
        throughput = args.num_runs / elapsed if elapsed > 0 else float("inf")
        stats = collect_run_stats(db_path, run_ids)
        iso = isolation_proof(db_path, run_ids)
        result.update(
            {
                "elapsed_s": elapsed,
                "throughput_runs_per_s": throughput,
                "peak_concurrency": drain["peak_concurrency"],
                "num_concurrency_samples": drain["num_samples"],
                "concurrency_violations": drain["concurrency_violations"],
                "final_state_counts": drain["final_state_counts"],
                "run_stats": stats,
                "isolation_proof": iso,
            }
        )
        log("=" * 70)
        log(f"RESULT [{label}]: {args.workers} workers, {args.num_runs} runs")
        log(f"  wall time         : {elapsed:.3f}s")
        log(f"  throughput        : {throughput:.2f} runs/sec")
        log(
            f"  peak concurrency  : {drain['peak_concurrency']} / {args.workers} workers ({drain['num_samples']} samples)"
        )
        log(f"  final states      : {drain['final_state_counts']}")
        log(f"  concurrency viol. : {len(drain['concurrency_violations'])} (must be 0)")
        log(f"  isolation verdict : {iso['verdict']}")
        log(
            f"  worker log errors : {len(log_errors)} worker(s) with ERROR/Traceback lines (must be 0)"
        )
        log("=" * 70)
    else:
        log(f"RESULT [{label}]: FAILED before/during drain: {error}")

    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    log(f"wrote results json -> {results_path}")

    ok = drain is not None and not error and not log_errors
    if ok:
        ok = result["isolation_proof"]["verdict"] == "PASS" and not result["concurrency_violations"]
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
