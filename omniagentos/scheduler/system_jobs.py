"""Read-only catalog of every recurring/automated job in the system.

The Loops UI (``/routines``) shows managed DB routines — rows in the
``routines`` table fired by :mod:`omniagentos.scheduler.routines_tick`. But most
of the owner's prebuilt automation does NOT live in that table: it is ~30
launchd agents rendered by ``scripts/*/install-*.sh`` (render-only by design,
loaded by hand), the CSI self-improvement pipeline routines declared in
``configs/self_improvement.yaml``, and a handful of remote cron jobs documented
in ``HANDOFF/LOOPS-VISIBILITY.md``. None of that was visible anywhere in the
product.

This module is the honest read-only answer:

* a static :data:`CATALOG`, one entry per job, each grounded in the repo file
  that defines it (installer script + plist template — never invented);
* parsers that derive the schedule from the ACTUAL plist template / rendered
  plist rather than a copied string, so a schedule edit in a template is
  reflected here automatically (``parse_plist`` tolerates ``{{PLACEHOLDER}}``
  templates as well as fully rendered plists);
* best-effort live enrichment: ``launchctl list`` (loaded? last exit status),
  installed-plist scan of ``~/Library/LaunchAgents`` (the machine's truth), and
  stdout/stderr log mtimes as the last-run proxy. Everything is injectable so
  tests never touch the real machine;
* a health state per HANDOFF/LOOPS-VISIBILITY.md §L2: ``healthy`` / ``stale``
  (not fired within 2× its cadence) / ``failing`` (non-zero last exit) /
  ``unknown`` (no observability — said so, never rendered healthy) /
  ``not_loaded``.

Nothing here mutates anything: no launchctl load/unload, no DB writes, no
secret values (env var NAMES only).
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omniagentos.scheduler.remote_probe import (
    RemoteProbeCache,
    RemoteProbeSnapshot,
    docker_service_health,
    remote_cron_present,
    sanitize_remote_text,
)
from omniagentos.scheduler.routines import _cron_field_matches

# Health states (HANDOFF/LOOPS-VISIBILITY.md §L2, plus not_loaded for jobs whose
# render-only installer was never followed by a manual `launchctl load`).
HEALTH_STATES = frozenset({"healthy", "stale", "failing", "unknown", "not_loaded"})

# Staleness rule from the handoff: "stale — has not fired within 2× its interval".
_STALE_FACTOR = 2.0
# Scan bound for cron / calendar next-fire computation (mirrors the routines
# engine's missed-fire catch-up bound).
_SCAN_DAYS = 8
# launchctl is a local command; never let a wedged launchd block the API.
_LAUNCHCTL_TIMEOUT_S = 3.0

_WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


# ---------------------------------------------------------------------------
# Schedule model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarEntry:
    """One launchd ``StartCalendarInterval`` entry (None = wildcard)."""

    hour: int | None = None
    minute: int | None = None
    weekday: int | None = None  # launchd: 0 and 7 both mean Sunday


@dataclass(frozen=True)
class Schedule:
    """A job's cadence. ``kind`` is one of interval|calendar|cron|window|unknown."""

    kind: str
    seconds: int | None = None
    entries: tuple[CalendarEntry, ...] = ()
    cron: str | None = None
    note: str = ""


def _humanize_seconds(seconds: int) -> str:
    if seconds < 60:
        return f"every {seconds}s"
    if seconds % 3600 == 0:
        hours = seconds // 3600
        return "hourly" if hours == 1 else f"every {hours}h"
    if seconds % 60 == 0:
        minutes = seconds // 60
        return "every minute" if minutes == 1 else f"every {minutes} minutes"
    return f"every {seconds}s"


def _fmt_entry(entry: CalendarEntry) -> str:
    hour = "*" if entry.hour is None else f"{entry.hour:02d}"
    minute = "00" if entry.minute is None else f"{entry.minute:02d}"
    return f"{hour}:{minute}"


def describe_schedule(schedule: Schedule) -> str:
    """Human-readable cadence, e.g. 'twice daily 03:30 + 15:30 local'."""
    if schedule.kind == "interval" and schedule.seconds:
        return _humanize_seconds(schedule.seconds)
    if schedule.kind == "calendar" and schedule.entries:
        weekday = next((e.weekday for e in schedule.entries if e.weekday is not None), None)
        times = " + ".join(_fmt_entry(e) for e in schedule.entries)
        if weekday is not None:
            return f"{_WEEKDAYS[weekday % 7]} {times} local"
        if len(schedule.entries) == 1:
            return f"daily {times} local"
        if len(schedule.entries) == 2:
            return f"twice daily {times} local"
        return f"{len(schedule.entries)}x daily {times} local"
    if schedule.kind == "cron" and schedule.cron:
        suffix = f" — {schedule.note}" if schedule.note else ""
        return f"cron {schedule.cron}{suffix}"
    if schedule.kind == "window":
        return schedule.note or "observation window"
    return schedule.note or "—"


# ---------------------------------------------------------------------------
# Static catalog — every entry grounded in a repo file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    name: str
    executor: str  # launchd | remote_cron | remote_docker | csi_pipeline
    category: str
    purpose: str
    source: str  # repo file that defines the job (grounding)
    label: str | None = None
    template: str | None = None  # repo-relative plist template to parse
    module: str | None = None
    schedule: Schedule = field(default_factory=lambda: Schedule(kind="unknown"))
    env_overrides: tuple[str, ...] = ()  # env var NAMES only, never values
    log_paths: tuple[str, ...] = ()  # repo-relative (var/...) or absolute (/tmp/...)
    managed_candidate: bool = False
    candidate_reason: str = ""
    health_note: str = ""
    # Remote-probe wiring (only set on executor in {remote_docker, remote_cron}):
    # a docker container name to look up in the probe's `docker ps` output, or
    # a substring to search for in the probe's captured remote crontab lines.
    remote_container: str | None = None
    remote_cron_fragment: str | None = None
    # A remote cron log path (must be one of remote_probe's grounded
    # _REMOTE_CRON_LOG_CANDIDATES) whose mtime is real fresh-vs-cadence
    # evidence for THIS job — only set when the path is actually documented
    # (HANDOFF); otherwise the job stays honestly `unknown`.
    remote_cron_log_path: str | None = None


def _cal(*entries: tuple[int, int] | tuple[int, int, int]) -> Schedule:
    return Schedule(
        kind="calendar",
        entries=tuple(
            CalendarEntry(hour=e[0], minute=e[1], weekday=e[2] if len(e) > 2 else None)
            for e in entries
        ),
    )


def _interval(seconds: int) -> Schedule:
    return Schedule(kind="interval", seconds=seconds)


CATALOG: tuple[CatalogEntry, ...] = (
    # -- routines engine ------------------------------------------------------
    CatalogEntry(
        key="routines-tick",
        name="Routines engine tick",
        executor="launchd",
        category="Routines engine",
        label="com.omniagentos.routines",
        template="scripts/scheduler/com.omniagentos.routines.plist.template",
        module="omniagentos.scheduler.routines_tick",
        schedule=_interval(300),
        purpose=(
            "Fires every due DB routine (creates its task + run), settles finished "
            "runs against their objective gates, auto-pauses under the 50% acceptance floor."
        ),
        source="scripts/scheduler/install-routines.sh",
        log_paths=("var/log/routines.log",),
        managed_candidate=False,
        candidate_reason="This IS the routines engine — it executes the managed routines.",
    ),
    # -- morning / docs -------------------------------------------------------
    CatalogEntry(
        key="morning-report",
        name="Morning ledger rollup",
        executor="launchd",
        category="Reporting",
        label="com.omniagentos.morning",
        template="scripts/scheduler/com.omniagentos.morning.plist.template",
        module="omniagentos.scheduler.morning_report",
        schedule=_cal((8, 0)),
        purpose="Daily rollup of run-ledger manifests (states, arms, cost) published to the vault.",
        source="scripts/scheduler/install.sh",
        env_overrides=("OMNIAGENTOS_MORNING_HOUR", "OMNIAGENTOS_MORNING_MINUTE"),
        log_paths=("/tmp/com.omniagentos.morning.out.log",),
        managed_candidate=True,
        candidate_reason="Deterministic CLI; an exit_code gate + budget cap would make it a managed loop.",
    ),
    CatalogEntry(
        key="archi-morning",
        name="Morning repo-map refresh",
        executor="launchd",
        category="Reporting",
        label="com.omniagentos.archi-morning",
        template="scripts/archi-morning/com.omniagentos.archi-morning.plist.template",
        module="scripts/archi-morning/archi-morning.sh",
        schedule=_cal((5, 30)),
        purpose=(
            "Read-only repo scan + owned-doc writes (ARCHI.md, docs/architecture/system-map.*) "
            "with a diff-guarded local commit, so morning reads see a fresh map."
        ),
        source="scripts/archi-morning/install-archi-morning.sh",
        env_overrides=("OMNIAGENTOS_ARCHI_MORNING_HOUR", "OMNIAGENTOS_ARCHI_MORNING_MINUTE"),
        log_paths=("var/log/archi-morning.log",),
        managed_candidate=True,
        candidate_reason="Script with a clear exit code; could declare an exit_code gate.",
    ),
    # -- model intelligence ---------------------------------------------------
    CatalogEntry(
        key="modelintel-daily",
        name="Model-intelligence refresh",
        executor="launchd",
        category="Model intelligence",
        label="com.omniagentos.modelintel",
        template="scripts/scheduler/com.omniagentos.modelintel.plist.template",
        module="omniagentos.modelintel.daily",
        schedule=_cal((7, 15)),
        purpose=(
            "Fetches live benchmarks, runs one Grok research sweep, rebuilds the model "
            "registry, and refreshes fusion rankings + vault notes (configs/modelintel.yaml)."
        ),
        source="scripts/scheduler/install-modelintel.sh",
        env_overrides=("OMNIAGENTOS_MODELINTEL_HOUR", "OMNIAGENTOS_MODELINTEL_MINUTE"),
        log_paths=("var/log/modelintel.log",),
        managed_candidate=True,
        candidate_reason="Exit-code gate; per-stage failures already degrade to last-known-good.",
    ),
    # -- finance --------------------------------------------------------------
    CatalogEntry(
        key="banking-daily",
        name="Banking snapshot (authoritative)",
        executor="launchd",
        category="Finance",
        label="com.omniagentos.banking",
        template="scripts/scheduler/com.omniagentos.banking.plist.template",
        module="omniagentos.banking.collect",
        schedule=_cal((2, 0)),
        purpose="2 AM ET authoritative snapshot of the just-closed ET day's balances/deposits/expenses (read-only Slash calls).",
        source="scripts/scheduler/install-banking.sh",
        env_overrides=("OMNIAGENTOS_BANKING_HOUR", "OMNIAGENTOS_BANKING_MINUTE"),
        log_paths=("/tmp/com.omniagentos.banking.out.log",),
        managed_candidate=True,
        candidate_reason="Read-only collector with an exit code; gate on exit_code, cap iterations.",
    ),
    CatalogEntry(
        key="banking-hourly",
        name="Banking refresh (hourly)",
        executor="launchd",
        category="Finance",
        label="com.omniagentos.banking.hourly",
        template="scripts/scheduler/com.omniagentos.steward.metrics.plist.template",
        module="omniagentos.banking.collect",
        schedule=_interval(3600),
        purpose="Keeps the current day fresh as late transactions post (store UPSERTs — safe).",
        source="scripts/scheduler/install-banking.sh",
        log_paths=("/tmp/com.omniagentos.banking.hourly.out.log",),
        managed_candidate=True,
        candidate_reason="Same collector as the daily snapshot; exit_code gate.",
    ),
    CatalogEntry(
        key="revenue-daily",
        name="Revenue snapshot (authoritative)",
        executor="launchd",
        category="Finance",
        label="com.omniagentos.revenue",
        template="scripts/scheduler/com.omniagentos.revenue.plist.template",
        module="omniagentos.revenue.collect",
        schedule=_cal((2, 0)),
        purpose="2 AM ET authoritative P&L snapshot of the just-closed ET day (read-only external calls).",
        source="scripts/scheduler/install-revenue.sh",
        env_overrides=("OMNIAGENTOS_REVENUE_HOUR", "OMNIAGENTOS_REVENUE_MINUTE"),
        log_paths=("/tmp/com.omniagentos.revenue.out.log",),
        managed_candidate=True,
        candidate_reason="Read-only collector with an exit code; exit_code gate.",
    ),
    CatalogEntry(
        key="revenue-hourly",
        name="Revenue refresh (hourly)",
        executor="launchd",
        category="Finance",
        label="com.omniagentos.revenue.hourly",
        template="scripts/scheduler/com.omniagentos.steward.metrics.plist.template",
        module="omniagentos.revenue.collect",
        schedule=_interval(3600),
        purpose="Keeps yesterday fresh as late refunds/settlements land (store UPSERTs — safe).",
        source="scripts/scheduler/install-revenue.sh",
        log_paths=("/tmp/com.omniagentos.revenue.hourly.out.log",),
        managed_candidate=True,
        candidate_reason="Same collector as the daily snapshot; exit_code gate.",
    ),
    CatalogEntry(
        key="piedpiper-pipeline-daily",
        name="AcmeUni PiedPiper pipeline snapshot (dark until granted)",
        executor="launchd",
        category="Finance",
        label="com.omniagentos.piedpiper-pipeline",
        template="scripts/scheduler/com.omniagentos.piedpiper-pipeline.plist.template",
        module="omniagentos.piedpiper.pipeline_report",
        schedule=_cal((3, 0)),
        purpose=(
            "Daily read-only rollup of AcmeUni's PiedPiper sales pipeline (open/total "
            "opportunities, contacts, conversations). Dark-safe: the collector preflights "
            "its piedpiper_acmeuni.read grant and writes nothing until an operator issues one."
        ),
        source="scripts/scheduler/install-piedpiper-pipeline.sh",
        log_paths=("/tmp/com.omniagentos.piedpiper-pipeline.out.log",),
        managed_candidate=True,
        candidate_reason="Read-only collector with an exit code; exit_code gate.",
    ),
    CatalogEntry(
        key="bank-balances",
        name="Bank-balance estimate",
        executor="launchd",
        category="Finance",
        label="com.omniagentos.bank-balances",
        module="scripts/banking/estimate_balances.py",
        schedule=_cal((8, 15)),
        purpose=(
            "Reads QBO book balances from the Google Sheet, applies configs/bank_anchors.json, "
            "writes var/bank_balances_latest.txt for the agent fleet."
        ),
        source="scripts/scheduler/install-bank-balances.sh",
        env_overrides=("OMNIAGENTOS_BALANCES_HOUR", "OMNIAGENTOS_BALANCES_MINUTE"),
        log_paths=("/tmp/com.omniagentos.bank-balances.out.log",),
        managed_candidate=True,
        candidate_reason="Deterministic script; exit_code gate + max_iterations cap.",
    ),
    # -- steward --------------------------------------------------------------
    CatalogEntry(
        key="steward-briefing",
        name="Steward briefing",
        executor="launchd",
        category="Steward",
        label="com.omniagentos.steward.briefing",
        template="scripts/scheduler/com.omniagentos.steward.briefing.plist.template",
        module="omniagentos.briefing.run",
        schedule=_cal((7, 30)),
        purpose="Daily digest (email/Slack/voice per configs/steward.yaml briefing.deliver_*).",
        source="scripts/scheduler/install-steward.sh",
        log_paths=("var/log/steward-briefing.log",),
        managed_candidate=True,
        candidate_reason="Schedule comes from configs/steward.yaml; exit_code gate.",
    ),
    CatalogEntry(
        key="steward-metrics",
        name="Steward goal metrics",
        executor="launchd",
        category="Steward",
        label="com.omniagentos.steward.metrics",
        template="scripts/scheduler/com.omniagentos.steward.metrics.plist.template",
        module="omniagentos.goals.collect",
        schedule=_interval(3600),
        purpose="Hourly goal-metric collection feeding the briefing and alerts.",
        source="scripts/scheduler/install-steward.sh",
        log_paths=("var/log/steward-metrics.log",),
        managed_candidate=True,
        candidate_reason="Deterministic collector; exit_code gate.",
    ),
    CatalogEntry(
        key="steward-alerts",
        name="Steward alerts monitor",
        executor="launchd",
        category="Steward",
        label="com.omniagentos.steward.alerts",
        template="scripts/scheduler/com.omniagentos.steward.alerts.plist.template",
        module="omniagentos.steward.alerts.monitor",
        schedule=_interval(900),
        purpose=(
            "Deterministic failure-rule scan (ROAS floor, spend spike, payment failures, "
            "revenue crash) with cooldown-aware escalation; also the reliability dead-man's switch."
        ),
        source="scripts/scheduler/install-steward.sh",
        log_paths=("var/log/steward-alerts.log",),
        managed_candidate=True,
        candidate_reason="Alert thresholds already declarative in configs/steward.yaml.",
    ),
    CatalogEntry(
        key="steward-comms",
        name="Steward comms extraction",
        executor="launchd",
        category="Steward",
        label="com.omniagentos.steward.comms",
        template="scripts/scheduler/com.omniagentos.steward.comms.plist.template",
        module="omniagentos.comms.extract_batch",
        schedule=_cal((2, 30)),
        purpose="Nightly inbound-comms batch extraction into the knowledge bridge.",
        source="scripts/scheduler/install-steward.sh",
        log_paths=("var/log/steward-comms.log",),
        managed_candidate=True,
        candidate_reason="Batch CLI with an exit code; exit_code gate.",
    ),
    CatalogEntry(
        key="comms-harper",
        name="Harper IMAP poll",
        executor="launchd",
        category="Comms",
        label="com.omniagentos.comms-harper",
        module="omniagentos.comms.poll",
        schedule=_interval(60),
        purpose="Polls the Harper IMAP source every 60s into the comms pipeline.",
        source="scripts/scheduler/install-comms.sh",
        log_paths=("/tmp/com.omniagentos.comms-harper.out.log",),
        managed_candidate=False,
        candidate_reason=(
            "60s cadence is finer than the routines engine's 5-minute tick — converting it "
            "would 5x its latency; keep on launchd."
        ),
    ),
    CatalogEntry(
        key="comms-slack-socket",
        name="Slack Socket Mode ingestion",
        executor="launchd",
        category="Comms",
        label="com.omniagentos.comms-slack-socket",
        module="omniagentos.comms.sockets.slack",
        schedule=Schedule(kind="window", note="KeepAlive — one long-lived WebSocket"),
        purpose=(
            "Sub-second push ingestion of public-channel Slack messages. The LATENCY half "
            "of a hybrid: Socket Mode never replays events missed while disconnected, so it "
            "is only safe alongside comms-slack-sweep, which closes any gap."
        ),
        source="scripts/scheduler/install-comms-slack.sh",
        log_paths=("var/log/comms-slack-socket.log",),
        managed_candidate=False,
        candidate_reason=(
            "A persistent WebSocket, not a batch CLI — it has no exit code to gate on. "
            "Liveness is the slack-socket comms_sources heartbeat, read by health-sentinel."
        ),
    ),
    CatalogEntry(
        key="comms-slack-sweep",
        name="Slack reconciliation sweep",
        executor="launchd",
        category="Comms",
        label="com.omniagentos.comms-slack-sweep",
        module="omniagentos.comms.poll",
        schedule=_interval(300),
        purpose=(
            "Re-reads conversations.history from the stored cursor every 5 minutes. The "
            "DETERMINISM half of the hybrid: it bounds undetected socket loss to one "
            "interval, and because dedupe is a DB constraint, `created > 0` here is proof "
            "the socket missed something."
        ),
        source="scripts/scheduler/install-comms-slack.sh",
        log_paths=("var/log/comms-slack-sweep.log",),
        managed_candidate=False,
        candidate_reason=(
            "Its cadence must stay strictly finer than health-sentinel's 1800s so the "
            "sentinel always has fresh reconciliation evidence; the routines engine's "
            "5-minute tick would make the two indistinguishable."
        ),
    ),
    # -- self-improvement -----------------------------------------------------
    CatalogEntry(
        key="selfimprove-curator",
        name="Self-improve skill curator",
        executor="launchd",
        category="Self-improvement",
        label="com.omniagentos.selfimprove-curator",
        template="scripts/selfimprove/com.omniagentos.selfimprove-curator.plist.template",
        module="omniagentos.selfimprove.curator",
        schedule=_cal((3, 30), (15, 30)),
        purpose=(
            "Scans the run ledger for COMPLETED runs and captures a reusable vault skill "
            "note for each (idempotent; every manifest passes a real VerificationGate)."
        ),
        source="scripts/selfimprove/launchd.py + docs/architecture/scheduling.md",
        log_paths=("/tmp/com.omniagentos.selfimprove-curator.out.log",),
        managed_candidate=True,
        candidate_reason="Idempotent read-ledger/write-vault pass; exit_code gate.",
    ),
    CatalogEntry(
        key="swarm-optimizer",
        name="Swarm optimizer",
        executor="launchd",
        category="Self-improvement",
        label="com.omniagentos.swarm-optimizer",
        template="scripts/swarm/com.omniagentos.swarm-optimizer.plist.template",
        module="omniagentos.swarm.optimize",
        schedule=_cal((3, 45), (15, 45)),
        purpose=(
            "Read-only swarm analysis + playbook.md/learned.json writes and at most one "
            "bounded Fable call; staggered 15m off the selfimprove-curator slots."
        ),
        source="scripts/swarm/install-swarm-optimizer.sh",
        env_overrides=(
            "OMNIAGENTOS_SWARM_OPTIMIZER_HOUR_1",
            "OMNIAGENTOS_SWARM_OPTIMIZER_MINUTE_1",
            "OMNIAGENTOS_SWARM_OPTIMIZER_HOUR_2",
            "OMNIAGENTOS_SWARM_OPTIMIZER_MINUTE_2",
        ),
        log_paths=("var/log/swarm-optimizer.log",),
        managed_candidate=True,
        candidate_reason="Bounded model call + deterministic analysis; exit_code gate + budget cap.",
    ),
    CatalogEntry(
        key="fable-curator",
        name="Fable curator (self-edit)",
        executor="launchd",
        category="Self-improvement",
        label="com.omniagentos.fable-curator",
        template="scripts/fable-curator/com.omniagentos.fable-curator.plist.template",
        module="scripts/fable-curator/fable-curator.sh",
        schedule=_cal((23, 0)),
        purpose=(
            "Nightly Fable-driven curation/self-edit pass; drives omniagentos/improvement_chain "
            "(the configs/loop_models.yaml Kimi-draft → Opus-edit → Fable-review chain). "
            "Hourly github-backup is its rollback guarantee."
        ),
        source="scripts/fable-curator/install.sh",
        env_overrides=("FABLE_CURATOR_HOUR", "FABLE_CURATOR_MINUTE"),
        log_paths=("var/log/fable-curator.log",),
        managed_candidate=True,
        candidate_reason="Script with an exit code; a budget_usd cap would bound the model spend.",
    ),
    CatalogEntry(
        key="curator",
        name="Vault curator agent",
        executor="launchd",
        category="Self-improvement",
        label="com.omniagentos.curator",
        template="scripts/curator/com.omniagentos.curator.plist.template",
        module="scripts/curator/run.sh",
        schedule=_cal((7, 0), (19, 0)),
        purpose="Twice-daily curator agent pass (L06); launchd env keeps the L02 protected-store pointer out of its environment.",
        source="scripts/curator/run.sh",
        env_overrides=(
            "OMNIAGENTOS_CURATOR_HOUR_1",
            "OMNIAGENTOS_CURATOR_MINUTE_1",
            "OMNIAGENTOS_CURATOR_HOUR_2",
            "OMNIAGENTOS_CURATOR_MINUTE_2",
        ),
        log_paths=("/tmp/com.omniagentos.curator.out.log",),
        managed_candidate=True,
        candidate_reason="Script with an exit code; exit_code gate.",
    ),
    CatalogEntry(
        key="lab-curation",
        name="Lab curation loop (observe-only)",
        executor="launchd",
        category="Self-improvement",
        label="com.omniagentos.lab-curation",
        template="ops/launchd/com.omniagentos.lab-curation.plist.template",
        module="scripts/lab/curation_loop.py",
        schedule=_cal((3, 20)),
        purpose=(
            "Proposes a 20-slot explore/exploit experiment portfolio against a THROWAWAY copy "
            "of the lab DB and records a fingerprint-guarded artifact; never promotes or executes."
        ),
        source="ops/launchd/com.omniagentos.lab-curation.plist.template + scripts/lab/curation_loop.py",
        log_paths=("var/log/lab-curation.log",),
        managed_candidate=True,
        candidate_reason="Already fingerprint-gated; an exit_code gate would surface failures in acceptance metrics.",
    ),
    # -- reliability ----------------------------------------------------------
    CatalogEntry(
        key="reliability-watch",
        name="Reliability watch",
        executor="launchd",
        category="Reliability",
        label="com.omniagentos.reliability-watch",
        template="scripts/reliability/com.omniagentos.reliability-watch.plist.template",
        module="omniagentos.reliability",
        schedule=_interval(600),
        purpose="Detect → dedup → safe recovery → critical alerts → monitoring-window checks/auto-rollback.",
        source="scripts/reliability/install.sh",
        log_paths=("var/log/reliability-watch.out.log", "var/log/reliability-watch.err.log"),
        managed_candidate=True,
        candidate_reason="CLI with --once mode; exit_code gate.",
    ),
    CatalogEntry(
        key="reliability-audit",
        name="Reliability audit",
        executor="launchd",
        category="Reliability",
        label="com.omniagentos.reliability-audit",
        template="scripts/reliability/com.omniagentos.reliability-audit.plist.template",
        module="omniagentos.reliability",
        schedule=_cal((6, 30), (18, 30)),
        purpose="Full sweep + department reviews + CTO pass; proposals → sandbox → judges → queue/apply + vault report.",
        source="scripts/reliability/install.sh",
        log_paths=("var/log/reliability-audit.out.log", "var/log/reliability-audit.err.log"),
        managed_candidate=True,
        candidate_reason="CLI with --once mode; exit_code gate.",
    ),
    CatalogEntry(
        key="reliability-daily",
        name="Reliability daily summary",
        executor="launchd",
        category="Reliability",
        label="com.omniagentos.reliability-daily",
        template="scripts/reliability/com.omniagentos.reliability-daily.plist.template",
        module="omniagentos.reliability",
        schedule=_cal((8, 5)),
        purpose="Consolidated daily improvement summary.",
        source="scripts/reliability/install.sh",
        log_paths=("var/log/reliability-daily.out.log", "var/log/reliability-daily.err.log"),
        managed_candidate=True,
        candidate_reason="CLI with --once mode; exit_code gate.",
    ),
    CatalogEntry(
        key="reliability-weekly",
        name="Reliability weekly review",
        executor="launchd",
        category="Reliability",
        label="com.omniagentos.reliability-weekly",
        template="scripts/reliability/com.omniagentos.reliability-weekly.plist.template",
        module="omniagentos.reliability",
        schedule=_cal((9, 0, 0)),  # Sunday 09:00 (launchd Weekday 0 == Sunday)
        purpose="CTO deep architecture review + scorecard trends + doc staleness.",
        source="scripts/reliability/install.sh",
        log_paths=("var/log/reliability-weekly.out.log", "var/log/reliability-weekly.err.log"),
        managed_candidate=True,
        candidate_reason="CLI with --once mode; exit_code gate.",
    ),
    # -- maintenance ----------------------------------------------------------
    CatalogEntry(
        key="cache-gc",
        name="Cache / derived-data GC",
        executor="launchd",
        category="Maintenance",
        label="com.omniagentos.cache-gc",
        template="scripts/scheduler/com.omniagentos.cache-gc.plist.template",
        module="omniagentos.maintenance.cache_gc",
        schedule=_cal((3, 0)),
        purpose=(
            "Tiered retention trim of DB-cached dashboards rows (7d SSE/approvals, 30d runs, "
            "365d financial facts); never touches audit-critical rows."
        ),
        source="scripts/scheduler/install-cache-gc.sh",
        env_overrides=("OMNIAGENTOS_CACHE_GC_HOUR", "OMNIAGENTOS_CACHE_GC_MINUTE"),
        log_paths=("/tmp/com.omniagentos.cache-gc.out.log",),
        managed_candidate=True,
        candidate_reason="--dry-run capable deterministic CLI; exit_code gate.",
    ),
    CatalogEntry(
        key="hygiene",
        name="Estate hygiene sweep",
        executor="launchd",
        category="Maintenance",
        label="com.omniagentos.hygiene",
        template="scripts/hygiene/com.omniagentos.hygiene.plist.template",
        module="scripts/hygiene/hygiene.sh",
        schedule=_cal((4, 15)),
        purpose="Nightly estate hygiene sweep, settled before the morning docs refresh reads the estate.",
        source="scripts/hygiene/install-hygiene.sh",
        env_overrides=("OMNIAGENTOS_HYGIENE_HOUR", "OMNIAGENTOS_HYGIENE_MINUTE"),
        log_paths=("var/log/hygiene.log",),
        managed_candidate=True,
        candidate_reason="Script with an exit code; exit_code gate.",
    ),
    CatalogEntry(
        key="golden-suite",
        name="Golden-suite sentinel",
        executor="launchd",
        category="Maintenance",
        label="com.omniagentos.golden-suite",
        template="scripts/golden-suite/com.omniagentos.golden-suite.plist.template",
        module="scripts/golden-suite/golden-suite.sh",
        schedule=_cal((1, 0)),
        purpose="Nightly golden-suite regression sentinel (run_golden.py).",
        source="scripts/golden-suite/install-golden-suite.sh",
        env_overrides=("OMNIAGENTOS_GOLDEN_SUITE_HOUR", "OMNIAGENTOS_GOLDEN_SUITE_MINUTE"),
        log_paths=("var/log/golden-suite.log",),
        managed_candidate=True,
        candidate_reason="A test suite IS an objective gate — the natural managed routine (test_command gate).",
    ),
    # -- reflection -----------------------------------------------------------
    CatalogEntry(
        key="reflection-nightly",
        name="Nightly reflection",
        executor="launchd",
        category="Reflection",
        label="com.omniagentos.reflection-nightly",
        template="scripts/reflection/com.omniagentos.reflection-nightly.plist.template",
        module="scripts/reflection/reflect-nightly.sh",
        schedule=_cal((2, 30)),
        purpose="Nightly reflection pass (observe-only; re-arm gated by OMNIAGENTOS_REFLECTION_REARM_MODE).",
        source="scripts/reflection/install-reflection.sh",
        env_overrides=("OMNIAGENTOS_REFLECTION_NIGHTLY_HOUR", "OMNIAGENTOS_REFLECTION_NIGHTLY_MINUTE"),
        log_paths=("var/log/reflection-nightly.log",),
        managed_candidate=True,
        candidate_reason="Script invoked via /bin/sh (exit-126-safe); exit_code gate.",
    ),
    CatalogEntry(
        key="reflection-watchdog",
        name="Reflection watchdog",
        executor="launchd",
        category="Reflection",
        label="com.omniagentos.reflection-watchdog",
        template="scripts/reflection/com.omniagentos.reflection-watchdog.plist.template",
        module="scripts/reflection/reflect-watchdog.sh",
        schedule=_cal((7, 30)),
        purpose="Morning watchdog verifying the nightly reflection actually landed (exit-126 guard).",
        source="scripts/reflection/install-reflection.sh",
        env_overrides=("OMNIAGENTOS_REFLECTION_WATCHDOG_HOUR", "OMNIAGENTOS_REFLECTION_WATCHDOG_MINUTE"),
        log_paths=("var/log/reflection-watchdog.log",),
        managed_candidate=True,
        candidate_reason="Script invoked via /bin/sh (exit-126-safe); exit_code gate.",
    ),
    # -- gates / canaries -----------------------------------------------------
    CatalogEntry(
        key="agent-watchdog",
        name="Agent watchdog",
        executor="launchd",
        category="Gates",
        label="com.omniagentos.agent-watchdog",
        template="scripts/gates/com.omniagentos.agent-watchdog.plist.template",
        module="scripts/gates/agent_watchdog.sh",
        schedule=_interval(300),
        purpose="5-minute watchdog over agent liveness (scripts/gates/agent_watchdog.sh).",
        source="scripts/gates/install-agent-watchdog.sh",
        env_overrides=("OMNIAGENTOS_AGENT_WATCHDOG_INTERVAL",),
        log_paths=("var/log/agent-watchdog.log",),
        managed_candidate=False,
        candidate_reason="Watchdog of the agent fleet itself — keep independent of the engine it watches.",
    ),
    CatalogEntry(
        key="planner-canary",
        name="Planner canary",
        executor="launchd",
        category="Gates",
        label="com.omniagentos.planner-canary",
        template="scripts/gates/com.omniagentos.planner-canary.plist.template",
        module="omniagentos.gates.planner_canary",
        schedule=_interval(3600),
        purpose="Hourly canary proving the planner path still works end to end.",
        source="scripts/gates/install-planner-canary.sh",
        env_overrides=("OMNIAGENTOS_PLANNER_CANARY_INTERVAL",),
        log_paths=("var/log/planner-canary.log",),
        managed_candidate=True,
        candidate_reason="A canary is a pass/fail probe — natural exit_code gate.",
    ),
    # -- backlog --------------------------------------------------------------
    CatalogEntry(
        key="backlog-executor",
        name="Backlog executor",
        executor="launchd",
        category="Backlog",
        label="com.omniagentos.backlog-executor",
        template="scripts/backlog-executor/com.omniagentos.backlog-executor.plist.template",
        module="scripts/backlog-executor/executor.py",
        schedule=_cal((0, 30)),
        purpose=(
            "Nightly backlog candidate collection + grok selection + digest; ships DRY-RUN "
            "(OMNIAGENTOS_BACKLOG_DRY_RUN=1) until an operator arms it."
        ),
        source="scripts/backlog-executor/install.sh",
        env_overrides=("OMNIAGENTOS_BACKLOG_HOUR", "OMNIAGENTOS_BACKLOG_MINUTE", "OMNIAGENTOS_BACKLOG_DRY_RUN"),
        log_paths=("var/log/backlog-executor.log",),
        managed_candidate=True,
        candidate_reason="Selection+dispatch loop with a dry-run mode; exit_code gate + human_checkpoint cap.",
    ),
    # -- provider truth -------------------------------------------------------
    CatalogEntry(
        key="provider-sentinel",
        name="Provider sentinel",
        executor="launchd",
        category="Model intelligence",
        label="com.omniagentos.provider-sentinel",
        template="scripts/provider-sentinel/com.omniagentos.provider-sentinel.plist.template",
        module="scripts/provider-sentinel/provider-sentinel.sh",
        schedule=_cal((22, 30)),
        purpose="Refreshes provider truth 30 minutes before the fable-curator night so overnight jobs start fresh.",
        source="scripts/provider-sentinel/install.sh",
        env_overrides=("PROVIDER_SENTINEL_HOUR", "PROVIDER_SENTINEL_MINUTE"),
        log_paths=("var/log/provider-sentinel.log",),
        managed_candidate=True,
        candidate_reason="Script with an exit code; exit_code gate.",
    ),
    # -- remote (grounded in HANDOFF/LOOPS-VISIBILITY.md, no local observability)
    CatalogEntry(
        key="kb-drift-check",
        name="KB drift check",
        executor="remote_cron",
        category="Remote (initech-crm)",
        schedule=Schedule(kind="cron", cron="0 8,20 * * *", note="UTC"),
        purpose="Twice-daily knowledge-card cited-file drift check (kb-maintain.sh → claude -p only when drift found).",
        source="HANDOFF/LOOPS-VISIBILITY.md",
        health_note="Remote cron; last verified healthy 2026-07-25 (clean, 25 cards) — no live observability wired (HANDOFF L3).",
        remote_cron_fragment="kb-maintain",
        remote_cron_log_path="/srv/initech-crm/logs/kb-drift-cron.log",
    ),
    CatalogEntry(
        key="globex-brain-daily-rollout-check",
        name="Globex brain rollout check",
        executor="remote_cron",
        category="Remote (initech-crm)",
        schedule=Schedule(kind="cron", cron="10 6 * * *", note="UTC"),
        purpose="Daily Globex brain rollout verification.",
        source="HANDOFF/LOOPS-VISIBILITY.md",
        health_note="Remote cron; no observability wired (HANDOFF L3).",
        remote_cron_fragment="rollout",
    ),
    CatalogEntry(
        key="globex-brain-resolution-pipeline-check",
        name="Globex brain resolution pipeline check",
        executor="remote_cron",
        category="Remote (initech-crm)",
        schedule=Schedule(kind="cron", cron="25 6 * * *", note="UTC"),
        purpose="Daily Globex brain resolution-pipeline verification.",
        source="HANDOFF/LOOPS-VISIBILITY.md",
        health_note="Remote cron; no observability wired (HANDOFF L3).",
        remote_cron_fragment="resolution",
    ),
    CatalogEntry(
        key="public-stripe-stats-refresh",
        name="Public Stripe stats refresh",
        executor="remote_cron",
        category="Remote (initech-crm)",
        schedule=Schedule(kind="cron", cron="7 */6 * * *", note="UTC"),
        purpose="Refreshes public Stripe stats every 6 hours.",
        source="HANDOFF/LOOPS-VISIBILITY.md",
        health_note="Remote cron; no observability wired (HANDOFF L3).",
        remote_cron_fragment="stripe",
    ),
    CatalogEntry(
        key="livesession-funnel-monitor",
        name="LiveSession funnel monitor",
        executor="remote_cron",
        category="Remote (initech-crm)",
        schedule=Schedule(kind="cron", cron="*/5 * * * *", note="UTC"),
        purpose="Funnel monitor polling every 5 minutes.",
        source="HANDOFF/LOOPS-VISIBILITY.md",
        health_note="Remote cron; no observability wired (HANDOFF L3).",
        remote_cron_fragment="livesession",
    ),
    CatalogEntry(
        key="chargeblast-auto-refund",
        name="Chargeblast auto-refund",
        executor="remote_docker",
        category="Remote (initech-crm)",
        schedule=_interval(43200),
        purpose=(
            "Twice-daily dispute auto-refund loop (≤$500/refund, exact/high confidence, usd, "
            "45-day lookback) + mark-handled and Stripe blocklist sweeps."
        ),
        source="HANDOFF/LOOPS-VISIBILITY.md (docker service initech-crm-chargeblast-auto-refund-1)",
        health_note=(
            "Verified healthy 2026-07-25 (docker logs hold the full audit trail); "
            "no live observability wired — a money-moving loop this size should report into OmniAgentOS (HANDOFF §L1)."
        ),
        remote_container="initech-crm-chargeblast-auto-refund-1",
    ),
    CatalogEntry(
        key="chargeblast-reconcile",
        name="Chargeblast reconcile",
        executor="remote_docker",
        category="Remote (initech-crm)",
        schedule=Schedule(kind="unknown", note="sibling docker service; cadence undocumented"),
        purpose="Sibling reconcile service to the auto-refund loop.",
        source="HANDOFF/LOOPS-VISIBILITY.md (docker service initech-crm-chargeblast-reconcile-1)",
        health_note="No observability wired (HANDOFF L3).",
        remote_container="initech-crm-chargeblast-reconcile-1",
    ),
)


# ---------------------------------------------------------------------------
# Discovered-job enrichment — purpose/category for launchd labels installed on
# this machine but with no CatalogEntry above. Every line here is grounded in
# recon-loops-inventory.md (2026-08-15 machine inventory of all 116 non-Apple
# launchd jobs): the recon table gives each job's real command, so a purpose
# derived from that command is evidence, not invention. Jobs the recon table
# only names (no dedicated explanatory note) are marked "(inferred from its
# command)" rather than asserted as fact. Unlisted labels keep the existing
# generic "Discovered (no repo definition)" fallback untouched.
# ---------------------------------------------------------------------------

DISCOVERED_ENRICHMENT: dict[str, tuple[str, str]] = {
    # -- OmniAgentOS product daemons (com.omniagentos.*) ------------------
    "com.omniagentos.api": (
        "Inferred from its command (`omniagentos.api` server process): the FastAPI backend "
        "serving the dashboard and this very endpoint.",
        "Serving / daemons",
    ),
    "com.omniagentos.balance-alerts": (
        "Inferred from its command (`omniagentos.team.balance_alerts`): polls agent-account "
        "balances and alerts when one runs low.",
        "Ops / maintenance",
    ),
    "com.omniagentos.blocked-session-detector": (
        "Inferred from its command (`blocked_session_detector`): scans for agent sessions "
        "stuck waiting on a blocked permission/approval and surfaces them.",
        "Ops / maintenance",
    ),
    "com.omniagentos.dashboard": (
        "Inferred from its command (`npm run start` under Ops/remote-access): the dashboard "
        "Node server kept alive for remote access.",
        "Serving / daemons",
    ),
    "com.omniagentos.dev-digest": (
        "Inferred from its command (`Ops/bin/dev-digest`): daily developer-activity digest.",
        "Reporting",
    ),
    "com.omniagentos.edc-gmail-poll": (
        "Inferred from its command (`edc-gmail-poll`): polls the EDC Gmail source every 5 minutes.",
        "Comms",
    ),
    "com.omniagentos.edc-triage": (
        "Inferred from its command (`edc-triage`): triages newly polled EDC inbound comms.",
        "Comms",
    ),
    "com.omniagentos.feature-health-filer": (
        "Inferred from its command (`scripts/feature_health/...`): files feature-health "
        "findings as tickets, three times daily.",
        "Ops / maintenance",
    ),
    "com.omniagentos.feature-health-nightly": (
        "Inferred from its command (`scripts/feature_health/run.sh tier2`): nightly tier-2 "
        "feature-health sweep.",
        "Ops / maintenance",
    ),
    "com.omniagentos.feature-health-tier1": (
        "Inferred from its command (`scripts/feature_health/run.sh tier1`): tier-1 "
        "feature-health sweep every 4 hours.",
        "Ops / maintenance",
    ),
    "com.omniagentos.gate-healthcheck-pinger": (
        "Inferred from its command (`Ops/bin/gate-healthcheck-pinger.py`): pings the gate "
        "healthcheck endpoint every 5 minutes.",
        "Gates",
    ),
    "com.omniagentos.gate-watch": (
        "Inferred from its command (`scripts/.../gate-watch`): watches gate state, every 2 minutes.",
        "Gates",
    ),
    "com.omniagentos.hang-recycler": (
        "Inferred from its command (`otel-span run --name hang.recycler.tick`): recycles hung "
        "launchd/estate jobs, every 2 minutes.",
        "Ops / maintenance",
    ),
    "com.omniagentos.health-sentinel": (
        "Inferred from its command (`health-sentinel`, 30-minute interval): estate-wide health "
        "sentinel referenced elsewhere in this catalog as the reliability dead-man's switch upstream.",
        "Ops / maintenance",
    ),
    "com.omniagentos.loop-cadence": (
        "Inferred from its command (`Ops/LoopCadence/cadence_watch.py`): watches loop firing "
        "cadence for drift, every 5 minutes.",
        "Ops / maintenance",
    ),
    "com.omniagentos.loop-watchdog": (
        "Inferred from its command (`pipeline/bridge/loop-watchdog.sh`): watchdog over the "
        "threeloops pipeline bridge, every 5 minutes.",
        "Ops / maintenance",
    ),
    "com.omniagentos.nscert-t1": (
        "Inferred from its command (`nscert-t1`): tier-1 Northstar certification run, daily.",
        "Ops / maintenance",
    ),
    "com.omniagentos.ops-dashboard": (
        "Inferred from its command (`Ops/local-ops-dashboard`): a local ops-dashboard web server.",
        "Serving / daemons",
    ),
    "com.omniagentos.otelcol": (
        "Inferred from its command (`otelcol-contrib`): the OpenTelemetry collector daemon.",
        "Ops / maintenance",
    ),
    "com.omniagentos.remote-proxy": (
        "Inferred from its command (`caddy run`): reverse proxy fronting the remote-access dashboard.",
        "Serving / daemons",
    ),
    "com.omniagentos.runner": (
        "Inferred from its command (`omniagentos runner`, persistent): the agent-fleet runner "
        "process kept alive by launchd.",
        "Serving / daemons",
    ),
    "com.omniagentos.team-decisions": (
        "Inferred from its command (`omniagentos.team.decisions`): collects team decision records.",
        "Comms",
    ),
    "com.omniagentos.team-dispatch": (
        "Inferred from its command (`omniagentos.team.dispatch --once`): dispatches queued team "
        "work items, every 5 minutes.",
        "Automation crew",
    ),
    "com.omniagentos.team-notify": (
        "Inferred from its command (`omniagentos.team.notify --morning`): sends the morning team notification.",
        "Comms",
    ),
    "com.omniagentos.team-overnight-runner": (
        "Inferred from its command (`omniagentos.team.overnight --launch`): launches the overnight team run.",
        "Automation crew",
    ),
    "com.omniagentos.team-pulse": (
        "Inferred from its command (`omniagentos.team.notify --pulse`): hourly daytime team pulse check-in.",
        "Comms",
    ),
    "com.omniagentos.team-pulse-overnight": (
        "Inferred from its command (`omniagentos.team.notify --pulse --overnight`): the overnight "
        "variant of the team pulse check-in.",
        "Comms",
    ),
    "com.omniagentos.team-report": (
        "Inferred from its command (`scripts/team-report-post.sh`): posts the daily team report.",
        "Reporting",
    ),
    "com.omniagentos.team-session-liveness": (
        "Inferred from its command (`omniagentos.team.session_liveness`): checks team agent "
        "session liveness, every 10 minutes.",
        "Ops / maintenance",
    ),
    "com.omniagentos.team-session-tracker": (
        "Inferred from its command (`omniagentos.team.session_tracker`): tracks team agent "
        "sessions through the working day.",
        "Ops / maintenance",
    ),
    "com.omniagentos.team-sweep": (
        "Inferred from its command (`omniagentos.team.sweep --once`): sweeps for stuck/overdue "
        "team work items, every 15 minutes.",
        "Automation crew",
    ),
    # -- OmniAgentOS Ops product (com.omniagentos.*) ---------------------------
    "com.omniagentos.agent-observability": (
        "Inferred from its command (`Ops/agent-observability/run-collector.sh`): collects agent "
        "observability metrics every 30 minutes.",
        "Ops / maintenance",
    ),
    "com.omniagentos.estate-check": (
        "Inferred from its command (`Ops/loops/bin/estate-check-notify`): daily estate-wide check + notify.",
        "Ops / maintenance",
    ),
    "com.omniagentos.loop-audit-collect": (
        "Inferred from its command (`Ops/loop-audits/collect_loop_audit.py`): collects daily loop audit evidence.",
        "Ops / maintenance",
    ),
    "com.omniagentos.prototype-autocommit": (
        "Inferred from its command (`Ops/prototype-git/autocommit.sh --sweep`): auto-commits "
        "dirty prototype repos, every 30 minutes.",
        "Automation crew",
    ),
    "com.omniagentos.wis-census": (
        "Inferred from its command (`Ops/wis/wis_census.py`): daily census of the WIS knowledge catalog.",
        "Ops / maintenance",
    ),
    "com.omniagentos.wis-knowledge-index": (
        "Inferred from its command (`wis knowledge index`): refreshes the WIS FTS5 knowledge "
        "index, every 15 minutes.",
        "Ops / maintenance",
    ),
    "com.omniagentos.wis-report": (
        "Inferred from its command (`wis report`): daily WIS knowledge-catalog report.",
        "Reporting",
    ),
    "com.omniagentos.wis-retention": (
        "Inferred from its command (`Ops/wis/wis_retention.py --max-db-mb`): trims the WIS DB to "
        "its retention cap, daily.",
        "Ops / maintenance",
    ),
    "com.omniagentos.wis-watcher": (
        "Inferred from its command (`Ops/wis/src/wis.py capture`): captures new content into WIS, "
        "every 5 minutes.",
        "Ops / maintenance",
    ),
    # -- Initech / workqueue -------------------------------------------------
    "com.initech.gemini-automation-crew": (
        "Inferred from its command (Gemini Automation Crew.app, persistent): the Gemini 3.6 "
        "Flash browser-automation crew daemon used for visual/computer-use goals.",
        "Automation crew",
    ),
    "com.omniagentos.wq-server": (
        "Inferred from its command: the shared workqueue (wq) TCP server other estate machines "
        "submit jobs to.",
        "Serving / daemons",
    ),
    # -- threeloops pipeline (explicitly documented in recon's "Repository
    # daemons" section, not just inferred from a command path) ----------------
    "com.threeloops.advice": (
        "The threeloops pipeline advice loop (pipeline/bridge), every 5 minutes.",
        "Automation crew",
    ),
    "com.threeloops.bridge": (
        "The threeloops pipeline bridge loop, every 15 minutes.",
        "Automation crew",
    ),
    "com.threeloops.claim-reaper": (
        "`pipeline/bridge/janitor.py --claims-only --apply` — reaps stale claims, every 5 minutes.",
        "Ops / maintenance",
    ),
    "com.threeloops.gate-loop": (
        "`pipeline/bridge/gate_loop.py` — the deterministic gate/lander, every 60 seconds.",
        "Gates",
    ),
    "com.threeloops.governor": (
        "`pipeline/bridge/governor.py` — the threeloops pipeline governor, every 5 minutes.",
        "Ops / maintenance",
    ),
    "com.threeloops.janitor": (
        "`pipeline/bridge/janitor.py --apply` — daily threeloops estate janitor sweep.",
        "Ops / maintenance",
    ),
    "com.threeloops.publish-queue": (
        "The threeloops publish queue drain loop, every 5 minutes.",
        "Automation crew",
    ),
    "com.threeloops.verdict-conveyor": (
        "`pipeline/bridge/verdict_conveyor.py` — the threeloops verdict conveyor, every 10 minutes.",
        "Ops / maintenance",
    ),
    "com.threeloops.integrity-absence": (
        "Inferred from its label: a threeloops integrity gate checking for favourable absence "
        "(plist on disk failed to parse per recon — 'Invalid file').",
        "Gates",
    ),
    "com.threeloops.integrity-invariants": (
        "Inferred from its label: a threeloops integrity gate checking pipeline invariants "
        "(plist on disk failed to parse per recon — 'Invalid file').",
        "Gates",
    ),
    "com.threeloops.integrity-liveness": (
        "Inferred from its label: a threeloops integrity gate checking pipeline component "
        "liveness (plist on disk failed to parse per recon — 'Invalid file').",
        "Gates",
    ),
    "com.threeloops.integrity-reachability": (
        "Inferred from its label: a threeloops integrity gate checking symbol reachability "
        "(plist on disk failed to parse per recon — 'Invalid file').",
        "Gates",
    ),
}

# Label PREFIXES sharing one purpose/category (the wq-tunnel and otel-tunnel
# families — 4 and 3 near-identical per-target variants respectively).
DISCOVERED_ENRICHMENT_PREFIXES: tuple[tuple[str, str, str], ...] = (
    (
        "com.omniagentos.wq-tunnel.",
        "Inferred from its command (`ssh -N ... -L ...`, persistent): an SSH reverse tunnel "
        "carrying workqueue traffic to/from another estate machine.",
        "Serving / daemons",
    ),
    (
        "com.omniagentos.otel-tunnel.",
        "Inferred from its command (`ssh -N ... -L ...`, persistent): an SSH reverse tunnel "
        "carrying OpenTelemetry data to/from another estate machine.",
        "Ops / maintenance",
    ),
)


def _enrich_discovered(label: str) -> tuple[str, str] | None:
    """(purpose, category) override for a discovered label, or None to keep
    the generic fallback. Grounded in DISCOVERED_ENRICHMENT above."""
    hit = DISCOVERED_ENRICHMENT.get(label)
    if hit is not None:
        return hit
    for prefix, purpose, category in DISCOVERED_ENRICHMENT_PREFIXES:
        if label.startswith(prefix):
            return purpose, category
    return None


# ---------------------------------------------------------------------------
# Plist parsing — rendered plists via plistlib, {{templates}} via ElementTree
# ---------------------------------------------------------------------------


def _et_walk_dict(element: ET.Element) -> dict[str, Any]:
    """Walk a plist <dict> element into a Python dict, tolerating {{PLACEHOLDER}}
    text (templates) which plistlib would reject."""
    out: dict[str, Any] = {}
    children = list(element)
    for i in range(0, len(children) - 1, 2):
        key_el, val_el = children[i], children[i + 1]
        if key_el.tag != "key":
            continue
        key = key_el.text or ""
        if val_el.tag == "dict":
            out[key] = _et_walk_dict(val_el)
        elif val_el.tag == "array":
            out[key] = [
                _et_walk_dict(item) if item.tag == "dict" else (item.text or "")
                for item in list(val_el)
            ]
        elif val_el.tag == "integer":
            text = (val_el.text or "").strip()
            out[key] = int(text) if text.lstrip("-").isdigit() else None
        elif val_el.tag in ("true", "false"):
            out[key] = val_el.tag == "true"
        else:
            out[key] = val_el.text or ""
    return out


def parse_plist(path: Path) -> dict[str, Any]:
    """Parse a rendered .plist OR a .plist.template into a plain dict.

    Rendered plists go through plistlib (strict). Templates contain
    ``{{PLACEHOLDER}}`` tokens that make <integer> values non-numeric, so they
    are parsed with ElementTree and placeholders surface as None. Raises
    ValueError when the file is neither readable XML nor a plist.
    """
    raw = path.read_bytes()
    if b"{{" not in raw:
        try:
            data = plistlib.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:
            pass  # fall through to the tolerant parser
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"unparseable plist: {path}") from exc
    dict_el = root.find("dict") if root.tag == "plist" else root
    if dict_el is None or dict_el.tag != "dict":
        raise ValueError(f"no top-level <dict> in plist: {path}")
    return _et_walk_dict(dict_el)


def schedule_from_plist(data: dict[str, Any]) -> Schedule | None:
    """Derive a Schedule from a parsed plist (template or rendered)."""
    interval = data.get("StartInterval")
    if isinstance(interval, int) and interval > 0:
        return Schedule(kind="interval", seconds=interval)
    cal = data.get("StartCalendarInterval")
    if isinstance(cal, dict):
        cal = [cal]
    if isinstance(cal, list) and cal:
        entries: list[CalendarEntry] = []
        for item in cal:
            if not isinstance(item, dict):
                continue
            entries.append(
                CalendarEntry(
                    hour=item.get("Hour") if isinstance(item.get("Hour"), int) else None,
                    minute=item.get("Minute") if isinstance(item.get("Minute"), int) else None,
                    weekday=item.get("Weekday") if isinstance(item.get("Weekday"), int) else None,
                )
            )
        if entries:
            return Schedule(kind="calendar", entries=tuple(entries))
    return None


def _merge_calendar_defaults(parsed: Schedule, default: Schedule) -> Schedule:
    """Templates carry {{HOUR}}/{{MINUTE}} placeholders; fill those from the
    installer's documented env defaults so the UI always shows real times."""
    if parsed.kind != "calendar" or default.kind != "calendar":
        return parsed
    merged: list[CalendarEntry] = []
    for i, entry in enumerate(parsed.entries):
        fallback = default.entries[min(i, len(default.entries) - 1)] if default.entries else CalendarEntry()
        merged.append(
            CalendarEntry(
                hour=entry.hour if entry.hour is not None else fallback.hour,
                minute=entry.minute if entry.minute is not None else fallback.minute,
                weekday=entry.weekday if entry.weekday is not None else fallback.weekday,
            )
        )
    return Schedule(kind="calendar", entries=tuple(merged))


# ---------------------------------------------------------------------------
# Next-fire / last-expected computation
# ---------------------------------------------------------------------------


def _calendar_fire_times(entries: tuple[CalendarEntry, ...], day: datetime) -> list[datetime]:
    """Fire times for *entries* on the local calendar day containing *day*."""
    out = []
    for entry in entries:
        if entry.hour is None:
            continue  # wildcard hours would fire 24x/day; too noisy to reason about
        if entry.weekday is not None and (day.weekday() + 1) % 7 != entry.weekday % 7:
            continue  # Python Mon=0..Sun=6 → launchd Sun=0..Sat=6
        out.append(day.replace(hour=entry.hour, minute=entry.minute or 0, second=0, microsecond=0))
    return out


def _cron_fire_times(cron_expr: str, start: datetime, end: datetime) -> list[datetime]:
    """UTC fire minutes for a 5-field cron between *start* (exclusive) and *end*."""
    fields = cron_expr.split()
    if len(fields) != 5:
        return []
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    out: list[datetime] = []
    cursor = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    while cursor <= end:
        if (
            _cron_field_matches(month_f, cursor.month, 1, 12)
            and _cron_field_matches(dom_f, cursor.day, 1, 31)
            and _cron_field_matches(dow_f, (cursor.weekday() + 1) % 7, 0, 6, dow=True)
            and _cron_field_matches(hour_f, cursor.hour, 0, 23)
            and _cron_field_matches(minute_f, cursor.minute, 0, 59)
        ):
            out.append(cursor)
        cursor += timedelta(minutes=1)
    return out


def _fire_times_around(schedule: Schedule, now: datetime) -> tuple[list[datetime], list[datetime]]:
    """(past, future) fire times bracketing *now*, best-effort per kind."""
    if schedule.kind == "calendar" and schedule.entries:
        past: list[datetime] = []
        future: list[datetime] = []
        today = now.astimezone()
        for delta in range(-_SCAN_DAYS, _SCAN_DAYS + 1):
            day = today + timedelta(days=delta)
            for fire in _calendar_fire_times(schedule.entries, day):
                (past if fire <= today else future).append(fire)
        return sorted(past), sorted(future)
    if schedule.kind == "cron" and schedule.cron:
        now_utc = now.astimezone(UTC)
        start = now_utc - timedelta(days=_SCAN_DAYS)
        end = now_utc + timedelta(days=_SCAN_DAYS)
        fires = _cron_fire_times(schedule.cron, start, end)
        past = [f for f in fires if f <= now_utc]
        future = [f for f in fires if f > now_utc]
        return past, future
    return [], []


def next_fire(schedule: Schedule, now: datetime, last_run: datetime | None) -> datetime | None:
    """Next expected fire, or None when it cannot be honestly derived."""
    if schedule.kind == "interval" and schedule.seconds:
        # launchd fires every N seconds from load; without an observed run the
        # phase is unknown — say so rather than inventing a time.
        return last_run + timedelta(seconds=schedule.seconds) if last_run else None
    _, future = _fire_times_around(schedule, now)
    return future[0] if future else None


def _cycle_seconds(schedule: Schedule, now: datetime) -> float | None:
    """The cadence in seconds (smallest gap between consecutive fires)."""
    if schedule.kind == "interval" and schedule.seconds:
        return float(schedule.seconds)
    past, future = _fire_times_around(schedule, now)
    fires = past + future
    if len(fires) < 2:
        return None
    # Pairwise sliding window: second operand is one shorter by construction.
    gaps = [(b - a).total_seconds() for a, b in zip(fires, fires[1:], strict=False)]
    return min(gaps) if gaps else None


# ---------------------------------------------------------------------------
# Live machine state — launchctl + installed plists + log mtimes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchctlProbe:
    """The outcome of reading ``launchctl list``, with did-it-run kept separate.

    ``entries`` is only a measurement when ``available`` is True. An empty
    ``entries`` with ``available=False`` means the probe COULD NOT RUN, which is
    not the same fact as "launchd is running nothing" — collapsing the two is
    what made every job report ``loaded=false`` on a host where launchctl simply
    could not be read.
    """

    entries: dict[str, int | None]
    available: bool
    reason: str = ""


def launchctl_list(runner: Any = subprocess.run) -> LaunchctlProbe:
    """``launchctl list`` → probe carrying {label: last_exit_status} (None while running).

    Returns ``available=False`` with a reason when the probe could not run at all
    (non-macOS, launchctl missing, timeout, non-zero exit). Callers MUST NOT read
    an unavailable probe as evidence that a job is off: see ``derive_health``,
    which maps it to ``unknown`` rather than ``not_loaded``.
    """
    try:
        result = runner(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=_LAUNCHCTL_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return LaunchctlProbe({}, False, "launchctl not found on this host (not macOS?).")
    except subprocess.TimeoutExpired:
        return LaunchctlProbe({}, False, f"launchctl list timed out after {_LAUNCHCTL_TIMEOUT_S}s.")
    except Exception as exc:  # noqa: BLE001 - probe must never raise into the API
        return LaunchctlProbe({}, False, f"launchctl list could not be run: {type(exc).__name__}.")
    if result.returncode != 0:
        return LaunchctlProbe(
            {}, False, f"launchctl list exited {result.returncode}; machine state was not read."
        )
    out: dict[str, int | None] = {}
    for line in result.stdout.splitlines()[1:]:  # header row
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        _pid, status, label = parts[0], parts[1], parts[2]
        try:
            out[label] = int(status)
        except ValueError:
            out[label] = None
    return LaunchctlProbe(out, True)


# ---------------------------------------------------------------------------
# Local crontab source (crontab -l) — HANDOFF: 1 entry, the hourly X/xAI
# watchdog poster, has never had a place in this catalog.
# ---------------------------------------------------------------------------

_CRONTAB_TIMEOUT_S = 3.0
_CRON_LOG_REDIRECT_RE = re.compile(r">>\s*(\S+)")
# `NAME=value` env-assignment lines (e.g. `MAILTO=ops@example.test`) — legal
# in a crontab, not a job. Only the NAME is ever kept (project convention:
# env var names only, never values) and it is attached to every job's
# `env_overrides`, mirroring what that variable actually does in a real
# crontab (it applies to the whole file, not one line).
_CRON_ENV_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*=")
# POSIX cron "nickname" schedules. `@reboot` has no cadence at all (kept as
# Schedule(kind="unknown") below) — everything else maps to its 5-field form
# so the existing next_fire/staleness machinery applies unchanged.
_CRON_NICKNAMES: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
# Tail-content error markers for a local cron's redirected log — mtime alone
# ("fresh output exists") is not proof of success (HEALTH-02); the content
# has to actually look clean.
_LOCAL_CRON_ERROR_MARKERS = ("traceback", "exception", "fatal", "error:")
_LOCAL_CRON_TAIL_READ_BYTES = 8192
_LOCAL_CRON_TAIL_MAX_LINES = 20

# Purpose/category for known crontab commands — grounded in the recon note
# (HANDOFF/LOOPS-VISIBILITY.md, recon-loops-inventory.md); unknown commands
# fall back to an explicitly INFERRED purpose built from the script path only.
_CRONTAB_KNOWN_COMMANDS: tuple[tuple[str, str, str], ...] = (
    (
        "watchdog-xai-poster",
        "Hourly watchdog for the X/xAI auto-poster — verifies the poster process/queue is "
        "alive and posting, and alerts/restarts it if not.",
        "Automation crew",
    ),
)


@dataclass(frozen=True)
class CrontabProbe:
    """The outcome of reading ``crontab -l``, mirroring :class:`LaunchctlProbe`:
    ``lines`` is only a measurement when ``available`` is True."""

    lines: tuple[str, ...]
    available: bool
    reason: str = ""


def crontab_list(runner: Any = subprocess.run) -> CrontabProbe:
    """``crontab -l`` → probe carrying the user's raw crontab lines.

    Degrades gracefully: no ``crontab`` binary, a timeout, or "no crontab for
    this user" (crontab's own not-an-error exit) all yield an honest empty/
    unavailable probe rather than raising."""
    try:
        result = runner(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            timeout=_CRONTAB_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return CrontabProbe((), False, "crontab not found on this host.")
    except subprocess.TimeoutExpired:
        return CrontabProbe((), False, f"crontab -l timed out after {_CRONTAB_TIMEOUT_S}s.")
    except Exception as exc:  # noqa: BLE001 - probe must never raise into the API
        return CrontabProbe((), False, f"crontab -l could not be run: {type(exc).__name__}.")
    if result.returncode != 0:
        stderr = (getattr(result, "stderr", "") or "").lower()
        if "no crontab" in stderr:
            # Not a probe failure — this user simply has no crontab installed.
            return CrontabProbe((), True, "")
        return CrontabProbe((), False, f"crontab -l exited {result.returncode}; local crontab was not read.")
    lines = tuple(
        line for line in (result.stdout or "").splitlines() if line.strip() and not line.strip().startswith("#")
    )
    return CrontabProbe(lines, True)


def _crontab_purpose(command: str) -> tuple[str, str]:
    for fragment, purpose, category in _CRONTAB_KNOWN_COMMANDS:
        if fragment in command:
            return purpose, category
    script = command.split()[0] if command.split() else command
    name = Path(script).name or script
    return (
        f"Inferred from its crontab command (`{name}`) only — no dedicated recon note explains this job's purpose.",
        "Local cron",
    )


def _local_cron_tail(path: Path) -> list[str] | None:
    """Best-effort tail of a redirected local-cron log — bounded read (last
    ``_LOCAL_CRON_TAIL_READ_BYTES``), never the whole file. ``None`` means
    unreadable (permission, gone between stat and read), distinct from "read
    fine and it's empty"."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _LOCAL_CRON_TAIL_READ_BYTES:
                handle.seek(-_LOCAL_CRON_TAIL_READ_BYTES, os.SEEK_END)
            raw = handle.read()
    except OSError:
        return None
    lines = [line for line in raw.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return lines[-_LOCAL_CRON_TAIL_MAX_LINES:]


def _local_cron_job_key(command: str, seen: dict[str, int]) -> str:
    """Collision-proof key: the same command at two different cadences (or
    two cron lines invoking the same script) must not collapse into one job —
    a repeat gets an index suffix."""
    key_source = Path(command.split()[0]).name if command.split() else command
    slug = re.sub(r"[^a-z0-9]+", "-", key_source.lower()).strip("-") or "job"
    base_key = f"local-cron-{slug}"
    count = seen.get(base_key, 0)
    seen[base_key] = count + 1
    return base_key if count == 0 else f"{base_key}-{count + 1}"


def parse_crontab(lines: tuple[str, ...], now: datetime) -> list[dict[str, Any]]:
    """One job per crontab line: 5-field cron schedule reused through the same
    ``Schedule(kind="cron")`` machinery as the remote crons, so next_fire and
    staleness-vs-cadence work identically. `NAME=value` env-assignment lines
    (e.g. ``MAILTO=...``) are not jobs — their NAMEs are collected and applied
    to every job's ``env_overrides``. ``@daily``/``@hourly``/... nicknames are
    supported; ``@reboot`` has no cadence (``kind="unknown"``, note "on
    reboot"). Health is honest, not invented: no log redirect -> ``unknown``
    (cron gives no exit status); a redirect whose tail contains an error
    marker -> ``failing``; unreadable tail -> ``unknown``; fresh + clean tail
    -> ``healthy`` (reason states this evidence basis explicitly); stale ->
    ``stale``. Job keys are collision-proof across repeated commands."""
    jobs: list[dict[str, Any]] = []
    env_names: list[str] = []
    seen_keys: dict[str, int] = {}

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        env_match = _CRON_ENV_LINE_RE.match(stripped)
        if env_match:
            env_names.append(env_match.group(1))
            continue

        if stripped.startswith("@"):
            token, _, rest = stripped.partition(" ")
            command = rest.strip()
            if not command:
                continue  # malformed nickname line — skip rather than guess
            if token == "@reboot":
                schedule = Schedule(kind="unknown", note="on reboot")
            else:
                cron_expr = _CRON_NICKNAMES.get(token)
                if cron_expr is None:
                    continue  # unrecognized nickname — skip rather than guess
                schedule = Schedule(kind="cron", cron=cron_expr)
        else:
            parts = stripped.split(None, 5)
            if len(parts) < 6:
                continue  # malformed line — skip rather than guess at a schedule
            schedule = Schedule(kind="cron", cron=" ".join(parts[:5]))
            command = parts[5]

        purpose, category = _crontab_purpose(command)
        key_source = Path(command.split()[0]).name if command.split() else command

        last_run: datetime | None = None
        if schedule.kind == "unknown":  # @reboot — no cadence, no exit status, ever
            health, reason = (
                "unknown",
                "Runs at boot (`@reboot`) — cron gives no exit status and there is no cadence "
                "to judge staleness against.",
            )
        else:
            tail_lines: list[str] | None = None
            missing_log_reason = ""
            redirect = _CRON_LOG_REDIRECT_RE.search(command)
            if redirect:
                log_path = Path(os.path.expanduser(redirect.group(1)))
                try:
                    last_run = datetime.fromtimestamp(log_path.stat().st_mtime, UTC)
                except OSError:
                    missing_log_reason = f"Log redirect target {log_path} was not found on this host."
                else:
                    tail_lines = _local_cron_tail(log_path)

            if last_run is None:
                health, reason = (
                    "unknown",
                    missing_log_reason
                    or (
                        "No `>> path` log redirect in this crontab line, and cron itself reports no "
                        "exit status — this job's health cannot be honestly derived from the crontab alone."
                    ),
                )
            elif tail_lines is None:
                health, reason = (
                    "unknown",
                    f"Log redirect target updated {_iso(last_run)} but its content could not be read "
                    "to confirm success (permission, or it disappeared).",
                )
            else:
                tail_text = "\n".join(tail_lines).lower()
                if any(marker in tail_text for marker in _LOCAL_CRON_ERROR_MARKERS):
                    last_line = sanitize_remote_text(tail_lines[-1]) if tail_lines else ""
                    health, reason = (
                        "failing",
                        f"Log redirect target updated {_iso(last_run)}, but its tail contains an "
                        f"error marker: {last_line!r}.",
                    )
                else:
                    cycle = _cycle_seconds(schedule, now)
                    age = (now - last_run).total_seconds()
                    if cycle is not None and age > _STALE_FACTOR * cycle:
                        health, reason = (
                            "stale",
                            f"No fresh output in over {_STALE_FACTOR:.0f}x its cadence "
                            f"({describe_schedule(schedule)}); last log update {_iso(last_run)}.",
                        )
                    else:
                        health, reason = (
                            "healthy",
                            f"Log redirect target updated {_iso(last_run)}: recent output, no error "
                            "markers found in its tail — cron itself gives no exit status, so this is "
                            "the strongest evidence available.",
                        )

        fires_at = next_fire(schedule, now, last_run)
        jobs.append(
            {
                "key": _local_cron_job_key(command, seen_keys),
                "name": key_source or "local cron job",
                "executor": "local_cron",
                "category": category,
                "label": None,
                "purpose": purpose,
                "source": "crontab -l",
                "module": sanitize_remote_text(command),
                "schedule": {
                    "kind": schedule.kind,
                    "seconds": schedule.seconds,
                    "description": describe_schedule(schedule),
                },
                "env_overrides": [],
                "loaded": True,  # present in the crontab = scheduled/active by definition
                "plist_present": False,
                "last_exit_status": None,
                "last_run_at": _iso(last_run),
                "next_fire_at": _iso(fires_at),
                "health": health,
                "health_reason": reason,
                "managed_candidate": False,
                "candidate_reason": "",
                "last_result": None,
            }
        )

    if env_names:
        names = sorted(set(env_names))
        for job in jobs:
            job["env_overrides"] = names
    return jobs


_LABEL_FALLBACK_RE = re.compile(rb"<key>\s*Label\s*</key>\s*<string>([^<]+)</string>")

# Sentinel key used to mark an installed-plist entry that exists on disk but
# failed strict parsing. Downstream code MUST check for this key and surface
# it loudly (unknown/error) rather than silently treating the entry as a
# normal, healthy plist -- and MUST NOT ever let a parse failure make the
# entry vanish, which is exactly what turned a loaded job into a favourable
# "not installed" absence (sha256:e63c660be4da172c0c3cd52522d8310620393dd).
PARSE_ERROR_KEY = "__parse_error__"


def _label_fallback(path: Path) -> str | None:
    """Best-effort ``Label`` extraction for a plist that failed strict
    parsing, so it still gets a stable, launchd-accurate dict key instead of
    falling back to the filename stem (which can differ from the real
    Label)."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    match = _LABEL_FALLBACK_RE.search(raw)
    return match.group(1).decode("utf-8", "replace") if match else None


def scan_installed_plists(launchd_dir: Path) -> tuple[dict[str, dict[str, Any]], list[Path]]:
    """Parse installed product plists; returns ({label: plist}, stale_backup_paths).

    A plist that exists on disk but fails to parse (malformed XML -- e.g. a
    bare ``--`` inside an XML comment, which every strict XML parser rejects
    while ``plutil -lint``/launchd's own CFPropertyList reader accepts) is
    NOT silently dropped. Dropping it collapses "instrument error: this file
    is unreadable" into "absent: this job is not installed", which is a
    favourable-absence bug -- a job that IS loaded in launchd then reads as
    not installed. Instead the entry is kept under a best-effort label,
    carrying :data:`PARSE_ERROR_KEY` so callers can render the failure
    honestly instead of rendering it as absence.
    """
    installed: dict[str, dict[str, Any]] = {}
    stale: list[Path] = []
    if not launchd_dir.is_dir():
        return installed, stale
    for path in sorted(launchd_dir.iterdir()):
        name = path.name
        if name.endswith(".bak-swarm") or ".plist.bak" in name:
            stale.append(path)
            continue
        if not name.endswith(".plist"):
            continue
        if not (name.startswith("com.omniagentos.") or name.startswith("com.omniagentos.")):
            continue
        try:
            data = parse_plist(path)
        except ValueError as exc:
            label = _label_fallback(path) or path.stem
            installed[label] = {
                "__path__": str(path),
                PARSE_ERROR_KEY: str(exc),
            }
            continue
        label = data.get("Label") or path.stem
        data["__path__"] = str(path)
        installed[label] = data
    return installed, stale


def _log_mtime(paths: list[Path]) -> datetime | None:
    newest: datetime | None = None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        stamp = datetime.fromtimestamp(mtime, UTC)
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def _iso(stamp: datetime | None) -> str | None:
    if stamp is None:
        return None
    return stamp.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def derive_health(
    *,
    executor: str,
    loaded: bool | None,
    last_exit: int | None,
    last_run: datetime | None,
    schedule: Schedule,
    now: datetime,
    health_note: str = "",
    unmeasured_reason: str = "",
) -> tuple[str, str]:
    """(state, reason) per HANDOFF §L2. Never renders unobservable work healthy."""
    if executor in ("remote_cron", "remote_docker"):
        return "unknown", health_note or "Remote job; no live observability wired (HANDOFF L3)."
    if executor == "csi_pipeline":
        return "unknown", health_note or "Runs inside the human-gated CSI pipeline; no per-routine schedule."
    if loaded is None:
        # A probe that could not run is an ABSENCE, not a measurement. Without
        # this branch a launchd job whose launchctl read failed falls through to
        # the last_exit/last_run rules below and can be rendered `healthy` off a
        # log mtime alone — reporting the automation fleet as fine precisely when
        # the machine state backing that claim is unreadable.
        return "unknown", (
            unmeasured_reason
            or "launchd state could not be read on this host, so loaded/off is unknown — not off."
        )
    if loaded is False:
        return "not_loaded", (
            "Not loaded into launchd. Rendered installers are load-by-hand after review; "
            "this job is configured but currently off."
        )
    if last_exit not in (None, 0):
        return "failing", f"launchd reports last exit status {last_exit}."
    if last_run is None:
        return "unknown", "Loaded, but no log output observed yet — last run cannot be derived."
    cycle = _cycle_seconds(schedule, now)
    if cycle is None:
        # `healthy` below is only reachable by SURVIVING the staleness rule, so
        # a schedule with no derivable cadence has nothing to survive. Falling
        # through printed "Fired within its expected cadence" for a comparison
        # that never ran — a KeepAlive WebSocket (kind='window') or a discovered
        # daemon plist (kind='unknown') rendered green off a log mtime years
        # old, because a log that exists is the only other evidence in play.
        # Absence of a cadence is exactly the `unknown` state this module
        # promises to use instead of overclaiming.
        return "unknown", (
            f"Loaded, last exit 0, last output {_iso(last_run)} — but no cadence can be "
            f"derived from its schedule ({describe_schedule(schedule)}), so staleness "
            "cannot be judged here."
        )
    age = (now - last_run).total_seconds()
    if age > _STALE_FACTOR * cycle:
        return "stale", (
            f"No output in {int(age // 3600)}h — over {_STALE_FACTOR:.0f}x its cadence "
            f"({describe_schedule(schedule)})."
        )
    return "healthy", "Fired within its expected cadence; last exit 0."


# ---------------------------------------------------------------------------
# CSI pipeline routines (configs/self_improvement.yaml)
# ---------------------------------------------------------------------------


def load_csi_routines(config_path: Path) -> list[dict[str, Any]]:
    """The 8 self-improvement routines declared in configs/self_improvement.yaml.

    They are recurring automation (observation windows + planner panels) but run
    inside the human-gated CSI pipeline, not on launchd — surfaced here so the
    Loops page shows them next to everything else. Best-effort: a missing or
    malformed config yields []."""
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    pipeline_enabled = bool(data.get("enabled")) and not data.get("global_halt", False)
    routines = data.get("routines")
    if not isinstance(routines, dict):
        return []
    out: list[dict[str, Any]] = []
    for name, cfg in sorted(routines.items()):
        if not isinstance(cfg, dict) or not cfg.get("enabled", False):
            continue
        window = cfg.get("window_days", "?")
        planners = ", ".join(str(p) for p in cfg.get("planners", []))
        health_note = (
            "Enabled in config; runs when an operator invokes the CSI pipeline "
            "(scripts/csi-human-pipeline.sh run-all), not on a clock."
        )
        out.append(
            {
                "key": f"csi-{name}",
                "name": f"CSI: {name.replace('_', ' ')}",
                "executor": "csi_pipeline",
                "category": "Self-improvement (CSI)",
                "label": None,
                "purpose": (
                    f"Offline evidence review + vault skill-note proposals over a {window}-day "
                    f"window (planners: {planners}). Human-gated: CSI never merges main."
                ),
                "source": "configs/self_improvement.yaml",
                "module": "scripts/csi-human-pipeline.sh",
                "schedule": {
                    "kind": "window",
                    "seconds": None,
                    "description": f"{window}-day observation window · run via scripts/csi-human-pipeline.sh",
                },
                "env_overrides": [],
                "pipeline_enabled": pipeline_enabled,
                "loaded": None,
                "plist_present": False,
                "last_exit_status": None,
                "last_run_at": None,
                "next_fire_at": None,
                "health": "unknown",
                "health_reason": health_note,
                "managed_candidate": False,
                "candidate_reason": (
                    "Already a governed pipeline with human approval gates; converting it to a "
                    "managed routine would bypass the CSI approve/implement flow."
                ),
                "last_result": None,
            }
        )
    return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def _resolve_repo_path(repo_root: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else repo_root / raw


def list_system_jobs(
    *,
    repo_root: Path,
    launchd_dir: Path | None = None,
    now: datetime | None = None,
    launchctl: dict[str, int | None] | LaunchctlProbe | None = None,
    crontab: CrontabProbe | tuple[str, ...] | None = None,
    remote_probe: RemoteProbeSnapshot | None = None,
) -> dict[str, Any]:
    """Full read-only snapshot for the API. Never raises on machine-state gaps.

    ``crontab`` and ``remote_probe`` default to "nothing available" (an empty
    local crontab, an unconfigured remote probe) rather than making a real
    subprocess/SSH call — this function stays pure and hermetic for tests. The
    real reads (``crontab_list()``, a ``RemoteProbeCache``) are wired up one
    level up, in :func:`cached_list_system_jobs`, which is what the API route
    actually calls.
    """
    now = now or datetime.now(UTC)
    if launchd_dir is None:
        launchd_dir = Path.home() / "Library" / "LaunchAgents"
    if launchctl is None:
        probe = launchctl_list()
    elif isinstance(launchctl, LaunchctlProbe):
        probe = launchctl
    else:
        # A bare dict from an existing caller is a MEASUREMENT by construction:
        # it was supplied rather than probed, so there is no failure to hide.
        probe = LaunchctlProbe(launchctl, True)
    launchctl = probe.entries
    unmeasured = None if probe.available else (probe.reason or "launchd state could not be read.")
    installed, stale_backups = scan_installed_plists(launchd_dir)

    if crontab is None:
        crontab_probe = CrontabProbe((), True)
    elif isinstance(crontab, CrontabProbe):
        crontab_probe = crontab
    else:
        crontab_probe = CrontabProbe(tuple(crontab), True)

    remote_snapshot = remote_probe or RemoteProbeSnapshot(
        False, "remote probe not configured for this call.", None, None
    )

    jobs: list[dict[str, Any]] = []
    seen_labels: set[str] = set()

    for entry in CATALOG:
        schedule = entry.schedule
        # 1. Parse the repo template (schedule shape is literal even when the
        #    calendar hours are {{placeholders}}).
        if entry.template:
            try:
                parsed = schedule_from_plist(parse_plist(repo_root / entry.template))
            except (ValueError, OSError):
                parsed = None
            if parsed is not None:
                schedule = _merge_calendar_defaults(parsed, schedule)
        # 2. The installed plist is the machine's truth and wins outright.
        installed_plist = installed.get(entry.label or "")
        log_candidates = [_resolve_repo_path(repo_root, p) for p in entry.log_paths]
        if installed_plist is not None:
            parsed_installed = schedule_from_plist(installed_plist)
            if parsed_installed is not None:
                schedule = parsed_installed
            for key in ("StandardOutPath", "StandardErrorPath"):
                raw_path = installed_plist.get(key)
                if isinstance(raw_path, str) and raw_path:
                    log_candidates.insert(0, Path(raw_path))
        if entry.label:
            seen_labels.add(entry.label)

        if entry.executor in ("remote_cron", "remote_docker"):
            loaded: bool | None = None
            last_exit: int | None = None
            last_run: datetime | None = None
        elif unmeasured is not None:
            loaded = None
            last_exit = None
            last_run = _log_mtime(log_candidates)
        else:
            loaded = (entry.label or "") in launchctl
            last_exit = launchctl.get(entry.label or "")
            last_run = _log_mtime(log_candidates)

        fires_at = next_fire(schedule, now, last_run)
        health, reason = derive_health(
            executor=entry.executor,
            loaded=loaded,
            last_exit=last_exit,
            last_run=last_run,
            schedule=schedule,
            now=now,
            health_note=entry.health_note,
            unmeasured_reason=unmeasured or "",
        )
        last_result: str | None = None
        if entry.executor in ("remote_docker", "remote_cron"):
            # Real evidence from the cached SSH probe, when one is available —
            # otherwise keep the honest "unknown" derive_health already gave us.
            if remote_snapshot.available and remote_snapshot.parsed is not None:
                if entry.executor == "remote_docker" and entry.remote_container:
                    health, reason, last_result = docker_service_health(
                        remote_snapshot.parsed, entry.remote_container
                    )
                elif entry.executor == "remote_cron" and entry.remote_cron_fragment:
                    present, line = remote_cron_present(remote_snapshot.parsed, entry.remote_cron_fragment)
                    if present:
                        last_result = line
                        mtime_iso = (
                            remote_snapshot.parsed.cron_log_mtimes.get(entry.remote_cron_log_path)
                            if entry.remote_cron_log_path
                            else None
                        )
                        last_seen = None
                        if mtime_iso:
                            try:
                                last_seen = datetime.strptime(mtime_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                            except ValueError:
                                last_seen = None
                        cycle = _cycle_seconds(schedule, now) if last_seen is not None else None
                        if last_seen is not None and cycle is not None:
                            last_run = last_seen  # real evidence — reflected in last_run_at below
                            age = (now - last_seen).total_seconds()
                            if age > _STALE_FACTOR * cycle:
                                health, reason = (
                                    "stale",
                                    f"Confirmed present in the remote crontab; its log last updated "
                                    f"{mtime_iso} — over {_STALE_FACTOR:.0f}x its cadence "
                                    f"({describe_schedule(schedule)}).",
                                )
                            else:
                                health, reason = (
                                    "healthy",
                                    f"Confirmed present in the remote crontab; its log last updated "
                                    f"{mtime_iso}, within its expected cadence ({describe_schedule(schedule)}).",
                                )
                        else:
                            health, reason = (
                                "unknown",
                                f"Confirmed present in the remote crontab ({line!r}); no log-mtime-vs-cadence "
                                "evidence was captured for this specific job, so staleness is not judged here.",
                            )
                    else:
                        health, reason = (
                            "unknown",
                            "Remote probe ran but this job's line could not be confirmed in the captured "
                            f"remote crontab — {entry.health_note or 'no further evidence available.'}",
                        )
            elif not remote_snapshot.available:
                reason = f"remote probe pending/failed: {remote_snapshot.reason}"
        if installed_plist is not None and PARSE_ERROR_KEY in installed_plist:
            # Instrument error, not an absence: the plist IS on disk (hence
            # plist_present below stays True) but failed to parse. Never let
            # this read as healthy/stale/not_loaded off machinery that could
            # not actually read the file.
            health, reason = (
                "unknown",
                f"Installed plist is present but failed to parse "
                f"({installed_plist[PARSE_ERROR_KEY]}); this is a broken file, not an "
                "absent job — fix the plist rather than trusting derived state.",
            )
        jobs.append(
            {
                "key": entry.key,
                "name": entry.name,
                "executor": entry.executor,
                "category": entry.category,
                "label": entry.label,
                "purpose": entry.purpose,
                "source": entry.source,
                "module": entry.module,
                "schedule": {
                    "kind": schedule.kind,
                    "seconds": schedule.seconds,
                    "description": describe_schedule(schedule),
                },
                "env_overrides": list(entry.env_overrides),
                "loaded": loaded,
                "plist_present": installed_plist is not None,
                "last_exit_status": last_exit,
                "last_run_at": _iso(last_run),
                "next_fire_at": _iso(fires_at),
                "health": health,
                "health_reason": reason,
                "managed_candidate": entry.managed_candidate,
                "candidate_reason": entry.candidate_reason,
                "last_result": last_result,
            }
        )

    # 3. CSI pipeline routines from configs/self_improvement.yaml.
    jobs.extend(load_csi_routines(repo_root / "configs" / "self_improvement.yaml"))

    # 3b. Local crontab source (crontab -l) — e.g. the hourly X/xAI poster watchdog.
    jobs.extend(parse_crontab(crontab_probe.lines, now))

    # 4. Discovered jobs: installed on this machine but with no repo definition
    #    in this tree (e.g. sibling-product api/dashboard/runner keep-alives).
    for label, data in sorted(installed.items()):
        if label in seen_labels:
            continue
        seen_labels.add(label)
        schedule = schedule_from_plist(data) or Schedule(kind="unknown", note="keep-alive / on-demand")
        log_candidates = [
            Path(data[key])
            for key in ("StandardOutPath", "StandardErrorPath")
            if isinstance(data.get(key), str) and data.get(key)
        ]
        last_run = _log_mtime(log_candidates)
        loaded = None if unmeasured is not None else label in launchctl
        last_exit = None if unmeasured is not None else launchctl.get(label)
        health, reason = derive_health(
            executor="launchd",
            loaded=loaded,
            last_exit=last_exit,
            last_run=last_run,
            schedule=schedule,
            now=now,
            unmeasured_reason=unmeasured or "",
        )
        parse_error = data.get(PARSE_ERROR_KEY)
        enrichment = _enrich_discovered(label)
        if enrichment is not None:
            purpose, category = enrichment
        else:
            purpose = (
                "Installed on this machine but not defined by any installer/template in "
                "this tree (likely a sibling-product or hand-installed job)."
            )
            category = "Discovered (no repo definition)"
        if parse_error:
            # Same instrument-error rule as the catalog loop above: a plist
            # that exists but fails to parse must read as broken, never as a
            # normal/healthy discovered job.
            health, reason = (
                "unknown",
                f"Plist is present on disk but failed to parse ({parse_error}); "
                "this is a broken file, not confirmation of a normal state.",
            )
            purpose = (
                f"{purpose} NOTE: the installed plist failed to parse as XML ({parse_error}) — "
                "likely a malformed comment or markup; fix the file, this job is NOT actually absent."
            )
        jobs.append(
            {
                "key": f"discovered-{label}",
                "name": label.rsplit(".", 1)[-1],
                "executor": "launchd",
                "category": category,
                "label": label,
                "purpose": purpose,
                "source": data.get("__path__", ""),
                "module": None,
                "schedule": {
                    "kind": schedule.kind,
                    "seconds": schedule.seconds,
                    "description": describe_schedule(schedule),
                },
                "env_overrides": [],
                "loaded": loaded,
                "plist_present": True,
                "last_exit_status": last_exit,
                "last_run_at": _iso(last_run),
                "next_fire_at": _iso(next_fire(schedule, now, last_run)),
                "health": health,
                "health_reason": reason,
                "managed_candidate": False,
                "candidate_reason": "",
                "last_result": None,
            }
        )

    # 5. Loaded into launchd but with no plist in the scanned LaunchAgents dir
    #    AND no catalog entry — e.g. loaded from a rendered plist under
    #    var/launchd/rendered. An operator staring at `launchctl list` should be
    #    able to find that same job here.
    for label, last_exit in sorted(launchctl.items()):
        if label in seen_labels:
            continue
        if not (label.startswith("com.omniagentos.") or label.startswith("com.omniagentos.")):
            continue
        seen_labels.add(label)
        schedule = Schedule(kind="unknown", note="keep-alive / on-demand")
        health, reason = derive_health(
            executor="launchd",
            loaded=True,
            last_exit=last_exit,
            last_run=None,
            schedule=schedule,
            now=now,
        )
        enrichment = _enrich_discovered(label)
        if enrichment is not None:
            purpose, category = enrichment
        else:
            purpose = (
                "Loaded into launchd but not defined by any installer/template in this "
                "tree and no plist in the scanned LaunchAgents dir (likely loaded from a "
                "rendered plist under var/launchd/rendered, or a sibling product)."
            )
            category = "Discovered (no repo definition)"
        jobs.append(
            {
                "key": f"discovered-{label}",
                "name": label.rsplit(".", 1)[-1],
                "executor": "launchd",
                "category": category,
                "label": label,
                "purpose": purpose,
                "source": "launchctl list",
                "module": None,
                "schedule": {
                    "kind": schedule.kind,
                    "seconds": schedule.seconds,
                    "description": describe_schedule(schedule),
                },
                "env_overrides": [],
                "loaded": True,
                "plist_present": False,
                "last_exit_status": last_exit,
                "last_run_at": None,
                "next_fire_at": None,
                "health": health,
                "health_reason": reason,
                "managed_candidate": False,
                "candidate_reason": "",
                "last_result": None,
            }
        )

    # 6. Stale backup plists (HANDOFF acceptance: api.bak-swarm / runner.bak-swarm
    #    must be flagged for removal, not silently ignored).
    for path in stale_backups:
        jobs.append(
            {
                "key": f"stale-{path.name}",
                "name": path.name,
                "executor": "launchd",
                "category": "Stale backup",
                "label": None,
                "purpose": "Backup plist left in the LaunchAgents directory — not loaded, candidate for removal.",
                "source": str(path),
                "module": None,
                "schedule": {"kind": "unknown", "seconds": None, "description": "—"},
                "env_overrides": [],
                "loaded": False,
                "plist_present": False,
                "last_exit_status": None,
                "last_run_at": None,
                "next_fire_at": None,
                "health": "unknown",
                "health_reason": "Stale backup plist — flag for removal (HANDOFF/LOOPS-VISIBILITY.md).",
                "managed_candidate": False,
                "candidate_reason": "",
                "last_result": None,
            }
        )

    jobs.sort(key=lambda j: (j["category"], j["name"]))
    return {
        "generated_at": _iso(now),
        # Whether the machine-state probe ran at all. Without this a consumer
        # cannot tell `loaded: 0` (nothing is loaded) from `loaded: 0` (nothing
        # was measured) — the same favourable absence the per-job `loaded` flag
        # was fixed for, one level up in the payload.
        "launchctl": {
            "available": probe.available,
            "reason": probe.reason,
        },
        "counts": {
            "total": len(jobs),
            "loaded": sum(1 for j in jobs if j["loaded"] is True),
            "loaded_unknown": sum(1 for j in jobs if j["loaded"] is None),
            "failing": sum(1 for j in jobs if j["health"] == "failing"),
            "stale": sum(1 for j in jobs if j["health"] == "stale"),
            # Additive (2026-08-15 loops-health-ui): the counts above only
            # ever covered failure states — an operator scanning the summary
            # strip had no honest denominator for "how many are actually fine".
            "healthy": sum(1 for j in jobs if j["health"] == "healthy"),
            "unknown": sum(1 for j in jobs if j["health"] == "unknown"),
            "not_loaded": sum(1 for j in jobs if j["health"] == "not_loaded"),
        },
        "jobs": jobs,
        # Additive: whether the remote SSH probe backing the 7 remote-catalog
        # jobs' health has real evidence right now.
        "remote_probe": {
            "available": remote_snapshot.available,
            "reason": remote_snapshot.reason,
            "probed_at": remote_snapshot.probed_at,
        },
        # Additive (OBS-02): whether the local `crontab -l` read that
        # produced any `local_cron` jobs above actually succeeded — an
        # unreadable crontab must be distinguishable from a genuinely empty
        # one; consumers must not read zero local_cron jobs as "nothing is
        # scheduled" when this says available=False.
        "local_cron": {
            "available": crontab_probe.available,
            "reason": crontab_probe.reason,
        },
    }


# ---------------------------------------------------------------------------
# Caching wrapper — the ONLY place that makes real crontab/SSH calls by
# default. `list_system_jobs` above stays pure/hermetic for tests; this
# function is what `omniagentos/api/routes/system_jobs.py` actually calls.
# ---------------------------------------------------------------------------


@dataclass
class SnapshotCache:
    """A tiny in-process TTL cache around one builder callable, with TRUE
    single-flight on a miss: ``get()`` holds a mutex across the entire check
    + (maybe) rebuild, so two concurrent callers arriving on an expired/empty
    cache never both invoke ``builder`` — the second blocks, then finds the
    first's fresh result waiting for it rather than re-running the full
    launchctl/plist scan a second time (CONC-01).

    Exists so a page load never re-runs that scan (and, via the wrapper
    below, never triggers more than one crontab read) more than once per
    ``ttl_s`` — ``?fresh=1`` on the route bypasses it."""

    ttl_s: float = 30.0
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    _snapshot: dict[str, Any] | None = field(default=None, init=False, repr=False)
    _cached_at: datetime | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def get(self, builder: Callable[[], dict[str, Any]], *, fresh: bool = False) -> dict[str, Any]:
        with self._lock:
            now = self.clock()
            if (
                not fresh
                and self._snapshot is not None
                and self._cached_at is not None
                and (now - self._cached_at).total_seconds() < self.ttl_s
            ):
                return self._snapshot
            snapshot = builder()
            self._snapshot = snapshot
            self._cached_at = now
            return snapshot


_SNAPSHOT_CACHE = SnapshotCache()
_remote_probe_caches: dict[Path, RemoteProbeCache] = {}
_remote_probe_caches_lock = threading.Lock()


def _get_default_remote_probe_cache(repo_root: Path) -> RemoteProbeCache:
    """One RemoteProbeCache per repo_root (keeps its own cache-file path and
    single-flight lock), lazily created and reused across calls."""
    with _remote_probe_caches_lock:
        cache = _remote_probe_caches.get(repo_root)
        if cache is None:
            cache = RemoteProbeCache(cache_path=repo_root / "var" / "cache" / "system_jobs_remote.json")
            _remote_probe_caches[repo_root] = cache
        return cache


def cached_list_system_jobs(
    *,
    repo_root: Path,
    launchd_dir: Path | None = None,
    fresh: bool = False,
    cache: SnapshotCache | None = None,
) -> dict[str, Any]:
    """What the API route calls: a TTL-cached snapshot wired up with the real
    crontab read and the cached/single-flight remote SSH probe. ``fresh=True``
    (``?fresh=1``) bypasses the snapshot TTL — it still serves the remote
    probe's own cache instantly, it just re-runs the local launchctl/plist/
    crontab scan."""
    cache = cache or _SNAPSHOT_CACHE

    def build() -> dict[str, Any]:
        remote_snapshot = _get_default_remote_probe_cache(repo_root).get()
        return list_system_jobs(
            repo_root=repo_root,
            launchd_dir=launchd_dir,
            crontab=crontab_list(),
            remote_probe=remote_snapshot,
        )

    return cache.get(build, fresh=fresh)
