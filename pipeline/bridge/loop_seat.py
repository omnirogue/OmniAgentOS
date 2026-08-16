#!/usr/bin/env python3
"""Seat-rotation state for run-loop.sh — kills the restart-churn tax.

WHY THIS EXISTS (measured, 2026-08-11→12): the implementer loop sat ~8h at 0
merges/hr. Cause was NOT account exhaustion of the whole ladder — the six
`claudeN` seats are DISTINCT Anthropic accounts with independent weekly quotas,
and claude1/2/3 produced rc=0 work throughout. Cause WAS a control-loop churn:

  * `run-loop.sh` always starts ACTIVE at the LAUNCHER (arg 2 = claude4). Every
    fresh process (operator restart, or loop-watchdog relaunch after a
    hang-recycle) re-enters at claude4, which was weekly-limited, and burns TWO
    iterations failing on it before rotating (68 restarts × 2 fails, measured).
  * The rotated ACTIVE lives only in the tmux process's memory. A hang-recycle +
    watchdog relaunch resets it back to claude4, so a working seat like claude1
    could never accumulate uninterrupted time — it was repeatedly abandoned.

This module gives run-loop.sh a tiny DURABLE rotation state so a restart can:
  (1) DEMOTE a seat that reported a weekly-limit to the BACK of the ladder (it
      stays available as a last resort, but is never LED with while limited), and
  (2) RESUME at the last seat the loop was actually using (`active`), instead of
      resetting to the exhausted launcher.

It deliberately does NOT solve the deeper `claude -p` mid-turn hang (that is the
hang-recycler's job and a separate follow-up) — it only stops the restart from
throwing away the progress the rotation already made.

Marker: <root>/state/<role>.rotation.json  (grouped with the other per-role
operational state — implementer.lock, budget.json, <role>.park.json). It is
runtime state under var/* and is git-ignored; it self-heals (a limited seat
un-demotes automatically once its parsed reset time passes) and never needs an
authenticated event to clear.

FAILURE DIRECTION (this sits in front of the live launcher — get it right):
  * `order` must NEVER break the launcher. On ANY error (missing/corrupt state,
    bad args, unparseable anything) it prints NOTHING and exits non-zero, and
    run-loop.sh keeps the operator's original seat order. A reorder helper that
    can wedge the loop is worse than no reorder at all.
  * `order` output is ALWAYS a permutation of the input seats — never drops or
    invents a seat — so demotion can only ever change PRIORITY, never
    availability. run-loop.sh additionally re-checks the permutation property.
  * A seat whose reset time cannot be parsed is demoted for a bounded fallback
    window only (FALLBACK_TTL_SECONDS), so a parse miss cannot strand a seat.
  * record-* commands fail soft: on any error they exit non-zero (bash guards
    them with `|| true`) and leave the prior state untouched — a failed write
    must never be able to corrupt a good marker (atomic replace).

CLI (called from run-loop.sh):
    loop_seat.py order        --root var/loopqueue --role implementer \\
        --seats claude4,claude1,claude2,claude3,claude6,claude7
        # prints the effective boot order, space-separated; exit 0.
        # exit != 0 and empty stdout  => caller keeps original order.
    loop_seat.py record-limit  --root .. --role .. --seat claude4 --text "<line>"
    loop_seat.py record-active --root .. --role .. --seat claude1
    loop_seat.py clear-limit   --root .. --role .. --seat claude1
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

# `datetime.UTC` is only a 3.11+ alias; `timezone.utc` is identical and works on
# 3.9/3.10, so the demote/resume optimization is not silently disabled on an
# older interpreter than the estate .venv. The noqa keeps UP017 from rewriting
# this to `datetime.UTC` (which would reintroduce that 3.11 floor); the repo
# ruff gate is a zero-count regression gate, so this one line must stay clean.
UTC = timezone.utc  # noqa: UP017

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - stdlib since 3.9, but never hard-fail
    ZoneInfo = None  # type: ignore[assignment]

# A weekly limit whose reset instant we could not parse from the provider text
# is treated as limited for this long from when we saw it — long enough that a
# fresh restart will not immediately re-lead with the dead seat, short enough
# that a parse miss cannot strand a seat that has since recovered. Demotion is
# harmless (the seat is still tried last), so this errs generous.
FALLBACK_TTL_SECONDS = 6 * 3600

# Default timezone for reset strings that omit one. Every observed live message
# used America/New_York ("resets Aug 13 at 2pm (America/New_York)").
DEFAULT_TZ = "America/New_York"

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
    )
}

# "resets Aug 13 at 2pm (America/New_York)" / "resets Aug 11 at 12:30am"
_RESET_RE = re.compile(
    r"resets\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+at\s+"
    r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b"
    r"(?:\s*\(([^)]+)\))?",
    re.IGNORECASE,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_reset_epoch(text: str, now: datetime | None = None) -> int | None:
    """Best-effort parse of a provider weekly-limit reset string to a UTC epoch.

    Returns None (never raises) when the instant cannot be determined; callers
    fall back to FALLBACK_TTL_SECONDS. The message omits the year, so we pick the
    year that puts the reset in the near future (weekly limits reset within a
    week); a match that lands in the past rolls forward one year.
    """
    if not text:
        return None
    try:
        now = now or _now()
        m = _RESET_RE.search(text)
        if not m:
            return None
        mon, day, hour, minute, ampm, tzname = m.groups()
        month = _MONTHS.get(mon[:3].title())
        if not month:
            return None
        hour = int(hour) % 12
        if ampm.lower() == "pm":
            hour += 12
        minute = int(minute) if minute else 0
        if ZoneInfo is None:
            return None
        try:
            tz = ZoneInfo(tzname.strip()) if tzname else ZoneInfo(DEFAULT_TZ)
        except Exception:
            tz = ZoneInfo(DEFAULT_TZ)
        # The message omits the year. A weekly reset is always NEAR now (within a
        # week), so pick the candidate year (prev/current/next) whose datetime is
        # CLOSEST to now. This is correct across BOTH year boundaries: a "Dec 31"
        # seen on Jan 2 resolves to last year (already reset — epoch in the past,
        # seat not demoted), not +360 days, and a "Jan 1" seen on Dec 31 resolves
        # to next year. A single forward-only roll could not do the former.
        base_year = now.astimezone(tz).year
        best: tuple[float, datetime] | None = None
        for yr in (base_year - 1, base_year, base_year + 1):
            try:
                cand = datetime(yr, month, int(day), hour, minute, tzinfo=tz)
            except ValueError:
                continue  # e.g. Feb 29 in a non-leap year
            dist = abs((cand.astimezone(UTC) - now).total_seconds())
            if best is None or dist < best[0]:
                best = (dist, cand)
        if best is None:
            return None
        return int(best[1].astimezone(UTC).timestamp())
    except Exception:
        return None


def _state_path(root: str, role: str) -> str:
    return os.path.join(root, "state", f"{role}.rotation.json")


def _load(root: str, role: str) -> dict:
    """Read the marker. Any problem => empty state (fail toward original order)."""
    try:
        with open(_state_path(root, role), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(root: str, role: str, data: dict) -> None:
    """Atomic replace so a crash mid-write cannot corrupt a good marker."""
    path = _state_path(root, role)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".rot-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _is_limited(entry: dict, now: datetime) -> bool:
    """A seat is limited until its parsed reset passes; if the reset could not be
    parsed, until FALLBACK_TTL_SECONDS after we recorded it."""
    if not isinstance(entry, dict):
        return False
    epoch = entry.get("reset_epoch")
    if isinstance(epoch, (int, float)):
        return now.timestamp() < float(epoch)
    rec = entry.get("recorded_ts")
    try:
        rec_dt = datetime.strptime(rec, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except Exception:
        return False  # cannot tell how old -> do not strand the seat
    return now < rec_dt + timedelta(seconds=FALLBACK_TTL_SECONDS)


def compute_order(seats: list[str], state: dict, now: datetime | None = None) -> list[str]:
    """Return a PERMUTATION of `seats`: preserved-active first (if healthy),
    then the remaining healthy seats in original order, then currently-limited
    seats demoted to the back in original order."""
    now = now or _now()
    limited_map = state.get("limited") or {}
    if not isinstance(limited_map, dict):
        limited_map = {}

    def limited(seat: str) -> bool:
        return _is_limited(limited_map.get(seat, {}), now)

    healthy = [s for s in seats if not limited(s)]
    demoted = [s for s in seats if limited(s)]

    order: list[str] = []
    active = state.get("active")
    if isinstance(active, str) and active in healthy:
        order.append(active)
        healthy.remove(active)  # remove ONE occurrence only — keep an exact permutation if the ladder has duplicates
    order.extend(healthy)
    order.extend(demoted)
    return order


# ---- commands -------------------------------------------------------------


def cmd_order(args) -> int:
    seats = [s for s in (args.seats or "").split(",") if s.strip()]
    seats = [s.strip() for s in seats]
    if not seats:
        return 1
    state = _load(args.root, args.role)
    order = compute_order(seats, state)
    # Invariant: output MUST be a permutation of the input. If anything violated
    # that, emit nothing so the caller keeps the operator's order.
    if sorted(order) != sorted(seats):
        return 1
    sys.stdout.write(" ".join(order) + "\n")
    return 0


def cmd_record_limit(args) -> int:
    state = _load(args.root, args.role)
    limited = state.get("limited")
    if not isinstance(limited, dict):
        limited = {}
    now = _now()
    limited[args.seat] = {
        "reset_text": (args.text or "").strip()[:200],
        "reset_epoch": parse_reset_epoch(args.text or "", now),
        "recorded_ts": _iso(now),
    }
    state["limited"] = limited
    _save(args.root, args.role, state)
    return 0


def cmd_record_active(args) -> int:
    state = _load(args.root, args.role)
    state["active"] = args.seat
    state["active_ts"] = _iso(_now())
    _save(args.root, args.role, state)
    return 0


def cmd_clear_limit(args) -> int:
    state = _load(args.root, args.role)
    limited = state.get("limited")
    if isinstance(limited, dict) and args.seat in limited:
        del limited[args.seat]
        state["limited"] = limited
        _save(args.root, args.role, state)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="run-loop.sh seat-rotation state")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in ("order", "record-limit", "record-active", "clear-limit"):
        sp = sub.add_parser(name)
        sp.add_argument("--root", required=True)
        sp.add_argument("--role", required=True)
        if name == "order":
            sp.add_argument("--seats", required=True)
        else:
            sp.add_argument("--seat", required=True)
        if name == "record-limit":
            sp.add_argument("--text", default="")
    args = p.parse_args(argv)
    try:
        if args.cmd == "order":
            return cmd_order(args)
        if args.cmd == "record-limit":
            return cmd_record_limit(args)
        if args.cmd == "record-active":
            return cmd_record_active(args)
        if args.cmd == "clear-limit":
            return cmd_clear_limit(args)
        return 2
    except Exception as exc:  # never break the caller; fail soft
        sys.stderr.write(f"loop_seat: {args.cmd} failed: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
