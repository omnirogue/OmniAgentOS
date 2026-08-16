#!/usr/bin/env python3
"""SSE hot-loop cost harness: measures the per-tick DB cost of GET /api/events.

WHAT THIS MEASURES
------------------
``omniagentos/api/routes/control.py`` (``GET /api/events``, the ``stream()``
generator) runs a loop that, per SSE connection, executes THREE database reads
and then ``await asyncio.sleep(0.25)`` -- i.e. 4 ticks per wall-second, per
connection, all of them on the single asyncio event-loop thread (the store and
the sessions DAL are synchronous, internally-``RLock``-serialized SQLite):

  1. ``store.get_heartbeats()``
  2. ``sessions_dal.list_sessions(limit=200)``   (only when session.updated is wanted)
  3. ``store.get_events_after(cursor, types=..., limit=500)``

Because every one of those is a blocking synchronous call made from inside an
``async def``, their cost is NOT overlapped: N concurrent SSE connections cost
N x (per-tick cost) x 4 CPU-milliseconds of event-loop time per wall-second.
When that product reaches 1000 CPU-ms per wall-second the loop thread is
saturated and every request served by that process -- SSE or not -- starts
queueing behind it. This harness measures the per-tick cost against a real
copy of the live database and reports the connection count at which that
happens.

SAFETY (READ BEFORE CHANGING) -- same idiom as scripts/loadtest_runner.py
------------------------------------------------------------------------
  - This script NEVER opens ``var/omniagentos.db`` for write. It takes a
    consistent online backup (``sqlite3.Connection.backup()``, a page-level
    online backup -- NOT ``cp``, because the live db is WAL-mode and is being
    written by the real runner/API right now) into an isolated copy, opening
    the live db read-only through a ``file:...?mode=ro`` URI, and then every
    subsequent operation targets ONLY the copy.
  - ``_refuse_if_live()`` is called before any non-backup connect: the harness
    hard-refuses (raises) if a path it is about to open resolves to the live db.
  - ``OMNIAGENTOS_DB`` / ``OMNIAGENTOS_LEDGER_DIR`` / ``OMNIAGENTOS_VAULT_DIR``
    / ``OMNIAGENTOS_WORKSPACE_DIR`` are redirected to the copy and to isolated
    scratch dirs BEFORE any omniagentos import, so that any code path that
    resolves ``default_db_path()`` / ``default_ledger_dir()`` on its own (e.g.
    ``omniagentos.api.routes.sessions.get_sessions_dal``) can only ever reach
    the copy. ``OMNIAGENTOS_MEMORY=0`` and the knowledge/Postgres env vars are
    cleared for the same reason.
  - No worker processes, no runs, no adapters, zero network calls, zero tokens.
    This harness only READS. It does open the copy read-write (``SqliteStore``
    runs migrations on connect), which is exactly why it must be a copy.
  - The live db's size/mtime are sampled before and after and reported. A
    change there is expected and harmless when the real runner/API is up (they
    write it continuously); it is reported for transparency, not asserted,
    because this process demonstrably holds no write handle on it.

RE-RUNNABILITY
--------------
Deterministic and read-only, so it can be re-run verbatim after T1.2 / T1.5
land to produce a comparable "after" curve:

  uv run python scripts/loadtest_sse.py --label before
  # ... land the fix ...
  uv run python scripts/loadtest_sse.py --label after \
      --baseline /tmp/loadtest-sse-before.json

``--baseline`` prints a side-by-side delta table (ms/call and saturation point)
against a previous run's JSON so the improvement is a measured number rather
than a claim.

USAGE
-----
  uv run python scripts/loadtest_sse.py
  uv run python scripts/loadtest_sse.py --iters 200 --warmup 20 --label before
  uv run python scripts/loadtest_sse.py --db /tmp/sse-copy.db --json-out /tmp/x.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
LIVE_DB = REPO_ROOT / "var" / "omniagentos.db"

sys.path.insert(0, str(REPO_ROOT))

# The tick cadence of the SSE stream loop: control.py does `await asyncio.sleep(0.25)`
# at the bottom of `while not await request.is_disconnected():`.
TICK_HZ = 4.0
# One event-loop thread has 1000 CPU-ms of budget per wall-second.
EVENT_LOOP_BUDGET_MS_PER_S = 1000.0
# The SSE loop's own literals (control.py): list_sessions(limit=200),
# get_events_after(..., limit=500).
SESSIONS_LIMIT = 200
EVENTS_LIMIT = 500


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Step 0: isolation guards
# ---------------------------------------------------------------------------


def _refuse_if_live(path: Path) -> Path:
    """Hard stop before opening anything that resolves to the live database."""
    resolved = path.expanduser().resolve()
    if resolved == LIVE_DB.resolve():
        raise SystemExit(
            f"REFUSING: {resolved} is the live database. This harness only ever "
            "operates on an online-backup copy."
        )
    return resolved


def isolate_environment(db_copy: Path, scratch_dir: Path) -> None:
    """Redirect every ambient omniagentos path at the copy / scratch dirs.

    Must run BEFORE importing omniagentos, because helpers such as
    ``get_sessions_dal()`` resolve ``default_db_path()`` at call time from the
    environment. Belt-and-braces: this harness also passes the copy path
    explicitly everywhere.
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)
    os.environ["OMNIAGENTOS_DB"] = str(db_copy)
    os.environ["OMNIAGENTOS_LEDGER_DIR"] = str(scratch_dir / "ledger")
    os.environ["OMNIAGENTOS_VAULT_DIR"] = str(scratch_dir / "vault")
    os.environ["OMNIAGENTOS_WORKSPACE_DIR"] = str(scratch_dir / "runs")
    os.environ["OMNIAGENTOS_MEMORY"] = "0"
    for stray in (
        "OMNIAGENTOS_KNOWLEDGE",
        "OMNIAGENTOS_KNOWLEDGE_PG_DSN",
        "OMNIAGENTOS_KNOWLEDGE_ADMIN_DSN",
        "OMNIAGENTOS_VAULT_AUTOCOMMIT",
    ):
        os.environ.pop(stray, None)


def backup_live_db(source: Path, dest: Path) -> None:
    """Consistent online backup of the live WAL-mode db into *dest*.

    Verbatim safety idiom from scripts/loadtest_runner.py:backup_live_db --
    ``Connection.backup()`` rather than ``cp`` (WAL + concurrent writers make a
    raw file copy an inconsistent snapshot), with the source opened read-only
    via a ``file:`` URI so this process never takes a write lock on the live db.
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


def _stat_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    st = path.stat()
    return {"exists": True, "size": st.st_size, "mtime_ns": st.st_mtime_ns}


# ---------------------------------------------------------------------------
# Step 1: timing primitives
# ---------------------------------------------------------------------------


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    idx = min(len(sorted_values) - 1, int(p * len(sorted_values)))
    return sorted_values[idx]


def bench(fn: Callable[[], Any], *, iters: int, warmup: int) -> dict[str, Any]:
    """Time *fn* and report wall + CPU milliseconds per call.

    CPU time uses ``time.process_time_ns`` (process CPU, excludes sleep/IO
    wait). For these queries wall ~= CPU because SQLite reads served from the
    page cache are pure CPU; a large wall-CPU gap would mean real disk IO and
    is reported so it is visible rather than assumed away.
    """
    for _ in range(warmup):
        fn()
    wall: list[float] = []
    cpu_total_start = time.process_time_ns()
    for _ in range(iters):
        t0 = time.perf_counter_ns()
        fn()
        wall.append((time.perf_counter_ns() - t0) / 1e6)
    cpu_total_ms = (time.process_time_ns() - cpu_total_start) / 1e6
    wall_sorted = sorted(wall)
    return {
        "iters": iters,
        "warmup": warmup,
        "wall_ms_mean": statistics.fmean(wall),
        "wall_ms_median": statistics.median(wall),
        "wall_ms_p95": _percentile(wall_sorted, 0.95),
        "wall_ms_min": wall_sorted[0],
        "wall_ms_max": wall_sorted[-1],
        "wall_ms_stdev": statistics.pstdev(wall),
        "cpu_ms_mean": cpu_total_ms / iters,
    }


class QueryCounter:
    """Counts SQL statements executed on a connection via set_trace_callback.

    Used to VERIFY (not assume) the N+1 claim about
    ``SessionsDal.list_sessions`` -- ``_attach_session_error`` runs one events
    query per returned row (omniagentos/sessions/dal.py:272-291), so a
    ``limit=200`` call that returns 200 rows should show 201 statements.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self.statements: list[str] = []
        self._armed = False

    def __enter__(self) -> QueryCounter:
        self.statements = []
        self._armed = True
        self._conn.set_trace_callback(self._trace)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._armed = False
        self._conn.set_trace_callback(None)

    def _trace(self, sql: str) -> None:
        if self._armed:
            self.statements.append(" ".join(str(sql).split()))

    @property
    def count(self) -> int:
        return len(self.statements)


# ---------------------------------------------------------------------------
# Step 2: EXPLAIN QUERY PLAN on the exact SQL the hot loop runs
# ---------------------------------------------------------------------------

# These strings are copied verbatim from the implementations so the plan we
# report is the plan the SSE loop actually gets:
#   omniagentos/sessions/dal.py:list_sessions      (state=None branch)
#   omniagentos/sessions/dal.py:_attach_session_error
#   omniagentos/db/store.py:get_events_after
#   omniagentos/db/store.py:get_heartbeats
_PLAN_TARGETS: list[tuple[str, str, tuple[Any, ...]]] = [
    (
        "list_sessions(limit=200)",
        "SELECT * FROM sessions ORDER BY created_at DESC, id DESC LIMIT ?",
        (SESSIONS_LIMIT,),
    ),
    (
        "list_sessions -> _attach_session_error (per row, N+1)",
        "SELECT payload_json FROM events WHERE target_type = 'session' AND target_id = ? "
        "AND action = 'session.spawn_failed' ORDER BY id DESC LIMIT 1",
        ("ses_probe",),
    ),
    (
        "get_events_after(cursor, types=None, limit=500)",
        "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
        (0, EVENTS_LIMIT),
    ),
    (
        "get_events_after(cursor, types=[3 types], limit=500)",
        "SELECT * FROM events WHERE id > ? AND type IN (?, ?, ?) ORDER BY id ASC LIMIT ?",
        (0, "run.updated", "step.updated", "audit", EVENTS_LIMIT),
    ),
    (
        "get_heartbeats()",
        "SELECT * FROM heartbeats ORDER BY worker_id ASC",
        (),
    ),
]


def explain_query_plans(db_copy: Path) -> list[dict[str, Any]]:
    conn = sqlite3.connect(f"file:{urllib.parse.quote(str(db_copy))}?mode=ro", uri=True)
    out: list[dict[str, Any]] = []
    try:
        for label, sql, params in _PLAN_TARGETS:
            rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
            plan = [str(r[3]) for r in rows]
            joined = " | ".join(plan)
            out.append(
                {
                    "label": label,
                    "sql": " ".join(sql.split()),
                    "plan": plan,
                    "has_scan": "SCAN" in joined,
                    "has_temp_btree": "TEMP B-TREE" in joined,
                    "verdict": (
                        "SCAN + USE TEMP B-TREE (full table scan then in-memory sort)"
                        if "SCAN" in joined and "TEMP B-TREE" in joined
                        else "SCAN (full table scan)"
                        if "SCAN" in joined
                        else "index/PK search"
                    ),
                }
            )
    finally:
        conn.close()
    return out


def table_counts(db_copy: Path, tables: tuple[str, ...]) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{urllib.parse.quote(str(db_copy))}?mode=ro", uri=True)
    counts: dict[str, int] = {}
    try:
        for table in tables:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            counts[table] = (
                conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if exists else -1
            )
    finally:
        conn.close()
    return counts


# ---------------------------------------------------------------------------
# Step 3: the measurement itself
# ---------------------------------------------------------------------------


def measure(
    db_copy: Path, *, iters: int, warmup: int, event_types: list[str] | None
) -> dict[str, Any]:
    from omniagentos.db.store import SqliteStore
    from omniagentos.sessions.dal import SessionsDal

    _refuse_if_live(db_copy)
    store = SqliteStore(str(db_copy))
    dal = SessionsDal(str(db_copy))

    latest = store.latest_event_id()
    # Two cursor regimes matter and they are NOT the same cost:
    #   steady-state -- the overwhelmingly common case: the stream has caught
    #     up, so cursor == latest and the query returns ZERO rows every tick;
    #   replay/backlog -- cursor is 500 behind, so the query materialises a
    #     full 500-row page (what a reconnecting client costs on its first tick).
    steady_cursor = latest
    backlog_cursor = max(0, latest - EVENTS_LIMIT)

    results: dict[str, Any] = {}

    log(f"benchmarking (iters={iters}, warmup={warmup}) against copy {db_copy}")

    results["get_heartbeats"] = bench(lambda: store.get_heartbeats(), iters=iters, warmup=warmup)
    results["list_sessions_200"] = bench(
        lambda: dal.list_sessions(limit=SESSIONS_LIMIT), iters=iters, warmup=warmup
    )
    results["get_events_after_steady"] = bench(
        lambda: store.get_events_after(steady_cursor, types=event_types, limit=EVENTS_LIMIT),
        iters=iters,
        warmup=warmup,
    )
    results["get_events_after_backlog"] = bench(
        lambda: store.get_events_after(backlog_cursor, types=event_types, limit=EVENTS_LIMIT),
        iters=iters,
        warmup=warmup,
    )

    # --- verify (do not assume) the N+1 shape of list_sessions ---------------
    sessions_rows = len(dal.list_sessions(limit=SESSIONS_LIMIT))
    with QueryCounter(dal._connection) as qc:  # noqa: SLF001 - deliberate instrumentation
        dal.list_sessions(limit=SESSIONS_LIMIT)
    n_plus_one = {
        "rows_returned": sessions_rows,
        "sql_statements_executed": qc.count,
        "expected_if_n_plus_one": sessions_rows + 1,
        "is_n_plus_one": qc.count == sessions_rows + 1 and sessions_rows > 1,
        "distinct_statement_shapes": sorted({s[:120] for s in qc.statements}),
    }

    with QueryCounter(store._connection) as qc2:  # noqa: SLF001
        store.get_events_after(steady_cursor, types=event_types, limit=EVENTS_LIMIT)
    events_stmt_count = qc2.count

    with QueryCounter(store._connection) as qc3:  # noqa: SLF001
        store.get_heartbeats()
    heartbeat_stmt_count = qc3.count

    # SqliteStore exposes no close(); the DAL does. Both handles die with the process.
    for handle in (store, dal):
        closer = getattr(handle, "close", None)
        if callable(closer):
            closer()

    return {
        "latest_event_id": latest,
        "steady_cursor": steady_cursor,
        "backlog_cursor": backlog_cursor,
        "event_types_filter": event_types,
        "timings": results,
        "n_plus_one_check": n_plus_one,
        "sql_statements_per_call": {
            "get_events_after": events_stmt_count,
            "get_heartbeats": heartbeat_stmt_count,
            "list_sessions_200": qc.count,
        },
    }


def project(timings: dict[str, Any]) -> dict[str, Any]:
    """Turn per-call cost into per-connection load and a saturation point."""

    def cpu(name: str) -> float:
        return float(timings[name]["cpu_ms_mean"])

    def wall(name: str) -> float:
        return float(timings[name]["wall_ms_mean"])

    tick_cpu_ms = cpu("get_heartbeats") + cpu("list_sessions_200") + cpu("get_events_after_steady")
    tick_wall_ms = (
        wall("get_heartbeats") + wall("list_sessions_200") + wall("get_events_after_steady")
    )
    per_conn_cpu_ms_per_s = tick_cpu_ms * TICK_HZ
    per_conn_wall_ms_per_s = tick_wall_ms * TICK_HZ

    # Same numbers with session.updated switched off (a client that passes a
    # `types=` filter excluding session.updated skips list_sessions entirely --
    # control.py only calls it when `want_session_updated`).
    tick_cpu_ms_no_sessions = cpu("get_heartbeats") + cpu("get_events_after_steady")
    per_conn_cpu_ms_per_s_no_sessions = tick_cpu_ms_no_sessions * TICK_HZ

    def saturation(per_conn: float) -> float:
        return EVENT_LOOP_BUDGET_MS_PER_S / per_conn if per_conn > 0 else float("inf")

    return {
        "tick_hz": TICK_HZ,
        "per_tick_cpu_ms": tick_cpu_ms,
        "per_tick_wall_ms": tick_wall_ms,
        "per_connection_cpu_ms_per_wall_second": per_conn_cpu_ms_per_s,
        "per_connection_wall_ms_per_wall_second": per_conn_wall_ms_per_s,
        "saturation_connections_cpu": saturation(per_conn_cpu_ms_per_s),
        "saturation_connections_wall": saturation(per_conn_wall_ms_per_s),
        "per_connection_cpu_ms_per_wall_second_without_session_updated": (
            per_conn_cpu_ms_per_s_no_sessions
        ),
        "saturation_connections_without_session_updated": saturation(
            per_conn_cpu_ms_per_s_no_sessions
        ),
        "loop_at_50_percent_connections": saturation(per_conn_cpu_ms_per_s) * 0.5,
        "share_of_tick_list_sessions": (
            cpu("list_sessions_200") / tick_cpu_ms if tick_cpu_ms > 0 else 0.0
        ),
    }


# ---------------------------------------------------------------------------
# Step 4: reporting
# ---------------------------------------------------------------------------


def print_report(result: dict[str, Any]) -> None:
    t = result["measurement"]["timings"]
    p = result["projection"]
    n1 = result["measurement"]["n_plus_one_check"]

    log("=" * 78)
    log(f"SSE HOT-LOOP BASELINE  label={result['label']}  db_copy={result['db_copy']}")
    log("=" * 78)
    log("row counts: " + ", ".join(f"{k}={v}" for k, v in result["table_counts"].items()))
    log("")
    log(f"{'call':<34}{'mean ms':>10}{'p50':>9}{'p95':>9}{'max':>9}{'cpu ms':>10}")
    for name in (
        "get_heartbeats",
        "list_sessions_200",
        "get_events_after_steady",
        "get_events_after_backlog",
    ):
        s = t[name]
        log(
            f"{name:<34}{s['wall_ms_mean']:>10.3f}{s['wall_ms_median']:>9.3f}"
            f"{s['wall_ms_p95']:>9.3f}{s['wall_ms_max']:>9.3f}{s['cpu_ms_mean']:>10.3f}"
        )
    log("")
    log("N+1 verification (list_sessions):")
    log(
        f"  rows returned={n1['rows_returned']}  sql statements={n1['sql_statements_executed']} "
        f"(expected {n1['expected_if_n_plus_one']} if N+1)  -> "
        f"{'CONFIRMED N+1' if n1['is_n_plus_one'] else 'NOT the N+1 shape'}"
    )
    log("")
    log("EXPLAIN QUERY PLAN:")
    for plan in result["query_plans"]:
        log(f"  {plan['label']}")
        for line in plan["plan"]:
            log(f"      {line}")
        log(f"      -> {plan['verdict']}")
    log("")
    log("PROJECTION (single asyncio event-loop thread, 1000 CPU-ms per wall-second):")
    log(f"  per tick (3 queries)                    : {p['per_tick_cpu_ms']:.3f} CPU-ms")
    log(
        f"  per connection @ {TICK_HZ:.0f} Hz                  : {p['per_connection_cpu_ms_per_wall_second']:.3f} CPU-ms / wall-second"
    )
    log(
        f"    of which list_sessions(200)           : {p['share_of_tick_list_sessions'] * 100:.1f}%"
    )
    log(
        f"  SATURATION (100% of one loop thread)    : {p['saturation_connections_cpu']:.1f} concurrent SSE connections"
    )
    log(
        f"  50% of loop thread consumed at          : {p['loop_at_50_percent_connections']:.1f} connections"
    )
    log(
        f"  without session.updated (no list_sessions): {p['per_connection_cpu_ms_per_wall_second_without_session_updated']:.3f} CPU-ms/s "
        f"-> saturation at {p['saturation_connections_without_session_updated']:.1f} connections"
    )
    log("=" * 78)


_BASELINE_FIELDS = (
    ("get_heartbeats", "timings.get_heartbeats.wall_ms_mean", "ms"),
    ("list_sessions_200", "timings.list_sessions_200.wall_ms_mean", "ms"),
    ("get_events_after_steady", "timings.get_events_after_steady.wall_ms_mean", "ms"),
    ("get_events_after_backlog", "timings.get_events_after_backlog.wall_ms_mean", "ms"),
)


def _dig(obj: dict[str, Any], dotted: str) -> Any:
    cur: Any = obj
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def print_comparison(baseline: dict[str, Any], current: dict[str, Any]) -> None:
    log("")
    log("=" * 78)
    log(f"COMPARISON  baseline={baseline.get('label')}  current={current.get('label')}")
    log("=" * 78)
    log(f"{'metric':<34}{'baseline':>12}{'current':>12}{'delta':>12}{'change':>10}")

    def row(name: str, before: float, after: float, unit: str) -> None:
        delta = after - before
        pct = (delta / before * 100) if before else 0.0
        log(
            f"{name + ' (' + unit + ')':<34}{before:>12.3f}{after:>12.3f}{delta:>+12.3f}{pct:>+9.1f}%"
        )

    for name, path, unit in _BASELINE_FIELDS:
        try:
            row(
                name,
                float(_dig(baseline["measurement"], path)),
                float(_dig(current["measurement"], path)),
                unit,
            )
        except (KeyError, TypeError):
            log(f"{name:<34}{'n/a':>12}")
    row(
        "per-conn cpu",
        float(baseline["projection"]["per_connection_cpu_ms_per_wall_second"]),
        float(current["projection"]["per_connection_cpu_ms_per_wall_second"]),
        "ms/s",
    )
    row(
        "saturation",
        float(baseline["projection"]["saturation_connections_cpu"]),
        float(current["projection"]["saturation_connections_cpu"]),
        "conns",
    )
    log("=" * 78)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source-db", default=str(LIVE_DB), help="Live db to back up FROM (read-only)"
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path for the isolated copy (default /tmp/loadtest-sse-<label>.db)",
    )
    parser.add_argument(
        "--label", default="baseline", help="Run label; also names the default output files"
    )
    parser.add_argument("--iters", type=int, default=100, help="Timed iterations per call")
    parser.add_argument("--warmup", type=int, default=10, help="Untimed warmup iterations per call")
    parser.add_argument(
        "--types",
        default=None,
        help="Comma-separated event types for get_events_after (default: no filter, as an "
        "unfiltered EventSource sends)",
    )
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--baseline", default=None, help="Previous run's JSON to diff against")
    parser.add_argument("--scratch-dir", default=None, help="Isolated ledger/vault/workspace dir")
    parser.add_argument(
        "--keep-copy", action="store_true", help="Do not delete the db copy on exit"
    )
    args = parser.parse_args()

    label = args.label
    source_db = Path(args.source_db).expanduser().resolve()
    db_copy = _refuse_if_live(Path(args.db) if args.db else Path(f"/tmp/loadtest-sse-{label}.db"))
    scratch_dir = (
        Path(args.scratch_dir).expanduser().resolve()
        if args.scratch_dir
        else Path(f"/tmp/loadtest-sse-artifacts-{label}")
    )
    json_out = (
        Path(args.json_out).expanduser().resolve()
        if args.json_out
        else Path(f"/tmp/loadtest-sse-{label}.json")
    )
    event_types = [t.strip() for t in (args.types or "").split(",") if t.strip()] or None

    log(f"=== OmniAgentOS SSE hot-loop cost harness: label={label} ===")
    log(f"live source (read-only backup source): {source_db}")
    log(f"isolated copy (all measurement runs here): {db_copy}")

    live_before = _stat_snapshot(source_db)
    isolate_environment(db_copy, scratch_dir)
    backup_live_db(source_db, db_copy)

    counts = table_counts(db_copy, ("sessions", "events", "heartbeats", "runs"))
    log(f"row counts on copy: {counts}")

    plans = explain_query_plans(db_copy)
    measurement = measure(db_copy, iters=args.iters, warmup=args.warmup, event_types=event_types)
    projection = project(measurement["timings"])
    live_after = _stat_snapshot(source_db)

    result: dict[str, Any] = {
        "label": label,
        "generated_at": _now(),
        "source_db": str(source_db),
        "db_copy": str(db_copy),
        "iters": args.iters,
        "warmup": args.warmup,
        "python": sys.version.split()[0],
        "sqlite_version": sqlite3.sqlite_version,
        "platform": sys.platform,
        "table_counts": counts,
        "query_plans": plans,
        "measurement": measurement,
        "projection": projection,
        "live_db_stat_before": live_before,
        "live_db_stat_after": live_after,
        "live_db_untouched_by_this_process": True,  # never opened read-write; see module docstring
        "live_db_changed_externally": live_before != live_after,
    }

    print_report(result)
    if result["live_db_changed_externally"]:
        log(
            "NOTE: the live db's size/mtime changed during this run. That is the real "
            "runner/API writing it, not this harness -- this process only ever opened it "
            "read-only (file:...?mode=ro) for the backup."
        )

    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    log(f"wrote results json -> {json_out}")

    if args.baseline:
        baseline_path = Path(args.baseline).expanduser().resolve()
        if baseline_path.is_file():
            print_comparison(json.loads(baseline_path.read_text(encoding="utf-8")), result)
        else:
            log(f"WARNING: baseline {baseline_path} not found; skipping comparison")

    if not args.keep_copy:
        for suffix in ("", "-wal", "-shm"):
            stale = db_copy.with_name(db_copy.name + suffix)
            if stale.exists():
                stale.unlink()
        log(f"removed db copy {db_copy} (pass --keep-copy to retain it)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
