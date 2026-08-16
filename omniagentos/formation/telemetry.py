"""Persist formation selection + ETAR observations (Phase B6 / L5 seed)."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from omniagentos.contracts import new_id, utc_now_iso


def record_selection(
    connection: sqlite3.Connection,
    *,
    task_id: str,
    goal: str,
    arm: str,
    formation_id: str | None = None,
    confidence: float | None = None,
    low_confidence: bool = False,
    topology: str | None = None,
    implementers: list[str] | None = None,
    reviewer: str | None = None,
    planner: str | None = None,
    mechanical_gate: bool = False,
    models: dict[str, Any] | None = None,
    predicted_etar_s: float | None = None,
    etar_components: dict[str, Any] | None = None,
    outcome: str = "predicted",
    repair_loops: int = 0,
    escalations: int = 0,
    first_pass_accept: bool | None = None,
    wall_clock_s: float | None = None,
    source: str = "calibration",
    task_fingerprint: str | None = None,
) -> str:
    """Insert one row into formation_selections. Returns row id."""
    row_id = new_id("fsel")
    connection.execute(
        "INSERT INTO formation_selections ("
        "id, task_id, task_fingerprint, goal, arm, formation_id, confidence, "
        "low_confidence, topology, implementers_json, reviewer, planner, "
        "mechanical_gate, models_json, predicted_etar_s, etar_components_json, "
        "outcome, repair_loops, escalations, first_pass_accept, wall_clock_s, "
        "source, created_at"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            row_id,
            task_id,
            task_fingerprint,
            goal,
            arm,
            formation_id,
            confidence,
            1 if low_confidence else 0,
            topology,
            json.dumps(list(implementers or [])),
            reviewer,
            planner,
            1 if mechanical_gate else 0,
            json.dumps(models or {}),
            predicted_etar_s,
            json.dumps(etar_components or {}),
            outcome,
            int(repair_loops),
            int(escalations),
            None if first_pass_accept is None else (1 if first_pass_accept else 0),
            wall_clock_s,
            source,
            utc_now_iso(),
        ),
    )
    connection.commit()
    return row_id


def list_selections(
    connection: sqlite3.Connection,
    *,
    source: str | None = "calibration",
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM formation_selections"
    params: list[Any] = []
    if source is not None:
        sql += " WHERE source = ?"
        params.append(source)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    rows = connection.execute(sql, params).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for k in ("implementers_json", "models_json", "etar_components_json"):
            if d.get(k):
                try:
                    d[k.replace("_json", "")] = json.loads(d[k])
                except (TypeError, json.JSONDecodeError):
                    pass
        out.append(d)
    return out


__all__ = ["list_selections", "record_selection"]
