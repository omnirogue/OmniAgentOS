"""Migration 083 (improve schema): tables, columns, CHECKs, indexes, immutability triggers."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from omniagentos.db.migrate import migrate


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    db = str(tmp_path / "improve.db")
    version = migrate(db)
    assert version >= 83
    return db


def _connect(db_path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _insert_configtest_run(connection: sqlite3.Connection, run_id: str, status: str) -> None:
    connection.execute(
        "INSERT INTO configtest_runs ("
        "  run_id, test_id, category, tier, formation_json, models_json, "
        "  bracket_budget_json, status, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, "test_1", "category_1", "tier_1", "{}", "[]", "{}", status, "2026-07-27"),
    )


def _insert_configtest_hypothesis(
    connection: sqlite3.Connection, hypothesis_id: str, state: str
) -> None:
    connection.execute(
        "INSERT INTO configtest_hypotheses ("
        "  hypothesis_id, state, independent_variable, evidence_json, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (hypothesis_id, state, "var_1", "{}", "2026-07-27", "2026-07-27"),
    )


def _insert_healer_decision(connection: sqlite3.Connection, decision_id: str, verdict: str) -> None:
    connection.execute(
        "INSERT INTO healer_decisions ("
        "  decision_id, failure_ref, fixer_model, reviewer_model, verdict, "
        "  attested_confidence, rationale, files_touched_json, gates_evidence_json, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id,
            "ref_1",
            "fixer",
            "reviewer",
            verdict,
            0.9,
            "rationale",
            "[]",
            "[]",
            "2026-07-27",
        ),
    )


def _insert_improve_verdict(
    connection: sqlite3.Connection, verdict_id: str, tier: str, stage: str
) -> None:
    connection.execute(
        "INSERT INTO improve_verdicts ("
        "  verdict_id, attempt_id, tier, judge_model, stage, vote, "
        "  base_sha, head_sha, tree_hash, diff_hash, judge_config_hash, created_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            verdict_id,
            "att_1",
            tier,
            "judge",
            stage,
            "vote_1",
            "sha_1",
            "sha_2",
            "hash_1",
            "hash_2",
            "hash_3",
            "2026-07-27",
        ),
    )


def _insert_improve_saga(
    connection: sqlite3.Connection, attempt_id: str, state: str, idempotency_key: str
) -> None:
    connection.execute(
        "INSERT INTO improve_saga ("
        "  attempt_id, state, idempotency_key, updated_at"
        ") VALUES (?, ?, ?, ?)",
        (attempt_id, state, idempotency_key, "2026-07-27"),
    )


def test_083_migrate_applies_cleanly(db_path: str) -> None:
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            "SELECT version FROM schema_migrations WHERE version = 83"
        ).fetchall()
        assert len(rows) == 1
    finally:
        connection.close()


def test_083_tables_and_columns_exist(db_path: str) -> None:
    connection = _connect(db_path)
    try:
        tables = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        expected_tables = {
            "configtest_runs",
            "configtest_hypotheses",
            "healer_decisions",
            "healer_outcomes",
            "improve_verdicts",
            "improve_saga",
        }
        assert expected_tables <= tables

        assert _columns(connection, "configtest_runs") == {
            "run_id",
            "test_id",
            "category",
            "tier",
            "formation_json",
            "models_json",
            "bracket_budget_json",
            "status",
            "gate_results_json",
            "wall_ms",
            "tokens_in",
            "tokens_out",
            "cost_usd",
            "escalation_events_json",
            "lab_tournament_id",
            "created_at",
        }
        assert _columns(connection, "configtest_hypotheses") == {
            "hypothesis_id",
            "state",
            "independent_variable",
            "evidence_json",
            "created_at",
            "updated_at",
        }
        assert _columns(connection, "healer_decisions") == {
            "decision_id",
            "failure_ref",
            "fixer_model",
            "reviewer_model",
            "verdict",
            "attested_confidence",
            "rationale",
            "files_touched_json",
            "gates_evidence_json",
            "created_at",
        }
        assert _columns(connection, "healer_outcomes") == {
            "outcome_id",
            "decision_id",
            "outcome",
            "evidence_json",
            "created_at",
        }
        assert _columns(connection, "improve_verdicts") == {
            "verdict_id",
            "attempt_id",
            "tier",
            "judge_model",
            "stage",
            "vote",
            "blocker_cited",
            "blocker_reproduced",
            "rationale",
            "base_sha",
            "head_sha",
            "tree_hash",
            "diff_hash",
            "judge_config_hash",
            "created_at",
        }
        assert _columns(connection, "improve_saga") == {
            "attempt_id",
            "state",
            "approved_sha",
            "merged_sha",
            "idempotency_key",
            "updated_at",
        }
    finally:
        connection.close()


def test_083_indexes_exist(db_path: str) -> None:
    connection = _connect(db_path)
    try:
        indexes = {
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
        expected_indexes = {
            "idx_improve_verdicts_attempt",
            "idx_improve_saga_state",
            "idx_configtest_runs_test",
            "idx_healer_outcomes_decision",
            "idx_configtest_hypotheses_state",
        }
        assert expected_indexes <= indexes
    finally:
        connection.close()


def test_083_enum_check_constraints_enforced(db_path: str) -> None:
    connection = _connect(db_path)
    try:
        # 1. configtest_runs.status check
        with pytest.raises(sqlite3.IntegrityError):
            _insert_configtest_run(connection, "run_invalid", "sprinting")
        _insert_configtest_run(connection, "run_valid", "pass")

        # 2. configtest_hypotheses.state check
        with pytest.raises(sqlite3.IntegrityError):
            _insert_configtest_hypothesis(connection, "hyp_invalid", "unknown_state")
        _insert_configtest_hypothesis(connection, "hyp_valid", "proposed")

        # 3. healer_decisions.verdict check
        with pytest.raises(sqlite3.IntegrityError):
            _insert_healer_decision(connection, "dec_invalid_verdict", "accept")
        _insert_healer_decision(connection, "dec_valid", "confirm_upgrade")

        # 4. healer_decisions.attested_confidence check
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO healer_decisions ("
                "  decision_id, failure_ref, fixer_model, reviewer_model, verdict, "
                "  attested_confidence, rationale, files_touched_json, "
                "  gates_evidence_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "dec_invalid_conf_high",
                    "ref_1",
                    "fixer",
                    "reviewer",
                    "confirm_upgrade",
                    1.1,
                    "rationale",
                    "[]",
                    "[]",
                    "2026-07-27",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO healer_decisions ("
                "  decision_id, failure_ref, fixer_model, reviewer_model, verdict, "
                "  attested_confidence, rationale, files_touched_json, "
                "  gates_evidence_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "dec_invalid_conf_low",
                    "ref_1",
                    "fixer",
                    "reviewer",
                    "confirm_upgrade",
                    -0.1,
                    "rationale",
                    "[]",
                    "[]",
                    "2026-07-27",
                ),
            )

        # 5. improve_verdicts.tier and stage checks
        with pytest.raises(sqlite3.IntegrityError):
            _insert_improve_verdict(connection, "verd_invalid_tier", "T3", "cheap")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_improve_verdict(connection, "verd_invalid_stage", "T0", "expensive")
        _insert_improve_verdict(connection, "verd_valid", "T0", "cheap")

        # improve_verdicts blocker_cited and blocker_reproduced check
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO improve_verdicts ("
                "  verdict_id, attempt_id, tier, judge_model, stage, vote, "
                "  blocker_cited, blocker_reproduced, base_sha, head_sha, tree_hash, diff_hash, "
                "  judge_config_hash, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "verd_invalid_cited",
                    "att_1",
                    "T0",
                    "judge",
                    "cheap",
                    "vote_1",
                    2,
                    0,
                    "sha_1",
                    "sha_2",
                    "hash_1",
                    "hash_2",
                    "hash_3",
                    "2026-07-27",
                ),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO improve_verdicts ("
                "  verdict_id, attempt_id, tier, judge_model, stage, vote, "
                "  blocker_cited, blocker_reproduced, base_sha, head_sha, tree_hash, diff_hash, "
                "  judge_config_hash, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "verd_invalid_reproduced",
                    "att_1",
                    "T0",
                    "judge",
                    "cheap",
                    "vote_1",
                    0,
                    3,
                    "sha_1",
                    "sha_2",
                    "hash_1",
                    "hash_2",
                    "hash_3",
                    "2026-07-27",
                ),
            )

        # 6. improve_saga.state check
        with pytest.raises(sqlite3.IntegrityError):
            _insert_improve_saga(connection, "saga_invalid", "MERGING", "idemp_invalid")
        _insert_improve_saga(connection, "saga_valid", "MERGED_SHA", "idemp_valid")
    finally:
        connection.close()


def test_083_healer_outcomes_foreign_key_enforced(db_path: str) -> None:
    connection = _connect(db_path)
    try:
        # First insert a valid healer_decision
        _insert_healer_decision(connection, "dec_ref", "confirm_upgrade")

        # Inserting healer_outcome referencing a non-existent decision must fail
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO healer_outcomes ("
                "  outcome_id, decision_id, outcome, evidence_json, created_at"
                ") VALUES (?, ?, ?, ?, ?)",
                ("out_bad", "dec_non_existent", "success", "{}", "2026-07-27"),
            )

        # Inserting healer_outcome referencing the valid decision must succeed
        connection.execute(
            "INSERT INTO healer_outcomes ("
            "  outcome_id, decision_id, outcome, evidence_json, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            ("out_good", "dec_ref", "success", "{}", "2026-07-27"),
        )
    finally:
        connection.close()


def test_083_immutability_triggers_enforced(db_path: str) -> None:
    connection = _connect(db_path)
    try:
        # Insert a valid healer_decision and healer_outcome
        _insert_healer_decision(connection, "dec_immutable", "confirm_upgrade")
        connection.execute(
            "INSERT INTO healer_outcomes ("
            "  outcome_id, decision_id, outcome, evidence_json, created_at"
            ") VALUES (?, ?, ?, ?, ?)",
            ("out_append_only", "dec_immutable", "success", "{}", "2026-07-27"),
        )

        # 1. healer_decisions is immutable (no UPDATE, no DELETE)
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            connection.execute(
                "UPDATE healer_decisions SET failure_ref = 'new_ref' WHERE decision_id = 'dec_immutable'"
            )
        assert "healer_decisions is immutable" in str(exc_info.value)

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            connection.execute("DELETE FROM healer_decisions WHERE decision_id = 'dec_immutable'")
        assert "healer_decisions is immutable" in str(exc_info.value)

        # 2. healer_outcomes is append-only (no UPDATE, no DELETE)
        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            connection.execute(
                "UPDATE healer_outcomes SET outcome = 'failed' WHERE outcome_id = 'out_append_only'"
            )
        assert "healer_outcomes is append-only" in str(exc_info.value)

        with pytest.raises(sqlite3.IntegrityError) as exc_info:
            connection.execute("DELETE FROM healer_outcomes WHERE outcome_id = 'out_append_only'")
        assert "healer_outcomes is append-only" in str(exc_info.value)
    finally:
        connection.close()
