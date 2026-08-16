"""Unit tests for Stage D (apply)."""

from __future__ import annotations

import json

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.reflection.apply import (
    apply_proposal,
    apply_yaml_change,
    auto_apply_eligible,
    score_outcomes,
    set_nested,
    write_document_change,
)
from omniagentos.reflection.fingerprint import proposal_fingerprint


def test_set_nested():
    """Verify dictionary nested path updater behaves correctly."""
    data = {"a": {"b": 1}}
    set_nested(data, ["a", "b"], 2)
    assert data["a"]["b"] == 2

    # Create key structure if missing
    set_nested(data, ["a", "c", "d"], "hello")
    assert data["a"]["c"]["d"] == "hello"


def test_apply_yaml_change(tmp_path):
    """Verify safe YAML configuration modifications."""
    repo_root = tmp_path
    configs_dir = repo_root / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    swarm_file = configs_dir / "swarm.yaml"
    swarm_file.write_text("lane_floors:\n  complex:\n    - old-model\n", encoding="utf-8")

    # Perform edit
    target = {"file": "configs/swarm.yaml", "key": "lane_floors.complex"}
    proposed = ["gemini-3.6-flash", "gpt-4o"]
    apply_yaml_change(repo_root, target, proposed)

    # Verify content
    import yaml

    updated_data = yaml.safe_load(swarm_file.read_text(encoding="utf-8"))
    assert updated_data["lane_floors"]["complex"] == ["gemini-3.6-flash", "gpt-4o"]


def test_write_document_change(tmp_path):
    """Verify lessons and AGENTS.md document writes."""
    repo_root = tmp_path
    lessons_dir = repo_root / "docs" / "lessons"
    lessons_dir.mkdir(parents=True, exist_ok=True)
    lesson_file = lessons_dir / "2026-07-26-reflection.md"

    write_document_change(
        repo_root, "lesson", str(lesson_file.relative_to(repo_root)), "Lesson details."
    )
    assert lesson_file.read_text(encoding="utf-8").strip() == "Lesson details."


def test_apply_proposal(tmp_path, monkeypatch):
    """Test full pipeline integration of apply_proposal."""
    db_path = str(tmp_path / "test.db")
    store = SqliteStore(db_path)

    # Create dummy config and seed proposal
    configs_dir = tmp_path / "configs"
    configs_dir.mkdir(parents=True, exist_ok=True)
    modelintel_file = configs_dir / "modelintel.yaml"
    modelintel_file.write_text("models:\n  gemini:\n    available: false\n", encoding="utf-8")

    now = utc_now_iso()
    with store._lock:
        store._connection.execute(
            """
            INSERT INTO reflection_proposals (
                id, kind, target, current, proposed, rationale, evidence_refs_json, predicted_impact, risk_class, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "prop_001",
                "model_config",
                json.dumps({"file": "configs/modelintel.yaml", "key": "models.gemini.available"}),
                "false",
                "true",
                "Certifying gemini model as active.",
                "[]",
                "Better performance",
                "low",
                "pending",
                now,
                now,
            ),
        )

    # Inject temporary environment variable
    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))

    # Run apply
    res = apply_proposal("prop_001", db_path=db_path)
    assert res is not None
    assert res["status"] == "success"

    # Verify db status transition
    with store._lock:
        row = store._connection.execute(
            "SELECT * FROM reflection_proposals WHERE id = 'prop_001'"
        ).fetchone()
        assert row["status"] == "promoted"

        outcome = store._connection.execute(
            "SELECT * FROM reflection_outcomes WHERE proposal_id = 'prop_001'"
        ).fetchone()
        assert outcome is not None
        assert outcome["status"] == "monitored"


def test_auto_apply_eligible(tmp_path, monkeypatch):
    """Test auto-apply of eligible proposals (lessons and brief templates)."""
    db_path = str(tmp_path / "test.db")
    store = SqliteStore(db_path)

    monkeypatch.setenv("OMNIAGENTOS_HOME", str(tmp_path))

    now = utc_now_iso()
    with store._lock:
        store._connection.execute(
            """
            INSERT INTO reflection_proposals (
                id, kind, target, current, proposed, rationale, evidence_refs_json, predicted_impact, risk_class, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "prop_lesson",
                "lesson",
                "docs/lessons/2026-07-26-reflection.md",
                "",
                "Automated lesson applied.",
                "Better stability.",
                "[]",
                "None",
                "low",
                "pending",
                now,
                now,
            ),
        )

    lesson_fp = proposal_fingerprint(
        "lesson", "docs/lessons/2026-07-26-reflection.md", "Automated lesson applied."
    )
    # Fail closed: no validated set provided → nothing is applied.
    assert auto_apply_eligible(db_path=db_path) == []
    # Ids outside the accepted set are skipped.
    assert auto_apply_eligible(db_path=db_path, accepted_fingerprints={"prop_other": lesson_fp}) == []
    # Content changed after validation (fingerprint mismatch) → skipped.
    assert (
        auto_apply_eligible(db_path=db_path, accepted_fingerprints={"prop_lesson": "stale-fp"})
        == []
    )
    results = auto_apply_eligible(
        db_path=db_path, accepted_fingerprints={"prop_lesson": lesson_fp}
    )
    assert len(results) == 1
    assert results[0]["proposal_id"] == "prop_lesson"


def test_score_outcomes_no_regression(tmp_path):
    """Test score_outcomes when regression is absent."""
    db_path = str(tmp_path / "test.db")
    store = SqliteStore(db_path)

    now = utc_now_iso()
    with store._lock:
        store._connection.execute(
            """
            INSERT INTO reflection_proposals (
                id, kind, target, current, proposed, rationale, evidence_refs_json, predicted_impact, risk_class, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "p_02",
                "model_config",
                "configs/modelintel.yaml",
                "{}",
                "{}",
                "test",
                "[]",
                "test",
                "low",
                "promoted",
                now,
                now,
            ),
        )
        store._connection.execute(
            """
            INSERT INTO reflection_outcomes (
                id, proposal_id, kind, target, applied_at, outcome_metrics_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "out_02",
                "p_02",
                "model_config",
                "configs/modelintel.yaml",
                now,
                "{}",
                "monitored",
                now,
                now,
            ),
        )

    res = score_outcomes(db_path=db_path)
    assert "out_02" in res["scored_outcomes"]
    assert len(res["rollbacks_filed"]) == 0
