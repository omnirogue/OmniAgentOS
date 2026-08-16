"""Compute-pool status for the Team Work OS: a read-only view of the work queue.

The load balancer's state lives in ``var/workqueue.sqlite3`` (owned by
:mod:`omniagentos.workqueue`). This module reads it **read-only** and
**degrade-silent** — a missing, unmigrated, or write-locked DB NEVER breaks the
hourly session-tracker report (the same tolerance :mod:`session_tracker` gives
the loopqueue ledger). When the pool DB is absent — the pool is simply not
deployed on this box — a single local-machine fallback row stands in from
``os.getloadavg()``/``os.cpu_count()`` so the operator still sees *some*
capacity signal rather than a blank.

Three things get surfaced:

* **Capacity** — per-machine ``(ncpu, load1, max_concurrent, in_flight, drain,
  freshness)`` plus pool totals and the queued depth.
* **Offloads** — per person (``submitted_by``) how much live work they have in
  the pool and on whose box it is running (``lease_owner`` → machine).
* **One suggestion** — first-match single line (the tracker's bottleneck
  discipline: a report that lists every true statement is a report nobody
  reads).

Consequential remediations (drain / spin-down / re-route) are NEVER executed
here. Detected pool problems become numbered ``balancer`` decisions through
:mod:`omniagentos.team.decisions` (confirm-first, never full-auto).
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.team.decisions import (
    _expire,
    _recent_same_kind,
    _state_lock,
    load_state,
    save_state,
)
from omniagentos.team.session_tracker import _iso, _parse_iso, _utcnow

WORKQUEUE_DB_NAME = "workqueue.sqlite3"
#: A machine that beat within this window is "live"; past it, the box is stale
#: (mirrors the pool's own liveness horizon — telemetry every ~few minutes).
FRESH_SECONDS = 10 * 60
#: Read-only connections wait at most this long on a busy writer, then degrade.
RO_TIMEOUT_S = 2.0
_LIVE_STATES = ("queued", "claimed", "running", "review")

# Colors reused from the styled-block palette so alert side-bars match the rest
# of the team surface (imported read-only; this module never edits slack_blocks).
RED = "#e01e5a"
AMBER = "#ecb22e"


@dataclass
class PoolMachine:
    """One enrolled machine's capacity snapshot."""

    machine_id: str
    ncpu: int | None
    load1: float | None
    max_concurrent: int
    in_flight: int
    drain: bool
    fresh: bool
    last_seen_age_s: float | None
    labels: list[str] = field(default_factory=list)

    @property
    def free_slots(self) -> int:
        """Claimable slots right now: 0 unless the box is live and not draining."""
        if self.drain or not self.fresh:
            return 0
        return max(0, self.max_concurrent - self.in_flight)


@dataclass
class PersonOffload:
    """One person's live footprint in the pool, and the boxes it runs on."""

    person: str
    queued: int = 0
    running: int = 0
    in_review: int = 0
    machines: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.queued + self.running + self.in_review


@dataclass
class Compute:
    """The whole compute-pool view the tracker and the alerters read."""

    source: str  # "pool" | "local" | "none"
    machines: list[PoolMachine] = field(default_factory=list)
    offloads: list[PersonOffload] = field(default_factory=list)
    unclaimable: list[dict[str, Any]] = field(default_factory=list)
    queued: int = 0
    running: int = 0
    in_review: int = 0
    total_slots: int = 0
    free_slots: int = 0
    in_flight: int = 0
    notes: list[str] = field(default_factory=list)
    # Label-sets of the queued units, kept for starvation detection.
    queued_labels: list[list[str]] = field(default_factory=list)

    def missing_labels(self) -> list[str]:
        """Labels demanded by unclaimable units that no live box advertises."""
        advertised: set[str] = set()
        for machine in self.machines:
            if not machine.drain:
                advertised |= set(machine.labels)
        wanted: set[str] = set()
        for unit in self.unclaimable:
            wanted |= {str(label) for label in unit.get("labels", [])}
        return sorted(wanted - advertised)

    def starving_machines(self) -> list[PoolMachine]:
        """Live boxes with free slots while a label-compatible unit sits queued."""
        starving: list[PoolMachine] = []
        for machine in self.machines:
            if machine.free_slots <= 0:
                continue
            declared = set(machine.labels)
            if any(set(labels) <= declared for labels in self.queued_labels):
                starving.append(machine)
        return starving

    def down_machines(self) -> list[PoolMachine]:
        """Boxes that beat before but have since gone stale (>10min) and drain=0."""
        return [
            machine
            for machine in self.machines
            if not machine.drain
            and machine.last_seen_age_s is not None
            and machine.last_seen_age_s > FRESH_SECONDS
        ]


# --------------------------------------------------------------------------
# reading the pool DB (read-only, degrade-silent)
# --------------------------------------------------------------------------


def _db_path(repo_root: Path) -> Path:
    """The workqueue DB under the resolved var root; repo tree as the fallback."""
    from omniagentos.runtime_paths import resolve_var_root

    try:
        return Path(resolve_var_root()) / WORKQUEUE_DB_NAME
    except Exception:  # noqa: BLE001 — sim-context refusal falls back to the repo tree
        return repo_root / "var" / WORKQUEUE_DB_NAME


def _labels(raw: object) -> list[str]:
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _local_fallback(note: str) -> Compute:
    """A single-row view of *this* machine when the pool DB is not present."""
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):
        load1 = None
    ncpu = os.cpu_count()
    machine = PoolMachine(
        machine_id=f"local:{os.uname().nodename}" if hasattr(os, "uname") else "local",
        ncpu=ncpu,
        load1=round(load1, 2) if load1 is not None else None,
        max_concurrent=0,
        in_flight=0,
        drain=False,
        fresh=True,
        last_seen_age_s=0.0,
    )
    return Compute(source="local", machines=[machine], notes=[note])


def gather_compute(repo_root: Path) -> Compute:
    """Read the pool's capacity/offloads read-only; never raise on a bad DB."""
    db = _db_path(repo_root)
    if not db.exists():
        return _local_fallback("workqueue DB absent — pool not deployed on this box")
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=RO_TIMEOUT_S)
    except sqlite3.Error as exc:
        return _local_fallback(f"workqueue DB unavailable: {exc}")
    try:
        connection.row_factory = sqlite3.Row
        return _read_pool(connection)
    except sqlite3.Error as exc:
        # Locked, mid-migration, or missing tables — the report survives it.
        return _local_fallback(f"workqueue DB unreadable: {exc}")
    finally:
        connection.close()


def _read_pool(connection: sqlite3.Connection) -> Compute:
    now = _utcnow()

    machine_rows = connection.execute(
        "SELECT machine_id, ncpu, max_concurrent, drain, last_seen_at, last_load1, labels "
        "FROM wq_machines ORDER BY machine_id ASC"
    ).fetchall()

    unit_rows = connection.execute(
        "SELECT id, state, labels, submitted_by, lease_owner, cancel_requested "
        "FROM wq_units WHERE state IN ('queued','claimed','running','review')"
    ).fetchall()

    # Per-machine in-flight from lease_owner ('<machine_id>:<worker>'); the first
    # field is the box (see store._offloads / store._in_flight).
    in_flight_by_machine: dict[str, int] = {}
    for row in unit_rows:
        if str(row["state"]) in ("claimed", "running") and row["lease_owner"]:
            machine = str(row["lease_owner"]).split(":", 1)[0]
            in_flight_by_machine[machine] = in_flight_by_machine.get(machine, 0) + 1

    machines: list[PoolMachine] = []
    total_slots = 0
    capable: list[set[str]] = []  # label-sets of non-drain boxes (unclaimable check)
    for row in machine_rows:
        machine_id = str(row["machine_id"])
        labels = _labels(row["labels"])
        drain = int(row["drain"] or 0) == 1
        age = (
            None
            if row["last_seen_at"] is None
            else max(0.0, now - _parse_iso(str(row["last_seen_at"])))
        )
        fresh = age is not None and age <= FRESH_SECONDS
        max_concurrent = int(row["max_concurrent"] or 0)
        if not drain:
            total_slots += max_concurrent
            capable.append(set(labels))
        machines.append(
            PoolMachine(
                machine_id=machine_id,
                ncpu=None if row["ncpu"] is None else int(row["ncpu"]),
                load1=None if row["last_load1"] is None else float(row["last_load1"]),
                max_concurrent=max_concurrent,
                in_flight=in_flight_by_machine.get(machine_id, 0),
                drain=drain,
                fresh=fresh,
                last_seen_age_s=age,
                labels=labels,
            )
        )

    queued = running = in_review = 0
    queued_labels: list[list[str]] = []
    offloads: dict[str, PersonOffload] = {}
    for row in unit_rows:
        state = str(row["state"])
        person = str(row["submitted_by"] or "").strip() or "(unattributed)"
        entry = offloads.setdefault(person, PersonOffload(person=person))
        if state == "queued":
            if int(row["cancel_requested"] or 0) == 1:
                continue  # a cancel-requested unit is not live offloaded work
            queued += 1
            queued_labels.append(_labels(row["labels"]))
            entry.queued += 1
        elif state in ("claimed", "running"):
            running += 1
            entry.running += 1
        elif state == "review":
            in_review += 1
            entry.in_review += 1
        owner = row["lease_owner"]
        if owner:
            machine = str(owner).split(":", 1)[0]
            if machine and machine not in entry.machines:
                entry.machines.append(machine)
    for entry in offloads.values():
        entry.machines.sort()

    # Unclaimable: a queued unit whose non-empty label-set no live box can cover
    # (mirrors store.status()).
    unclaimable: list[dict[str, Any]] = []
    for labels in queued_labels:
        wanted = set(labels)
        if not wanted:
            continue
        if not any(wanted <= declared for declared in capable):
            unclaimable.append({"labels": sorted(wanted), "reason": "unclaimable"})

    in_flight = running
    return Compute(
        source="pool",
        machines=machines,
        offloads=[offloads[p] for p in sorted(offloads) if offloads[p].total > 0],
        unclaimable=unclaimable,
        queued=queued,
        running=running,
        in_review=in_review,
        total_slots=total_slots,
        free_slots=max(0, total_slots - in_flight),
        in_flight=in_flight,
        queued_labels=queued_labels,
    )


# --------------------------------------------------------------------------
# the one suggestion (first-match, single line)
# --------------------------------------------------------------------------


def suggestion(compute: Compute, bottleneck: str) -> str:
    """First-match single-line balancer suggestion (tracker bottleneck style)."""
    reason = (bottleneck or "").lower()
    free = compute.free_slots
    if free > 0 and (
        "proposal" in reason or "no active session" in reason or "no session" in reason
    ):
        return (
            "enqueue proposal-generation (free pool slots idle, proposals/sessions the bottleneck)"
        )
    if free > 0 and ("gate" in reason or "merge" in reason):
        return "enqueue gate runs (free pool slots idle while the merge gate is the bottleneck)"
    if compute.unclaimable:
        missing = compute.missing_labels()
        label_text = ", ".join(missing) if missing else "the requested labels"
        return f"enroll a machine advertising {label_text} (queued units no box can claim)"
    if free == 0 and compute.queued > 0 and compute.total_slots > 0:
        return f"enroll a machine / raise max_concurrent ({compute.queued} queued, pool saturated)"
    return "balanced"


# --------------------------------------------------------------------------
# tracker seam: compact lines for Overall.notes (text + styled context)
# --------------------------------------------------------------------------


def compute_notes(compute: Compute, bottleneck: str, *, max_offloads: int = 8) -> list[str]:
    """Compute lines for the tracker's ``Overall.notes`` seam (both surfaces)."""
    lines: list[str] = []
    if compute.source == "local":
        machine = compute.machines[0] if compute.machines else None
        load = f"{machine.load1}" if machine and machine.load1 is not None else "?"
        cpu = f"{machine.ncpu}" if machine and machine.ncpu is not None else "?"
        lines.append(f"COMPUTE — pool not deployed here (local load1 {load} / {cpu} cpu)")
        return lines
    # No capacity summary line for a healthy pool: #342's capacity block
    # (gather_capacity + append_capacity) is the single authoritative summary —
    # a second one from this path can disagree with it (review finding, #355).
    ranked = sorted(compute.offloads, key=lambda o: (-o.total, o.person))
    for offload in ranked[:max_offloads]:
        parts = []
        if offload.queued:
            parts.append(f"{offload.queued} queued")
        if offload.running:
            parts.append(f"{offload.running} running")
        if offload.in_review:
            parts.append(f"{offload.in_review} in-review")
        where = f" on {', '.join(offload.machines)}" if offload.machines else ""
        lines.append(f"offload {offload.person} — {' / '.join(parts)}{where}")
    if len(ranked) > max_offloads:
        lines.append(f"offload … {len(ranked) - max_offloads} more people")
    lines.append(f"suggestion — {suggestion(compute, bottleneck)}")
    return lines


# --------------------------------------------------------------------------
# pool-health alerts
# --------------------------------------------------------------------------


@dataclass
class PoolAlert:
    """A detected pool-health problem: informational post + (maybe) a decision."""

    kind: str  # 'box_down' | 'capacity_exhausted' | 'starving'
    color: str  # RED | AMBER
    text: str
    remediation: str  # the consequential action a confirmed decision authorizes
    machine_id: str | None = None

    def dedup_key(self) -> str:
        return f"balancer:{self.kind}:{self.machine_id or '-'}"


def detect_pool_alerts(compute: Compute) -> list[PoolAlert]:
    """Detect box-down, capacity-exhausted, and queue-starvation conditions."""
    alerts: list[PoolAlert] = []
    if compute.source != "pool":
        return alerts

    for machine in compute.down_machines():
        age_min = int((machine.last_seen_age_s or 0) // 60)
        alerts.append(
            PoolAlert(
                kind="box_down",
                color=RED,
                text=(
                    f"🔴 Pool: {machine.machine_id} last beat {age_min}m ago (>10m) — "
                    f"box may be DOWN ({machine.in_flight} unit(s) were in flight)."
                ),
                remediation=f"drain {machine.machine_id} and re-route its in-flight work",
                machine_id=machine.machine_id,
            )
        )

    if compute.free_slots == 0 and compute.queued > 0 and compute.total_slots > 0:
        exhausted_color = RED if compute.queued >= compute.total_slots else AMBER
        alerts.append(
            PoolAlert(
                kind="capacity_exhausted",
                color=exhausted_color,
                text=(
                    f"{'🔴' if exhausted_color == RED else '🟠'} Pool: capacity exhausted — "
                    f"0 free slots of {compute.total_slots} with {compute.queued} queued and climbing."
                ),
                remediation="enroll a machine or raise max_concurrent to clear the backlog",
            )
        )

    for machine in compute.starving_machines():
        alerts.append(
            PoolAlert(
                kind="starving",
                color=AMBER,
                text=(
                    f"🟠 Pool: {machine.machine_id} has {machine.free_slots} free slot(s) but "
                    f"queued work it could claim is not being placed — starvation."
                ),
                remediation=f"re-route: investigate why {machine.machine_id} is not claiming queued units",
                machine_id=machine.machine_id,
            )
        )
    return alerts


def post_alerts(alerts: list[PoolAlert], notifier: Any) -> int:
    """Post each detected alert to the team channel, colored. Returns count posted."""
    posted = 0
    for alert in alerts:
        if notifier.post_channel(alert.text, color=alert.color):
            posted += 1
    return posted


# --------------------------------------------------------------------------
# consequential remediations → numbered 'balancer' decisions (confirm-first)
# --------------------------------------------------------------------------


def register_balancer_proposals(repo_root: Path, alerts: list[PoolAlert]) -> list[dict[str, Any]]:
    """Turn consequential alerts into numbered pending ``balancer`` decisions.

    Shares the one repo-wide number space (:mod:`decisions`), deduplicates on
    ``(kind, machine)``, and persists before returning. Nothing executes here —
    a roster human authorizes each with ``N yes`` and the decisions processor
    dispatches the ``balancer`` kind (confirm-first, never full-auto).

    The whole load->allocate->save cycle runs inside
    :func:`decisions._state_lock` — the shared-namespace lock that
    ``register_repair_proposals`` takes for exactly this registrar — so a
    concurrent repair registration can never read the same stale
    ``next_number`` or lose a registration to a last-write-wins save.
    """
    with _state_lock(repo_root):
        state = load_state(repo_root)
        _expire(state)
        for alert in alerts:
            dedup_key = alert.dedup_key()
            if _recent_same_kind(state, dedup_key):
                continue
            number = int(state["next_number"])
            state["next_number"] = number + 1
            state["decisions"].append(
                {
                    "number": number,
                    "kind": "balancer",
                    "dedup_key": dedup_key,
                    "title": f"{alert.remediation}",
                    "payload": {
                        "alert_kind": alert.kind,
                        "machine_id": alert.machine_id,
                        "remediation": alert.remediation,
                        "symptom": alert.text,
                    },
                    "status": "pending",
                    "proposed_at": _iso(_utcnow()),
                    "decided_by": None,
                    "decided_at": None,
                    "result": None,
                }
            )
        save_state(repo_root, state)
        return [
            decision
            for decision in state["decisions"]
            if decision.get("status") == "pending" and decision.get("kind") == "balancer"
        ]


def render_balancer_proposals(pending: list[dict[str, Any]]) -> str:
    """Render open balancer decisions as numbered, reply-here lines."""
    if not pending:
        return ""
    lines = [
        "⚖️ Balancer actions — reply IN THIS CHANNEL (not in a thread) with the "
        "number and yes/no (e.g. `1 yes`); confirm-first, the first reply decides:"
    ]
    lines.extend(f"balancer {decision['number']}. {decision['title']}" for decision in pending)
    return "\n".join(lines)
