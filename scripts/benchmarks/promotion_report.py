"""Deterministic, offline Phase-2 dark features promotion-evidence report.

This script evaluates four Phase-2 dark features by reading telemetry from sqlite tables,
lease ledgers, and toolplane observations, and grades each promotion threshold.
INSUFFICIENT EVIDENCE is treated as a first-class verdict when required telemetry is missing.

CLI Usage:
    uv run python -m scripts.benchmarks.promotion_report
    uv run python -m scripts.benchmarks.promotion_report --format json --out report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omniagentos.contracts import default_db_path, default_ledger_dir, utc_now_iso

REPORT_VERSION = "1"
MET = "MET"
NOT_MET = "NOT MET"
INSUFFICIENT = "INSUFFICIENT EVIDENCE"
DEFAULT_MIN_SAMPLES = 20
OBSERVATIONS_SUBDIR = "toolplane-observations"
DENIAL_ERRORS = frozenset(
    {
        "out_of_scope",
        "not_allowed",
        "secret_path",
        "broker_denied",
        "unknown_capability",
    }
)
DISCLOSURE_ERRORS = frozenset({"out_of_scope", "unknown_capability"})


@dataclass
class Threshold:
    id: str
    description: str
    target: str
    status: str = INSUFFICIENT
    measured: float | None = None
    basis: str = ""
    missing: list[str] = field(default_factory=list)
    group: str = "all_of"  # "all_of" | "any_of"

    def to_dict(self) -> dict[str, Any]:
        """Convert threshold to dictionary with keys in a deterministic order."""
        return {
            "id": self.id,
            "description": self.description,
            "target": self.target,
            "status": self.status,
            "measured": self.measured,
            "basis": self.basis,
            "missing": self.missing,
            "group": self.group,
        }


def _round(value: float | None, places: int = 4) -> float | None:
    """Pass None through; otherwise round value to specified decimal places."""
    if value is None:
        return None
    return round(float(value), places)


def _window(timestamps: list[str]) -> dict[str, str | None]:
    """Return the min and max timestamps as a window dictionary."""
    valid_ts = [ts for ts in timestamps if ts]
    if not valid_ts:
        return {"first": None, "last": None}
    return {"first": min(valid_ts), "last": max(valid_ts)}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """Check if a table exists in the SQLite database."""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
    return cursor.fetchone() is not None


def _connect(db_path: str | Path | None) -> sqlite3.Connection | None:
    """Connect to a SQLite database in read-only mode."""
    if db_path is None:
        return None
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        abs_path = path.resolve()
        conn = sqlite3.connect(f"file:{abs_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _rate(numerator: int, denominator: int) -> float | None:
    """Return numerator / denominator or None if denominator is zero."""
    if denominator == 0:
        return None
    return numerator / denominator


def read_task_shape_evidence(conn: sqlite3.Connection | None) -> dict[str, Any]:
    """Read and summarize evidence from task_shape_decisions table."""
    default_res = {
        "available": False,
        "rows": 0,
        "window": {"first": None, "last": None},
        "reason": "task_shape_decisions table not found",
        "applied": 0,
        "shadow_only": 0,
        "routes": {},
        "topologies": {},
        "sequential_rows": 0,
        "sequential_multi_worker": 0,
        "mean_latency_ms": None,
        "mean_confidence": None,
        "decisions": [],
    }
    if conn is None:
        return default_res
    try:
        if not _table_exists(conn, "task_shape_decisions"):
            return default_res

        cursor = conn.cursor()
        cursor.execute("SELECT * FROM task_shape_decisions")
        rows = cursor.fetchall()
    except sqlite3.Error as e:
        default_res["reason"] = f"Database error: {e}"
        return default_res

    total_rows = len(rows)
    created_ats = []
    applied_count = 0
    routes_counter: Counter[str] = Counter()
    topologies_counter: Counter[str] = Counter()
    sequential_rows = 0
    sequential_multi_worker = 0
    latency_ms_list = []
    confidence_list = []
    decisions_list = []

    for r in rows:
        created_at = r["created_at"]
        if created_at:
            created_ats.append(str(created_at))

        applied_val = r["applied"]
        if applied_val == 1:
            applied_count += 1

        route_val = r["route"]
        if route_val is not None:
            routes_counter[str(route_val)] += 1
        else:
            routes_counter["unknown"] += 1

        top_val = r["topology"]
        if top_val is not None:
            top_str = str(top_val)
        else:
            top_str = "unknown"
        topologies_counter[top_str] += 1

        worker_count = r["worker_count"]
        if top_str == "sequential":
            sequential_rows += 1
            w_cnt = worker_count if worker_count is not None else 0
            if w_cnt > 1:
                sequential_multi_worker += 1

        lat = r["latency_ms"]
        if lat is not None:
            latency_ms_list.append(float(lat))

        conf = r["confidence"]
        if conf is not None:
            confidence_list.append(float(conf))

        decisions_list.append(
            {
                "board_task_id": r["board_task_id"] if r["board_task_id"] is not None else None,
                "applied": 1 if applied_val == 1 else 0,
                "task_class": r["task_class"] if r["task_class"] is not None else None,
            }
        )

    mean_lat = sum(latency_ms_list) / len(latency_ms_list) if latency_ms_list else None
    mean_conf = sum(confidence_list) / len(confidence_list) if confidence_list else None

    return {
        "available": True,
        "rows": total_rows,
        "window": _window(created_ats),
        "applied": applied_count,
        "shadow_only": total_rows - applied_count,
        "routes": {k: routes_counter[k] for k in sorted(routes_counter.keys())},
        "topologies": {k: topologies_counter[k] for k in sorted(topologies_counter.keys())},
        "sequential_rows": sequential_rows,
        "sequential_multi_worker": sequential_multi_worker,
        "mean_latency_ms": _round(mean_lat),
        "mean_confidence": _round(mean_conf),
        "decisions": decisions_list,
    }


def read_formation_evidence(conn: sqlite3.Connection | None) -> dict[str, Any]:
    """Read and summarize evidence from formation_selections table."""
    default_res = {
        "available": False,
        "rows": 0,
        "window": {"first": None, "last": None},
        "reason": "formation_selections table not found",
        "outcomes": {},
        "accepted": 0,
        "rejected": 0,
        "terminal": 0,
        "accepted_rate": None,
        "mean_wall_clock_s": None,
        "task_ids": [],
        "task_ids_truncated": False,
        "task_outcomes_data": {},
    }
    if conn is None:
        return default_res
    try:
        if not _table_exists(conn, "formation_selections"):
            return default_res

        cursor = conn.cursor()
        cursor.execute("SELECT * FROM formation_selections")
        rows = cursor.fetchall()
    except sqlite3.Error as e:
        default_res["reason"] = f"Database error: {e}"
        return default_res

    total_rows = len(rows)
    created_ats = []
    outcomes_counter: Counter[str] = Counter()
    accepted_count = 0
    rejected_count = 0
    wall_clocks = []
    terminal_task_ids = set()
    task_outcomes_data = {}

    for r in rows:
        created_at = r["created_at"]
        if created_at:
            created_ats.append(str(created_at))

        outcome = r["outcome"]
        if outcome is not None:
            outcome_str = str(outcome)
        else:
            outcome_str = "unknown"
        outcomes_counter[outcome_str] += 1

        if outcome_str == "accepted":
            accepted_count += 1
        elif outcome_str == "rejected":
            rejected_count += 1

        wall_clock = r["wall_clock_s"]
        if wall_clock is not None:
            wall_clocks.append(float(wall_clock))

        task_id = r["task_id"]
        if outcome_str in ("accepted", "rejected") and task_id is not None:
            terminal_task_ids.add(str(task_id))
            task_outcomes_data[str(task_id)] = {
                "outcome": outcome_str,
                "wall_clock_s": float(wall_clock) if wall_clock is not None else None,
            }

    terminal_count = accepted_count + rejected_count
    accepted_rate = _round(_rate(accepted_count, terminal_count))
    mean_wall_clock = sum(wall_clocks) / len(wall_clocks) if wall_clocks else None

    sorted_terminal_task_ids = sorted(terminal_task_ids)
    truncated = False
    if len(sorted_terminal_task_ids) > 200:
        sorted_terminal_task_ids = sorted_terminal_task_ids[:200]
        truncated = True

    return {
        "available": True,
        "rows": total_rows,
        "window": _window(created_ats),
        "outcomes": {k: outcomes_counter[k] for k in sorted(outcomes_counter.keys())},
        "accepted": accepted_count,
        "rejected": rejected_count,
        "terminal": terminal_count,
        "accepted_rate": accepted_rate,
        "mean_wall_clock_s": _round(mean_wall_clock),
        "task_ids": sorted_terminal_task_ids,
        "task_ids_truncated": truncated,
        "task_outcomes_data": task_outcomes_data,
    }


def read_lease_evidence(ledger_dir: str | Path) -> dict[str, Any]:
    """Read and summarize lease records from jsonl ledger files."""
    if not ledger_dir:
        ledger_dir = ""
    path = Path(ledger_dir)
    default_res = {
        "available": False,
        "files": [],
        "records": 0,
        "malformed": 0,
        "window": {"first": None, "last": None},
        "reason": f"no leases-*.jsonl under {ledger_dir}",
        "events": {},
        "modes": {},
        "by_mode_event": {},
        "refused": 0,
        "issued": 0,
        "launched": 0,
        "refusal_reasons": {},
        "escapes": 0,
        "net_policies": {},
    }

    if not path.is_dir():
        return default_res

    try:
        files = sorted(list(path.glob("leases-*.jsonl")))
    except OSError:
        return default_res

    if not files:
        return default_res

    records_count = 0
    malformed_count = 0
    recorded_ats = []
    events_counter: Counter[str] = Counter()
    modes_counter: Counter[str] = Counter()
    by_mode_event_counter: Counter[str] = Counter()
    refused_count = 0
    issued_count = 0
    launched_count = 0
    refusal_reasons_counter: Counter[str] = Counter()
    escapes_count = 0
    net_policies_counter: Counter[str] = Counter()

    for f in files:
        try:
            with open(f, encoding="utf-8") as file_handle:
                for line in file_handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            malformed_count += 1
                            continue
                    except json.JSONDecodeError:
                        malformed_count += 1
                        continue

                    records_count += 1

                    recorded_at = record.get("recorded_at")
                    if recorded_at:
                        recorded_ats.append(str(recorded_at))

                    event = record.get("event")
                    if event:
                        event_str = str(event)
                        events_counter[event_str] += 1
                        if event_str == "refused":
                            refused_count += 1
                        elif event_str == "issued":
                            issued_count += 1
                        elif event_str == "launched":
                            launched_count += 1

                    mode = record.get("mode")
                    if mode:
                        mode_str = str(mode)
                        modes_counter[mode_str] += 1

                    if mode and event:
                        by_mode_event_counter[f"{mode}:{event}"] += 1

                    if mode == "enforce" and event in ("issued", "launched"):
                        if record.get("signed") is False or record.get("enforced") is False:
                            escapes_count += 1

                    if event == "refused":
                        reason = record.get("reason")
                        if reason is not None:
                            refusal_reasons_counter[str(reason)] += 1

                    net_policy = record.get("net_policy")
                    if net_policy is not None:
                        net_policies_counter[str(net_policy)] += 1
                    else:
                        net_policies_counter["unknown"] += 1
        except OSError:
            pass

    return {
        "available": True,
        "files": [f.name for f in files],
        "records": records_count,
        "malformed": malformed_count,
        "window": _window(recorded_ats),
        "events": {k: events_counter[k] for k in sorted(events_counter.keys())},
        "modes": {k: modes_counter[k] for k in sorted(modes_counter.keys())},
        "by_mode_event": {
            k: by_mode_event_counter[k] for k in sorted(by_mode_event_counter.keys())
        },
        "refused": refused_count,
        "issued": issued_count,
        "launched": launched_count,
        "refusal_reasons": {
            k: refusal_reasons_counter[k] for k in sorted(refusal_reasons_counter.keys())
        },
        "escapes": escapes_count,
        "net_policies": {k: net_policies_counter[k] for k in sorted(net_policies_counter.keys())},
    }


def read_toolplane_evidence(observations_dir: str | Path) -> dict[str, Any]:
    """Read and summarize toolplane observations from json files."""
    if not observations_dir:
        observations_dir = ""
    path = Path(observations_dir)
    default_res = {
        "available": False,
        "records": 0,
        "malformed": 0,
        "window": {"first": None, "last": None},
        "reason": f"no observation records under {observations_dir}",
        "statuses": {},
        "tools": {},
        "errors": {},
        "denied": 0,
        "failed": 0,
        "success": 0,
        "unauthorized_disclosures": 0,
        "mean_duration_ms": None,
        "total_duration_ms": 0,
        "sessions": 0,
    }

    if not path.is_dir():
        return default_res

    try:
        files = sorted(list(path.glob("*.json")))
    except OSError:
        return default_res

    if not files:
        return default_res

    records_count = 0
    malformed_count = 0
    tss = []
    statuses_counter: Counter[str] = Counter()
    tools_counter: Counter[str] = Counter()
    errors_counter: Counter[str] = Counter()
    denied_count = 0
    failed_count = 0
    success_count = 0
    unauthorized_disclosures_count = 0
    duration_ms_list = []
    sessions_set = set()

    for f in files:
        try:
            with open(f, encoding="utf-8") as file_handle:
                content = file_handle.read().strip()
                if not content:
                    continue
                try:
                    record = json.loads(content)
                    if not isinstance(record, dict):
                        malformed_count += 1
                        continue
                except json.JSONDecodeError:
                    malformed_count += 1
                    continue

                records_count += 1

                ts = record.get("ts")
                if ts:
                    tss.append(str(ts))

                status = record.get("status")
                if status:
                    status_str = str(status)
                    statuses_counter[status_str] += 1
                    if status_str == "denied":
                        denied_count += 1
                    elif status_str == "failed":
                        failed_count += 1
                    elif status_str == "success":
                        success_count += 1

                tool = record.get("tool")
                if tool:
                    tools_counter[str(tool)] += 1

                error = record.get("error")
                if error is not None:
                    errors_counter[str(error)] += 1

                if status == "denied" and error in DISCLOSURE_ERRORS:
                    unauthorized_disclosures_count += 1

                dur = record.get("duration_ms")
                if dur is not None:
                    duration_ms_list.append(float(dur))

                session_id = record.get("session_id")
                if session_id is not None:
                    sessions_set.add(str(session_id))
        except OSError:
            pass

    mean_duration = sum(duration_ms_list) / len(duration_ms_list) if duration_ms_list else None
    total_duration = sum(duration_ms_list)

    return {
        "available": True,
        "records": records_count,
        "malformed": malformed_count,
        "window": _window(tss),
        "statuses": {k: statuses_counter[k] for k in sorted(statuses_counter.keys())},
        "tools": {k: tools_counter[k] for k in sorted(tools_counter.keys())},
        "errors": {k: errors_counter[k] for k in sorted(errors_counter.keys())},
        "denied": denied_count,
        "failed": failed_count,
        "success": success_count,
        "unauthorized_disclosures": unauthorized_disclosures_count,
        "mean_duration_ms": _round(mean_duration),
        "total_duration_ms": total_duration,
        "sessions": len(sessions_set),
    }


def evaluate_task_shape(
    shape: dict[str, Any], formation: dict[str, Any], *, min_samples: int
) -> list[Threshold]:
    """Evaluate thresholds for task-shape routing feature."""
    thresholds = []

    # 1. accepted_rate_delta
    t1 = Threshold(
        id="accepted_rate_delta",
        description="Routed work is accepted more often than unrouted work",
        target=">= +5.0pp accepted-rate vs unrouted control",
        group="any_of",
    )
    missing_t1 = []
    if not shape.get("available", False):
        missing_t1.append("task_shape_decisions: source unavailable")
    elif shape.get("rows", 0) < min_samples:
        missing_t1.append(f"task_shape_decisions: {shape['rows']} rows < min_samples {min_samples}")

    if not formation.get("available", False):
        missing_t1.append("formation_selections: source unavailable")
    elif formation.get("terminal", 0) < min_samples:
        missing_t1.append(
            f"formation_selections: {formation['terminal']} rows < min_samples {min_samples}"
        )

    if missing_t1:
        t1.status = INSUFFICIENT
        t1.missing = missing_t1
        t1.basis = "Insufficient evidence to compute accepted-rate delta."
    else:
        if shape.get("applied", 0) == 0:
            t1.status = INSUFFICIENT
            t1.measured = None
            t1.missing = [
                "no task_shape_decisions row has applied=1; shadow-mode evidence has no "
                "routed arm to compare against the control"
            ]
            t1.basis = (
                "Every recorded decision is shadow-only, so routed and unrouted "
                "accepted-rates are the same population."
            )
        else:
            decisions = shape.get("decisions", [])
            routed_ids = {
                d["board_task_id"]
                for d in decisions
                if d["applied"] == 1 and d["board_task_id"] is not None
            }
            control_ids = {
                d["board_task_id"]
                for d in decisions
                if d["applied"] == 0 and d["board_task_id"] is not None
            }

            task_outcomes_data = formation.get("task_outcomes_data", {})
            routed_terminal_rows = [
                task_outcomes_data[tid] for tid in routed_ids if tid in task_outcomes_data
            ]
            control_terminal_rows = [
                task_outcomes_data[tid] for tid in control_ids if tid in task_outcomes_data
            ]

            if len(routed_terminal_rows) == 0 or len(control_terminal_rows) == 0:
                t1.status = INSUFFICIENT
                t1.measured = None
                t1.missing = [
                    "task_shape_decisions.board_task_id does not join any "
                    "formation_selections.task_id; the two telemetry streams share no "
                    "key in this window"
                ]
                t1.basis = (
                    "The keys in the task shape and formation selections tables do "
                    "not overlap, so no comparison is possible."
                )
            else:
                routed_accepted = sum(1 for r in routed_terminal_rows if r["outcome"] == "accepted")
                routed_rate = routed_accepted / len(routed_terminal_rows)
                control_accepted = sum(
                    1 for r in control_terminal_rows if r["outcome"] == "accepted"
                )
                control_rate = control_accepted / len(control_terminal_rows)

                measured_val = (routed_rate - control_rate) * 100.0
                t1.measured = _round(measured_val, 2)
                if t1.measured is not None and t1.measured >= 5.0:
                    t1.status = MET
                else:
                    t1.status = NOT_MET
                t1.basis = (
                    f"Measured routed accepted-rate of {routed_rate * 100:.1f}% "
                    f"({routed_accepted}/{len(routed_terminal_rows)}) vs control "
                    f"accepted-rate of {control_rate * 100:.1f}% "
                    f"({control_accepted}/{len(control_terminal_rows)}), "
                    f"a delta of {t1.measured:+.2f}pp."
                )

    thresholds.append(t1)

    # 2. in_class_speedup
    t2 = Threshold(
        id="in_class_speedup",
        description="Routed work finishes faster within the same task class",
        target=">= 20% lower mean wall_clock_s in-class",
        group="any_of",
    )
    missing_t2 = []
    if not shape.get("available", False):
        missing_t2.append("task_shape_decisions: source unavailable")
    elif shape.get("rows", 0) < min_samples:
        missing_t2.append(f"task_shape_decisions: {shape['rows']} rows < min_samples {min_samples}")

    if not formation.get("available", False):
        missing_t2.append("formation_selections: source unavailable")
    elif formation.get("terminal", 0) < min_samples:
        missing_t2.append(
            f"formation_selections: {formation['terminal']} rows < min_samples {min_samples}"
        )

    if missing_t2:
        t2.status = INSUFFICIENT
        t2.missing = missing_t2
        t2.basis = "Insufficient evidence to compute speedup."
    else:
        if shape.get("applied", 0) == 0:
            t2.status = INSUFFICIENT
            t2.measured = None
            t2.missing = [
                "no applied=1 decisions; no routed arm to time against the control",
                "formation_selections.wall_clock_s is not keyed by "
                "task_shape_decisions.task_class; in-class pairing needs a shared "
                "class label on the outcome row",
            ]
            t2.basis = (
                "Every recorded decision is shadow-only, so no routed speedup can be measured."
            )
        else:
            decisions = shape.get("decisions", [])
            routed_ids = {
                d["board_task_id"]
                for d in decisions
                if d["applied"] == 1 and d["board_task_id"] is not None
            }
            control_ids = {
                d["board_task_id"]
                for d in decisions
                if d["applied"] == 0 and d["board_task_id"] is not None
            }

            task_outcomes_data = formation.get("task_outcomes_data", {})
            routed_terminal_rows = [
                task_outcomes_data[tid] for tid in routed_ids if tid in task_outcomes_data
            ]
            control_terminal_rows = [
                task_outcomes_data[tid] for tid in control_ids if tid in task_outcomes_data
            ]

            routed_walls = [
                r["wall_clock_s"] for r in routed_terminal_rows if r["wall_clock_s"] is not None
            ]
            control_walls = [
                r["wall_clock_s"] for r in control_terminal_rows if r["wall_clock_s"] is not None
            ]

            if len(routed_walls) == 0 or len(control_walls) == 0:
                t2.status = INSUFFICIENT
                t2.measured = None
                t2.missing = [
                    "no wall_clock_s values found to compute speedup for routed or control set"
                ]
                t2.basis = (
                    "Missing wall_clock_s measurements in one or both of the merged key sets."
                )
            else:
                routed_mean = sum(routed_walls) / len(routed_walls)
                control_mean = sum(control_walls) / len(control_walls)
                if control_mean == 0:
                    t2.status = INSUFFICIENT
                    t2.measured = None
                    t2.missing = ["control mean wall_clock_s is zero"]
                    t2.basis = "Control wall_clock_s mean is zero; division by zero prevented."
                else:
                    measured_val = (1.0 - routed_mean / control_mean) * 100.0
                    t2.measured = _round(measured_val, 2)
                    if t2.measured is not None and t2.measured >= 20.0:
                        t2.status = MET
                    else:
                        t2.status = NOT_MET
                    t2.basis = (
                        f"Routed mean wall clock of {routed_mean:.2f}s vs control mean "
                        f"of {control_mean:.2f}s, a speedup of {t2.measured:.2f}%."
                    )

    thresholds.append(t2)

    # 3. no_unnecessary_workers
    t3 = Threshold(
        id="no_unnecessary_workers",
        description="Sequential topologies never provision more than one worker",
        target="0 sequential decisions with worker_count > 1",
        group="all_of",
    )
    missing_t3 = []
    if not shape.get("available", False):
        missing_t3.append("task_shape_decisions: source unavailable")
    elif shape.get("rows", 0) < min_samples:
        missing_t3.append(f"task_shape_decisions: {shape['rows']} rows < min_samples {min_samples}")

    if missing_t3:
        t3.status = INSUFFICIENT
        t3.missing = missing_t3
        t3.basis = "Insufficient evidence to verify sequential worker counts."
    else:
        if shape.get("sequential_rows", 0) == 0:
            t3.status = INSUFFICIENT
            t3.missing = ["no sequential-topology decisions recorded"]
            t3.basis = "No sequential topology decisions were found to evaluate."
        else:
            measured_val = float(shape.get("sequential_multi_worker", 0))
            t3.measured = measured_val
            if measured_val == 0.0:
                t3.status = MET
            else:
                t3.status = NOT_MET
            t3.basis = (
                f"Found {int(measured_val)} sequential decisions with worker_count > 1 "
                f"out of {shape['sequential_rows']} sequential decisions."
            )

    thresholds.append(t3)

    return thresholds


def evaluate_tool_disclosure(tools: dict[str, Any], *, min_samples: int) -> list[Threshold]:
    """Evaluate thresholds for tool disclosure feature."""
    thresholds = []

    # 1. initial_schema_token_reduction
    t1 = Threshold(
        id="initial_schema_token_reduction",
        description="Deferred catalog cuts the tool schema tokens loaded up front",
        target=">= 70% fewer initial schema tokens",
        group="all_of",
        status=INSUFFICIENT,
        measured=None,
        missing=[
            "toolplane observations record no token counts (metadata-only by design); "
            "needs a per-session initial-schema token measurement from "
            "omniagentos/toolplane/exposure.py captured under catalog off vs shadow"
        ],
        basis="Token accounting is absent from the only durable toolplane telemetry, so no reduction can be computed.",
    )
    thresholds.append(t1)

    # 2. selection_parity
    t2 = Threshold(
        id="selection_parity",
        description="Deferred search picks the same tool the full catalog would have",
        target="selection accuracy within 2pp of the full-catalog control",
        group="all_of",
        status=INSUFFICIENT,
        measured=None,
        missing=[
            "observations carry no ground-truth 'tool the full catalog would have selected'; "
            "needs a paired off/shadow replay labelled per call"
        ],
        basis="No ground truth is recorded in the live telemetry stream to determine selection parity.",
    )
    thresholds.append(t2)

    # 3. zero_unauthorized_disclosure
    t3 = Threshold(
        id="zero_unauthorized_disclosure",
        description="No tool outside the exposed manifest is ever reachable",
        target="0 denials caused by an out-of-scope or unknown capability",
        group="all_of",
    )
    missing_t3 = []
    if not tools.get("available", False):
        missing_t3.append("toolplane_observations: source unavailable")
    elif tools.get("records", 0) < min_samples:
        missing_t3.append(
            f"toolplane_observations: {tools['records']} records < min_samples {min_samples}"
        )

    if missing_t3:
        t3.status = INSUFFICIENT
        t3.missing = missing_t3
        t3.basis = "Insufficient evidence to verify unauthorized disclosure."
    else:
        measured_val = float(tools.get("unauthorized_disclosures", 0))
        t3.measured = measured_val
        if measured_val == 0.0:
            t3.status = MET
        else:
            t3.status = NOT_MET
        t3.basis = (
            f"Counts denied observations whose error is in {sorted(list(DISCLOSURE_ERRORS))} "
            f"as a proxy for disclosure leakage. Found {int(measured_val)} such leakage event(s) "
            f"across {tools.get('records', 0)} toolplane observation record(s)."
        )

    thresholds.append(t3)

    return thresholds


def evaluate_resource_aware(tools: dict[str, Any], *, min_samples: int) -> list[Threshold]:
    """Evaluate thresholds for resource-aware execution feature."""
    thresholds = []

    # 1. wall_reduction
    t1 = Threshold(
        id="wall_reduction",
        description="Scheduling admission reduces wall time",
        target=">= 20% lower wall time with the scheduler on",
        group="any_of",
        status=INSUFFICIENT,
        measured=None,
        missing=[
            "observation records carry no tool_scheduler mode field, so scheduler-on and "
            "scheduler-off calls cannot be separated",
            "no paired scheduler-on/scheduler-off capture exists",
        ],
    )
    if tools.get("available", False):
        records = tools.get("records", 0)
        total_duration_ms = tools.get("total_duration_ms", 0)
        mean_duration_ms = tools.get("mean_duration_ms")
        mean_str = f"{mean_duration_ms:.2f}" if mean_duration_ms is not None else "-"
        t1.basis = (
            f"Observed {records} calls totalling {total_duration_ms}ms "
            f"(mean {mean_str}ms) with no mode label to split them."
        )
    else:
        t1.basis = "No toolplane observations available."
    thresholds.append(t1)

    # 2. token_reduction
    t2 = Threshold(
        id="token_reduction",
        description="Scheduling admission reduces token consumption",
        target=">= 20% fewer billed tokens with the scheduler on",
        group="any_of",
        status=INSUFFICIENT,
        measured=None,
        missing=[
            "toolplane observations record no token counts; billed-token deltas need "
            "run-level usage joined to a scheduler mode"
        ],
        basis="Token counts are absent from the toolplane telemetry.",
    )
    thresholds.append(t2)

    # 3. serial_equivalence
    t3 = Threshold(
        id="serial_equivalence",
        description="Scheduled execution is functionally equivalent to serial execution",
        target="scheduled waves produce results identical to serial execution",
        group="all_of",
        status=INSUFFICIENT,
        measured=None,
        missing=[
            "no serial-vs-scheduled equivalence replay is recorded; needs a paired "
            "capture asserting identical results"
        ],
        basis="No replay verification has been executed.",
    )
    thresholds.append(t3)

    return thresholds


def evaluate_lease(lease: dict[str, Any], *, min_samples: int) -> list[Threshold]:
    """Evaluate thresholds for autonomy lease feature."""
    thresholds = []

    # 1. permission_prompt_reduction
    t1 = Threshold(
        id="permission_prompt_reduction",
        description="A lease removes repeated permission prompts from long-running work",
        target=">= 50% fewer permission prompts vs unleased control",
        group="all_of",
        status=INSUFFICIENT,
        measured=None,
        missing=[
            "the lease ledger records issue/launch/refuse decisions, not human permission "
            "prompts; needs approval-event telemetry counted per run under lease off "
            "vs enforce"
        ],
    )
    if lease.get("available", False):
        issued = lease.get("issued", 0)
        launched = lease.get("launched", 0)
        refused = lease.get("refused", 0)
        modes_dict = lease.get("modes", {})
        modes_str = ", ".join(f"{k}={v}" for k, v in modes_dict.items())
        t1.basis = (
            f"Ledger holds {issued} issued / {launched} launched / {refused} refused "
            f"records across modes {{{modes_str}}}, none of which counts a prompt."
        )
    else:
        t1.basis = "No lease ledger available."
    thresholds.append(t1)

    # 2. zero_escapes
    t2 = Threshold(
        id="zero_escapes",
        description="No leased launch escapes its lease",
        target="0 enforce-mode records that are unsigned or not enforced",
        group="all_of",
    )
    missing_t2 = []
    if not lease.get("available", False):
        missing_t2.append("lease_records: source unavailable")
    elif lease.get("records", 0) < min_samples:
        missing_t2.append(f"lease_records: {lease['records']} records < min_samples {min_samples}")

    if missing_t2:
        t2.status = INSUFFICIENT
        t2.missing = missing_t2
        t2.basis = "Insufficient evidence to verify lease escapes."
    else:
        measured_val = float(lease.get("escapes", 0))
        t2.measured = measured_val
        if measured_val == 0.0:
            t2.status = MET
        else:
            t2.status = NOT_MET
        t2.basis = (
            f"Counts enforce-mode issued/launched records whose signed is False or "
            f"enforced is False. Found {int(measured_val)} such escape event(s) "
            f"across {lease.get('records', 0)} lease record(s)."
        )

    thresholds.append(t2)

    return thresholds


def _group_status(thresholds: list[Threshold]) -> str:
    """Evaluate overall status for a list of thresholds using all_of logic."""
    if not thresholds:
        return MET
    if all(t.status == MET for t in thresholds):
        return MET
    if any(t.status == NOT_MET for t in thresholds):
        return NOT_MET
    return INSUFFICIENT


def _any_of_status(thresholds: list[Threshold]) -> str:
    """Evaluate overall status for a list of thresholds using any_of logic."""
    if not thresholds:
        return MET
    if any(t.status == MET for t in thresholds):
        return MET
    if any(t.status == NOT_MET for t in thresholds) and not any(
        t.status == INSUFFICIENT for t in thresholds
    ):
        return NOT_MET
    return INSUFFICIENT


def feature_verdict(thresholds: list[Threshold], *, safety_count: int) -> str:
    """Compute verdict for a feature based on thresholds and safety count."""
    if safety_count > 0:
        return "REJECT"

    all_of_thresholds = [t for t in thresholds if t.group == "all_of"]
    any_of_thresholds = [t for t in thresholds if t.group == "any_of"]

    all_status = _group_status(all_of_thresholds)
    any_status = _any_of_status(any_of_thresholds)

    if all_status == MET and any_status == MET:
        return "PROMOTE"
    if all_status == NOT_MET or any_status == NOT_MET:
        return "HOLD"
    return "HOLD"


def build_report(
    *,
    db_path: str | None,
    ledger_dir: str,
    observations_dir: str,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    now: str,
) -> dict[str, Any]:
    """Gather all evidence, evaluate thresholds, and compile the final report."""
    db_present = False
    if db_path is not None:
        try:
            db_present = Path(db_path).is_file()
        except OSError:
            pass

    conn = None
    if db_present:
        conn = _connect(db_path)

    try:
        shape_evidence = read_task_shape_evidence(conn)
        formation_evidence = read_formation_evidence(conn)
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    lease_evidence = read_lease_evidence(ledger_dir)
    tool_evidence = read_toolplane_evidence(observations_dir)

    # Evaluate feature thresholds
    task_shape_thresholds = evaluate_task_shape(
        shape_evidence, formation_evidence, min_samples=min_samples
    )
    tool_disclosure_thresholds = evaluate_tool_disclosure(tool_evidence, min_samples=min_samples)
    resource_aware_thresholds = evaluate_resource_aware(tool_evidence, min_samples=min_samples)
    lease_thresholds = evaluate_lease(lease_evidence, min_samples=min_samples)

    # Feature safety counts
    shape_safety = shape_evidence.get("sequential_multi_worker", 0)
    tool_safety = tool_evidence.get("unauthorized_disclosures", 0)
    resource_safety = 0
    lease_safety = lease_evidence.get("escapes", 0)

    # Feature verdicts
    task_shape_verdict = feature_verdict(task_shape_thresholds, safety_count=shape_safety)
    tool_disclosure_verdict = feature_verdict(tool_disclosure_thresholds, safety_count=tool_safety)
    resource_aware_verdict = feature_verdict(
        resource_aware_thresholds, safety_count=resource_safety
    )
    lease_verdict = feature_verdict(lease_thresholds, safety_count=lease_safety)

    features = [
        {
            "feature": "task_shape_routing",
            "title": "Task-shape routing",
            "evidence_count": shape_evidence.get("rows", 0),
            "window": shape_evidence.get("window", {"first": None, "last": None}),
            "safety_count": shape_safety,
            "thresholds": [t.to_dict() for t in task_shape_thresholds],
            "verdict": task_shape_verdict,
        },
        {
            "feature": "tool_disclosure",
            "title": "Tool disclosure",
            "evidence_count": tool_evidence.get("records", 0),
            "window": tool_evidence.get("window", {"first": None, "last": None}),
            "safety_count": tool_safety,
            "thresholds": [t.to_dict() for t in tool_disclosure_thresholds],
            "verdict": tool_disclosure_verdict,
        },
        {
            "feature": "resource_aware_execution",
            "title": "Resource-aware execution",
            "evidence_count": tool_evidence.get("records", 0),
            "window": tool_evidence.get("window", {"first": None, "last": None}),
            "safety_count": resource_safety,
            "thresholds": [t.to_dict() for t in resource_aware_thresholds],
            "verdict": resource_aware_verdict,
        },
        {
            "feature": "autonomy_lease",
            "title": "Autonomy lease",
            "evidence_count": lease_evidence.get("records", 0),
            "window": lease_evidence.get("window", {"first": None, "last": None}),
            "safety_count": lease_safety,
            "thresholds": [t.to_dict() for t in lease_thresholds],
            "verdict": lease_verdict,
        },
    ]

    # Overall safety
    safety_total = shape_safety + tool_safety + lease_safety

    # Overall verdict
    if safety_total > 0:
        overall_verdict = "REJECT"
    elif all(f["verdict"] == "PROMOTE" for f in features):
        overall_verdict = "PROMOTE"
    else:
        overall_verdict = "HOLD"

    return {
        "report_version": REPORT_VERSION,
        "generated_at": now,
        "min_samples": min_samples,
        "sources": {
            "db": str(db_path) if db_path is not None else None,
            "db_present": db_present,
            "ledger_dir": str(ledger_dir),
            "observations_dir": str(observations_dir),
        },
        "evidence": {
            "task_shape_decisions": shape_evidence,
            "formation_selections": formation_evidence,
            "lease_records": lease_evidence,
            "toolplane_observations": tool_evidence,
        },
        "features": features,
        "safety": {
            "sequential_multi_worker": shape_safety,
            "unauthorized_disclosures": tool_safety,
            "lease_escapes": lease_safety,
            "total": safety_total,
        },
        "verdict": overall_verdict,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render report details into structured and deterministic markdown."""
    lines = []
    lines.append("# Phase-2 promotion report")
    lines.append("")
    lines.append(f"- Generated: {report['generated_at']}")
    lines.append(f"- Verdict: **{report['verdict']}**")
    lines.append(f"- Min samples: {report['min_samples']}")
    lines.append(f"- DB: {report['sources']['db'] if report['sources']['db'] is not None else '-'}")
    lines.append(f"- Ledger dir: {report['sources']['ledger_dir']}")
    lines.append(f"- Observations dir: {report['sources']['observations_dir']}")
    lines.append("")

    lines.append("## Safety")
    lines.append("")
    lines.append("| Count | Value |")
    lines.append("| --- | --- |")
    lines.append(f"| sequential_multi_worker | {report['safety']['sequential_multi_worker']} |")
    lines.append(f"| unauthorized_disclosures | {report['safety']['unauthorized_disclosures']} |")
    lines.append(f"| lease_escapes | {report['safety']['lease_escapes']} |")
    lines.append(f"| TOTAL | {report['safety']['total']} |")
    lines.append("")
    lines.append("Any non-zero safety count rejects promotion outright.")
    lines.append("")

    lines.append("## Evidence")
    lines.append("")
    lines.append("| Source | Available | Records | First | Last |")
    lines.append("| --- | --- | --- | --- | --- |")

    evidence = report.get("evidence", {})
    sources_to_render = [
        ("task_shape_decisions", evidence.get("task_shape_decisions", {})),
        ("formation_selections", evidence.get("formation_selections", {})),
        ("lease_records", evidence.get("lease_records", {})),
        ("toolplane_observations", evidence.get("toolplane_observations", {})),
    ]

    for name, data in sources_to_render:
        avail = "True" if data.get("available", False) else "False"
        if "rows" in data:
            recs = data.get("rows", 0)
        else:
            recs = data.get("records", 0)

        first = data.get("window", {}).get("first")
        last = data.get("window", {}).get("last")
        first_str = str(first) if first is not None else "-"
        last_str = str(last) if last is not None else "-"

        lines.append(f"| {name} | {avail} | {recs} | {first_str} | {last_str} |")
    lines.append("")

    for f in report.get("features", []):
        lines.append(f"## {f['title']}")
        lines.append("")
        lines.append(f"Verdict: **{f['verdict']}** — evidence: {f['evidence_count']} record(s)")
        lines.append("")
        lines.append("| Threshold | Target | Status | Measured |")
        lines.append("| --- | --- | --- | --- |")

        for t in f.get("thresholds", []):
            meas = t.get("measured")
            meas_str = str(meas) if meas is not None else "-"
            desc = str(t.get("description", "")).replace("\n", " ")
            targ = str(t.get("target", "")).replace("\n", " ")
            stat = str(t.get("status", "")).replace("\n", " ")
            lines.append(f"| {desc} | {targ} | {stat} | {meas_str} |")

        lines.append("")

        missing_items = []
        for t in f.get("thresholds", []):
            m_list = t.get("missing", [])
            if m_list:
                for item in m_list:
                    item_clean = str(item).replace("\n", " ")
                    missing_items.append(f"- `{t['id']}`: {item_clean}")

        if missing_items:
            lines.append("### Missing evidence")
            lines.append("")
            lines.extend(missing_items)
            lines.append("")

        basis_items = []
        for t in f.get("thresholds", []):
            b_val = t.get("basis")
            if b_val:
                b_clean = str(b_val).replace("\n", " ")
                basis_items.append(f"- `{t['id']}`: {b_clean}")

        if basis_items:
            lines.append("### Basis")
            lines.append("")
            lines.extend(basis_items)
            lines.append("")

    markdown_content = "\n".join(lines)
    cleaned_lines = [line.rstrip() for line in markdown_content.splitlines()]
    result = "\n".join(cleaned_lines)
    return result.strip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for CLI operations."""
    parser = argparse.ArgumentParser(
        prog="python -m scripts.benchmarks.promotion_report",
        description="A deterministic promotion-evidence report for the Phase-2 dark features.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to sqlite DB (defaults to default_db_path() at runtime)",
    )
    parser.add_argument(
        "--ledger-dir",
        default=None,
        help="Path to ledger directory (defaults to default_ledger_dir() at runtime)",
    )
    parser.add_argument(
        "--observations-dir", default=None, help="Path to toolplane observations directory"
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=DEFAULT_MIN_SAMPLES,
        help=f"Minimum samples required (default: {DEFAULT_MIN_SAMPLES})",
    )
    parser.add_argument(
        "--format",
        choices=["md", "json"],
        default="md",
        help="Output format: md or json (default: md)",
    )
    parser.add_argument("--out", default=None, help="Output file path (defaults to stdout)")
    parser.add_argument(
        "--now",
        default=None,
        help="Timestamp to use for generated_at (defaults to utc_now_iso())",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Main execution entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)

    db_path = args.db if args.db is not None else default_db_path()
    ledger_dir = args.ledger_dir if args.ledger_dir is not None else default_ledger_dir()

    if args.observations_dir is not None:
        observations_dir = args.observations_dir
    else:
        observations_dir = os.path.join(ledger_dir, OBSERVATIONS_SUBDIR)

    now = args.now if args.now is not None else utc_now_iso()

    report = build_report(
        db_path=db_path,
        ledger_dir=ledger_dir,
        observations_dir=observations_dir,
        min_samples=args.min_samples,
        now=now,
    )

    if args.format == "json":
        output_str = json.dumps(report, indent=2, sort_keys=True)
    else:
        output_str = render_markdown(report)

    if args.out is not None:
        out_path = Path(args.out)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(output_str)
        except OSError as e:
            print(f"Error writing to {out_path}: {e}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(output_str)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
