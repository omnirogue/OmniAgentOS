"""Fleet preflight: "what is stopping more parallelism RIGHT NOW?" in one call.

The 200-agent fleet has ceilings in five different places -- a YAML file, two
module constants, a per-account limit derived from configs/accounts.yaml, and
the OS file-descriptor limit -- and an operator staring at a fleet that refuses
to widen has no way to tell which one is binding. This module reads all of them,
pairs each with its live usage, and names the ONE with the least headroom.

Read-only and side-effect free: it opens the database, counts, and closes. Wired
nowhere on purpose -- import it (:func:`preflight`) or run it:

    python -m omniagentos.routing.fleet_preflight
    python -m omniagentos.routing.fleet_preflight --json

Ceiling sources (and which of them the code actually ENFORCES):

  fleet.sessions_global      configs/swarm.yaml max_sessions_global — ENFORCED by
                             limit_state.fleet_available().
  fleet.swarm_sessions       max_sessions_global - reserved_small_task_slots —
                             ENFORCED by fleet_available().
  fleet.concurrent_swarms    configs/swarm.yaml max_concurrent_swarms — ENFORCED
                             at admission by intake.service (run #21 queues).
  run.slots                  swarm.scheduler.MAX_SLOTS / planner target cap —
                             ENFORCED per run, informational at fleet level.
  provider.<name>.inflight   enabled accounts x max_inflight_per_account —
                             ENFORCED by limit_state.reserve_account(). This is
                             usually the real ceiling; the YAML numbers above are
                             admission policy, this one is physics.
  os.file_descriptors        RLIMIT_NOFILE (soft) vs the fds the live fleet needs
                             — ENFORCED by the kernel, and the failure mode is
                             not a queued run but an OSError mid-spawn.
  project.sessions           configs/concurrency.yaml max_sessions_per_project —
                             NOT ENFORCED anywhere (advisory; reported so nobody
                             mistakes it for a live guardrail).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import resource
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from omniagentos.contracts import default_db_path
from omniagentos.db.store import _connect
from omniagentos.routing.limit_state import (
    _DEFAULT_MAX_SESSIONS_GLOBAL,
    _DEFAULT_RESERVED_SMALL_TASK_SLOTS,
    count_live_sessions,
    count_live_swarm_sessions,
    inflight_by_account,
    load_swarm_config,
    max_agents_per_account,
    max_inflight_per_account,
    provider_pressure,
)

LOG = logging.getLogger(__name__)

#: Active swarm-run statuses (mirrors ``swarm.dal._ACTIVE_STATUSES``; duplicated
#: rather than imported so preflight never pulls the scheduler package in).
_ACTIVE_RUN_STATUSES = ("planning", "running", "merging")

#: File descriptors a live session costs the supervisor process. Measured floor
#: is 1 (the merged stdout/stderr pipe from ``subprocess.Popen``); the factor
#: covers the sandbox wrapper's own transient descriptors and the per-session
#: transcript/manifest opens. Deliberately generous: an fd preflight that says
#: "fine" and is wrong fails at spawn time with an unhelpful OSError.
FDS_PER_SESSION = 4

#: Descriptors the process needs before any session exists: SQLite main/wal/shm
#: for each open store (sessions, swarm, collab, orchestrations, store), the
#: HTTP listener, logs, and the interpreter's own.
FDS_BASE = 128

#: What `ulimit -n` (and a launchd SoftResourceLimits/NumberOfFiles entry) should
#: be for the 200-agent fleet. macOS's kern.maxfilesperproc is 245760, so this is
#: nowhere near a system limit -- but a launchd-started job inherits 256 unless
#: the plist says otherwise, which is roughly 30 sessions' worth.
RECOMMENDED_NOFILE = 8192


@dataclass(frozen=True)
class Ceiling:
    """One ceiling, its live usage, and whether anything enforces it."""

    name: str
    limit: int
    used: int
    source: str
    enforced: bool = True

    @property
    def headroom(self) -> int:
        """Slots still available. Negative means the ceiling is already over-subscribed."""
        return self.limit - self.used

    @property
    def utilization(self) -> float:
        return (self.used / self.limit) if self.limit > 0 else 1.0

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["headroom"] = self.headroom
        data["utilization"] = round(self.utilization, 4)
        return data


@dataclass(frozen=True)
class ProviderInflight:
    """Live per-provider account capacity."""

    provider: str
    enabled_accounts: int
    inflight: int
    capacity: int
    max_inflight_per_account: int
    pressure: float

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["pressure"] = round(self.pressure, 4)
        return data


@dataclass(frozen=True)
class FleetPreflight:
    """The answer to "what's stopping more parallelism right now?"."""

    ceilings: list[Ceiling]
    providers: list[ProviderInflight]
    live_sessions: int
    live_swarm_sessions: int
    active_swarm_runs: int
    warnings: list[str] = field(default_factory=list)

    @property
    def binding(self) -> Ceiling | None:
        """The ENFORCED ceiling with the least headroom (ties: lowest headroom,
        then highest utilization, then name — deterministic for tests)."""
        enforced = [ceiling for ceiling in self.ceilings if ceiling.enforced]
        if not enforced:
            return None
        return min(
            enforced,
            key=lambda ceiling: (ceiling.headroom, -ceiling.utilization, ceiling.name),
        )

    def as_dict(self) -> dict[str, Any]:
        binding = self.binding
        return {
            "live_sessions": self.live_sessions,
            "live_swarm_sessions": self.live_swarm_sessions,
            "active_swarm_runs": self.active_swarm_runs,
            "binding_constraint": binding.name if binding is not None else None,
            "binding_headroom": binding.headroom if binding is not None else None,
            "ceilings": [ceiling.as_dict() for ceiling in self.ceilings],
            "providers": [provider.as_dict() for provider in self.providers],
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Config readers
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent


def concurrency_config_path() -> Path:
    """configs/concurrency.yaml (the ramp runbook; NOT read by any hot path)."""
    override = os.environ.get("OMNIAGENTOS_CONCURRENCY_CONFIG")
    if override:
        return Path(override)
    return _repo_root() / "configs" / "concurrency.yaml"


def load_concurrency_config(path: Path | None = None) -> dict[str, Any]:
    """configs/concurrency.yaml as a plain dict; missing/broken file -> ``{}``."""
    try:
        raw = yaml.safe_load((path or concurrency_config_path()).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def config_disagreements() -> list[str]:
    """Keys where configs/concurrency.yaml contradicts the authoritative
    configs/swarm.yaml. Empty list = the two files agree."""
    swarm = load_swarm_config()
    fleet = load_concurrency_config().get("fleet")
    if not isinstance(fleet, dict):
        return []
    problems: list[str] = []
    for key in ("max_sessions_global", "reserved_small_task_slots", "max_concurrent_swarms"):
        if key not in fleet or key not in swarm:
            continue
        if _int(fleet[key], -1) != _int(swarm[key], -2):
            problems.append(
                f"configs/concurrency.yaml fleet.{key}={fleet[key]} disagrees with "
                f"configs/swarm.yaml {key}={swarm[key]} (swarm.yaml wins)"
            )
    return problems


# ---------------------------------------------------------------------------
# Live counts
# ---------------------------------------------------------------------------


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def count_active_swarm_runs(*, db_path: str | None = None) -> int:
    """Swarm runs holding fleet capacity (planning/running/merging)."""
    conn = _connect(db_path or default_db_path())
    try:
        if not _table_exists(conn, "swarm_runs"):
            return 0
        placeholders = ",".join("?" * len(_ACTIVE_RUN_STATUSES))
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM swarm_runs WHERE status IN ({placeholders})",
            _ACTIVE_RUN_STATUSES,
        ).fetchone()
        return int(row["n"])
    finally:
        conn.close()


def provider_inflight(*, db_path: str | None = None) -> list[ProviderInflight]:
    """Per-provider account capacity and live usage, for every provider that has
    at least one ENABLED account (a provider with none contributes no capacity)."""
    resolved = db_path or default_db_path()
    conn = _connect(resolved)
    try:
        if not _table_exists(conn, "claude_accounts"):
            return []
        rows = conn.execute("SELECT id, provider FROM claude_accounts WHERE enabled = 1").fetchall()
        by_provider: dict[str, list[str]] = {}
        for row in rows:
            by_provider.setdefault(str(row["provider"]), []).append(str(row["id"]))
        result: list[ProviderInflight] = []
        for provider in sorted(by_provider):
            ids = by_provider[provider]
            ceiling = max_inflight_per_account(provider)
            inflight = sum(inflight_by_account(conn, ids).values())
            try:
                pressure = provider_pressure(provider, db_path=resolved)
            except sqlite3.Error:  # pragma: no cover - defensive
                LOG.debug("provider_pressure failed for %s", provider, exc_info=True)
                pressure = 1.0
            result.append(
                ProviderInflight(
                    provider=provider,
                    enabled_accounts=len(ids),
                    inflight=inflight,
                    capacity=len(ids) * ceiling,
                    max_inflight_per_account=ceiling,
                    pressure=pressure,
                )
            )
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# File descriptors
# ---------------------------------------------------------------------------


def nofile_soft_limit() -> int:
    """Current RLIMIT_NOFILE soft limit for THIS process."""
    return int(resource.getrlimit(resource.RLIMIT_NOFILE)[0])


def sessions_supported_by_fds(soft_limit: int | None = None) -> int:
    """How many concurrent sessions the current fd limit can support."""
    limit = nofile_soft_limit() if soft_limit is None else soft_limit
    return max(0, (limit - FDS_BASE) // FDS_PER_SESSION)


def raise_nofile_soft_limit(target: int = RECOMMENDED_NOFILE) -> int:
    """Best-effort raise of this process's RLIMIT_NOFILE soft limit to ``target``.

    Returns the soft limit in force afterwards. A soft limit may always be
    raised up to the HARD limit without privileges, so on a stock macOS
    (hard limit = kern.maxfilesperproc, ~245k) this simply works; where it does
    not, the current limit is returned unchanged rather than raising, because a
    fleet that can run 30 sessions is better than one that will not start.

    NOT called on import and NOT called by the supervisor -- a library must not
    mutate process-wide limits behind its caller's back. This is here so a
    launcher (or an operator at a REPL) has one honest place to do it.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft >= target:
        return int(soft)
    ceiling = target if hard == resource.RLIM_INFINITY else min(target, hard)
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (ceiling, hard))
    except (ValueError, OSError):
        LOG.warning(
            "could not raise RLIMIT_NOFILE from %s to %s; the fleet is capped at ~%s "
            "concurrent sessions",
            soft,
            ceiling,
            sessions_supported_by_fds(int(soft)),
        )
        return int(soft)
    return int(resource.getrlimit(resource.RLIMIT_NOFILE)[0])


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight(*, db_path: str | None = None) -> FleetPreflight:
    """Every effective ceiling, its live usage, and the binding constraint."""
    from omniagentos.swarm.scheduler import MAX_SLOTS

    config = load_swarm_config()
    max_sessions_global = _int(config.get("max_sessions_global"), _DEFAULT_MAX_SESSIONS_GLOBAL)
    reserved = _int(config.get("reserved_small_task_slots"), _DEFAULT_RESERVED_SMALL_TASK_SLOTS)
    max_swarms = _int(config.get("max_concurrent_swarms"), 20)
    concurrency = load_concurrency_config()
    fleet_section = concurrency.get("fleet")
    per_project = _int(
        (fleet_section or {}).get("max_sessions_per_project")
        if isinstance(fleet_section, dict)
        else None,
        0,
    )

    warnings = list(config_disagreements())

    live_sessions = 0
    live_swarm_sessions = 0
    active_runs = 0
    providers: list[ProviderInflight] = []
    db_ok = True
    try:
        live_sessions = count_live_sessions(db_path=db_path)
        live_swarm_sessions = count_live_swarm_sessions(db_path=db_path)
        active_runs = count_active_swarm_runs(db_path=db_path)
        providers = provider_inflight(db_path=db_path)
    except sqlite3.Error as error:
        # A preflight that cannot read the database still reports the CONFIGURED
        # ceilings (and says so) rather than failing the operator's one call.
        db_ok = False
        warnings.append(
            f"database unreadable ({error}); live counts are 0 and per-account capacity is unknown"
        )

    ceilings: list[Ceiling] = [
        Ceiling(
            name="fleet.sessions_global",
            limit=max_sessions_global,
            used=live_sessions,
            source="configs/swarm.yaml max_sessions_global",
        ),
        Ceiling(
            name="fleet.swarm_sessions",
            limit=max(0, max_sessions_global - reserved),
            used=live_swarm_sessions,
            source="configs/swarm.yaml max_sessions_global - reserved_small_task_slots",
        ),
        Ceiling(
            name="fleet.concurrent_swarms",
            limit=max_swarms,
            used=active_runs,
            source="configs/swarm.yaml max_concurrent_swarms",
        ),
        Ceiling(
            name="os.file_descriptors",
            limit=sessions_supported_by_fds(),
            used=live_sessions,
            source=(
                f"RLIMIT_NOFILE soft={nofile_soft_limit()} "
                f"(base {FDS_BASE} + {FDS_PER_SESSION}/session)"
            ),
        ),
    ]
    total_capacity = sum(provider.capacity for provider in providers)
    total_inflight = sum(provider.inflight for provider in providers)
    ceilings.append(
        Ceiling(
            name="provider.account_inflight",
            limit=total_capacity,
            used=total_inflight,
            source=(
                "sum over providers of enabled accounts x "
                "configs/swarm.yaml max_inflight_per_account"
            ),
            # An unreadable database means "unknown", not "zero capacity" —
            # reporting it as the binding constraint would be a confident lie.
            enforced=db_ok,
        )
    )
    # PKG-INSESSION-FANOUT: agents = live sessions + committed live grant
    # budgets, per account. Reported only while the feature flag is on — a
    # dark feature adds no effective ceiling. Claude-scoped: it is the only
    # provider whose CLI has a Task tool to grant.
    try:
        from omniagentos.swarm.insession import (
            insession_enabled,
            live_grant_budget_total,
        )

        if insession_enabled():
            claude = next((p for p in providers if p.provider == "claude"), None)
            enabled_claude = claude.enabled_accounts if claude else 0
            claude_inflight = claude.inflight if claude else 0
            granted = live_grant_budget_total("claude", db_path=db_path) if db_ok else 0
            ceilings.append(
                Ceiling(
                    name="provider.account_agents",
                    limit=enabled_claude * max_agents_per_account("claude"),
                    used=claude_inflight + granted,
                    source=(
                        "enabled claude accounts x configs/swarm.yaml "
                        "max_agents_per_account (sessions + live in-session "
                        "grant budgets)"
                    ),
                    enforced=db_ok,
                )
            )
    except Exception:  # noqa: BLE001 — preflight must still report without the module
        warnings.append("in-session agent ceiling unavailable (insession module error)")

    # Reported but never selected as binding: nothing enforces these.
    ceilings.append(
        Ceiling(
            name="run.slots",
            limit=MAX_SLOTS,
            used=0,
            source="swarm.scheduler.MAX_SLOTS (per RUN, not per fleet)",
            enforced=False,
        )
    )
    if per_project > 0:
        ceilings.append(
            Ceiling(
                name="project.sessions",
                limit=per_project,
                used=0,
                source="configs/concurrency.yaml fleet.max_sessions_per_project (ADVISORY)",
                enforced=False,
            )
        )

    if nofile_soft_limit() < RECOMMENDED_NOFILE:
        warnings.append(
            f"RLIMIT_NOFILE soft limit is {nofile_soft_limit()}; the 200-agent fleet "
            f"wants >= {RECOMMENDED_NOFILE} (`ulimit -n {RECOMMENDED_NOFILE}`, or a "
            "launchd SoftResourceLimits/NumberOfFiles entry in the plist)"
        )
    if total_capacity and total_capacity < max_sessions_global - reserved:
        warnings.append(
            f"per-account capacity ({total_capacity}) is below the swarm session "
            f"ceiling ({max_sessions_global - reserved}): raising the YAML ceilings "
            "further cannot help — add accounts or raise max_inflight_per_account"
        )

    return FleetPreflight(
        ceilings=ceilings,
        providers=providers,
        live_sessions=live_sessions,
        live_swarm_sessions=live_swarm_sessions,
        active_swarm_runs=active_runs,
        warnings=warnings,
    )


def render(report: FleetPreflight) -> str:
    """Human-readable preflight, binding constraint first."""
    lines = [
        "fleet preflight",
        f"  live sessions        {report.live_sessions}",
        f"  live swarm sessions  {report.live_swarm_sessions}",
        f"  active swarm runs    {report.active_swarm_runs}",
        "",
        f"  {'ceiling':<28} {'used':>6} {'limit':>7} {'free':>7}  source",
    ]
    binding = report.binding
    for ceiling in report.ceilings:
        marker = "<<" if ceiling is binding else "  "
        suffix = "" if ceiling.enforced else "  [advisory, not enforced]"
        lines.append(
            f"{marker}{ceiling.name:<28} {ceiling.used:>6} {ceiling.limit:>7} "
            f"{ceiling.headroom:>7}  {ceiling.source}{suffix}"
        )
    if report.providers:
        lines.append("")
        lines.append(f"  {'provider':<12} {'accts':>6} {'inflight':>9} {'cap':>5} {'pressure':>9}")
        for provider in report.providers:
            lines.append(
                f"  {provider.provider:<12} {provider.enabled_accounts:>6} "
                f"{provider.inflight:>9} {provider.capacity:>5} {provider.pressure:>9.2f}"
            )
    lines.append("")
    if binding is None:
        lines.append("  BINDING CONSTRAINT: none (no enforced ceiling found)")
    else:
        lines.append(
            f"  BINDING CONSTRAINT: {binding.name} — {binding.headroom} more "
            f"session(s) before it blocks ({binding.source})"
        )
    for warning in report.warnings:
        lines.append(f"  WARNING: {warning}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m omniagentos.routing.fleet_preflight",
        description="Report effective fleet ceilings and the binding constraint.",
    )
    parser.add_argument("--db", default=None, help="database path (default: OMNIAGENTOS_DB)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    report = preflight(db_path=args.db)
    if args.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())


__all__ = [
    "FDS_BASE",
    "FDS_PER_SESSION",
    "RECOMMENDED_NOFILE",
    "Ceiling",
    "FleetPreflight",
    "ProviderInflight",
    "concurrency_config_path",
    "config_disagreements",
    "count_active_swarm_runs",
    "load_concurrency_config",
    "main",
    "nofile_soft_limit",
    "preflight",
    "provider_inflight",
    "raise_nofile_soft_limit",
    "render",
    "sessions_supported_by_fds",
]
