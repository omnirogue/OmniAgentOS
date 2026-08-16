#!/usr/bin/env python3
"""Park-marker logic shared by run-loop.sh (writer) and loop-watchdog.sh
(reader). Fixes the defect where `run-loop.sh`'s terminal "every seat
failed — PARKING" exit (rc=2, Ruling #4 could-not-run) got converted back
into "retry this input forever" by a watchdog that only asked
`tmux has-session` and had no notion of a park.

Marker location: var/loopqueue/state/<role>.park.json — grouped with the
other per-role OPERATIONAL state files already in that directory
(implementer.lock, implementer.pid, budget.json). This is deliberately NOT
var/loopqueue/parked/: that directory holds CONTRACT-governed content items
(proposals/candidates/findings) that only leave via an authenticated
`unparked` ledger event, and janitor.py's retention sweep treats them as
exempt-while-present. A loop-ROLE liveness park is a different kind of
thing — it self-clears on the next successful iteration (see clear_park)
and must not be swept, exempted, or unparked by that machinery.

FAILURE DIRECTION (this is a liveness supervisor, get this right):
  * An unreadable / corrupt / unparseable marker must fail TOWARD
    RESTARTING — a loop kept down forever by a bad marker is worse than a
    spurious restart. See check_park(). The corrupt marker is immediately
    replaced with a short fallback-backoff park so a corrupt marker cannot
    itself reproduce the every-tick restart storm this exists to fix.
  * A marker that IS present and valid must be honoured exactly.
  * No single park may exceed MAX_PARK_SECONDS, and no CHAIN of renewed
    parks for the same role may run longer than MAX_PARK_SECONDS from the
    first park in the chain (first_parked_at) without one alert — a park
    must never be able to keep a loop down indefinitely with the operator
    never told.

Usage (CLI, called from bash — see run-loop.sh / loop-watchdog.sh):
    loop_park.py park  --root var/loopqueue --role reviewer \\
        --reason "every seat failed" --seats claude6,claude4 \\
        --alerts-file var/loopqueue/ALERTS.md [--log-tail-file LOG]
    loop_park.py check --root var/loopqueue --role reviewer \\
        --alerts-file var/loopqueue/ALERTS.md
        # exit 0 = restart permitted, exit 1 = refuse (parked, unexpired)
    loop_park.py clear --root var/loopqueue --role reviewer
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py>=3.9 always has it
    ZoneInfo = None  # type: ignore[assignment]

# Floor/ceiling for a park whose `until` cannot be derived from the
# provider's own reset message. 900s matches the existing rc=2
# ("could-not-run") backoff already used elsewhere in run-loop.sh, so a
# fallback park and a could-not-run backoff feel like the same policy.
FALLBACK_BASE_SECONDS = 900          # 15 min floor
FALLBACK_CAP_SECONDS = 4 * 3600      # 4h ceiling on any single fallback park

# No single park, and no unbroken CHAIN of renewed parks for one role, may
# exceed this — even a correctly-parsed provider reset date this far out is
# clamped, and a chain of repeated fallback parks past this triggers a
# one-time "needs a human" alert rather than silently repeating forever.
MAX_PARK_SECONDS = 7 * 24 * 3600     # 7 days

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}

# "You've hit your weekly limit · resets Aug 14 at 4am (America/New_York)"
# Also tolerates "4:30pm" and a plain hyphen/space before the parenthesised zone.
RESET_RE = re.compile(
    r"resets\s+([A-Za-z]{3,9})\s+(\d{1,2})\s+at\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(\s*([^)]+?)\s*\)",
    re.IGNORECASE,
)

TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(TS_FMT)


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, TS_FMT).replace(tzinfo=UTC)


def parse_reset_time(text: str, now_utc: datetime | None = None) -> datetime | None:
    """Parse a provider reset message into an aware UTC datetime, or None.

    Handles the observed shape: 'resets Aug 14 at 4am (America/New_York)'.
    Rolls forward a year if the same-year candidate would already be in the
    past — a provider reset date in a message is always in the future.
    """
    if not text:
        return None
    m = RESET_RE.search(text)
    if not m:
        return None
    mon_s, day_s, hour_s, min_s, ampm, tzname = m.groups()
    mon = _MONTHS.get(mon_s[:3].lower())
    if mon is None or ZoneInfo is None:
        return None
    try:
        tz = ZoneInfo(tzname.strip())
    except Exception:
        return None
    now_utc = now_utc or datetime.now(UTC)
    now_local = now_utc.astimezone(tz)
    try:
        hour = int(hour_s) % 12
        if ampm.lower() == "pm":
            hour += 12
        minute = int(min_s) if min_s else 0
        candidate = datetime(now_local.year, mon, int(day_s), hour, minute, tzinfo=tz)
    except (ValueError, OverflowError):
        return None
    if candidate <= now_local:
        try:
            candidate = candidate.replace(year=candidate.year + 1)
        except ValueError:  # Feb 29 rolling into a non-leap year
            return None
    return candidate.astimezone(UTC)


def compute_until(reason_text: str, streak: int, now: datetime) -> tuple[datetime, str]:
    """Returns (until, source); source is 'provider-reset' or 'fallback-backoff'."""
    parsed = parse_reset_time(reason_text, now)
    if parsed is not None:
        return parsed, "provider-reset"
    backoff = min(FALLBACK_CAP_SECONDS, FALLBACK_BASE_SECONDS * (2 ** max(0, streak - 1)))
    return now + timedelta(seconds=backoff), "fallback-backoff"


def marker_path(loopqueue_root: Path, role: str) -> Path:
    return loopqueue_root / "state" / f"{role}.park.json"


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n")
    tmp.replace(path)   # same-directory rename is atomic — CONTRACT.md pattern


def _append_alerts(alerts_file: Path | None, lines: list[str]) -> None:
    if not alerts_file or not lines:
        return
    alerts_file.parent.mkdir(parents=True, exist_ok=True)
    with open(alerts_file, "a") as f:
        for line in lines:
            f.write(line + "\n")


def write_park(
    root: Path,
    role: str,
    reason: str,
    seats: list[str],
    *,
    alerts_file: Path | None = None,
    log_tail_file: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Write (or refresh) the park marker for `role`.

    Idempotent w.r.t. the ALERTS.md line: if an UNEXPIRED marker already
    covers this park, nothing is written and no alert line is appended —
    that is what makes "alerting once" true regardless of how many times
    this gets called while still parked.
    """
    now = now or datetime.now(UTC)
    path = marker_path(root, role)

    prev: dict[str, Any] | None = None
    prev_readable = True
    if path.exists():
        try:
            prev = json.loads(path.read_text())
        except Exception:
            prev = None
            prev_readable = False  # do not propagate state from garbage

    if prev is not None and prev_readable:
        try:
            prev_until = _parse_ts(prev["until"])
        except Exception:
            prev_until = None
        if prev_until is not None and now < prev_until:
            return {"action": "skipped-already-parked", "marker": prev, "alert_lines": []}

    streak = 1
    first_parked_at = now
    prev_breached = False
    if prev is not None and prev_readable:
        if prev.get("until_source") == "fallback-backoff":
            try:
                streak = int(prev.get("fallback_streak") or 0) + 1
            except (TypeError, ValueError):
                streak = 1
        fp = prev.get("first_parked_at") or prev.get("parked_at")
        if fp:
            try:
                first_parked_at = _parse_ts(fp)
            except Exception:
                first_parked_at = now
        prev_breached = bool(prev.get("cap_breached"))

    log_tail = ""
    if log_tail_file is not None:
        try:
            log_tail = Path(log_tail_file).read_text(errors="replace")[-4000:]
        except Exception:
            log_tail = ""

    raw_until, source = compute_until(f"{reason}\n{log_tail}", streak, now)
    cumulative_ceiling = first_parked_at + timedelta(seconds=MAX_PARK_SECONDS)
    per_park_ceiling = now + timedelta(seconds=MAX_PARK_SECONDS)
    until = min(raw_until, cumulative_ceiling, per_park_ceiling)
    cap_breached = prev_breached or (raw_until > cumulative_ceiling)

    marker: dict[str, Any] = {
        "role": role,
        "parked_at": _iso(now),
        "first_parked_at": _iso(first_parked_at),
        "until": _iso(until),
        "reason": reason,
        "seats": list(seats),
        "until_source": source,
        "fallback_streak": streak if source == "fallback-backoff" else 0,
        "cap_breached": cap_breached,
        "alerted": True,
        "refusal_logged": False,
    }

    alert_lines: list[str] = [
        f"- {_iso(now)} {role}-loop parked: every account failed "
        f"(until {_iso(until)}, {source}; seats: {','.join(seats) or 'none'})"
    ]
    if cap_breached and not prev_breached:
        alert_lines.append(
            f"- {_iso(now)} {role}-loop has been parked continuously since "
            f"{_iso(first_parked_at)}, past the {MAX_PARK_SECONDS // 3600}h cap "
            "— needs a human"
        )
    _atomic_write_json(path, marker)
    _append_alerts(alerts_file, alert_lines)
    return {"action": "parked", "marker": marker, "alert_lines": alert_lines}


def clear_park(root: Path, role: str) -> bool:
    """Called after a successful iteration. Returns True if a marker existed."""
    path = marker_path(root, role)
    if path.exists():
        path.unlink()
        return True
    return False


def check_park(
    root: Path,
    role: str,
    *,
    alerts_file: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Liveness decision consumed by loop-watchdog.sh before it restarts a
    dead session.

    Returns a dict with:
      restart_allowed: bool
      message: str          human line to log; '' means "say nothing"
                             (used to make the refusal log ONCE per park,
                             not once per tick)
      corrupt: bool          True if the marker could not be parsed
    """
    now = now or datetime.now(UTC)
    path = marker_path(root, role)
    if not path.exists():
        return {"restart_allowed": True, "message": "", "corrupt": False}

    try:
        raw = path.read_text()
        marker = json.loads(raw)
        until = _parse_ts(marker["until"])
    except Exception as exc:
        # FAIL TOWARD LIVENESS: allow the restart. But replace the corrupt
        # marker with a short fallback park FIRST so a corrupt marker that
        # keeps reappearing cannot reproduce the every-tick restart storm —
        # the next tick sees a valid, unexpired marker and is rate-limited
        # by FALLBACK_BASE_SECONDS instead of restarting immediately again.
        msg = (
            f"loop-watchdog: {role} park marker at {path} is unreadable/corrupt "
            f"({type(exc).__name__}: {exc}) — ALLOWING restart (fail toward "
            f"liveness) and replacing it with a {FALLBACK_BASE_SECONDS}s "
            "fallback park to rate-limit any repeat"
        )
        try:
            write_park(
                root, role,
                reason=f"corrupt park marker replaced ({type(exc).__name__}: {exc})",
                seats=[], alerts_file=alerts_file, now=now,
            )
        except Exception:
            pass  # never let a write failure block the fail-toward-restart path
        return {"restart_allowed": True, "message": msg, "corrupt": True}

    if now < until:
        if marker.get("refusal_logged"):
            return {"restart_allowed": False, "message": "", "corrupt": False}
        marker["refusal_logged"] = True
        try:
            _atomic_write_json(path, marker)
        except Exception:
            pass
        reason = marker.get("reason", "")
        return {
            "restart_allowed": False,
            "message": (
                f"loop-watchdog: {role} is parked until {marker['until']} "
                f"({reason}) — refusing restart"
            ),
            "corrupt": False,
        }

    return {
        "restart_allowed": True,
        "message": f"loop-watchdog: {role} park expired at {marker['until']} — restart permitted",
        "corrupt": False,
    }


def _cli() -> int:
    # --root/--role live on a shared PARENT parser and are attached to each
    # subcommand (not just the top-level parser): argparse hands everything
    # after the subcommand token to that subparser, so `park --root X ...`
    # would otherwise fail with "the following arguments are required:
    # --root, --role" even though they were given — every caller in
    # run-loop.sh / loop-watchdog.sh puts the subcommand FIRST.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", required=True, type=Path, help="var/loopqueue root")
    common.add_argument("--role", required=True)

    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_park = sub.add_parser("park", parents=[common])
    p_park.add_argument("--reason", required=True)
    p_park.add_argument("--seats", default="", help="comma-separated seat list")
    p_park.add_argument("--alerts-file", type=Path, default=None)
    p_park.add_argument("--log-tail-file", type=Path, default=None)

    p_check = sub.add_parser("check", parents=[common])
    p_check.add_argument("--alerts-file", type=Path, default=None)

    sub.add_parser("clear", parents=[common])

    args = ap.parse_args()

    if args.cmd == "park":
        seats = [s for s in args.seats.split(",") if s]
        result = write_park(
            args.root, args.role, args.reason, seats,
            alerts_file=args.alerts_file, log_tail_file=args.log_tail_file,
        )
        m = result["marker"]
        print(f"{result['action']}: {args.role} until {m['until']} ({m['until_source']})")
        return 0

    if args.cmd == "check":
        result = check_park(args.root, args.role, alerts_file=args.alerts_file)
        if result["message"]:
            print(result["message"])
        return 0 if result["restart_allowed"] else 1

    if args.cmd == "clear":
        cleared = clear_park(args.root, args.role)
        print(f"cleared: {args.role}" if cleared else f"no-marker: {args.role}")
        return 0

    return 2  # pragma: no cover - argparse enforces cmd in {park,check,clear}


if __name__ == "__main__":
    sys.exit(_cli())

