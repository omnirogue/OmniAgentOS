"""Hourly session tracker: pipeline totals + who is working on what, to Slack.

One message, two halves:

* **Overall** — Proposals on file, Candidates (7-day archive), Merge queue
  (submitted-not-terminal in the loopqueue ledger), Merged last hour, Failed
  merges last hour, Total active sessions, and the Current bottleneck (first
  matching rule, one line — a report that lists every true statement is a
  report nobody reads).
* **Per person** — total active sessions and one line per session saying what
  it is working on, sourced from the collector drop-files
  (``var/team-sessions/<employee>.json``, written locally by
  :mod:`omniagentos.team.session_collector` on each machine and remotely via
  ``POST /api/team/sessions/report`` or the interim Slack-webhook transport).
  A person with no fresh drop-file says so explicitly — "no report" and
  "no sessions" are different answers.

Counts are labeled with what they actually count. ``proposals/`` and
``candidates/`` are working directories with archival semantics (a merged
candidate's file lingers for days), so the headline pipeline numbers are the
LEDGER-derived ones (merged/failed last hour, merge queue); the directory
totals are context, not backlog claims.
"""

from __future__ import annotations

import argparse
import base64
import calendar
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.team.notify import (
    DEFAULT_CHANNEL,
    SlackNotifier,
    _safe_title,
    load_slack_env,
)
from omniagentos.team.session_collector import collect as collect_local_sessions

REPO_ROOT = Path("/Users/youruser/OmniAgentOS")
DROP_DIR_NAME = "team-sessions"
DROP_FRESH_SECONDS = 2 * 3600
LEDGER_TAIL_BYTES = 2_000_000  # ~last few thousand events; plenty for 48h
MERGE_QUEUE_WINDOW_HOURS = 48
OPERATOR_EMPLOYEE_ID = "emp_owner"

# Compute-pool status is read from the local wq-server over loopback. The tracker
# runs on the pool host, so 127.0.0.1 + the bearer token is all it needs. launchd
# strips the shell env, so the token is read from connections.env by ABSOLUTE path
# when it is not already exported.
WQ_STATUS_URL = "http://127.0.0.1:8487"
WQ_CONNECTIONS_ENV = "/Users/youruser/.config/omni/connections.env"
#: A machine silent past this is flagged non-idle: a dead worker must never read
#: as available capacity (drain OR staleness → flagged, load-bearing).
MACHINE_STALE_SECONDS = 120.0


def _utcnow() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _parse_iso(value: str) -> float:
    try:
        # timegm interprets the struct as UTC — mktime would apply the LOCAL
        # zone (and its DST state), silently skewing every window by hours.
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, TypeError):
        return 0.0


@dataclass
class Overall:
    proposals: int = 0
    candidates: int = 0
    merge_queue: int = 0
    merged_last_hour: int = 0
    failed_merges_last_hour: int = 0
    active_sessions: int = 0
    bottleneck: str = "none"
    notes: list[str] = field(default_factory=list)


def _count_dir(path: Path) -> int:
    try:
        return sum(1 for item in path.iterdir() if not item.name.startswith("."))
    except OSError:
        return 0


def _ledger_events(ledger: Path, since: float) -> list[dict[str, Any]]:
    """Events newer than ``since`` from the tail of the append-only ledger."""
    events: list[dict[str, Any]] = []
    try:
        size = ledger.stat().st_size
        with ledger.open("rb") as handle:
            if size > LEDGER_TAIL_BYTES:
                handle.seek(size - LEDGER_TAIL_BYTES)
                handle.readline()  # drop the torn first line
            for raw in handle:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue  # torn line: reader-side tolerance, never repair the file
                if _parse_iso(str(event.get("ts") or "")) >= since:
                    events.append(event)
    except OSError:
        pass
    return events


def _gate_result(event: dict[str, Any]) -> str:
    detail = event.get("detail")
    return str(detail.get("result") or "") if isinstance(detail, dict) else ""


def gather_overall(
    repo_root: Path,
    db_path: str,
    reports: dict[str, dict[str, Any]] | None = None,
) -> Overall:
    overall = Overall()
    loopqueue = repo_root / "var" / "loopqueue"
    overall.proposals = _count_dir(loopqueue / "proposals")
    overall.candidates = _count_dir(loopqueue / "candidates")

    now = _utcnow()
    events = _ledger_events(loopqueue / "ledger.jsonl", now - MERGE_QUEUE_WINDOW_HOURS * 3600)
    hour_ago = now - 3600
    terminal: set[str] = set()
    submitted: dict[str, float] = {}
    for event in events:
        kind, ident = str(event.get("event") or ""), str(event.get("id") or "")
        ts = _parse_iso(str(event.get("ts") or ""))
        if kind == "submitted" and ident:
            submitted[ident] = ts
        elif kind in ("merged", "rejected", "parked") and ident:
            terminal.add(ident)
        if ts < hour_ago:
            continue
        if kind == "merged" and _gate_result(event) in ("", "pass"):
            overall.merged_last_hour += 1
        elif kind in ("gated", "merged") and _gate_result(event) not in ("", "pass"):
            overall.failed_merges_last_hour += 1
    overall.merge_queue = sum(1 for ident in submitted if ident not in terminal)

    # The live signal is the collector drop-files (each machine's actual
    # transcripts); the longhaul task_sessions table is a supplement — it is
    # frozen/deprecated and usually empty, so it must never mask real work.
    from_reports = 0
    for report in (reports or {}).values():
        if report.get("_age_seconds", DROP_FRESH_SECONDS + 1) <= DROP_FRESH_SECONDS:
            from_reports += int(
                report.get("active_count")
                or sum(
                    1
                    for session in report.get("sessions", [])
                    if isinstance(session, dict) and session.get("active")
                )
            )
    task_session_rows = 0
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM task_sessions WHERE ended_at IS NULL"
            ).fetchone()
            task_session_rows = int(row[0]) if row else 0
        finally:
            connection.close()
    except sqlite3.Error as exc:
        overall.notes.append(f"task_sessions unavailable: {exc}")
    overall.active_sessions = max(from_reports, task_session_rows)
    if task_session_rows and task_session_rows != from_reports:
        overall.notes.append(
            f"task_sessions live rows: {task_session_rows} (collector total: {from_reports})"
        )

    # First match wins — one bottleneck, not a list.
    if overall.failed_merges_last_hour > 0 and overall.merged_last_hour == 0:
        overall.bottleneck = "merge gate red (failures with zero merges this hour)"
    elif overall.merge_queue >= 5:
        overall.bottleneck = f"gate throughput ({overall.merge_queue} in merge queue)"
    elif overall.proposals > 0 and overall.active_sessions == 0:
        overall.bottleneck = "proposals waiting, no active sessions"
    elif overall.active_sessions == 0:
        overall.bottleneck = "no active sessions"
    return overall


@dataclass
class Capacity:
    """Pre-formatted compute-pool display strings for the hourly report.

    Every number is computed and formatted HERE (the module's number-fidelity
    rule: the renderers only join). ``available`` is False when the pool could
    not be read for any reason — the report still posts, it just says so.
    """

    available: bool
    aggregate: str
    machines: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        """One block of text (aggregate + one line per machine) — the SAME
        strings both the plaintext and the Slack surfaces render, so parity is
        structural, not a promise."""
        return "\n".join([self.aggregate, *self.machines])


_CAPACITY_UNAVAILABLE = Capacity(False, "🖥️ Pool: compute unavailable", [])


def _wq_token() -> str:
    """Bearer token from the environment, or from connections.env by absolute
    path (launchd has no shell profile). An exported token always wins."""
    token = os.environ.get("WQ_TOKEN") or ""
    if token or not os.path.exists(WQ_CONNECTIONS_ENV):
        return token
    try:
        with open(WQ_CONNECTIONS_ENV, encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip().removeprefix("export ").strip() == "WQ_TOKEN":
                    return value.strip().strip('"').strip("'")
    except OSError:
        return ""
    return ""


def _fetch_pool_status() -> dict[str, Any] | None:
    """The live ``GET /v1/status`` payload, or None on ANY failure.

    Never raises: an unreachable server, a missing token, or a transport error
    is an absent capacity signal, not a reason to sink the hourly report.
    """
    try:
        from omniagentos.workqueue.client import HttpQueueClient

        client = HttpQueueClient(WQ_STATUS_URL, token=_wq_token(), timeout_s=5.0)
        status = client.status()
        return status if isinstance(status, dict) else None
    except Exception as exc:  # noqa: BLE001 — capacity is best-effort, never fatal
        print(f"session-tracker: pool status unavailable: {exc}", file=sys.stderr)
        return None


def _num(value: Any) -> str:
    """Faithful display of a numeric field: a missing value reads as '?' rather
    than as a plausible-but-wrong 0."""
    return "?" if value is None else str(value)


def _machine_flag(drain: Any, last_seen_age_s: Any) -> str:
    """Liveness flag. drain=1 OR silence past the stale horizon (INCLUDING an
    absent age) flags the box, so a drained or dead worker can never be read as
    idle-available capacity."""
    if _as_int(drain) == 1:
        return "[drained]"
    age = _as_float(last_seen_age_s)
    # `age != age` catches NaN: a corrupt liveness signal must fail CLOSED to
    # [stale] (nan > horizon is False in IEEE-754, which would otherwise read a
    # dead/corrupt box as [alive] — the exact favourable-absence this flag exists
    # to prevent).
    if age is None or age != age or age > MACHINE_STALE_SECONDS:
        return "[stale]"
    return "[alive]"


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def gather_capacity(status: dict[str, Any] | None = None) -> Capacity:
    """Compute-pool capacity as pre-formatted display strings.

    Pass ``status`` to format a known payload (tests); omit it to fetch the live
    ``GET /v1/status`` from the local wq-server. Server unreachable, status
    missing, or the expected keys absent → the 'compute unavailable' marker.
    NEVER raises: the hourly report must still post.
    """
    if status is None:
        status = _fetch_pool_status()
    if not isinstance(status, dict):
        return _CAPACITY_UNAVAILABLE
    capacity = status.get("capacity")
    machines = status.get("machines")
    if not isinstance(capacity, dict) or not isinstance(machines, list):
        return _CAPACITY_UNAVAILABLE
    required = ("total_cores", "in_flight", "total_slots", "free_slots")
    if any(key not in capacity for key in required):
        return _CAPACITY_UNAVAILABLE
    try:
        aggregate = (
            f"🖥️ Pool: {_num(capacity['total_cores'])}c"
            f" · {_num(capacity['in_flight'])}/{_num(capacity['total_slots'])} slots used"
            f" · {_num(capacity['free_slots'])} free"
        )
        lines: list[str] = []
        for machine in machines:
            if not isinstance(machine, dict):
                continue
            lines.append(
                f"{machine.get('machine_id') or '?'}"
                f" · {_num(machine.get('ncpu'))}c"
                f" · load {_num(machine.get('load1'))}"
                f" · {_num(machine.get('in_flight'))}/{_num(machine.get('max_concurrent'))} slots"
                f" · {_num(machine.get('mem_free_gb'))}/{_num(machine.get('mem_gb'))}GB"
                f" {_machine_flag(machine.get('drain'), machine.get('last_seen_age_s'))}"
            )
    except Exception as exc:  # noqa: BLE001 — a malformed payload degrades, never raises
        print(f"session-tracker: capacity format failed: {exc}", file=sys.stderr)
        return _CAPACITY_UNAVAILABLE
    return Capacity(True, aggregate, lines)


def _drop_dir(repo_root: Path) -> Path:
    """One authority with the API route: the resolved var root's team-sessions/."""
    from omniagentos.runtime_paths import resolve_var_root

    try:
        return Path(resolve_var_root()) / DROP_DIR_NAME
    except Exception:  # noqa: BLE001 — sim-context refusal falls back to the repo tree
        return repo_root / "var" / DROP_DIR_NAME


def read_person_reports(repo_root: Path) -> dict[str, dict[str, Any]]:
    """Freshest collector report per employee, plus staleness bookkeeping."""
    reports: dict[str, dict[str, Any]] = {}
    directory = _drop_dir(repo_root)
    if not directory.is_dir():
        return reports
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 — a JSON bomb (RecursionError)
            # or any exotic decode failure is still just ONE bad drop-file;
            # it must never abort the whole tracker run (review API-02-R4).
            print(f"session-tracker: unreadable {path.name}: {exc}", file=sys.stderr)
            payload = None
        if not isinstance(payload, dict):
            # Corrupt bytes or valid JSON that is not a report ([]/scalar):
            # an explicit UNREADABLE stub row, same treatment as
            # balance_alerts._load_reports — .get() on a list aborted the
            # whole tracker before this guard existed (review TRK-05). The
            # stub's age is pinned past DROP_FRESH_SECONDS so a spurious
            # corrupt file can never win "freshest" over a real report.
            if payload is not None:
                print(f"session-tracker: not a report shape {path.name}", file=sys.stderr)
            payload = {
                "employee_id": path.stem,
                "host": path.stem,
                "_unreadable": True,
                "sessions": [],
                "_age_seconds": float(DROP_FRESH_SECONDS + 1),
            }
            employee = str(path.stem)
            current = reports.get(employee)
            if current is None:
                reports[employee] = payload
            continue
        # Dict-shaped corruption is still corruption: a report whose core
        # fields are the wrong TYPE (sessions: 1, active_count: "x") is as
        # unreadable as bad bytes — a corrupt field poisons trust in its
        # siblings, and sanitizing would render fabricated partial truth
        # (review TRK-05-R5; same principle as TRK-04).
        raw_sessions = payload.get("sessions")
        counts_valid = True
        for count_key in ("active_count", "recent_count"):
            value = payload.get(count_key)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and (value != value or not value.is_integer()))
                or value < 0
            ):
                counts_valid = False
        if (raw_sessions is not None and not isinstance(raw_sessions, list)) or not counts_valid:
            print(f"session-tracker: malformed report shape {path.name}", file=sys.stderr)
            employee = str(payload.get("employee_id") or path.stem)
            stub = {
                "employee_id": employee,
                "host": str(payload.get("host") or path.stem),
                "_unreadable": True,
                "sessions": [],
                "_age_seconds": float(DROP_FRESH_SECONDS + 1),
            }
            current = reports.get(employee)
            if current is None:
                reports[employee] = stub
            continue
        if isinstance(raw_sessions, list):
            payload["sessions"] = [s for s in raw_sessions if isinstance(s, dict)]
        employee = str(payload.get("employee_id") or path.stem)
        raw_age = _utcnow() - _parse_iso(str(payload.get("generated_at") or ""))
        # A clock skewed into the future must not win "freshest" forever: more
        # than 5 minutes ahead reads as stale-with-a-broken-clock, not as new.
        payload["_age_seconds"] = (
            max(0.0, raw_age) if raw_age >= -300 else float(DROP_FRESH_SECONDS + 1)
        )
        current = reports.get(employee)
        if (
            current is None
            or current.get("_unreadable")  # a real report always beats a stub
            or payload["_age_seconds"] < current["_age_seconds"]
        ):
            reports[employee] = payload
    return reports


def collect_local(repo_root: Path, employee_id: str) -> None:
    """Refresh the operator's own drop-file from this machine's transcripts."""
    from omniagentos.team.session_collector import _write_atomic

    report = collect_local_sessions(employee_id, window_min=75, active_min=15)
    _write_atomic(_drop_dir(repo_root) / f"{employee_id}.json", report)


def _mapped_employee_ids(repo_root: Path) -> set[str]:
    """Employee ids named in the github identity map — the reporting team."""
    try:
        import yaml

        raw = yaml.safe_load(
            (repo_root / "configs" / "team_github_map.yaml").read_text(encoding="utf-8")
        )
        return {str(v) for v in raw.values()} if isinstance(raw, dict) else set()
    except Exception:  # noqa: BLE001 — an unreadable map falls back to the full roster
        return set()


def _roster(
    db_path: str, repo_root: Path, reports: dict[str, dict[str, Any]]
) -> list[tuple[str, str]]:
    """The reporting roster: identity-mapped employees plus anyone who reported.

    The employees table carries seeded/test rows the team report never shows;
    listing them here as eternally "no session report received" would train
    everyone to skim past that line — which is the line that matters.
    """
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT id, name FROM employees ORDER BY id").fetchall()
            everyone = [(str(r[0]), str(r[1] or r[0])) for r in rows]
        finally:
            connection.close()
    except sqlite3.Error:
        everyone = []
    wanted = _mapped_employee_ids(repo_root) | set(reports)
    if not wanted:
        return everyone
    roster = [(eid, name) for eid, name in everyone if eid in wanted]
    roster.extend((eid, eid) for eid in sorted(wanted) if eid not in {e for e, _ in roster})
    return roster


# Absolute paths: the launchd plist PATH has no ~/.local/bin (measured trap,
# HANDOFF-hourly-report-ai-balances-2026-08-12).
_CODEX_ROSTER = "/Users/youruser/.local/bin/codex-roster"
_CLAUDE_ROSTER = "/Users/youruser/.local/bin/claude-roster"
_WQ_SERVER = os.environ.get("WQ_SERVER") or "http://127.0.0.1:8487"
_BALANCE_LOW_PCT = 10.0  # matches balance_alerts.BREACH_BELOW_PCT


def _roster_json(command: list[str]) -> dict[str, Any] | list[Any] | None:
    """Run a roster CLI and parse its JSON — total, never raises.

    A nonzero exit is a FAILED probe even when stdout happens to parse:
    trusting favorable-looking output from a failed command is the
    missing-source-as-favourable shape (review TRK-02 R2). Both estate
    rosters exit 0 on a successful --json render."""
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:  # noqa: BLE001 — a balance probe must never sink the report
        return None


def _pool_capacity() -> dict[str, Any] | None:
    """The workqueue's capacity block — total, never raises (pool down = None)."""
    try:
        from omniagentos.workqueue.client import HttpQueueClient

        capacity = HttpQueueClient(_WQ_SERVER, timeout_s=5.0).status().get("capacity")
        return capacity if isinstance(capacity, dict) else None
    except Exception:  # noqa: BLE001
        return None


def gather_balances(reports: dict[str, dict[str, Any]] | None = None) -> str:
    """Pre-formatted ``AI BALANCES`` section for the hourly report ('' if nothing).

    Number-fidelity rule: every number and suggestion is computed AND formatted
    here; the renders only append the returned string. Sources: claude-roster
    --json (local Claude accounts, deduped by account uuid), codex-roster --json
    (deduped on duplicate_of — two homes on one account share ONE window), the
    fleet drop-files' ``claude_usage`` blocks (remote machines), and the
    workqueue /v1/status capacity. xAI/Gemini have no quota meter — say so,
    never fabricate one.
    """
    lines: list[str] = []
    suggestions: list[str] = []

    claude = _roster_json([sys.executable, _CLAUDE_ROSTER, "--json"])
    if not (isinstance(claude, dict) and isinstance(claude.get("accounts"), list)):
        lines.append("Claude (this Mac): roster probe failed — balances unknown")
    if isinstance(claude, dict) and isinstance(claude.get("accounts"), list):
        accounts = [a for a in claude["accounts"] if isinstance(a, dict)]
        distinct = [a for a in accounts if not a.get("duplicate_of")]

        def _finite_pct(value: Any) -> float | None:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            result = float(value)
            return result if result == result and abs(result) != float("inf") else None

        measured = [
            a
            for a in distinct
            if a.get("authed") and _finite_pct(a.get("remaining_percent")) is not None
        ]
        out_n = sum(1 for a in measured if float(a["remaining_percent"]) < _BALANCE_LOW_PCT)
        best = max(measured, key=lambda a: float(a["remaining_percent"])) if measured else None
        if best is not None:
            marker = " ⚠️ LOW" if float(best["remaining_percent"]) < _BALANCE_LOW_PCT else ""
            lines.append(
                f"Claude (this Mac): best {best['remaining_percent']:.0f}% left"
                f" ({os.path.basename(str(best['config_dir']))})"
                f" · {len(measured)} measured / {len(distinct)} distinct accounts"
                f" · {out_n} out{marker}"
            )
            if float(best["remaining_percent"]) >= _BALANCE_LOW_PCT:
                suggestions.append(f"CLAUDE_CONFIG_DIR={best['config_dir']}")
        else:
            lines.append("Claude (this Mac): no measurable authenticated account")

    for report in (reports or {}).values():
        if not isinstance(report, dict):
            continue
        usage = report.get("claude_usage")
        host = str(report.get("host") or "?")
        if report.get("_unreadable") and host != "?":
            lines.append(f"Claude ({host}): balance unknown (drop-file unreadable)")
            continue
        if host in (socket.gethostname(), "?") or not isinstance(usage, dict):
            continue
        age_s = report.get("_age_seconds")
        if isinstance(age_s, (int, float)) and age_s > DROP_FRESH_SECONDS:
            # A stale report's last-known percent must not render as a live
            # favorable balance — unknown, with the age named (review TRK-02).
            lines.append(
                f"Claude ({host}): balance unknown (report {age_s / 3600.0:.1f}h old — stale)"
            )
            continue
        best_pct = usage.get("best_remaining_percent")

        def _valid_count(value: Any) -> int | None:
            # Counts are whole and non-negative; anything else is corrupt
            # telemetry, and a corrupt count poisons trust in the sibling
            # percent (review TRK-04 — same rule as balance_alerts._as_int).
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            if isinstance(value, float) and (value != value or not value.is_integer()):
                return None
            return int(value) if value >= 0 else None

        authed_int = _valid_count(usage.get("authed_accounts"))
        if (
            authed_int is not None
            and isinstance(best_pct, (int, float))
            and not isinstance(best_pct, bool)
            and best_pct == best_pct
            and abs(float(best_pct)) != float("inf")
        ):
            authed_str = str(authed_int)
            marker = " ⚠️ LOW" if float(best_pct) < _BALANCE_LOW_PCT else ""
            # basename + clip: best_dir is remote-supplied text; the local
            # path already basenames, and an unbounded string distorts the
            # Slack block layout (Grok review F2).
            best_dir = os.path.basename(str(usage.get("best_dir") or "?").rstrip("/"))[:60]
            lines.append(
                f"Claude ({host}): best {float(best_pct):.0f}% left"
                f" ({best_dir or '?'})"
                f" · {authed_str} authed{marker}"
            )
        else:
            # Malformed counts poison trust in the whole block: a report whose
            # authed_accounts is garbage must not render its percent as truth.
            lines.append(f"Claude ({host}): balance unknown (no measured account)")

    codex = _roster_json([sys.executable, _CODEX_ROSTER, "--json"])
    if not isinstance(codex, list):
        lines.append("Codex: roster probe failed — balances unknown")
    if isinstance(codex, list):
        rows = [r for r in codex if isinstance(r, dict)]
        distinct_rows = [r for r in rows if not r.get("duplicate_of")]

        def _codex_pct(row: dict[str, Any]) -> float | None:
            # window_used_percent is the ACCOUNT-level number (deduped across
            # homes sharing one login); used_percent is this home's rollout,
            # which can be stale on the primary while a duplicate is current.
            # If the account-level key is PRESENT but malformed, the account
            # is unreadable — falling back to the stale home number would
            # launder corruption into a routing suggestion (TRK-01 R2). The
            # home-level fallback exists only for rosters that predate the
            # account-level field.
            def _finite(value: Any) -> float | None:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    return None
                result = float(value)
                return result if result == result and abs(result) != float("inf") else None

            if "window_used_percent" in row:
                return _finite(row.get("window_used_percent"))
            return _finite(row.get("used_percent"))

        measured_rows = [r for r in distinct_rows if r.get("login") and _codex_pct(r) is not None]
        best_row = (
            min(measured_rows, key=lambda r: float(_codex_pct(r) or 0.0)) if measured_rows else None
        )
        if best_row is not None:
            left = 100.0 - float(_codex_pct(best_row) or 0.0)
            out_n = sum(1 for r in measured_rows if float(_codex_pct(r) or 0.0) >= 90.0)
            marker = " ⚠️ LOW" if left < _BALANCE_LOW_PCT else ""
            lines.append(
                f"Codex: best {left:.0f}% left ({best_row.get('short') or best_row.get('home')})"
                f" · {len(measured_rows)} measured / {len(distinct_rows)} distinct"
                f" · {out_n} out{marker}"
            )
            if left >= _BALANCE_LOW_PCT:
                suggestions.append(f"CODEX_HOME={best_row.get('home')}")
        elif rows:
            lines.append("Codex: no logged-in account with usage data")

    capacity = _pool_capacity()
    if isinstance(capacity, dict):
        lines.append(
            f"Pool: {capacity.get('free_slots', '?')} slots free"
            f" / {capacity.get('total_slots', '?')} total"
            f" · {capacity.get('total_cores', '?')} cores"
            f" · {capacity.get('in_flight', '?')} in flight"
        )

    if not lines:
        return ""
    lines.append("xAI / Gemini: no quota meter exists — not measured")
    if suggestions:
        lines.append("Suggest: " + " · ".join(suggestions))
    return "AI BALANCES\n" + "\n".join(f"  {line}" for line in lines)


def render(
    overall: Overall,
    roster: list[tuple[str, str]],
    reports: dict[str, dict[str, Any]],
    capacity: Capacity | None = None,
) -> str:
    lines = [
        "SESSION TRACKER — " + _iso(_utcnow()),
        (
            f"Overall: proposals {overall.proposals} · candidates {overall.candidates}"
            f" · merge queue (48h) {overall.merge_queue} · merged 1h {overall.merged_last_hour}"
            f" · failed merges 1h {overall.failed_merges_last_hour}"
            f" · active sessions {overall.active_sessions}"
        ),
        f"Bottleneck: {overall.bottleneck}",
    ]
    lines.extend(f"note: {note}" for note in overall.notes)
    for employee_id, name in roster:
        report = reports.get(employee_id)
        if report is None:
            lines.append(f"{name}: no session report received")
            continue
        if report.get("_unreadable"):
            lines.append(f"{name}: drop-file unreadable — state unknown")
            continue
        if report.get("opted_out"):
            # The owner used the kill switch. Say so — a switched-off feed
            # must read as a choice, never as the person being offline/AWOL.
            since = str(report.get("opted_out_since") or "unknown time")
            lines.append(
                f"{name}: telemetry off (opted out since {since})"
                f" on {report.get('host', '?')} — no data collected"
            )
            continue
        age = report["_age_seconds"]
        sessions = [s for s in report.get("sessions", []) if isinstance(s, dict)]
        active_n = int(report.get("active_count") or sum(1 for s in sessions if s.get("active")))
        recent_n = int(report.get("recent_count") or len(sessions))
        stale = " (stale report)" if age > DROP_FRESH_SECONDS else ""
        lines.append(
            f"{name}: {active_n} active / {recent_n} recent on {report.get('host', '?')}{stale}"
        )
        for session in sessions[:8]:
            marker = "▶" if session.get("active") else "·"
            description = _safe_title(str(session.get("description") or ""))
            project = str(session.get("project") or "")
            suffix = f" [{project}]" if project and project not in description else ""
            account = str(session.get("account") or "")
            if account and account != "default":
                suffix += f" @{account}"
            lines.append(f"  {marker} {description}{suffix}")
    if capacity is not None:
        lines.append("COMPUTE")
        lines.append(capacity.aggregate)
        lines.extend(f"  {line}" for line in capacity.machines)
    return "\n".join(lines)


def maybe_ingest_slack_reports(repo_root: Path, channel: str) -> None:
    """Adopt SESSIONREPORT lines from channel history into drop-files.

    The interim transport for teammates without tunnel access: their collector
    posts ``SESSIONREPORT <employee> <base64-json>`` via a webhook; this pass
    folds the freshest one per employee into ``var/team-sessions/`` so the
    renderer has ONE source. Missing token or API failure degrades silently —
    the drop-files simply stay as they were.
    """
    import urllib.parse
    import urllib.request

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return
    query = urllib.parse.urlencode({"channel": channel, "limit": 200})
    request = urllib.request.Request(
        f"https://slack.com/api/conversations.history?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"session-tracker: slack history unavailable: {exc}", file=sys.stderr)
        return
    if not payload.get("ok"):
        return
    from omniagentos.team.session_collector import _write_atomic

    freshest: dict[str, dict[str, Any]] = {}
    for message in payload.get("messages", []):
        text = str(message.get("text") or "")
        if not text.startswith("SESSIONREPORT "):
            continue
        parts = text.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            report = json.loads(base64.b64decode(parts[2].split("\n", 1)[0]))
        except (ValueError, TypeError):
            continue
        for session in report.get("sessions", []):
            if isinstance(session, dict):
                session["description"] = str(session.get("description") or "")[:300]
        employee = str(report.get("employee_id") or "")
        # Channel-posted text is the least-trusted input in this module: an
        # employee id is a filename component, so anything outside the strict
        # charset is refused BEFORE any path construction (path traversal).
        if re.fullmatch(r"[A-Za-z0-9_-]+", employee) is None:
            continue
        generated = _parse_iso(str(report.get("generated_at") or ""))
        if generated > _utcnow() + 300:
            continue  # future-clocked report must not win freshest forever
        if employee and generated > _parse_iso(
            str(freshest.get(employee, {}).get("generated_at") or "")
        ):
            freshest[employee] = report
    for employee, report in freshest.items():
        existing = _drop_dir(repo_root) / f"{employee}.json"
        try:
            current = json.loads(existing.read_text(encoding="utf-8"))
            if _parse_iso(str(current.get("generated_at") or "")) >= _parse_iso(
                str(report.get("generated_at") or "")
            ):
                continue
        except (OSError, ValueError):
            pass
        _write_atomic(existing, report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=os.environ.get("OMNIAGENTOS_DB", ""))
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--channel", default=os.environ.get("OMNI_TEAM_PULSE_CHANNEL", ""))
    parser.add_argument(
        "--collect-local",
        metavar="EMPLOYEE_ID",
        help="refresh this machine's own drop-file first (the operator's plist "
        f"passes {OPERATOR_EMPLOYEE_ID})",
    )
    parser.add_argument(
        "--no-slack-ingest",
        action="store_true",
        help="skip folding SESSIONREPORT channel lines into drop-files",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    db_path = args.db or str(repo_root / "var" / "runtime" / "state.sqlite3")
    load_slack_env()
    channel = args.channel or os.environ.get("OMNI_TEAM_REPORT_CHANNEL", DEFAULT_CHANNEL)

    if args.collect_local:
        collect_local(repo_root, args.collect_local)
    if not args.no_slack_ingest and not args.dry_run:
        maybe_ingest_slack_reports(repo_root, channel)

    from omniagentos.team import compute as compute_mod
    from omniagentos.team import decisions, slack_blocks

    reports = read_person_reports(repo_root)
    overall = gather_overall(repo_root, db_path, reports)
    # Compute-pool capacity: aggregate + one line per machine, pre-formatted.
    # gather_capacity never raises — an unreachable pool degrades to a marker.
    capacity = gather_capacity()
    # Compute-pool status: capacity, per-person offloads, and one suggestion,
    # folded into the notes seam (rendered on both the text and styled surfaces).
    # Degrade-silent — a missing/locked workqueue DB never sinks the report.
    compute = None
    try:
        compute = compute_mod.gather_compute(repo_root)
        overall.notes.extend(compute_mod.compute_notes(compute, overall.bottleneck))
    except Exception as exc:  # noqa: BLE001 — compute must never sink the report
        print(f"session-tracker: compute status unavailable: {exc}", file=sys.stderr)
    roster = _roster(db_path, repo_root, reports)
    message = render(overall, roster, reports, capacity)
    try:
        pending = decisions.register_repair_proposals(repo_root, overall)
    except Exception as exc:  # noqa: BLE001 — proposals must never sink the report
        print(f"session-tracker: repair proposals unavailable: {exc}", file=sys.stderr)
        pending = []
    proposals_text = decisions.render_proposals(pending)
    if proposals_text:
        message = f"{message}\n{proposals_text}"
    # Pool-health alerts → numbered confirm-first balancer decisions (drain /
    # spin-down / re-route never auto-fire). Registered here so the open slate
    # rides the hourly report; the informational colored posts fire at the end
    # of this same tick via compute_mod.post_alerts.
    balancer_text = ""
    if compute is not None:
        try:
            alerts = compute_mod.detect_pool_alerts(compute)
            balancer_pending = compute_mod.register_balancer_proposals(repo_root, alerts)
            balancer_text = compute_mod.render_balancer_proposals(balancer_pending)
        except Exception as exc:  # noqa: BLE001 — balancer proposals never sink the report
            print(f"session-tracker: balancer proposals unavailable: {exc}", file=sys.stderr)
    if balancer_text:
        message = f"{message}\n{balancer_text}"
    try:
        balances_text = gather_balances(reports)
    except Exception as exc:  # noqa: BLE001 — balances must never sink the report
        print(f"session-tracker: balances unavailable: {exc}", file=sys.stderr)
        balances_text = "AI BALANCES\n  probe error — balances unavailable this hour"
    if balances_text:
        message = f"{message}\n{balances_text}"
    color, blocks = slack_blocks.tracker_blocks(
        overall,
        roster,
        reports,
        stamp=f"{_iso(_utcnow())} · hourly session tracker",
        fresh_seconds=DROP_FRESH_SECONDS,
    )
    # Compute section — one styled group mirroring the plaintext COMPUTE lines
    # (same pre-formatted strings → structural text↔Slack parity).
    blocks = slack_blocks.append_capacity(blocks, capacity.aggregate, capacity.machines)
    if proposals_text:
        blocks = slack_blocks.append_proposals(blocks, proposals_text)
    if balancer_text:
        blocks = slack_blocks.append_proposals(blocks, balancer_text)
    if balances_text:
        blocks = slack_blocks.append_balances(blocks, balances_text)
    if args.dry_run:
        print(f"[dry-run -> {channel}] color={color} blocks={len(blocks)}\n{message}")
        return 0
    notifier = SlackNotifier(os.environ.get("SLACK_BOT_TOKEN", ""), channel=channel)
    if not notifier.token:
        print("session-tracker: no SLACK_BOT_TOKEN", file=sys.stderr)
        return 1
    if not notifier.post_channel(message, blocks=blocks, color=color):
        print(f"session-tracker: post failed: {notifier.last_error}", file=sys.stderr)
        return 1
    # post_alerts's production call site: pool-health alerts ride this same
    # tick, colored, through the same notifier; remediations stay confirm-first.
    if compute is not None:
        try:
            compute_mod.post_alerts(compute_mod.detect_pool_alerts(compute), notifier)
        except Exception as exc:  # noqa: BLE001 — alerts must never sink the report
            print(f"session-tracker: pool alerts unavailable: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
