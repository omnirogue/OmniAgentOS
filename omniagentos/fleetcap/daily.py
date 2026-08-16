"""Render the deterministic fleet session health brief and morning Slack update."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from omniagentos.fleetcap.migrate import default_db_path
from omniagentos.runtime_paths import resolve_var_root
from omniagentos.team.report import post

DEFAULT_CHANNEL = "C0000EXAMPLE"
REPO_ROOT = Path(os.environ.get("FLEETCAP_REPO", Path(__file__).resolve().parents[2])).resolve()
DEFAULT_CONFIG = REPO_ROOT / "configs/fleetcap/devices.yaml"
# FLEETCAP_REPO stays in front (documented override, an env anchor). The
# fallback is the resolver's PACKAGE anchor (env_keys=(), environ={}): the
# reflection writers are package-anchored, so following OMNIAGENTOS_VAR here
# would read a different tree than they write (review B1); environ={} also
# keeps this import-time evaluation from consulting simulation state (B2).
_FLEETCAP_REPO_ENV = os.environ.get("FLEETCAP_REPO", "").strip()
DEFAULT_REFLECTION_ROOT = (
    Path(_FLEETCAP_REPO_ENV).resolve() / "var/reflection"
    if _FLEETCAP_REPO_ENV
    else resolve_var_root(env_keys=(), environ={}, leaf=("reflection",))
)
_MENTION_RE = re.compile(r"<[!@#][^>]{0,60}>")
_TOKEN_RE = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:xox[a-z]*-|sk-|xai-|ghp_|github_pat_|"
    r"AKIA|AIza|pit-|EAA|gsk_|api[_-]?key[=:])\S*)",
    re.I,
)


def sanitize(value: object) -> str:
    single_line = " ".join(str(value or "").splitlines())
    return _MENTION_RE.sub("[mention omitted]", _TOKEN_RE.sub("[token omitted]", single_line))


def _config_devices(path: Path) -> tuple[list[str], str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], f"config unavailable ({path})"
    # Only rsync-pulled devices have an ingest_root tree whose mtime measures
    # freshness. A "local" device (a dev-upload clone read in place) has none,
    # so listing it here would report it dark on every brief, forever.
    return [
        str(item["device"]) for item in data.get("devices", []) if item.get("mode") != "local"
    ], None


class SessionRows(list[dict[str, Any]]):
    """Query result retaining schema availability even for a zero-row window."""

    def __init__(self, values: Iterable[dict[str, Any]], available_columns: set[str]) -> None:
        super().__init__(values)
        self.available_columns = available_columns


def _latest_mtime(path: Path) -> float | None:
    latest: float | None = None
    if not path.is_dir():
        return None
    for candidate in path.rglob("*"):
        try:
            if candidate.is_file():
                latest = max(latest or 0, candidate.stat().st_mtime)
        except OSError:
            continue
    return latest


def _rows(connection: sqlite3.Connection, since: float) -> SessionRows:
    available = {str(row[1]) for row in connection.execute("PRAGMA table_info(sessions)")}
    wanted = (
        "session_id",
        "cli",
        "device",
        "dispatcher",
        "dispatch_class",
        "outcome",
        "n_err",
        "wall_s",
        "model_s",
        "tool_s",
        "active_s",
        "events",
        "tools",
        "cwd",
        "end_ts",
        "capture_method",
        "dispatch_evidence",
    )
    expressions = [name if name in available else f"NULL AS {name}" for name in wanted]
    time_column = "end_ts" if "end_ts" in available else "created_ts"
    estimator_filter = (
        " AND COALESCE(capture_method, '') != 'estimated-v2'"
        if "capture_method" in available
        else ""
    )
    query = (
        f"SELECT {', '.join(expressions)} FROM sessions "
        f"WHERE COALESCE({time_column}, 0) >= ?{estimator_filter}"
    )
    cursor = connection.execute(query, (since,))
    result = [dict(zip(wanted, row, strict=True)) for row in cursor]
    for item in result:
        item["_available_columns"] = available
    return SessionRows(result, available)


def _hours(value: float) -> str:
    return f"{value / 3600:.1f}h"


def render(
    rows: list[dict[str, Any]],
    *,
    day: date,
    ingest_root: Path,
    devices: Iterable[str],
    reflection_digest: str = "",
    now: float | None = None,
    brief_path: Path,
    config_error: str | None = None,
) -> tuple[str, str]:
    current = now if now is not None else time.time()
    available = set(getattr(rows, "available_columns", ()))
    if not available:
        available = set(rows[0].get("_available_columns", ())) if rows else set()
    if not available and rows:
        available = set(rows[0])
    tools_available = {"events", "tools"} <= available
    tool_time_available = "tool_s" in available
    coverage = Counter(
        (str(row.get("device") or "unknown"), str(row.get("cli") or "unknown")) for row in rows
    )
    attributed = sum(bool(row.get("dispatcher")) for row in rows)
    percent = 100 * attributed / len(rows) if rows else 0.0
    evidence_text = [str(row.get("dispatch_evidence") or "").lower() for row in rows]
    hook_evidence = sum(
        "hook evidence" in evidence and "no hook evidence" not in evidence
        for evidence in evidence_text
    )
    hook_percent = 100 * hook_evidence / len(rows) if rows else 0.0
    dark: list[str] = []
    for device in devices:
        freshness = _latest_mtime(ingest_root / device)
        if freshness is None or current - freshness > 86400:
            dark.append(device)

    failures = [
        row
        for row in rows
        if int(row.get("n_err") or 0) or str(row.get("outcome") or "").startswith("failed")
    ]
    failure_groups = Counter(
        (str(row.get("device") or "unknown"), str(row.get("dispatcher") or "unknown"))
        for row in failures
    )
    by_dispatcher: dict[str, dict[str, float]] = defaultdict(
        lambda: {"sessions": 0, "wall": 0, "model": 0, "tool": 0}
    )
    for row in rows:
        totals = by_dispatcher[str(row.get("dispatcher") or "unknown")]
        totals["sessions"] += 1
        totals["wall"] += float(row.get("wall_s") or 0)
        totals["model"] += float(row.get("model_s") or row.get("active_s") or 0)
        totals["tool"] += float(row.get("tool_s") or 0)

    retry_groups = Counter(
        (row.get("dispatcher"), row.get("cwd"), row.get("outcome")) for row in rows
    )
    retries = [
        (key, count)
        for key, count in retry_groups.items()
        if count >= 3 and str(key[2]).startswith("failed")
    ]
    permission = (
        [row for row in rows if "permission" in f"{row.get('events')} {row.get('tools')}".lower()]
        if tools_available
        else []
    )
    slowest = sorted(rows, key=lambda item: float(item.get("wall_s") or 0), reverse=True)[:5]

    improvements: list[tuple[str, str]] = []
    if dark:
        improvements.append(
            (f"Restore capture on dark devices: {', '.join(sorted(dark))}.", "ingest freshness")
        )
    if failures:
        evidence = ", ".join(str(row.get("session_id")) for row in failures[:3])
        improvements.append(
            (
                f"Investigate {len(failures)} failed/abnormal sessions by device and dispatcher.",
                evidence,
            )
        )
    if retries:
        ids = [
            str(row.get("session_id"))
            for row in rows
            if (row.get("dispatcher"), row.get("cwd"), row.get("outcome")) == retries[0][0]
        ][:3]
        improvements.append(
            (
                f"Break {retries[0][1]}-session retry storm for {sanitize(retries[0][0][0])}.",
                ", ".join(ids),
            )
        )
    if permission:
        improvements.append(
            (
                f"Reduce {len(permission)} permission-prompt hotspots.",
                ", ".join(str(row.get("session_id")) for row in permission[:3]),
            )
        )
    if slowest and float(slowest[0].get("wall_s") or 0) > 3600:
        improvements.append(
            (
                "Inspect the longest session for idle/tool bottlenecks.",
                str(slowest[0].get("session_id")),
            )
        )
    if reflection_digest.strip() and len(improvements) < 5:
        improvements.append(
            ("Review the nightly reflection alongside session telemetry.", "reflection digest")
        )
    improvements = improvements[:5]

    lines = [
        f"# Session improvements — {day.isoformat()}",
        "",
        "## Coverage (capture health)",
        "",
        f"- {len(rows)} sessions; {attributed}/{len(rows)} attributed ({percent:.1f}%).",
        f"- Hook evidence: {hook_evidence}/{len(rows)} sessions ({hook_percent:.1f}%).",
        f"- Dark >24h: {', '.join(sorted(dark)) if dark else 'none'}.",
    ]
    if config_error:
        lines.append(f"- {config_error}.")
    lines.extend(
        f"- {device} / {cli}: {count}" for (device, cli), count in sorted(coverage.items())
    )
    lines.extend(["", "## Reliability", "", f"- Failed or abnormal: {len(failures)}."])
    lines.extend(
        f"- {device} / {dispatcher}: {count}"
        for (device, dispatcher), count in sorted(failure_groups.items())
    )
    lines.append(
        f"- Retry storms: {sum(count for _, count in retries)} sessions across {len(retries)} groups."
    )
    permission_text = (
        f"{len(permission)} sessions" if tools_available else "unavailable (column absent)"
    )
    lines.append(f"- Permission-prompt hotspots: {permission_text}.")
    lines.append(
        "- Five slowest: "
        + (
            ", ".join(
                f"{row.get('session_id')} ({_hours(float(row.get('wall_s') or 0))})"
                for row in slowest
            )
            or "none"
        )
    )
    lines.extend(["", "## Throughput", ""])
    for dispatcher, total in sorted(by_dispatcher.items()):
        tool_text = _hours(total["tool"]) if tool_time_available else "unavailable (column absent)"
        lines.append(
            f"- {sanitize(dispatcher)}: {int(total['sessions'])} sessions; "
            f"wall {_hours(total['wall'])}; model {_hours(total['model'])}; tool {tool_text}."
        )
    lines.extend(["", "## Improvements", ""])
    lines.extend(
        f"{index}. {sanitize(text)} Evidence: `{sanitize(evidence)}`"
        for index, (text, evidence) in enumerate(improvements, 1)
    )
    if not improvements:
        lines.append("1. No actionable regression detected. Evidence: `24h fleet window`. ")
    if reflection_digest.strip():
        lines.extend(["", "## Reflection input", "", sanitize(reflection_digest.strip())])
    brief = "\n".join(lines).rstrip() + "\n"

    coverage_line = f"Coverage: {len(rows)} sessions, {percent:.0f}% attributed; dark >24h: {', '.join(sorted(dark)) if dark else 'none'}"
    hook_line = f"Hook evidence: {hook_evidence}/{len(rows)} sessions ({hook_percent:.1f}%)"
    reliability_line = (
        f"Reliability: {len(failures)} failed/abnormal; "
        f"{sum(count for _, count in retries)} retry-storm sessions; "
        f"{permission_text} permission hotspots"
    )
    slack_lines = [
        f"Fleet sessions — {day.isoformat()}",
        coverage_line,
        hook_line,
        reliability_line,
    ]
    slack_lines.extend(
        f"{index}. {sanitize(text)} [{sanitize(evidence)}]"
        for index, (text, evidence) in enumerate(improvements[:3], 1)
    )
    slack_lines.append(f"Full brief: {brief_path}")
    return brief, "\n".join(slack_lines[:15])


def run(
    db: Path,
    *,
    day: date,
    output_dir: Path,
    ingest_root: Path,
    config: Path,
    reflection_root: Path,
    dry_run: bool,
    now: float | None = None,
) -> tuple[Path, str]:
    current = now if now is not None else time.time()
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA busy_timeout=5000")
        rows = _rows(connection, current - 86400)
    reflection_path = reflection_root / day.isoformat() / "digest.md"
    try:
        reflection = reflection_path.read_text(encoding="utf-8")
    except OSError:
        reflection = ""
    output_dir.mkdir(parents=True, exist_ok=True)
    brief_path = output_dir / f"session-improvements-{day.isoformat()}.md"
    devices, config_error = _config_devices(config)
    brief, slack = render(
        rows,
        day=day,
        ingest_root=ingest_root,
        devices=devices,
        reflection_digest=reflection,
        now=current,
        brief_path=brief_path,
        config_error=config_error,
    )
    brief_path.write_text(brief, encoding="utf-8")
    if dry_run:
        print(slack)
    else:
        channel = os.environ.get("FLEETCAP_SLACK_CHANNEL", DEFAULT_CHANNEL)
        ok = post(slack, channel=channel)
        print(f"fleetcap daily: slack channel={channel} ok={ok}", file=sys.stderr)
        if not ok:
            raise RuntimeError(f"Slack delivery failed for channel {channel}")
    return brief_path, slack


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_db_path())
    parser.add_argument("--day", type=date.fromisoformat, default=datetime.now(UTC).date())
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("~/.omniagentos/ops/telemetry/briefs").expanduser(),
    )
    parser.add_argument(
        "--ingest-root", type=Path, default=Path("~/.omniagentos/ops/telemetry/ingest").expanduser()
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--reflection-root", type=Path, default=DEFAULT_REFLECTION_ROOT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        run(
            args.db,
            day=args.day,
            output_dir=args.output_dir,
            ingest_root=args.ingest_root,
            config=args.config,
            reflection_root=args.reflection_root,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        print(f"fleetcap daily: error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
