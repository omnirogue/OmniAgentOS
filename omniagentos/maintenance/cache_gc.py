"""Cache / derived-data garbage collector.

Run as ``python -m omniagentos.maintenance.cache_gc`` (a launchd template installs
it daily — see ``scripts/scheduler/com.omniagentos.cache-gc.plist.template``).

The dashboard is fast because the SLOW surfaces (revenue, cash, benchmarks,
knowledge, capabilities, vault, model rankings) are already DB-cached by the
collectors and read in ~10ms. Those caches and the high-volume SSE/run churn grow
without bound, so this GC trims clearly-derivable / re-collectable rows past a
configurable retention window. It NEVER touches audit-critical rows the operator
relies on: it keeps the SSE replay window, pending approvals, the real audit event
trail (``audit.event`` / ``approval.*`` / ``pause.changed``), active runs, and any
metric snapshot still linked to a goal fact.

Retention is tiered because "old" means different things per table:

* ``--days`` (default 7)        transient SSE notification events + resolved approvals
  + archived board tasks.
* ``--runs-days`` (default 30)  finished runs (and their steps/artifacts/approvals).
* ``--facts-days`` (default 365) revenue / bank / metric-snapshot history — the
  financial + trend record the operator actually reads, so it is kept for a year
  by default rather than a week.

Each is overridable via CLI flag or the matching ``OMNIAGENTOS_CACHE_GC_*`` env var.
``--dry-run`` reports what WOULD be deleted without writing.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import UTC, datetime, timedelta

from omniagentos.contracts import default_db_path
from omniagentos.db.store import _connect

# Pure SSE-notification event types: they are reconstructable at any time from the
# runs / steps / tasks tables, so once they fall out of the replay window and past
# retention they are safe to drop. The durable audit trail (audit.event,
# approval.requested, approval.decided, pause.changed) is intentionally preserved.
_TRANSIENT_EVENT_TYPES = ("run.updated", "step.updated", "task.updated")

# Resolved approvals only — pending approvals are governance-critical and kept.
_RESOLVED_APPROVAL_STATES = ("approved", "rejected", "expired")

# Terminal run states whose rows (and children) are eligible for pruning.
_TERMINAL_RUN_STATES = ("completed", "failed", "cancelled")

# Any of these set (non-empty) marks a simulation context; the destructive disk
# sweep then refuses to guess its var root — see _gc_chrome_profiles.
# Two deliberate asymmetries vs the sessions/* sim gates (recorded decisions,
# not accidents): (1) an explicit, existing OMNIAGENTOS_VAR/VAR_DIR is trusted
# without a legacy-inode refusal — explicit config is operator intent and the
# payload is re-collectable browser caches, not credentials; (2) ANY non-empty
# value (including "0") counts as a sim indicator, stricter than the sessions
# `== "1"` predicate, because this guards a destructive fallback where
# "ambiguously sim" must already refuse.
_SIM_ENV_KEYS = (
    "OMNIAGENTOS_SIM_MODE",
    "OMNIAGENTOS_SIM_CAMPAIGN",
    "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
)

# Rows kept regardless of age so the SSE stream can always replay recent history.
DEFAULT_REPLAY_KEEP = 500

DEFAULT_DAYS = 7
DEFAULT_RUNS_DAYS = 30
DEFAULT_FACTS_DAYS = 365

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _ts_cutoff(days: int, *, now: datetime | None = None) -> str:
    """UTC timestamp `days` in the past, in the canonical `utc_now_iso` format."""
    moment = (now or datetime.now(UTC)) - timedelta(days=days)
    return moment.strftime(_TS_FMT)


def _day_cutoff(days: int, *, now: datetime | None = None) -> str:
    """Eastern-agnostic YYYY-MM-DD cutoff for the daily `day` fact columns."""
    moment = (now or datetime.now(UTC)) - timedelta(days=days)
    return moment.strftime("%Y-%m-%d")


def _count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()
    return int(row[0]) if row else 0


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str] | None:
    """Return column names for ``table``, or ``None`` if the table is missing.

    ``PRAGMA table_info`` returns an empty result set for unknown tables rather
    than raising; treat that as schema uncertainty so callers can fail closed.
    """
    # table is an internal identifier, never operator input.
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except sqlite3.OperationalError:
        return None
    if not rows:
        return None
    return {str(row[1]) for row in rows}


# --- Individual sweeps ------------------------------------------------------
# Each returns a dict {table: rows}. When `apply` is False it counts only.


def _gc_events(
    conn: sqlite3.Connection, ts_cutoff: str, replay_keep: int, *, apply: bool
) -> dict[str, int]:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()
    latest = int(row[0]) if row else 0
    keep_above = latest - replay_keep
    placeholders = ",".join("?" for _ in _TRANSIENT_EVENT_TYPES)
    where = f"FROM events WHERE type IN ({placeholders}) AND ts < ? AND id < ?"
    params = (*_TRANSIENT_EVENT_TYPES, ts_cutoff, keep_above)
    if not apply:
        return {"events": _count(conn, f"SELECT id {where}", params)}
    cur = conn.execute(f"DELETE {where}", params)
    return {"events": cur.rowcount}


def _gc_metric_snapshots(
    conn: sqlite3.Connection, ts_cutoff: str, *, apply: bool
) -> dict[str, int]:
    # Metric snapshots back the goals/steward trend charts, so they follow the
    # long facts retention. Never drop a snapshot still linked to a goal fact.
    where = (
        "FROM metric_snapshots WHERE captured_at IS NOT NULL AND captured_at < ? "
        "AND id NOT IN (SELECT fact_id FROM goal_facts WHERE fact_id IS NOT NULL)"
    )
    params = (ts_cutoff,)
    if not apply:
        return {"metric_snapshots": _count(conn, f"SELECT id {where}", params)}
    cur = conn.execute(f"DELETE {where}", params)
    return {"metric_snapshots": cur.rowcount}


def _gc_resolved_approvals(
    conn: sqlite3.Connection, ts_cutoff: str, *, apply: bool
) -> dict[str, int]:
    placeholders = ",".join("?" for _ in _RESOLVED_APPROVAL_STATES)
    where = (
        f"FROM approvals WHERE state IN ({placeholders}) AND COALESCE(decided_at, created_at) < ?"
    )
    params = (*_RESOLVED_APPROVAL_STATES, ts_cutoff)
    if not apply:
        return {"approvals": _count(conn, f"SELECT id {where}", params)}
    cur = conn.execute(f"DELETE {where}", params)
    return {"approvals": cur.rowcount}


def _gc_financial_facts(
    conn: sqlite3.Connection, day_cutoff: str, *, apply: bool
) -> dict[str, int]:
    result: dict[str, int] = {}
    for table in ("revenue_facts", "bank_facts", "bank_transactions"):
        where = f"FROM {table} WHERE day < ?"
        if not apply:
            result[table] = _count(conn, f"SELECT rowid {where}", (day_cutoff,))
        else:
            result[table] = conn.execute(f"DELETE {where}", (day_cutoff,)).rowcount
    return result


def _gc_runs(conn: sqlite3.Connection, ts_cutoff: str, *, apply: bool) -> dict[str, int]:
    # Only FINISHED runs older than the cutoff. foreign_keys=ON, so children that
    # REFERENCE runs (steps, artifacts, approvals) MUST be removed first; the
    # subquery re-selects the same runs while they still exist. Runs referenced by
    # anything the operator reads later are protected by keeping this to terminal,
    # aged rows only.
    placeholders = ",".join("?" for _ in _TERMINAL_RUN_STATES)
    candidates = (
        f"SELECT id FROM runs WHERE state IN ({placeholders}) "
        "AND finished_at IS NOT NULL AND finished_at < ?"
    )
    params = (*_TERMINAL_RUN_STATES, ts_cutoff)
    if not apply:
        return {
            "runs": _count(conn, candidates, params),
            "steps": _count(conn, f"SELECT id FROM steps WHERE run_id IN ({candidates})", params),
        }
    steps = conn.execute(f"DELETE FROM steps WHERE run_id IN ({candidates})", params).rowcount
    conn.execute(f"DELETE FROM artifacts WHERE run_id IN ({candidates})", params)
    conn.execute(f"DELETE FROM approvals WHERE run_id IN ({candidates})", params)
    conn.execute(f"DELETE FROM idempotency WHERE run_id IN ({candidates})", params)
    runs = conn.execute(f"DELETE FROM runs WHERE id IN ({candidates})", params).rowcount
    return {"runs": runs, "steps": steps}


def _gc_archived_board_tasks(
    conn: sqlite3.Connection, ts_cutoff: str, *, apply: bool
) -> dict[str, int]:
    # Only archived board tasks older than the cutoff. These are completed cards
    # that the operator has marked as done and archived; keeping them would just
    # clutter the board history. Un-archived cards are never touched.
    where = "FROM board_tasks WHERE archived_at IS NOT NULL AND archived_at < ?"
    params = (ts_cutoff,)
    if not apply:
        return {"board_tasks": _count(conn, f"SELECT id {where}", params)}
    cur = conn.execute(f"DELETE {where}", params)
    return {"board_tasks": cur.rowcount}


def _gc_notifications(conn: sqlite3.Connection, ts_cutoff: str, *, apply: bool) -> dict[str, int]:
    """H-01 / F7: prune aged *read* notifications; never touch unread rows.

    Real schema (migrations 030/050): unread means ``read_at IS NULL``. There is
    no ``dismissed`` or ``status`` column. The keep-unread predicate must use
    those real columns only.

    Fail closed on schema/predicate uncertainty: if the table is missing, or
    ``id`` / ``created_at`` / ``read_at`` cannot be proven present, or the
    DELETE/COUNT itself fails, delete nothing rather than age-only sweeping
    unread rows.
    """
    columns = _table_columns(conn, "notifications")
    required = {"id", "created_at", "read_at"}
    if columns is None or not required.issubset(columns):
        return {"notifications": 0}

    # Unread = read_at IS NULL. Only aged + already-read rows are eligible.
    where = "FROM notifications WHERE created_at < ? AND read_at IS NOT NULL"
    params: tuple[object, ...] = (ts_cutoff,)
    try:
        if not apply:
            return {"notifications": _count(conn, f"SELECT id {where}", params)}
        cur = conn.execute(f"DELETE {where}", params)
        return {"notifications": cur.rowcount}
    except sqlite3.OperationalError:
        return {"notifications": 0}


def _gc_chrome_profiles(*, apply: bool) -> dict[str, int]:
    """F3: remove browser profile caches under managed project workspaces.

    Var-root knob order matches reliability/memory.py: ``OMNIAGENTOS_VAR``, then
    ``OMNIAGENTOS_VAR_DIR``, then the pre-existing fallbacks (cwd when both are
    unset — ``Path("")`` is ``"."`` — or the package anchor when set-but-dangling).
    scripts/launch-env.sh exports both knobs, so launcher-managed runs resolve on
    the first. In a simulation context no fallback is trusted: a guessed root can
    be the production checkout's var, so the sweep refuses instead.
    """
    import shutil
    from pathlib import Path

    var_raw = os.environ.get("OMNIAGENTOS_VAR")
    var_root = Path(var_raw or "").expanduser()
    env_rooted = bool(var_raw) and var_root.is_dir()
    if not env_rooted:
        var_dir_raw = os.environ.get("OMNIAGENTOS_VAR_DIR")
        if var_dir_raw:
            candidate = Path(var_dir_raw).expanduser()
            if candidate.is_dir():
                var_root = candidate
                env_rooted = True
    if not env_rooted:
        active_sim = [key for key in _SIM_ENV_KEYS if os.environ.get(key)]
        if active_sim:
            raise RuntimeError(
                "cache_gc: refusing chrome-profile GC in a simulation context "
                f"({', '.join(active_sim)} set): neither OMNIAGENTOS_VAR nor "
                "OMNIAGENTOS_VAR_DIR points at an existing var root, so the "
                "sweep would fall back outside the campaign sandbox. Set one "
                "of them to the campaign var root."
            )
    if not var_root.is_dir():
        # Default next to package: <repo>/var
        var_root = Path(__file__).resolve().parents[2] / "var"
    if not var_root.is_dir():
        return {"chrome_profile_dirs": 0}
    removed = 0
    patterns = (".jambot-chrome", "*-chrome")
    projects = var_root / "projects"
    if not projects.is_dir():
        return {"chrome_profile_dirs": 0}
    for proj in projects.iterdir():
        if not proj.is_dir():
            continue
        workspace = proj / "workspace"
        if not workspace.is_dir():
            continue
        for child in workspace.iterdir():
            name = child.name
            if name == ".jambot-chrome" or (name.endswith("-chrome") and child.is_dir()):
                if apply:
                    try:
                        shutil.rmtree(child, ignore_errors=True)
                        removed += 1
                    except OSError:
                        pass
                else:
                    removed += 1
    del patterns
    return {"chrome_profile_dirs": removed}


def collect_garbage(
    db_path: str,
    *,
    days: int = DEFAULT_DAYS,
    runs_days: int = DEFAULT_RUNS_DAYS,
    facts_days: int = DEFAULT_FACTS_DAYS,
    replay_keep: int = DEFAULT_REPLAY_KEEP,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete aged cache/derived rows and return {table: rows_removed}.

    With ``dry_run=True`` nothing is written and the counts are what *would* be
    deleted. All sweeps run inside a single IMMEDIATE transaction so the DB is
    never left half-collected.
    """
    events_cutoff = _ts_cutoff(days, now=now)
    approvals_cutoff = events_cutoff
    board_cutoff = events_cutoff
    runs_cutoff = _ts_cutoff(runs_days, now=now)
    facts_ts_cutoff = _ts_cutoff(facts_days, now=now)
    facts_day_cutoff = _day_cutoff(facts_days, now=now)
    apply = not dry_run

    conn = _connect(db_path)
    report: dict[str, int] = {}
    try:
        if apply:
            conn.execute("BEGIN IMMEDIATE")
        report.update(_gc_events(conn, events_cutoff, replay_keep, apply=apply))
        report.update(_gc_metric_snapshots(conn, facts_ts_cutoff, apply=apply))
        report.update(_gc_resolved_approvals(conn, approvals_cutoff, apply=apply))
        report.update(_gc_financial_facts(conn, facts_day_cutoff, apply=apply))
        report.update(_gc_runs(conn, runs_cutoff, apply=apply))
        report.update(_gc_archived_board_tasks(conn, board_cutoff, apply=apply))
        report.update(_gc_notifications(conn, events_cutoff, apply=apply))
        if apply:
            conn.commit()
    except BaseException:
        if apply and conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()
    # Disk sweep outside the DB transaction (F3 browser profiles).
    report.update(_gc_chrome_profiles(apply=apply))
    return report


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.maintenance.cache_gc",
        description="Delete aged cache / derived rows from the OmniAgentOS DB.",
    )
    parser.add_argument("--db", default=default_db_path(), help="SQLite DB path.")
    parser.add_argument(
        "--days",
        type=int,
        default=_int_env("OMNIAGENTOS_CACHE_GC_DAYS", DEFAULT_DAYS),
        help="Retention (days) for transient SSE events + resolved approvals + archived board tasks.",
    )
    parser.add_argument(
        "--runs-days",
        type=int,
        default=_int_env("OMNIAGENTOS_CACHE_GC_RUNS_DAYS", DEFAULT_RUNS_DAYS),
        help="Retention (days) for finished runs and their steps/artifacts.",
    )
    parser.add_argument(
        "--facts-days",
        type=int,
        default=_int_env("OMNIAGENTOS_CACHE_GC_FACTS_DAYS", DEFAULT_FACTS_DAYS),
        help="Retention (days) for revenue/bank/metric-snapshot history.",
    )
    parser.add_argument(
        "--replay-keep",
        type=int,
        default=_int_env("OMNIAGENTOS_CACHE_GC_REPLAY_KEEP", DEFAULT_REPLAY_KEEP),
        help="Most-recent events kept regardless of age (SSE replay window).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be deleted without writing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = collect_garbage(
        args.db,
        days=args.days,
        runs_days=args.runs_days,
        facts_days=args.facts_days,
        replay_keep=args.replay_keep,
        dry_run=args.dry_run,
    )
    verb = "would delete" if args.dry_run else "deleted"
    total = sum(report.values())
    for table in sorted(report):
        print(f"{verb} {report[table]:>8} rows from {table}")
    print(f"{verb} {total:>8} rows total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
