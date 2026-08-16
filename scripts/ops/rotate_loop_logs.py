#!/usr/bin/env python3
"""Size-based copytruncate rotation for `var/loopqueue/logs/*.log`.

Why copytruncate, not rename+reopen
------------------------------------
The gate-loop's daemons (bridge, governor, advice, claim-reaper, ...) each hold
their own log fd open for the process lifetime -- some via a shell `tee`, some
via a plain `open(..., "a")` -- and none of them re-open on SIGHUP. A
rename-based rotation (`mv foo.log foo.log.1; touch foo.log`) orphans every
live writer on the moved inode: the process keeps appending to the renamed
file forever and the new "current" `foo.log` stays empty. copytruncate keeps
the SAME inode: gzip-copy the bytes that exist right now, then `truncate(0)`
that same file in place. Every writer holding the fd (append-mode writes seek
to EOF before each write) keeps writing to the same file, no signal or
reopen required.

This is NOT atomic against a writer landing bytes between the copy and the
truncate -- that write can be lost from both the archive and the live file.
Accepted: these are best-effort operational logs, not an audit trail, and the
alternative (coordinating a lock across independently launched daemons) is
not available here.

Only `*.log` files directly under the logs directory are touched -- `.err`
sidecars and anything else are left alone, and the existing
`<name>.log.rotated-*.gz` archives (already excluded by the `*.log` glob)
are pruned to the newest three per log.
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_LOGS_DIR = REPO / "var" / "loopqueue" / "logs"
DEFAULT_THRESHOLD_BYTES = 512 * 1024 * 1024  # 512MB
KEEP_ARCHIVES = 3


def _archive_path(log_path: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return log_path.with_name(f"{log_path.name}.rotated-{stamp}.gz")


def _rotate_one(log_path: Path, *, dry_run: bool, now: datetime | None = None) -> Path:
    """gzip-copy `log_path`, then truncate it in place (same inode).

    Returns the archive path. In `dry_run` mode nothing is written or
    truncated -- the path returned is only for reporting.
    """
    archive = _archive_path(log_path, now)
    if dry_run:
        return archive
    with log_path.open("rb") as src, gzip.open(archive, "wb") as dst:
        shutil.copyfileobj(src, dst)
    # Truncate the SAME file (same inode/fd table entry as any live writer),
    # never unlink+recreate -- that would orphan an already-open fd.
    with log_path.open("r+b") as fh:
        fh.truncate(0)
    return archive


def _prune_archives(log_path: Path, *, dry_run: bool) -> list[Path]:
    """Delete all but the `KEEP_ARCHIVES` newest archives for this one log.

    The rotation stamp (`%Y%m%dT%H%M%SZ`) sorts lexicographically the same as
    chronologically, so a plain name sort is enough to rank them.
    """
    pattern = f"{log_path.name}.rotated-*.gz"
    archives = sorted(log_path.parent.glob(pattern), key=lambda p: p.name, reverse=True)
    stale = archives[KEEP_ARCHIVES:]
    if not dry_run:
        for path in stale:
            path.unlink()
    return stale


def rotate_logs(
    logs_dir: Path,
    *,
    threshold_bytes: int = DEFAULT_THRESHOLD_BYTES,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, list[str]]:
    """Rotate every oversized `*.log` directly under `logs_dir`.

    Non-recursive, `.log`-suffix only -- `.err` sidecars and any other file
    in the directory are never touched. Returns a report dict with
    `rotated`/`pruned`/`skipped` name lists for logging/testing.
    """
    report: dict[str, list[str]] = {"rotated": [], "pruned": [], "skipped": []}
    if not logs_dir.is_dir():
        return report
    for log_path in sorted(logs_dir.glob("*.log")):
        try:
            size = log_path.stat().st_size
        except FileNotFoundError:
            continue
        if size <= threshold_bytes:
            report["skipped"].append(log_path.name)
            continue
        archive = _rotate_one(log_path, dry_run=dry_run, now=now)
        report["rotated"].append(f"{log_path.name} -> {archive.name} ({size} bytes)")
        pruned = _prune_archives(log_path, dry_run=dry_run)
        report["pruned"].extend(p.name for p in pruned)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Size-based copytruncate rotation for var/loopqueue/logs/*.log"
    )
    parser.add_argument(
        "--logs-dir",
        type=Path,
        default=DEFAULT_LOGS_DIR,
        help=f"directory of *.log files to rotate (default: {DEFAULT_LOGS_DIR})",
    )
    parser.add_argument(
        "--threshold-bytes",
        type=int,
        default=DEFAULT_THRESHOLD_BYTES,
        help=f"rotate a .log once it exceeds this size (default: {DEFAULT_THRESHOLD_BYTES})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would rotate/prune; touch nothing",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = rotate_logs(
        args.logs_dir,
        threshold_bytes=args.threshold_bytes,
        dry_run=args.dry_run,
    )
    prefix = "[dry-run] " if args.dry_run else ""
    for name in report["rotated"]:
        print(f"{prefix}rotated {name}", file=sys.stderr)
    for name in report["pruned"]:
        print(f"{prefix}pruned archive {name}", file=sys.stderr)
    if not report["rotated"]:
        print(
            f"{prefix}nothing exceeded threshold under {args.logs_dir}"
            f" ({len(report['skipped'])} log(s) checked)",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
