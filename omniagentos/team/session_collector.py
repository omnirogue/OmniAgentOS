"""Collect this machine's live AI work sessions into one JSON report.

SELF-CONTAINED BY DESIGN: stdlib only, no omniagentos imports, so the file can
be copied to any teammate's laptop or server and run with a bare ``python3`` —
that is the whole deployment story for remote machines (see
docs/operations/team-dev-setup.md, "Hourly session reporting").

What it reads (never writes): Claude Code transcripts under
``~/.claude/projects/<slug>/<session>.jsonl`` and Codex rollouts under
``~/.codex/sessions/**/rollout-*.jsonl``. A session is REPORTED when its file
changed inside ``--window`` minutes and ACTIVE when it changed inside
``--active-window`` minutes. The description is the session's first user
message (one line, truncated) — the honest "what is this session working on"
without any model call.

Transports (compose freely):
  --out FILE          atomic local write (the tracker reads these drop-files)
  --post URL          POST the JSON to the team API (through the tunnel)
  --slack-webhook URL interim transport before the tunnel exists: posts one
                      compact line the tracker can parse back out of channel
                      history (``SESSIONREPORT <employee> <base64-json>``)

Every transport failure is loud on stderr and the exit code is non-zero only
when NO transport succeeded — a laptop that is offline for one hour should not
page anyone, but a report that went nowhere must not look delivered.

KILL SWITCH: the machine's owner can stop collection at any time by creating
``~/.ai-telemetry-off`` (``telemetry_ctl.py off`` does this; ``on`` removes
it). While the marker exists, NOTHING is scanned — no transcripts opened, no
usage read — and the only thing reported is an "opted out since <time>"
marker, so dashboards show WHY the feed stopped instead of the person looking
offline. No approval is needed and nothing re-enables it except the owner.
Privacy contract: docs/operations/team-telemetry-privacy.md.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

SCHEMA = 1
MAX_SESSIONS = 20
MAX_DESCRIPTION = 140
# Read at most this many lines hunting for the first user message; a transcript
# whose early lines are all tool traffic still yields the project name.
_SCAN_LINES = 120


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# Owner kill switch. Duplicated verbatim in transcript_uploader.py and
# telemetry_ctl.py — the standalone-copy contract forbids importing it.
OPT_OUT_MARKER = ".ai-telemetry-off"


def _opt_out_since(home: Path) -> str | None:
    """ISO time telemetry was switched off, or None when it is on.

    Any marker file counts as OFF — a hand-made empty file must work, not just
    the JSON telemetry_ctl writes — so an unparseable marker falls back to the
    file's mtime rather than being ignored (fail toward the owner's choice).
    A marker that EXISTS but cannot be read (permissions, I/O error) also
    counts as OFF: an unreadable off-switch is still an off-switch — only a
    genuinely absent marker means ON (cross-lineage review finding 1).
    """
    path = home / OPT_OUT_MARKER
    try:
        raw = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return "unknown (marker unreadable)"
    try:
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("off_since"):
            return str(data["off_since"])
    except ValueError:
        pass
    try:
        return _iso(path.stat().st_mtime)
    except OSError:
        return _iso(time.time())


def _one_line(text: str) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed[:MAX_DESCRIPTION]


def _first_user_text(path: Path) -> str:
    """The first human-authored message in a JSONL transcript, best-effort."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(_SCAN_LINES):
                line = handle.readline()
                if not line:
                    break
                if '"user"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                # Codex rollouts nest the message under "payload"; Claude Code
                # nests it under "message"; some writers inline "content".
                payload = record.get("payload")
                if isinstance(payload, dict) and payload.get("role") == "user":
                    record = payload
                message = record.get("message")
                if isinstance(message, dict) and message.get("role") not in (None, "user"):
                    continue
                content: Any = (
                    message.get("content") if isinstance(message, dict) else None
                ) or record.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        str(part.get("text", "")) for part in content if isinstance(part, dict)
                    )
                text = _one_line(str(content or ""))
                # Skip tool-result echoes and empty shells; a real goal has words.
                if text and not text.startswith(("<", "{", "[")):
                    return text
    except OSError:
        pass
    return ""


def _claude_config_dirs(home: Path) -> list[Path]:
    """Every Claude config profile on this machine, default first.

    Multi-account fleets run sessions under either sibling
    ``~/.claude-account-N`` profiles or named directories below
    ``~/.claude-accounts/`` (via CLAUDE_CONFIG_DIR); each keeps its own
    ``projects/`` transcript tree.
    Scanning only ``~/.claude`` reports one profile of a many-profile fleet.
    """
    dirs = [home / ".claude"]
    # The name must carry a real suffix: a stray bare `~/.claude-account-`
    # directory exists on at least one fleet machine and would label sessions
    # as the meaningless "@account-".
    dirs.extend(
        sorted(
            p for p in home.glob(".claude-account-*") if p.is_dir() and p.name != ".claude-account-"
        )
    )
    account_root = home / ".claude-accounts"
    if account_root.is_dir():
        dirs.extend(sorted(p for p in account_root.iterdir() if p.is_dir()))
    return dirs


def _account_label(config_dir: Path) -> str:
    name = config_dir.name
    return "default" if name == ".claude" else name.removeprefix(".claude-")


def _scan_claude(home: Path, since: float) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    for config_dir in _claude_config_dirs(home):
        projects = config_dir / "projects"
        if not projects.is_dir():
            continue
        account = _account_label(config_dir)
        for transcript in projects.glob("*/*.jsonl"):
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            if mtime < since:
                continue
            slug = transcript.parent.name.lstrip("-").replace("-", "/")
            sessions.append(
                {
                    "harness": "claude",
                    "account": account,
                    "id": transcript.stem,
                    "project": slug,
                    "description": _first_user_text(transcript) or slug,
                    "last_active": _iso(mtime),
                    "_mtime": mtime,
                }
            )
    return sessions


def _scan_codex(home: Path, since: float) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    root = home / ".codex" / "sessions"
    if not root.is_dir():
        return sessions
    for rollout in root.rglob("rollout-*.jsonl"):
        try:
            mtime = rollout.stat().st_mtime
        except OSError:
            continue
        if mtime < since:
            continue
        sessions.append(
            {
                "harness": "codex",
                "id": rollout.stem,
                "project": "",
                "description": _first_user_text(rollout) or "codex session",
                "last_active": _iso(mtime),
                "_mtime": mtime,
            }
        )
    return sessions


def _usage_json(config_dir: Path) -> dict[str, Any] | None:
    """The json carrying ``oauthAccount``/``cachedUsageUtilization`` for a profile.

    The default ``~/.claude`` keeps it OUTSIDE the dir (``~/.claude.json``);
    profiles keep it inside. Prefer whichever candidate actually has usage."""
    fallback: dict[str, Any] | None = None
    for path in (config_dir / ".claude.json", Path(str(config_dir) + ".json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("cachedUsageUtilization"), dict):
            return data
        fallback = fallback or data
    return fallback


def _worst_weekly_percent(data: dict[str, Any]) -> tuple[float | None, str | None, float | None]:
    """(worst weekly percent consumed, weekly reset, snapshot age hours).

    The 5h session window recovers on its own; the weekly windows are the real
    balance, so the worst WEEKLY window is the binding number. A payload with
    NO weekly entry reads as unmeasured (None) — a session-only percentage
    must never be promoted to a weekly balance (cross-lineage review COL-01).
    Pre-``limits[]`` CLIs use the ``seven_day`` scalar, which IS weekly."""
    cached = data.get("cachedUsageUtilization")
    if not isinstance(cached, dict):
        return None, None, None
    util = cached.get("utilization")
    if not isinstance(util, dict):
        return None, None, None
    age_h: float | None = None
    ms = cached.get("fetchedAtMs")
    if isinstance(ms, (int, float)) and not isinstance(ms, bool):
        age_h = max(0.0, (time.time() - ms / 1000.0) / 3600.0)
    weekly: list[float] = []
    reset: str | None = None
    limits = util.get("limits")
    if isinstance(limits, list):
        for entry in limits:
            if not isinstance(entry, dict):
                continue
            pct = entry.get("percent")
            if isinstance(pct, bool) or not isinstance(pct, (int, float)):
                continue
            if float(pct) != float(pct) or float(pct) in (float("inf"), float("-inf")):
                continue  # NaN/inf is corrupt telemetry, not a measurement
            if entry.get("kind") in ("weekly_all", "weekly_scoped"):
                weekly.append(float(pct))
                if entry.get("kind") == "weekly_all" and entry.get("resets_at"):
                    reset = str(entry["resets_at"])
    else:  # pre-limits[] fallback: the seven_day scalar (a weekly window)
        block = util.get("seven_day")
        if isinstance(block, dict):
            pct = block.get("utilization")
            if isinstance(pct, (int, float)) and not isinstance(pct, bool):
                weekly.append(float(pct))
                if block.get("resets_at"):
                    reset = str(block["resets_at"])
    return (max(weekly) if weekly else None), reset, age_h


def collect_claude_usage(home: Path) -> dict[str, Any]:
    """Per-account Claude balance for this machine — the dispatcher/alert input.

    Deduplicates by ``oauthAccount.accountUuid``: two config dirs logged into
    the same Anthropic account share ONE window and must never be counted as
    two fallbacks. ``best_remaining_percent`` is computed over authenticated,
    distinct, MEASURED accounts only; authenticated accounts with no snapshot
    yet are counted in ``authed_no_snapshot`` (likely fresh/full — a possible
    fallback, but never presented as a measured number)."""
    accounts: list[dict[str, Any]] = []
    dirs = [d for d in _claude_config_dirs(home) if d.is_dir()]
    dirs.extend(sorted(p for p in home.glob(".claude-twin*") if p.is_dir() and p not in dirs))
    for config_dir in dirs:
        data = _usage_json(config_dir)
        email = ""
        uuid = ""
        if isinstance(data, dict):
            oauth = data.get("oauthAccount")
            if isinstance(oauth, dict):
                email = str(oauth.get("emailAddress") or oauth.get("email") or "")
                uuid = str(oauth.get("accountUuid") or "")
        worst, reset, age_h = (
            _worst_weekly_percent(data) if isinstance(data, dict) else (None, None, None)
        )
        try:
            authed = os.path.getsize(config_dir / ".credentials.json") > 2
        except OSError:
            authed = False
        accounts.append(
            {
                "dir": config_dir.name,
                "email": email,
                "authed": authed,
                "worst_used_percent": worst,
                "remaining_percent": (None if worst is None else round(100.0 - worst, 1)),
                "snapshot_age_hours": (None if age_h is None else round(age_h, 1)),
                "weekly_reset": reset,
                "duplicate_of": None,
                "_uuid": uuid,
            }
        )
    # Evidence-first dedupe (cross-lineage review COL-01): among dirs logged
    # into the same Anthropic account, the PRIMARY is the one that carries the
    # binding evidence — authed+measured first, then authed, then first-seen —
    # so a stale unauthenticated copy can never mask the account's real state.
    by_uuid: dict[str, list[dict[str, Any]]] = {}
    for entry in accounts:
        uuid = str(entry.pop("_uuid"))
        if uuid:
            by_uuid.setdefault(uuid, []).append(entry)

    def _evidence_rank(entry: dict[str, Any]) -> tuple[int, float, float]:
        # Lower sorts first. Measured+authed beats authed beats anything;
        # among measured duplicates of ONE account the FRESHEST snapshot is
        # the binding evidence (they describe the same window at different
        # times), and on an age tie the WORST percent wins — conservative,
        # a 5%-used stale copy must never mask a 99%-used current one
        # (review COL-01 R2).
        measured = entry["authed"] and entry["remaining_percent"] is not None
        if not measured:
            return (1 if entry["authed"] else 2, 0.0, 0.0)
        age = entry["snapshot_age_hours"]
        age_key = float(age) if age is not None else float("1e9")
        return (0, age_key, -float(entry["worst_used_percent"]))

    for group in by_uuid.values():
        if len(group) < 2:
            continue
        primary = min(group, key=_evidence_rank)
        for entry in group:
            if entry is not primary:
                entry["duplicate_of"] = primary["dir"]
    distinct = [a for a in accounts if not a["duplicate_of"]]
    measured = [a for a in distinct if a["authed"] and a["remaining_percent"] is not None]
    best = max(measured, key=lambda a: a["remaining_percent"]) if measured else None
    return {
        "accounts": accounts,
        "distinct_accounts": len(distinct),
        "authed_accounts": sum(1 for a in distinct if a["authed"]),
        "authed_no_snapshot": sum(
            1 for a in distinct if a["authed"] and a["remaining_percent"] is None
        ),
        # None means NO measured authenticated account — unknown, never "fine".
        "best_remaining_percent": best["remaining_percent"] if best else None,
        "best_dir": best["dir"] if best else None,
    }


def _opted_out_report(employee_id: str, opted_out_since: str, now: float) -> dict[str, Any]:
    """The ONLY payload that may leave an opted-out machine.

    Keeps every key its readers touch (zeros/empties) so an older tracker
    renders it as harmless silence, and the opted_out fields let a current
    tracker say WHY instead of "no report received".
    """
    return {
        "schema": SCHEMA,
        "employee_id": employee_id,
        "host": socket.gethostname(),
        "generated_at": _iso(now),
        "opted_out": True,
        "opted_out_since": opted_out_since,
        "active_count": 0,
        "recent_count": 0,
        "sessions": [],
        "claude_usage": {
            "accounts": [],
            "distinct_accounts": 0,
            "authed_accounts": 0,
            "authed_no_snapshot": 0,
            "best_remaining_percent": None,
            "best_dir": None,
        },
    }


def collect(
    employee_id: str,
    window_min: int,
    active_min: int,
    *,
    include_codex: bool = False,
) -> dict[str, Any]:
    """Scan this machine's Claude profiles (all accounts) for work sessions.

    Codex (and other CLI harnesses) typically run as SUB-AGENTS of a Claude
    session on this estate, so counting them separately double-counts the same
    work — they are excluded unless ``include_codex`` is explicitly requested
    (operator ruling, 2026-08-11).
    """
    now = time.time()
    home = Path.home()
    opted_out_since = _opt_out_since(home)
    if opted_out_since is not None:
        # OFF means off: no transcript is opened, no usage json is read.
        return _opted_out_report(employee_id, opted_out_since, now)
    since = now - window_min * 60
    found = _scan_claude(home, since)
    if include_codex:
        found.extend(_scan_codex(home, since))
    for item in found:
        item["active"] = (now - item["_mtime"]) <= active_min * 60
    active_count = sum(1 for item in found if item["active"])
    # active_count/recent_count are computed BEFORE the cap so the totals stay
    # honest when a fleet outgrows MAX_SESSIONS. Activity is derived from
    # mtime, so actives are definitionally the newest files and the mtime-desc
    # cap always retains them; the explicit active-first key simply keeps that
    # guarantee if activity ever stops being mtime-derived.
    found.sort(key=lambda s: (s["active"], s["_mtime"]), reverse=True)
    sessions = []
    for item in found[:MAX_SESSIONS]:
        item.pop("_mtime")
        sessions.append(item)
    return {
        "schema": SCHEMA,
        "employee_id": employee_id,
        "host": socket.gethostname(),
        "generated_at": _iso(now),
        "active_count": active_count,
        "recent_count": len(found),
        "sessions": sessions,
        "claude_usage": collect_claude_usage(home),
    }


def _write_atomic(path: Path, payload: dict[str, Any] | list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=str(path.parent), prefix=".session-report-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o644)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def _post_json(url: str, payload: dict[str, Any], token: str | None) -> bool:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 — every transport failure reports the same way
        print(f"session-collector: post failed: {exc}", file=sys.stderr)
        return False


_WEBHOOK_TEXT_LIMIT = 3500  # Slack truncates webhook text ~4000; stay clear of it


def _slack_escape(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _project_label(value: Any) -> str:
    parts = [part for part in str(value or "unknown project").split("/") if part]
    return "/".join(parts[-4:]) if parts else "unknown project"


def _post_webhook(url: str, payload: dict[str, Any]) -> bool:
    """Post readable Slack blocks with a parseable machine fallback."""
    # Active first; within each group MOST RECENT first, so trimming drops the
    # stalest entries (last_active is an ISO string — reverse-sortable as text).
    sessions = sorted(
        payload["sessions"],
        key=lambda s: (s.get("active", False), s.get("last_active", "")),
        reverse=True,
    )
    keep = len(sessions)
    while keep >= 0:
        trimmed = dict(payload, sessions=sessions[:keep])
        encoded = base64.b64encode(json.dumps(trimmed, sort_keys=True).encode("utf-8")).decode(
            "ascii"
        )
        active = payload.get("active_count") or sum(
            1 for s in trimmed["sessions"] if s.get("active")
        )
        dropped = len(sessions) - keep
        recent = payload.get("recent_count") or len(trimmed["sessions"])
        lines = [
            f"*AI session report — {_slack_escape(payload['employee_id'])}*",
            f"*{active} active* · {recent} recent · `{_slack_escape(payload['host'])}` · {_slack_escape(payload['generated_at'])}",
            "",
        ]
        if payload.get("opted_out"):
            lines.insert(
                1,
                f"_Telemetry switched OFF by this machine's owner since "
                f"{_slack_escape(payload.get('opted_out_since') or 'unknown')} — "
                f"no session data collected._",
            )
        for session in trimmed["sessions"]:
            marker = "🟢" if session.get("active") else "⚪"
            lines.append(
                f"{marker} *{_slack_escape(_project_label(session.get('project')))}* "
                f"· `{_slack_escape(session.get('account') or 'default')}` "
                f"· {_slack_escape(session.get('last_active') or 'unknown time')}"
            )
            lines.append(
                f"   {_slack_escape(session.get('description') or 'No session description found.')}"
            )
        if dropped:
            lines.extend(("", f"_{dropped} older sessions omitted to fit Slack's message limit._"))
        readable = "\n".join(lines)
        fallback = f"SESSIONREPORT {payload['employee_id']} {encoded}"
        if len(readable) <= 3000 and len(fallback) <= _WEBHOOK_TEXT_LIMIT:
            if dropped:
                print(
                    f"session-collector: webhook payload trimmed by {dropped} sessions",
                    file=sys.stderr,
                )
            return _post_json(
                url,
                {
                    "text": fallback,
                    "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": readable}}],
                },
                token=None,
            )
        keep -= 1
    print("session-collector: report cannot fit the webhook limit", file=sys.stderr)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--employee", required=True, help="employee id, e.g. emp_alice")
    parser.add_argument("--window", type=int, default=75, help="report window (minutes)")
    parser.add_argument(
        "--active-window", type=int, default=15, help="'active now' threshold (minutes)"
    )
    parser.add_argument(
        "--include-codex",
        action="store_true",
        help="also scan ~/.codex rollouts (normally sub-agents of Claude sessions; off by default)",
    )
    parser.add_argument("--out", help="write the JSON report to this file (atomic)")
    parser.add_argument("--post", help="POST the report to this team-API URL")
    parser.add_argument("--token", help="bearer token for --post")
    parser.add_argument("--slack-webhook", help="post a parseable line to this webhook")
    parser.add_argument("--print", dest="print_json", action="store_true")
    args = parser.parse_args(argv)

    report = collect(
        args.employee, args.window, args.active_window, include_codex=args.include_codex
    )
    # Re-check the kill switch AFTER collection, before anything leaves the
    # machine: an owner who flips it mid-run gets the collected data DISCARDED,
    # not delivered (cross-lineage review finding 2).
    if not report.get("opted_out"):
        late = _opt_out_since(Path.home())
        if late is not None:
            report = _opted_out_report(args.employee, late, time.time())
    if args.print_json or not (args.out or args.post or args.slack_webhook):
        print(json.dumps(report, indent=1, sort_keys=True))
        return 0

    delivered = False
    if args.out:
        _write_atomic(Path(args.out), report)
        delivered = True
    if args.post:
        delivered = _post_json(args.post, report, args.token) or delivered
    if args.slack_webhook:
        delivered = _post_webhook(args.slack_webhook, report) or delivered
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
