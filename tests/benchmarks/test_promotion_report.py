"""Unit tests for the deterministic Phase-2 promotion report.

These tests seed every evidence source into tmp_path and pin the graded output, because a
promotion report that silently degrades to "looks fine" is worse than no report.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from scripts.benchmarks.promotion_report import (
    INSUFFICIENT,
    MET,
    NOT_MET,
    Threshold,
    build_report,
    evaluate_lease,
    evaluate_tool_disclosure,
    feature_verdict,
    main,
    read_lease_evidence,
    read_toolplane_evidence,
    render_markdown,
)


def _seed_db(
    path: Path,
    *,
    shape_rows: list[dict[str, Any]],
    formation_rows: list[dict[str, Any]],
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE task_shape_decisions (
            id                      TEXT PRIMARY KEY,
            created_at              TEXT NOT NULL,
            board_task_id           TEXT,
            run_id                  TEXT,
            brief_hash              TEXT NOT NULL,
            config_version          TEXT,
            feat_d                  REAL,
            feat_i                  REAL,
            feat_s                  REAL,
            feat_u                  REAL,
            feat_v                  REAL,
            feat_g                  REAL,
            feat_c                  REAL,
            feat_m                  REAL,
            feat_r                  REAL,
            feat_k                  REAL,
            feat_w                  REAL,
            feat_p                  REAL,
            confidence              REAL,
            task_class              TEXT,
            tool_density            REAL,
            context_breadth         REAL,
            merge_cost              REAL,
            shared_state_coupling   REAL,
            route                   TEXT NOT NULL,
            topology                TEXT,
            worker_count            INTEGER,
            rationale               TEXT NOT NULL DEFAULT '',
            applied                 INTEGER NOT NULL DEFAULT 0,
            latency_ms              REAL,
            cbm_parallel_candidates INTEGER
        )
    """)

    cursor.execute("""
        CREATE TABLE formation_selections (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            task_fingerprint TEXT,
            goal TEXT NOT NULL DEFAULT '',
            arm TEXT NOT NULL,
            formation_id TEXT,
            confidence REAL,
            low_confidence INTEGER NOT NULL DEFAULT 0,
            topology TEXT,
            implementers_json TEXT NOT NULL DEFAULT '[]',
            reviewer TEXT,
            planner TEXT,
            mechanical_gate INTEGER NOT NULL DEFAULT 0,
            models_json TEXT NOT NULL DEFAULT '{}',
            predicted_etar_s REAL,
            etar_components_json TEXT NOT NULL DEFAULT '{}',
            outcome TEXT,
            repair_loops INTEGER NOT NULL DEFAULT 0,
            escalations INTEGER NOT NULL DEFAULT 0,
            first_pass_accept INTEGER,
            wall_clock_s REAL,
            source TEXT NOT NULL DEFAULT 'calibration',
            created_at TEXT NOT NULL
        )
    """)

    shape_meta = [
        ("id", True, None),
        ("created_at", True, "2026-07-26T10:00:00+00:00"),
        ("board_task_id", False, None),
        ("run_id", False, None),
        ("brief_hash", True, "abc"),
        ("config_version", False, None),
        ("feat_d", False, None),
        ("feat_i", False, None),
        ("feat_s", False, None),
        ("feat_u", False, None),
        ("feat_v", False, None),
        ("feat_g", False, None),
        ("feat_c", False, None),
        ("feat_m", False, None),
        ("feat_r", False, None),
        ("feat_k", False, None),
        ("feat_w", False, None),
        ("feat_p", False, None),
        ("confidence", False, None),
        ("task_class", False, None),
        ("tool_density", False, None),
        ("context_breadth", False, None),
        ("merge_cost", False, None),
        ("shared_state_coupling", False, None),
        ("route", True, "solo_strong"),
        ("topology", False, None),
        ("worker_count", False, None),
        ("rationale", True, ""),
        ("applied", True, 0),
        ("latency_ms", False, None),
        ("cbm_parallel_candidates", False, None),
    ]

    formation_meta = [
        ("id", True, None),
        ("task_id", True, "task-abc"),
        ("task_fingerprint", False, None),
        ("goal", True, ""),
        ("arm", True, "formation"),
        ("formation_id", False, None),
        ("confidence", False, None),
        ("low_confidence", True, 0),
        ("topology", False, None),
        ("implementers_json", True, "[]"),
        ("reviewer", False, None),
        ("planner", False, None),
        ("mechanical_gate", True, 0),
        ("models_json", True, "{}"),
        ("predicted_etar_s", False, None),
        ("etar_components_json", True, "{}"),
        ("outcome", False, None),
        ("repair_loops", True, 0),
        ("escalations", True, 0),
        ("first_pass_accept", False, None),
        ("wall_clock_s", False, None),
        ("source", True, "calibration"),
        ("created_at", True, "2026-07-26T10:00:00+00:00"),
    ]

    for i, row in enumerate(shape_rows):
        full_row = {}
        for col, is_nn, default in shape_meta:
            if col in row:
                full_row[col] = row[col]
            else:
                if col == "id":
                    full_row[col] = f"td-{i}"
                else:
                    full_row[col] = default if is_nn else None

        placeholders = ", ".join(f":{col}" for col, _, _ in shape_meta)
        columns_str = ", ".join(col for col, _, _ in shape_meta)
        cursor.execute(
            f"INSERT INTO task_shape_decisions ({columns_str}) VALUES ({placeholders})",
            full_row,
        )

    for i, row in enumerate(formation_rows):
        full_row = {}
        for col, is_nn, default in formation_meta:
            if col in row:
                full_row[col] = row[col]
            else:
                if col == "id":
                    full_row[col] = f"fs-{i}"
                else:
                    full_row[col] = default if is_nn else None

        placeholders = ", ".join(f":{col}" for col, _, _ in formation_meta)
        columns_str = ", ".join(col for col, _, _ in formation_meta)
        cursor.execute(
            f"INSERT INTO formation_selections ({columns_str}) VALUES ({placeholders})",
            full_row,
        )

    conn.commit()
    conn.close()
    return path


def _seed_leases(
    directory: Path,
    records: list[dict[str, Any]],
    *,
    name: str = "leases-202607.jsonl",
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    file_path = directory / name
    with open(file_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return file_path


def _seed_observations(directory: Path, records: list[dict[str, Any]]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index, r in enumerate(records):
        file_path = directory / f"obs_{index:03d}.json"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(r))
    return directory


def _lease(**overrides: Any) -> dict[str, Any]:
    record = {
        "event": "issued",
        "mode": "enforce",
        "recorded_at": "2026-07-26T10:00:00+00:00",
        "lease_id": "lease-1",
        "signed": True,
        "enforced": True,
        "net_policy": "open",
        "subject": {"run_id": "r1"},
    }
    record.update(overrides)
    return record


def _obs(**overrides: Any) -> dict[str, Any]:
    record = {
        "version": "1",
        "ts": "2026-07-26T10:00:00+00:00",
        "tool": "read_file",
        "run_id": "r1",
        "session_id": "s1",
        "holder_generation": 1,
        "correlation_id": "c1",
        "status": "success",
        "ok": True,
        "error": None,
        "duration_ms": 10,
        "source": "toolplane",
    }
    record.update(overrides)
    return record


def test_absent_sources_are_insufficient_not_zero(tmp_path: Path) -> None:
    db = tmp_path / "nope.db"
    ledger = tmp_path / "no-ledger"
    obs = tmp_path / "no-obs"

    report = build_report(
        db_path=str(db),
        ledger_dir=str(ledger),
        observations_dir=str(obs),
        now="2026-01-01T00:00:00Z",
    )

    assert report["sources"]["db_present"] is False
    assert report["evidence"]["task_shape_decisions"]["available"] is False
    assert report["evidence"]["formation_selections"]["available"] is False
    assert report["evidence"]["lease_records"]["available"] is False
    assert report["evidence"]["toolplane_observations"]["available"] is False

    for feature in report["features"]:
        for threshold in feature["thresholds"]:
            assert threshold["status"] == INSUFFICIENT

    assert report["safety"]["total"] == 0
    assert report["verdict"] == "HOLD"


def test_malformed_records_are_counted_not_fatal(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    leases_file = ledger_dir / "leases-202607.jsonl"
    with open(leases_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(_lease()) + "\n")
        f.write("not json\n")
        f.write("\n")
        f.write(json.dumps(_lease(lease_id="lease-2")) + "\n")

    ev = read_lease_evidence(ledger_dir)
    assert ev["records"] == 2
    assert ev["malformed"] == 1
    assert ev["available"] is True


def test_zero_escapes_is_met_with_enough_clean_records(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "ledger"
    records = [_lease(lease_id=f"lease-{i}") for i in range(25)]
    _seed_leases(ledger_dir, records)

    ev = read_lease_evidence(ledger_dir)
    thresholds = evaluate_lease(ev, min_samples=20)

    zero_escapes_t = next(t for t in thresholds if t.id == "zero_escapes")
    assert zero_escapes_t.status == MET
    assert zero_escapes_t.measured == 0.0

    prompt_reduction_t = next(t for t in thresholds if t.id == "permission_prompt_reduction")
    assert prompt_reduction_t.status == INSUFFICIENT
    assert len(prompt_reduction_t.missing) > 0


def test_an_unsigned_enforce_record_is_an_escape(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "ledger"
    records = [_lease(lease_id=f"lease-{i}") for i in range(24)] + [
        _lease(lease_id="lease-24", signed=False)
    ]
    _seed_leases(ledger_dir, records)

    ev = read_lease_evidence(ledger_dir)
    assert ev["escapes"] == 1

    thresholds = evaluate_lease(ev, min_samples=20)
    zero_escapes_t = next(t for t in thresholds if t.id == "zero_escapes")
    assert zero_escapes_t.status == NOT_MET
    assert zero_escapes_t.measured == 1.0


def test_enforced_false_under_enforce_is_an_escape(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "ledger"
    records = [_lease(lease_id=f"lease-{i}") for i in range(24)] + [
        _lease(lease_id="lease-24", enforced=False)
    ]
    _seed_leases(ledger_dir, records)

    ev = read_lease_evidence(ledger_dir)
    assert ev["escapes"] == 1

    thresholds = evaluate_lease(ev, min_samples=20)
    zero_escapes_t = next(t for t in thresholds if t.id == "zero_escapes")
    assert zero_escapes_t.status == NOT_MET
    assert zero_escapes_t.measured == 1.0


def test_shadow_mode_unsigned_record_is_not_an_escape(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "ledger"
    records = [_lease(lease_id=f"lease-{i}") for i in range(24)] + [
        _lease(lease_id="lease-24", mode="shadow", signed=False)
    ]
    _seed_leases(ledger_dir, records)

    ev = read_lease_evidence(ledger_dir)
    assert ev["escapes"] == 0

    thresholds = evaluate_lease(ev, min_samples=20)
    zero_escapes_t = next(t for t in thresholds if t.id == "zero_escapes")
    assert zero_escapes_t.status == MET
    assert zero_escapes_t.measured == 0.0


def test_too_few_lease_records_is_insufficient(tmp_path: Path) -> None:
    ledger_dir = tmp_path / "ledger"
    records = [_lease(lease_id=f"lease-{i}") for i in range(3)]
    _seed_leases(ledger_dir, records)

    ev = read_lease_evidence(ledger_dir)
    thresholds = evaluate_lease(ev, min_samples=20)
    zero_escapes_t = next(t for t in thresholds if t.id == "zero_escapes")
    assert zero_escapes_t.status == INSUFFICIENT
    assert len(zero_escapes_t.missing) > 0


def test_unauthorized_disclosure_is_detected(tmp_path: Path) -> None:
    obs_dir = tmp_path / "observations"
    records = [_obs(correlation_id=f"c-{i}") for i in range(24)] + [
        _obs(correlation_id="c-24", status="denied", error="out_of_scope")
    ]
    _seed_observations(obs_dir, records)

    ev = read_toolplane_evidence(obs_dir)
    assert ev["unauthorized_disclosures"] == 1
    assert ev["denied"] == 1

    thresholds = evaluate_tool_disclosure(ev, min_samples=20)
    unauthorized_t = next(t for t in thresholds if t.id == "zero_unauthorized_disclosure")
    assert unauthorized_t.status == NOT_MET
    assert unauthorized_t.measured == 1.0


def test_an_operational_failure_is_not_a_disclosure(tmp_path: Path) -> None:
    obs_dir = tmp_path / "observations"
    records = (
        [_obs(correlation_id=f"c-{i}") for i in range(23)]
        + [_obs(correlation_id="c-23", status="failed", error="timeout")]
        + [_obs(correlation_id="c-24", status="denied", error="broker_denied")]
    )
    _seed_observations(obs_dir, records)

    ev = read_toolplane_evidence(obs_dir)
    assert ev["unauthorized_disclosures"] == 0

    thresholds = evaluate_tool_disclosure(ev, min_samples=20)
    unauthorized_t = next(t for t in thresholds if t.id == "zero_unauthorized_disclosure")
    assert unauthorized_t.status == MET
    assert unauthorized_t.measured == 0.0


def test_token_thresholds_are_always_insufficient(tmp_path: Path) -> None:
    obs_dir = tmp_path / "observations"
    records = [_obs(correlation_id=f"c-{i}") for i in range(50)]
    _seed_observations(obs_dir, records)

    ev = read_toolplane_evidence(obs_dir)
    thresholds = evaluate_tool_disclosure(ev, min_samples=20)

    t1 = next(t for t in thresholds if t.id == "initial_schema_token_reduction")
    assert t1.status == INSUFFICIENT
    assert any("token" in m.lower() for m in t1.missing)

    t2 = next(t for t in thresholds if t.id == "selection_parity")
    assert t2.status == INSUFFICIENT
    assert len(t2.missing) > 0


def test_sequential_multi_worker_is_a_safety_violation(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    shape_rows = [
        {
            "created_at": "2026-07-26T10:00:00+00:00",
            "brief_hash": "hash-1",
            "route": "solo_strong",
            "topology": "sequential",
            "worker_count": 1,
            "applied": 0,
        }
        for _ in range(24)
    ] + [
        {
            "created_at": "2026-07-26T10:00:00+00:00",
            "brief_hash": "hash-2",
            "route": "solo_strong",
            "topology": "sequential",
            "worker_count": 4,
            "applied": 0,
        }
    ]
    _seed_db(db_path, shape_rows=shape_rows, formation_rows=[])

    ledger_dir = tmp_path / "no-ledger"
    obs_dir = tmp_path / "no-obs"

    report = build_report(
        db_path=str(db_path),
        ledger_dir=str(ledger_dir),
        observations_dir=str(obs_dir),
        min_samples=20,
        now="2026-07-27T10:00:00Z",
    )

    shape_ev = report["evidence"]["task_shape_decisions"]
    assert shape_ev["sequential_multi_worker"] == 1

    feat = next(f for f in report["features"] if f["feature"] == "task_shape_routing")
    t3 = next(t for t in feat["thresholds"] if t["id"] == "no_unnecessary_workers")
    assert t3["status"] == NOT_MET
    assert t3["measured"] == 1.0

    assert report["safety"]["sequential_multi_worker"] == 1
    assert report["safety"]["total"] == 1
    assert feat["verdict"] == "REJECT"
    assert report["verdict"] == "REJECT"


def test_clean_sequential_rows_meet_the_worker_threshold(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    shape_rows = [
        {
            "created_at": "2026-07-26T10:00:00+00:00",
            "brief_hash": "hash-1",
            "route": "solo_strong",
            "topology": "sequential",
            "worker_count": 1,
            "applied": 0,
        }
        for _ in range(25)
    ]
    _seed_db(db_path, shape_rows=shape_rows, formation_rows=[])

    ledger_dir = tmp_path / "no-ledger"
    obs_dir = tmp_path / "no-obs"

    report = build_report(
        db_path=str(db_path),
        ledger_dir=str(ledger_dir),
        observations_dir=str(obs_dir),
        min_samples=20,
        now="2026-07-27T10:00:00Z",
    )

    feat = next(f for f in report["features"] if f["feature"] == "task_shape_routing")
    t3 = next(t for t in feat["thresholds"] if t["id"] == "no_unnecessary_workers")
    assert t3["status"] == MET
    assert t3["measured"] == 0.0

    assert report["safety"]["sequential_multi_worker"] == 0
    assert report["safety"]["total"] == 0
    assert report["verdict"] == "HOLD"


def test_shadow_only_corpus_cannot_compute_a_routed_delta(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    shape_rows = [
        {
            "created_at": "2026-07-26T10:00:00+00:00",
            "brief_hash": "hash-1",
            "route": "solo_strong",
            "topology": "sequential",
            "worker_count": 1,
            "applied": 0,
            "board_task_id": f"task-{i}",
        }
        for i in range(25)
    ]
    formation_rows = [
        {
            "created_at": "2026-07-26T10:00:00+00:00",
            "task_id": f"task-{i}",
            "outcome": "accepted" if i % 2 == 0 else "rejected",
            "wall_clock_s": 10.0,
        }
        for i in range(25)
    ]
    _seed_db(db_path, shape_rows=shape_rows, formation_rows=formation_rows)

    ledger_dir = tmp_path / "no-ledger"
    obs_dir = tmp_path / "no-obs"

    report = build_report(
        db_path=str(db_path),
        ledger_dir=str(ledger_dir),
        observations_dir=str(obs_dir),
        min_samples=20,
        now="2026-07-27T10:00:00Z",
    )

    feat = next(f for f in report["features"] if f["feature"] == "task_shape_routing")
    t1 = next(t for t in feat["thresholds"] if t["id"] == "accepted_rate_delta")
    assert t1["status"] == INSUFFICIENT
    assert any("applied" in m.lower() or "shadow" in m.lower() for m in t1["missing"])


def test_feature_verdict_rejects_on_any_safety_count() -> None:
    t = Threshold(id="a", description="d", target="t", status=MET)
    assert feature_verdict([t], safety_count=1) == "REJECT"


def test_all_met_promotes() -> None:
    t1 = Threshold(id="t1", description="d", target="t", status=MET, group="all_of")
    t2 = Threshold(id="t2", description="d", target="t", status=MET, group="all_of")
    t3 = Threshold(id="t3", description="d", target="t", status=MET, group="any_of")
    assert feature_verdict([t1, t2, t3], safety_count=0) == "PROMOTE"


def test_insufficient_never_promotes() -> None:
    t1 = Threshold(id="t1", description="d", target="t", status=MET, group="all_of")
    t2 = Threshold(id="t2", description="d", target="t", status=INSUFFICIENT, group="all_of")
    assert feature_verdict([t1, t2], safety_count=0) == "HOLD"


def test_any_of_group_needs_only_one_met() -> None:
    t1 = Threshold(id="t1", description="d", target="t", status=MET, group="any_of")
    t2 = Threshold(id="t2", description="d", target="t", status=NOT_MET, group="any_of")
    t3 = Threshold(id="t3", description="d", target="t", status=MET, group="all_of")
    assert feature_verdict([t1, t2, t3], safety_count=0) == "PROMOTE"


def test_a_not_met_all_of_holds() -> None:
    t1 = Threshold(id="t1", description="d", target="t", status=NOT_MET, group="all_of")
    t2 = Threshold(id="t2", description="d", target="t", status=MET, group="all_of")
    assert feature_verdict([t1, t2], safety_count=0) == "HOLD"


def test_markdown_is_deterministic_and_well_formed(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    shape_rows = [
        {
            "created_at": "2026-07-26T10:00:00+00:00",
            "brief_hash": "hash-1",
            "route": "solo_strong",
            "topology": "sequential",
            "worker_count": 1,
            "applied": 0,
        }
        for _ in range(25)
    ]
    _seed_db(db_path, shape_rows=shape_rows, formation_rows=[])

    report = build_report(
        db_path=str(db_path),
        ledger_dir=str(tmp_path / "no-ledger"),
        observations_dir=str(tmp_path / "no-obs"),
        min_samples=20,
        now="2026-07-27T10:00:00Z",
    )

    md = render_markdown(report)
    assert md.startswith("# Phase-2 promotion report")
    assert md.endswith("\n")
    assert md == render_markdown(report)
    assert all(line == line.rstrip() for line in md.splitlines())

    assert "## Task-shape routing" in md
    assert "## Tool disclosure" in md
    assert "## Resource-aware execution" in md
    assert "## Autonomy lease" in md

    status_strings_in_md = set()
    for line in md.splitlines():
        if line.startswith("|") and line.endswith("|"):
            parts = [p.strip() for p in line.split("|")]
            if len(parts) == 6:
                stat = parts[3]
                if stat not in ("Status", "---"):
                    status_strings_in_md.add(stat)

    allowed = {MET, NOT_MET, INSUFFICIENT}
    assert status_strings_in_md.issubset(allowed)


def test_report_is_reproducible_for_identical_inputs(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _seed_db(db_path, shape_rows=[], formation_rows=[])

    kwargs = {
        "db_path": str(db_path),
        "ledger_dir": str(tmp_path / "ledger"),
        "observations_dir": str(tmp_path / "obs"),
        "min_samples": 20,
        "now": "2026-07-27T10:00:00Z",
    }
    report_a = build_report(**kwargs)
    report_b = build_report(**kwargs)
    assert json.dumps(report_a, sort_keys=True) == json.dumps(report_b, sort_keys=True)


def test_cli_writes_json_to_out(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    _seed_db(db_path, shape_rows=[], formation_rows=[])
    ledger_dir = tmp_path / "ledger"
    obs_dir = tmp_path / "obs"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    obs_dir.mkdir(parents=True, exist_ok=True)

    out_file = tmp_path / "sub" / "r.json"

    argv = [
        "--db",
        str(db_path),
        "--ledger-dir",
        str(ledger_dir),
        "--observations-dir",
        str(obs_dir),
        "--format",
        "json",
        "--out",
        str(out_file),
        "--now",
        "2026-01-01T00:00:00Z",
    ]

    rc = main(argv)
    assert rc == 0
    assert out_file.exists()

    with open(out_file, encoding="utf-8") as f:
        data = json.load(f)

    assert data["report_version"] == "1"
    assert data["generated_at"] == "2026-01-01T00:00:00Z"
    assert "features" in data
    assert "safety" in data
    assert "verdict" in data


def test_cli_markdown_to_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "test.db"
    _seed_db(db_path, shape_rows=[], formation_rows=[])
    ledger_dir = tmp_path / "ledger"
    obs_dir = tmp_path / "obs"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    obs_dir.mkdir(parents=True, exist_ok=True)

    argv = [
        "--db",
        str(db_path),
        "--ledger-dir",
        str(ledger_dir),
        "--observations-dir",
        str(obs_dir),
        "--format",
        "md",
        "--now",
        "2026-01-01T00:00:00Z",
    ]

    rc = main(argv)
    assert rc == 0

    out, err = capsys.readouterr()
    assert "# Phase-2 promotion report" in out
