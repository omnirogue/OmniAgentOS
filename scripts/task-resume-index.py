#!/usr/bin/env python3
"""List interrupted work from runtime stores without changing their contents."""

import argparse
import datetime
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESUME_NEEDLE = b"```resume"
RESUME_TAIL_BYTES = 16 * 1024
ROLE_ALIASES = {"planning": "planner", "repair": "reviewer", "integration": "implementer"}


def _utc_iso(timestamp):
    return (
        datetime.datetime.fromtimestamp(timestamp, datetime.UTC).isoformat().replace("+00:00", "Z")
    )


def _now_iso():
    return datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")


def _git_root(directory):
    """Return a containing Git root, if Git can determine one."""
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return None
    return (
        Path(result.stdout.strip()).resolve()
        if result.returncode == 0 and result.stdout.strip()
        else None
    )


def _resolve_paths(args):
    """Keep filesystem sources rooted with an explicit DB or repository selection."""
    if args.repo_root:
        repo_root = args.repo_root.resolve()
    elif args.db:
        repo_root = _git_root(args.db.parent) or REPO_ROOT
    else:
        repo_root = _git_root(Path.cwd()) or REPO_ROOT
    db_path = args.db.resolve() if args.db else repo_root / "var" / "runtime" / "state.sqlite3"
    swarm_root = args.swarm_root.resolve() if args.swarm_root else repo_root / "var" / "swarm"
    ledger_path = (
        args.ledger.resolve() if args.ledger else repo_root / "var" / "loopqueue" / "ledger.jsonl"
    )
    return repo_root, db_path, swarm_root, ledger_path


def _open_db(path, immutable=False):
    """Open an existing SQLite database read-only; immutable requires a quiescent DB."""
    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    return sqlite3.connect(f"file:{path}?{query}", uri=True)


def _query(conn, statement, source=None):
    """Run a query, recording a per-table absence rather than laundering it to []."""
    try:
        cursor = conn.execute(statement)
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            if source is not None:
                source.update(status="table_absent", detail=f"not found: {exc}")
            return []
        raise
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _source(path_key, path, status="ok", detail="", **extra):
    return {path_key: str(path), "status": status, "detail": detail, **extra}


def _collect_db_rows(db_path, sources, immutable=False):
    """Collect DB sections and receipts without mutating database sidecars."""
    empty = {"board_tasks": [], "tasks": [], "live_sessions": []}
    if not db_path.exists():
        sources["db"].update(status="missing", detail=f"Database not found: {db_path}")
        for name in ("board_tasks", "tasks", "task_sessions"):
            sources[name].update(status="table_absent", detail="database not found")
        return empty, [f"Database not found: {db_path}"]

    sidecars = [Path(f"{db_path}{suffix}") for suffix in ("-wal", "-shm")]
    existed_before = {path: path.exists() for path in sidecars}
    conn = None
    try:
        conn = _open_db(db_path, immutable=immutable)
        board_statement = (
            "SELECT id, title, status, updated_at FROM board_tasks "
            "WHERE status NOT IN ('done', 'cancelled') AND archived_at IS NULL"
        )
        try:
            board_rows = _query(conn, board_statement, sources["board_tasks"])
        except sqlite3.OperationalError as exc:
            if "no such column" not in str(exc).lower() or "archived_at" not in str(exc).lower():
                raise
            sources["board_tasks"]["detail"] = (
                "archived_at column unavailable; archive filter skipped"
            )
            board_rows = _query(
                conn,
                "SELECT id, title, status, updated_at FROM board_tasks "
                "WHERE status NOT IN ('done', 'cancelled')",
                sources["board_tasks"],
            )
        rows = {
            "board_tasks": board_rows,
            "tasks": _query(
                conn,
                "SELECT id, title, state, updated_at FROM tasks "
                "WHERE state NOT IN ('completed', 'cancelled')",
                sources["tasks"],
            ),
            "live_sessions": _query(
                conn,
                "SELECT id, board_task_id, seq, harness, model, started_at FROM task_sessions "
                "WHERE ended_at IS NULL",
                sources["task_sessions"],
            ),
        }
        try:
            rows["_known_run_ids"] = {
                row["id"] for row in _query(conn, "SELECT id FROM swarm_runs")
            }
        except sqlite3.Error:
            rows["_known_run_ids"] = None
    except (OSError, sqlite3.Error) as exc:
        sources["db"].update(status="unreadable", detail=f"Unable to read database: {exc}")
        raise RuntimeError(f"Unable to open database read-only: {db_path}: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()
        # A mode=ro reader may create WAL coordination sidecars.  Never unlink
        # them: a concurrent writer can create the same files after our snapshot.
        if not immutable and any(not existed_before[path] and path.exists() for path in sidecars):
            detail = "read created wal-index sidecars (-wal/-shm); they are recreated by any reader and were left in place"
            sources["db"]["detail"] = "; ".join(
                value for value in (sources["db"].get("detail"), detail) if value
            )

    return rows, []


def _workbook_has_resume_block(path):
    """Probe a tail window, returning None when an older block is unknowable."""
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - RESUME_TAIL_BYTES - (len(RESUME_NEEDLE) - 1)))
        tail = handle.read()
    if RESUME_NEEDLE in tail:
        return True
    return None if size > RESUME_TAIL_BYTES else False


def _resume_probe(value, workbook_exists=True):
    if not workbook_exists:
        return "no_workbook"
    return "tail_last_16384_bytes; truncated/unknown" if value is None else "tail_last_16384_bytes"


def _is_namespace_dir(path):
    return path.name in {
        "clones",
        "worktrees",
        "sessions",
        "quarantine",
        "verdicts",
        "sol-verdicts",
        "plan-reviews",
        "a2a",
        "role-ab",
    } or path.name.startswith("worktree-salvage")


def _collect_swarm_docs(swarm_root, known_run_ids):
    """Inspect each task directory plus up to two nested contract directories."""
    source = _source("root", swarm_root)
    source["detail"] = "resume_block_v1 is forward-looking; U1's producer may not be landed yet."
    if not swarm_root.is_dir():
        source.update(status="missing", detail=f"Swarm directory not found: {swarm_root}")
        return [], [], source
    entries, unmatched = [], []
    try:
        run_dirs = list(swarm_root.iterdir())
    except OSError as exc:
        source.update(
            status="missing", detail=f"Unable to read swarm directory: {swarm_root}: {exc}"
        )
        return [], [], source
    skipped_namespace_dirs = [path.name for path in run_dirs if path.is_dir() and _is_namespace_dir(path)]
    if skipped_namespace_dirs:
        source["namespace_dirs_skipped"] = sorted(skipped_namespace_dirs)
    run_dirs = [path for path in run_dirs if not (path.is_dir() and _is_namespace_dir(path))]
    top_level_ids = {path.name for path in run_dirs if path.is_dir()}
    if known_run_ids is not None and not (known_run_ids & top_level_ids):
        # Historic stores can have a swarm_runs table but no directory-id mapping.
        # Keep that scan explicitly unknown rather than calling every document noise.
        known_run_ids = None
        source["detail"] += (
            " No swarm_runs.id matches a top-level directory; run identity is unknown."
        )
    for run_dir in run_dirs:
        if not run_dir.is_dir():
            continue
        try:
            task_dirs = [path for path in run_dir.iterdir() if path.is_dir()]
        except OSError as exc:
            unmatched.append(
                {
                    "run_id": run_dir.name,
                    "task_id": None,
                    "dir": str(run_dir),
                    "error": str(exc),
                    "run_known": False if known_run_ids is not None else None,
                }
            )
            continue
        for task_dir in task_dirs:
            doc_dirs = [task_dir]
            for pattern in ("*", "*/*"):
                doc_dirs.extend(path for path in task_dir.glob(pattern) if path.is_dir())
            for doc_dir in doc_dirs:
                task_path, workbook_path = doc_dir / "TASK.md", doc_dir / "WORKBOOK.md"
                if not task_path.is_file() and not workbook_path.is_file():
                    continue
                try:
                    workbook_exists = workbook_path.is_file()
                    probe = _workbook_has_resume_block(workbook_path) if workbook_exists else False
                    row = {
                        "run_id": run_dir.name,
                        "task_id": task_dir.name,
                        "dir": str(doc_dir),
                        "task_md_path": str(task_path) if task_path.is_file() else None,
                        "task_md_exists": task_path.is_file(),
                        "task_md_mtime": _utc_iso(task_path.stat().st_mtime)
                        if task_path.is_file()
                        else None,
                        "workbook_md_path": str(workbook_path) if workbook_exists else None,
                        "workbook_md_exists": workbook_exists,
                        "workbook_md_mtime": _utc_iso(workbook_path.stat().st_mtime)
                        if workbook_exists
                        else None,
                        "resume_block_v1": probe,
                        "resume_probe": _resume_probe(probe, workbook_exists),
                        "run_known": (run_dir.name in known_run_ids)
                        if known_run_ids is not None
                        else None,
                    }
                except OSError as exc:
                    row = {
                        "run_id": run_dir.name,
                        "task_id": task_dir.name,
                        "dir": str(doc_dir),
                        "error": str(exc),
                        "run_known": (run_dir.name in known_run_ids)
                        if known_run_ids is not None
                        else None,
                    }
                (entries if row["run_known"] is not False else unmatched).append(row)

    def sort_key(row):
        return (
            (row.get("workbook_md_mtime") or "") > (row.get("task_md_mtime") or ""),
            max(row.get("task_md_mtime") or "", row.get("workbook_md_mtime") or ""),
        )

    entries.sort(key=sort_key, reverse=True)
    unmatched.sort(key=sort_key, reverse=True)
    return entries, unmatched, source


def _collect_role_stats(ledger_path):
    """Stream every ledger event, retaining receipts for missing/unreadable input."""
    source = _source("path", ledger_path, lines_read=0, lines_unparseable=0)
    stats = {}
    if not ledger_path.exists():
        source.update(status="missing", detail=f"Ledger not found: {ledger_path}")
        return stats, source
    try:
        with ledger_path.open(encoding="utf-8") as handle:
            for line in handle:
                source["lines_read"] += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    source["lines_unparseable"] += 1
                    continue
                role, event = record.get("role"), record.get("event")
                if not isinstance(role, str) or not isinstance(event, str):
                    continue
                normalized = ROLE_ALIASES.get(role.lower(), role.lower())
                if normalized:
                    stats.setdefault(normalized, {})[event] = (
                        stats.setdefault(normalized, {}).get(event, 0) + 1
                    )
    except OSError as exc:
        source.update(status="unreadable", detail=f"Unable to read ledger: {exc}")
    return dict(sorted(stats.items())), source


def _apply_filters(
    rows,
    timestamp,
    limit,
    since,
    statuses=None,
    status_key=None,
    live_ids=frozenset(),
    resume_boost=False,
):
    if since:
        cutoff = _as_utc_datetime(since)
        rows = [
            row
            for row in rows
            if not row.get(timestamp)
            or _timestamp_is_at_or_after(row.get(timestamp), cutoff)
        ]
    if statuses and status_key:
        rows = [row for row in rows if row.get(status_key) in statuses]
    rows.sort(
        # A live session is the strongest DB resume clue; a workbook newer than
        # its contract is the corresponding filesystem clue.
        key=lambda row: (
            (row.get("workbook_md_mtime") or "") > (row.get("task_md_mtime") or "")
            if resume_boost
            else row.get("id") in live_ids,
            row.get(timestamp) or "",
        ),
        reverse=True,
    )
    return rows[:limit]


def _as_utc_datetime(value):
    if isinstance(value, datetime.datetime):
        parsed = value
    else:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.UTC)
    return parsed.astimezone(datetime.UTC)


def _timestamp_is_at_or_after(value, cutoff):
    """Keep malformed row timestamps: one bad receipt must not hide its row."""
    try:
        return _as_utc_datetime(value) >= cutoff
    except (AttributeError, TypeError, ValueError):
        # AttributeError: a non-string, non-datetime value (int/float/bytes)
        # hits `.replace` in _as_utc_datetime. Unreachable on a runtime DB
        # (all timestamp columns are TEXT NOT NULL) but possible on a foreign
        # DB — keep the row, per this function's contract, never crash the query.
        return True


def _limited_section(rows, source, timestamp, limit, since, statuses=None, status_key=None, **kwargs):
    filtered = _apply_filters(
        rows, timestamp, None, since, statuses, status_key, **kwargs
    )
    shown = filtered[:limit]
    source.update(total=len(filtered), shown=len(shown), truncated=len(filtered) > len(shown))
    return shown


def _collect_index(db_path, swarm_root, ledger_path, immutable, limit, since, statuses):
    sources = {
        "db": _source("path", db_path),
        "board_tasks": {"status": "ok", "detail": ""},
        "tasks": {"status": "ok", "detail": ""},
        "task_sessions": {"status": "ok", "detail": ""},
        "live_sessions": {"status": "ok", "detail": ""},
    }
    db_error = None
    try:
        database_rows, notes = _collect_db_rows(db_path, sources, immutable)
    except RuntimeError as exc:
        # Role statistics and filesystem evidence remain useful when a DB is corrupt.
        # main preserves exit 1 for the default DB-only invocation, but --role-stats
        # renders this partial result with the failure receipt below.
        db_error = exc
        notes = [str(exc)]
        database_rows = {
            "board_tasks": [],
            "tasks": [],
            "live_sessions": [],
            "_known_run_ids": None,
        }
        for name in ("board_tasks", "tasks", "task_sessions", "live_sessions"):
            sources[name].update(status="unreadable", detail="database unreadable")
    live_ids = {
        row.get("board_task_id")
        for row in database_rows["live_sessions"]
        if row.get("board_task_id")
    }
    sources["live_sessions"].update(
        status=sources["task_sessions"]["status"], detail=sources["task_sessions"]["detail"]
    )
    known_ids = database_rows.pop("_known_run_ids", None)
    swarm_docs, unmatched_dirs, swarm_source = _collect_swarm_docs(swarm_root, known_ids)
    role_stats, ledger_source = _collect_role_stats(ledger_path)
    database_rows["board_tasks"] = _limited_section(
        database_rows["board_tasks"], sources["board_tasks"], "updated_at", limit, since,
        statuses, "status", live_ids=live_ids
    )
    database_rows["tasks"] = _limited_section(
        database_rows["tasks"], sources["tasks"], "updated_at", limit, since, statuses,
        "state", live_ids=live_ids
    )
    database_rows["live_sessions"] = _limited_section(
        database_rows["live_sessions"], sources["live_sessions"], "started_at", limit, since
    )
    sources["swarm"] = swarm_source
    sources["swarm_docs"] = dict(swarm_source)
    sources["unmatched_dirs"] = dict(swarm_source)
    database_rows["swarm_docs"] = _limited_section(
        swarm_docs, sources["swarm_docs"], "workbook_md_mtime", limit, since, resume_boost=True
    )
    database_rows["unmatched_dirs"] = _limited_section(
        unmatched_dirs, sources["unmatched_dirs"], "workbook_md_mtime", limit, since,
        resume_boost=True
    )
    sources["ledger"] = ledger_source
    return database_rows, notes, sources, role_stats, db_error


def _format_table(headers, rows):
    rendered = [
        [str(row.get(key, "") if row.get(key) is not None else "") for key in headers]
        for row in rows
    ]
    widths = [
        max(len(header), *(len(row[i]) for row in rendered)) for i, header in enumerate(headers)
    ]
    lines = [
        "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)),
        "  ".join("-" * width for width in widths),
    ]
    lines.extend(
        "  ".join(value.ljust(widths[i]) for i, value in enumerate(row)) for row in rendered
    )
    return "\n".join(lines)


def _print_index(payload):
    sections = (
        ("board_tasks", ("id", "title", "status", "updated_at")),
        ("tasks", ("id", "title", "state", "updated_at")),
        ("live_sessions", ("id", "board_task_id", "seq", "harness", "model", "started_at")),
        (
            "swarm_docs",
            ("run_id", "task_id", "dir", "resume_block_v1", "resume_probe", "run_known", "error"),
        ),
        ("unmatched_dirs", ("run_id", "task_id", "dir", "run_known", "error")),
    )
    for name, headers in sections:
        print(f"\n{name}:")
        print(_format_table(headers, payload[name]) if payload[name] else "No rows found.")
        source = payload["sources"][name]
        print(
            f"showing {source['shown']} of {source['total']} "
            f"({'truncated by limit' if source['truncated'] else 'not truncated'}, limit={payload['limit']})"
        )
    print("\nSources:")
    for name, source in payload["sources"].items():
        receipt = source.get("detail") or source.get("path") or source.get("root") or "no issues"
        if name == "ledger":
            receipt += f"; lines_read={source['lines_read']} lines_unparseable={source['lines_unparseable']}"
        print(f"{name}: {source['status']} — {receipt}")


def _print_role_stats(stats, source):
    events = sorted({event for counts in stats.values() for event in counts})
    if not stats:
        print("No role stats found.")
    else:
        print(
            _format_table(
                ("role", *events), [{"role": role, **counts} for role, counts in stats.items()]
            )
        )
    print(
        f"Ledger: {source['status']} — lines_read={source['lines_read']} lines_unparseable={source['lines_unparseable']}"
    )


def _parse_since(value):
    try:
        _as_utc_datetime(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be ISO 8601") from exc
    return _as_utc_datetime(value)


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description=(
            "Read-only interrupted-work discovery index. It reports both board_tasks and tasks and presupposes neither is authoritative — that decision is pending. Terminal rows excluded: board_tasks done/cancelled; tasks completed/cancelled."
        )
    )
    parser.add_argument(
        "--db", type=Path, help="SQLite database path; defaults to the current Git repository."
    )
    parser.add_argument(
        "--repo-root", type=Path, help="Repository root; inferred from --db's parent when possible."
    )
    parser.add_argument(
        "--swarm-root", type=Path, help="Swarm root (default: <repo-root>/var/swarm)."
    )
    parser.add_argument(
        "--ledger", type=Path, help="Ledger path (default: <repo-root>/var/loopqueue/ledger.jsonl)."
    )
    parser.add_argument(
        "--immutable",
        action="store_true",
        help="Use SQLite immutable=1. Safe only for a quiescent database; live WAL changes are invisible/inconsistent.",
    )
    parser.add_argument(
        "--limit", type=int, default=50, help="Maximum rows per section (default: 50)."
    )
    parser.add_argument(
        "--since", type=_parse_since, help="Include rows at or after this ISO 8601 timestamp."
    )
    parser.add_argument(
        "--status", help="Comma-separated board_tasks.status/tasks.state allow-list."
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of a human-readable table."
    )
    parser.add_argument(
        "--role-stats", action="store_true", help="Add streamed per-role event counts to the index."
    )
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be positive")
    return args


def main(argv=None):
    args = _parse_args(argv)
    repo_root, db_path, swarm_root, ledger_path = _resolve_paths(args)
    statuses = (
        {status.strip() for status in args.status.split(",") if status.strip()}
        if args.status
        else None
    )
    try:
        index, notes, sources, role_stats, db_error = _collect_index(
            db_path, swarm_root, ledger_path, args.immutable, args.limit, args.since, statuses
        )
    except RuntimeError as exc:  # pragma: no cover - defensive future-proofing
        print(f"task-resume-index: {exc}", file=sys.stderr)
        return 1
    payload = {
        "generated_at": _now_iso(),
        "repo_root": str(repo_root),
        **index,
        "sources": sources,
        "notes": notes,
        "limit": args.limit,
    }
    if args.role_stats:
        payload["role_stats"] = role_stats
    if db_error and not args.role_stats:
        print(f"task-resume-index: {db_error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        _print_index(payload)
        if args.role_stats:
            print("\nrole_stats:")
            _print_role_stats(role_stats, sources["ledger"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
