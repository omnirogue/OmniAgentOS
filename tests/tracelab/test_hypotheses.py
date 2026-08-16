"""Hypothesis rules fire only on supported evidence; DB emit targets the real
migration-083 schema and never demotes a lane-owned lifecycle state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from omniagentos.tracelab.events import EventKind, Outcome, Trace, TraceEvent
from omniagentos.tracelab.hypotheses import (
    MIN_GROUP,
    emit_to_improvement_lane,
    generate_hypotheses,
)
from omniagentos.tracelab.mine import mine_traces

_MIGRATION = (
    Path(__file__).resolve().parents[2] / "omniagentos" / "db" / "migrations" / "083_improve.sql"
)


def _trace(idx: int, *, outcome: Outcome, error_streak: int, verify: bool) -> Trace:
    trace = Trace(trace_id=f"t{idx}", dataset="synthetic", source_path="mem", outcome=outcome)
    seq = 0
    for _ in range(error_streak):
        # deliberately NOT a verification-shaped command ("build"/"pytest"/…)
        trace.events.append(
            TraceEvent(seq=seq, kind=EventKind.TOOL_CALL, tool_name="bash", excerpt="./run_job.sh")
        )
        seq += 1
        trace.events.append(
            TraceEvent(
                seq=seq,
                kind=EventKind.TOOL_RESULT,
                is_error=True,
                excerpt="Timed out waiting for job",
            )
        )
        seq += 1
    if verify:
        trace.events.append(
            TraceEvent(seq=seq, kind=EventKind.TOOL_CALL, tool_name="bash", excerpt="pytest -q")
        )
        seq += 1
        trace.events.append(
            TraceEvent(
                seq=seq,
                kind=EventKind.TOOL_RESULT,
                content_chars=50,
                is_error=False,
            )
        )
        seq += 1
    if not trace.events:
        trace.events.append(
            TraceEvent(seq=0, kind=EventKind.TOOL_CALL, tool_name="bash", excerpt="ls")
        )
    return trace


def _labeled_corpus() -> list[Trace]:
    traces = []
    for i in range(MIN_GROUP + 2):
        traces.append(_trace(i, outcome=Outcome.SUCCESS, error_streak=0, verify=True))
    for i in range(MIN_GROUP + 2):
        traces.append(_trace(100 + i, outcome=Outcome.FAILURE, error_streak=4, verify=False))
    return traces


def test_correlational_rules_fire_on_contrasted_corpus() -> None:
    result = mine_traces(_labeled_corpus())
    hypotheses = generate_hypotheses(result)
    variables = {h.independent_variable for h in hypotheses}
    assert "error_circuit_breaker=2" in variables
    assert "mandatory_verification_gate=on" in variables
    by_var = {h.independent_variable: h for h in hypotheses}
    assert by_var["error_circuit_breaker=2"].confidence == "correlational"
    # evidence carries exemplar trace pointers
    exemplars = by_var["error_circuit_breaker=2"].evidence["exemplars"]
    assert isinstance(exemplars, list) and exemplars


def test_no_rules_fire_without_support() -> None:
    traces = [_trace(i, outcome=Outcome.UNKNOWN, error_streak=0, verify=True) for i in range(5)]
    result = mine_traces(traces)
    assert generate_hypotheses(result) == []


def test_taxon_rule_fires_at_threshold() -> None:
    traces = []
    for i in range(35):
        trace = _trace(i, outcome=Outcome.UNKNOWN, error_streak=1, verify=True)
        traces.append(trace)
    result = mine_traces(traces)
    hypotheses = generate_hypotheses(result)
    assert any(h.independent_variable == "command_timeout_wrapper=on" for h in hypotheses)


def test_emit_to_improvement_lane_upserts_without_demoting(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_MIGRATION.read_text())

    result = mine_traces(_labeled_corpus())
    hypotheses = generate_hypotheses(result)
    assert hypotheses
    written = emit_to_improvement_lane(hypotheses, db_path)
    assert written == len(hypotheses)

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT hypothesis_id, state, evidence_json FROM configtest_hypotheses"
        ).fetchall()
    assert len(rows) == len(hypotheses)
    assert all(state == "proposed" for _, state, _ in rows)
    assert all(json.loads(evidence)["statement"] for _, _, evidence in rows)

    # lane promotes one hypothesis; re-emit must refresh evidence, keep state
    promoted_id = rows[0][0]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE configtest_hypotheses SET state='testing' WHERE hypothesis_id=?",
            (promoted_id,),
        )
    emit_to_improvement_lane(hypotheses, db_path)
    with sqlite3.connect(db_path) as conn:
        (state,) = conn.execute(
            "SELECT state FROM configtest_hypotheses WHERE hypothesis_id=?", (promoted_id,)
        ).fetchone()
    assert state == "testing"
