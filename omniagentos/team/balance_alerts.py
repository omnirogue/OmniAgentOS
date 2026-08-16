"""Fleet Claude-balance watch: alert when a machine is nearly out with no fallback.

Operator directive (the operator, 2026-08-12): every machine in the fleet (this Mac, the
twins, Alice's and Bob's laptops) runs multiple Claude accounts; dispatchers
route by remaining balance; and the estate must ALERT when any machine's
remaining balance drops below 10% **without a fallback** — i.e. no other
authenticated, distinct account on that machine has measurable balance left.

Input is deliberately uniform: the collector drop-files under
``var/team-sessions/<employee>.json``. Every enrolled machine — local via the
10-minute launchd job, remote via ``--post``/webhook — lands the same shape,
and ``session_collector.collect_claude_usage`` gives each report a
``claude_usage`` block (accounts deduped by Anthropic account uuid, so two
logins of one account never count as two fallbacks).

Alerting discipline (matches the estate's transition-edge convention):
- alert on the TRANSITION into breach, re-alert at most every ``REALERT_H``
  hours while the breach persists, post one recovery note when it clears;
- a machine whose report is stale or whose collector predates ``claude_usage``
  renders as UNKNOWN in the alert context — never as healthy (the
  missing-source-as-favourable class);
- an authenticated account with no usage snapshot yet counts as a *possible*
  fallback (fresh logins have no cache and are usually full) — it suppresses
  the no-fallback alert but is named in the message as unverified.

Run: ``python -m omniagentos.team.balance_alerts [--dry-run]`` (launchd,
every 15 min; see configs/launchd/com.omniagentos.balance-alerts.plist).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

BREACH_BELOW_PCT = 10.0  # remaining % under this = effectively out
REPORT_STALE_S = 3 * 3600.0  # a drop-file older than this is UNKNOWN, not truth
REALERT_H = 6.0  # re-alert cadence while a breach persists
STATE_FILENAME = ".balance-alert-state.json"


@dataclass
class MachineBalance:
    """One machine's balance verdict, derived from its drop-file."""

    host: str
    employee_id: str
    status: str  # ok | breached | no_auth | unknown
    reason: str
    best_remaining: float | None = None
    best_dir: str | None = None
    authed_accounts: int = 0
    authed_no_snapshot: int = 0
    accounts_out: list[str] = field(default_factory=list)


def _sessions_dir(var_root: str | Path) -> Path:
    return Path(var_root) / "team-sessions"


def _load_reports(var_root: str | Path) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    directory = _sessions_dir(var_root)
    if not directory.is_dir():
        return reports
    for path in sorted(directory.glob("*.json")):
        if path.name.startswith("."):
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception:  # noqa: BLE001 — includes RecursionError from a
            # JSON bomb: ANY decode failure is one bad drop-file and one
            # unknown row, never a fleet-wide abort (review API-02-R4).
            reports.append(_unreadable_stub(path))
            continue
        if isinstance(data, dict) and (data.get("host") or data.get("employee_id")):
            reports.append(data)
        else:
            # Parseable but not a report (a JSON list, an empty object, a
            # host-less dict): same unknown-row treatment as corrupt bytes —
            # never a silent skip (review API-01 R3).
            reports.append(_unreadable_stub(path))
    return reports


def _unreadable_stub(path: Path) -> dict[str, Any]:
    return {
        "employee_id": path.stem,
        "host": path.stem,
        "_unreadable": True,
        "_source": str(path),
    }


def _parse_iso(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        import datetime as _dt

        return _dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    """Remote-supplied count -> int; None on any malformed shape (never raises).

    None, not 0: a malformed count degraded to a favorable state would page
    ``no_auth`` on an unreadable machine — or worse, silently clear a standing
    breach via ``no_claude``. Counts are whole and non-negative; a fractional
    or negative value is corruption, not a count (review API-01 R2)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and (value != value or not value.is_integer()):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _as_pct(value: Any) -> float | None:
    """Remote-supplied percentage -> finite float, None on any malformed shape."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if result == result and abs(result) != float("inf") else None


def assess_machine(report: dict[str, Any], *, now: float | None = None) -> MachineBalance:
    """One drop-file -> a balance verdict. Total: never raises on odd shapes."""
    now = time.time() if now is None else now
    host = str(report.get("host") or "?")
    employee = str(report.get("employee_id") or "?")
    if report.get("_unreadable"):
        return MachineBalance(
            host=host,
            employee_id=employee,
            status="unknown",
            reason="drop-file is corrupt or unreadable — machine state unknown",
        )
    if report.get("opted_out"):
        # The owner switched telemetry off: the report's zeroed claude_usage is
        # deliberate ABSENCE of measurement, not "no Claude profiles" — and
        # unknown never pages and never clears, so a standing breach survives
        # the opt-out instead of silently reading as recovered.
        since = str(report.get("opted_out_since") or "unknown time")
        return MachineBalance(
            host=host,
            employee_id=employee,
            status="unknown",
            reason=f"owner opted out of telemetry (since {since}) — balance not measured",
        )
    generated = _parse_iso(report.get("generated_at"))
    if generated is None or (now - generated) > REPORT_STALE_S:
        age = "unknown age" if generated is None else f"{(now - generated) / 3600.0:.1f}h old"
        return MachineBalance(
            host=host,
            employee_id=employee,
            status="unknown",
            reason=f"report is stale ({age}) — balance not current",
        )
    usage = report.get("claude_usage")
    if not isinstance(usage, dict):
        return MachineBalance(
            host=host,
            employee_id=employee,
            status="unknown",
            reason="collector predates claude_usage — machine needs the updated collector",
        )
    authed = _as_int(usage.get("authed_accounts"))
    if "authed_no_snapshot" not in usage:
        no_snap: int | None = 0  # absent field: older collector, nothing to report
    else:
        # A PRESENT null/garbage value is malformed telemetry, not a legacy
        # absence — conflating them can silently clear a standing breach
        # (review API-01 R3).
        no_snap = _as_int(usage.get("authed_no_snapshot"))
    best = _as_pct(usage.get("best_remaining_percent"))
    distinct = _as_int(usage.get("distinct_accounts"))
    if authed is None or distinct is None or no_snap is None:
        return MachineBalance(
            host=host,
            employee_id=employee,
            status="unknown",
            reason="claude_usage counts are malformed — machine state unreadable",
        )
    raw_accounts = usage.get("accounts")
    accounts = raw_accounts if isinstance(raw_accounts, list) else []
    out_dirs = [
        str(a.get("dir"))
        for a in accounts
        if isinstance(a, dict)
        and not a.get("duplicate_of")
        and a.get("authed")
        and _as_pct(a.get("remaining_percent")) is not None
        and float(a["remaining_percent"]) < BREACH_BELOW_PCT
    ]
    common: dict[str, Any] = {
        "host": host,
        "employee_id": employee,
        "best_remaining": best,
        "best_dir": usage.get("best_dir"),
        "authed_accounts": authed,
        "authed_no_snapshot": no_snap,
        "accounts_out": out_dirs,
    }
    if distinct == 0:
        # No Claude profile exists at all: this machine was never provisioned
        # for Claude work. Context, not a breach — a box that never had Claude
        # must not page every re-alert window forever.
        return MachineBalance(
            status="no_claude",
            reason="no Claude profiles on this machine (never provisioned)",
            **common,
        )
    if authed == 0:
        return MachineBalance(
            status="no_auth",
            reason=(
                f"{distinct} Claude profile(s) exist but NONE is authenticated "
                "(auth expired or logged out)"
            ),
            **common,
        )
    if best is not None and best < BREACH_BELOW_PCT:
        if no_snap > 0:
            # NOT "ok": an unmeasured fresh login is an unverified fallback.
            # at_risk neither pages nor clears a standing breach — an "ok"
            # here posted a false recovery and froze the state machine while
            # every measured account sat under the threshold (review ALT-01).
            return MachineBalance(
                status="at_risk",
                reason=(
                    f"measured accounts under {BREACH_BELOW_PCT:.0f}% but {no_snap} "
                    "authenticated account(s) have no snapshot yet (likely fresh/full) "
                    "— unverified fallback"
                ),
                **common,
            )
        return MachineBalance(
            status="breached",
            reason=(
                f"best remaining {best:.1f}% < {BREACH_BELOW_PCT:.0f}% and no fallback "
                f"account has balance"
            ),
            **common,
        )
    if best is None:
        return MachineBalance(
            status="unknown",
            reason=(
                "authenticated accounts but no measurable usage snapshot on any"
                + (f" ({no_snap} fresh/unmeasured)" if no_snap else "")
            ),
            **common,
        )
    return MachineBalance(status="ok", reason="has balance", **common)


def assess_fleet(var_root: str | Path, *, now: float | None = None) -> list[MachineBalance]:
    machines: list[MachineBalance] = []
    for report in _load_reports(var_root):
        try:
            machines.append(assess_machine(report, now=now))
        except Exception as exc:  # noqa: BLE001 — one bad drop-file, one unknown row
            machines.append(
                MachineBalance(
                    host=str(report.get("host") or "?"),
                    employee_id=str(report.get("employee_id") or "?"),
                    status="unknown",
                    reason=f"report could not be assessed ({type(exc).__name__})",
                )
            )
    return machines


# ------------------------------------------------------------------ edge state


def _state_path(var_root: str | Path) -> Path:
    return _sessions_dir(var_root) / STATE_FILENAME


def _load_state(var_root: str | Path) -> dict[str, Any]:
    try:
        with _state_path(var_root).open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(var_root: str | Path, state: dict[str, Any]) -> None:
    from omniagentos.team.session_collector import _write_atomic

    _write_atomic(_state_path(var_root), state)


def decide_notifications(
    machines: list[MachineBalance],
    state: dict[str, Any],
    *,
    now: float | None = None,
) -> tuple[list[MachineBalance], list[MachineBalance], dict[str, Any]]:
    """(newly/re-alerting breaches, recoveries, next state), keyed by employee.

    Breach kinds that page: ``breached`` and ``no_auth``. ``unknown`` and
    ``at_risk`` never page and never clear a breach (an unverified fallback is
    not a recovery); ``no_claude`` never pages and clears silently —
    a machine that stops reporting mid-breach stays breached in state until a
    fresh report proves recovery.
    """
    now = time.time() if now is None else now
    alerts: list[MachineBalance] = []
    recoveries: list[MachineBalance] = []
    next_state = dict(state)
    for machine in machines:
        key = _state_key(machine)
        entry = state.get(key)
        entry = dict(entry) if isinstance(entry, dict) else {}
        was_breached = entry.get("state") == "breached"
        if machine.status in ("breached", "no_auth"):
            last = entry.get("last_alert_ts")
            last = float(last) if isinstance(last, (int, float)) else None
            due = last is None or (now - last) >= REALERT_H * 3600.0
            if not was_breached or due:
                alerts.append(machine)
                entry["last_alert_ts"] = now
            entry["state"] = "breached"
            entry["reason"] = machine.reason
        elif machine.status in ("ok", "no_claude"):
            # no_claude clears a stale breach silently: the machine left the
            # Claude fleet, it did not "recover" balance.
            if was_breached and machine.status == "ok":
                recoveries.append(machine)
            entry["state"] = "ok"
            entry.pop("reason", None)
        # unknown: leave the previous state untouched (no page, no clear)
        next_state[key] = entry
    return alerts, recoveries, next_state


def _state_key(machine: MachineBalance) -> str:
    """Edge-state key: the roster-validated employee identity, never the host.

    ``host`` is unconstrained and non-unique — two laptops named
    ``MacBook-Pro.local`` (or a forged host under another employee's report,
    the shared-tunnel-principal threat model) would collide into one state
    slot, producing false recoveries and silenced breaches (Grok review F1).
    ``employee_id`` is validated against the roster on the API ingest path and
    is the drop-file identity everywhere else. Host only breaks ties for
    legacy/degenerate rows with no employee at all."""
    if machine.employee_id and machine.employee_id != "?":
        return machine.employee_id
    return f"host:{machine.host}"


# ------------------------------------------------------------------ rendering


def _fmt_machine(machine: MachineBalance) -> str:
    best = "—" if machine.best_remaining is None else f"{machine.best_remaining:.0f}%"
    line = (
        f"{machine.host} ({machine.employee_id}): best remaining {best}"
        f" · authed accounts {machine.authed_accounts}"
    )
    if machine.accounts_out:
        line += f" · out: {', '.join(machine.accounts_out)}"
    return f"{line} — {machine.reason}"


def render_text(
    alerts: list[MachineBalance],
    recoveries: list[MachineBalance],
    unknowns: list[MachineBalance],
) -> str:
    lines: list[str] = []
    if alerts:
        lines.append("CLAUDE BALANCE ALERT — machine(s) below 10% with no fallback:")
        lines.extend(f"  • {_fmt_machine(m)}" for m in alerts)
        lines.append(
            "  Remedy: log a fresh account in on that machine "
            "(CLAUDE_CONFIG_DIR=~/.claude-account-N claude login), or wait for the "
            "earliest weekly reset. `claude-roster` on the machine shows per-account detail."
        )
    if recoveries:
        lines.append("Recovered (balance restored):")
        lines.extend(f"  • {_fmt_machine(m)}" for m in recoveries)
    if unknowns:
        lines.append("Not assessable (stale or pre-claude_usage collector):")
        lines.extend(f"  • {_fmt_machine(m)}" for m in unknowns)
    return "\n".join(lines)


def run_once(*, dry_run: bool = False, now: float | None = None) -> int:
    from omniagentos.runtime_paths import resolve_var_root

    var_root = resolve_var_root()
    machines = assess_fleet(var_root, now=now)
    state = _load_state(var_root)
    alerts, recoveries, next_state = decide_notifications(machines, state, now=now)
    unknowns = [m for m in machines if m.status == "unknown"]

    if dry_run:
        # Dry runs never persist state: marking a breach "alerted" without an
        # actual post would silence the real job for REALERT_H hours.
        if alerts or recoveries:
            print(render_text(alerts, recoveries, unknowns))
        else:
            print("balance-alerts: nothing to send")
        for machine in machines:
            print(f"  {machine.status:8s} {_fmt_machine(machine)}")
        return 0

    if not alerts and not recoveries:
        _save_state(var_root, next_state)
        return 0

    text = render_text(alerts, recoveries, unknowns)

    from omniagentos.team.notify import SlackNotifier
    from omniagentos.team.report import load_slack_env
    from omniagentos.team.slack_blocks import GREEN, RED

    load_slack_env()
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("balance-alerts: no SLACK_BOT_TOKEN — not posted", file=sys.stderr)
        return 1
    notifier = SlackNotifier(token)
    color = RED if alerts else GREEN
    posted = notifier.post_channel(text, color=color)
    if not posted:
        # A failed post must NOT record last_alert_ts — the breach stays due so
        # the next tick retries instead of going silent for REALERT_H hours.
        print(
            f"balance-alerts: Slack post failed ({notifier.last_error})",
            file=sys.stderr,
        )
        return 1
    _save_state(var_root, next_state)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print instead of posting to Slack")
    args = parser.parse_args(argv)
    return run_once(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
