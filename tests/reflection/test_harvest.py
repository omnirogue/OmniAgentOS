"""Unit and integration tests for the nightly reflection harvesting engine and adapters."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

from omniagentos.reflection.adapters import (
    ClaudeSourceAdapter,
    GeminiSourceAdapter,
    KimiSourceAdapter,
    enforce_token_cap,
    read_and_sample_file,
)
from omniagentos.reflection.contracts import ImprovementProposal, ReflectionEvidence
from omniagentos.reflection.harvest import harvest_evidence, query_sqlite_metrics
from omniagentos.reflection.taxonomy import classify_content


def test_taxonomy_classification() -> None:
    """Verify regex-based mechanical taxonomy tagging for various error patterns."""
    assert "rate_limit" in classify_content("We hit a rate limit while calling Gemini CLI.")
    assert "rate_limit" in classify_content("429 error: too many requests")
    assert "wrong_model_id" in classify_content(
        "ModelNotFoundError: model gemini-3.1-pro not found"
    )
    assert "uncommitted_work" in classify_content("Your tree is dirty or has uncommitted work.")
    assert "scope_violation" in classify_content("workspace outside approved roots")
    assert "timeout" in classify_content("TTFT watchdog triggered a timeout.")
    assert "policy_denial" in classify_content("Request denied: openhands_gate policy block")
    assert "hung" in classify_content("Process hung and never returned.")
    assert "killed" in classify_content("Task killed due to supervisor-shutdown.")


def test_enforce_token_cap() -> None:
    """Verify that token capping truncates long texts based on character approximation."""
    short_text = "Hello world"
    txt, hit = enforce_token_cap(short_text, token_cap=10)
    assert not hit
    assert txt == short_text

    long_text = "A" * 50
    txt, hit = enforce_token_cap(long_text, token_cap=5)
    assert hit
    assert len(txt) <= 20 or "... [TRUNCATED" in txt


def test_read_and_sample_file(tmp_path: Path) -> None:
    """Verify reading within byte caps and head/tail sampling for oversized files."""
    small_file = tmp_path / "small.txt"
    small_file.write_text("Hello standard file size", encoding="utf-8")

    content, hit, total = read_and_sample_file(small_file, byte_cap=100)
    assert not hit
    assert content == "Hello standard file size"
    assert total == len("Hello standard file size")

    large_file = tmp_path / "large.txt"
    # Create an oversized file with some error patterns in the middle
    large_text = (
        ("Line standard data\n" * 2000)
        + "ModelNotFoundError: failed model\n"
        + ("Line standard data\n" * 2000)
    )
    large_file.write_text(large_text, encoding="utf-8")

    content, hit, total = read_and_sample_file(large_file, byte_cap=2000)
    assert hit
    assert "... [TRUNCATED" in content
    # Grepped error section should be included
    assert "[ERRORS GREPPED FROM TRUNCATED CONTENT]" in content
    assert "ModelNotFoundError" in content


def test_improvement_proposal_schema() -> None:
    """Verify the Pydantic ImprovementProposal schema matches defined layout."""
    proposal_data = {
        "id": "imp_123",
        "kind": "model_config",
        "target": {"file": "configs/modelintel.yaml", "key": "gemini-3.1-pro.available"},
        "current": "true",
        "proposed": "false",
        "rationale": "Model fails liveness probe",
        "evidence_refs": ["db:run:run_123"],
        "predicted_impact": "High stability",
        "risk_class": "low",
        "promotion_status": "pending",
    }
    proposal = ImprovementProposal.model_validate(proposal_data)
    assert proposal.id == "imp_123"
    assert proposal.target.file == "configs/modelintel.yaml"


def test_sqlite_query_metrics(tmp_path: Path) -> None:
    """Verify that sqlite querying handles schema variation and fetches windowed rows."""
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE swarm_runs (id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT)")
    conn.execute(
        "INSERT INTO swarm_runs (id, created_at, updated_at) "
        "VALUES ('swr_1', '2026-07-26T12:00:00Z', '2026-07-26T12:00:00Z')"
    )
    conn.commit()
    conn.close()

    metrics = query_sqlite_metrics(str(db_file), "2026-07-26T00:00:00Z")
    assert len(metrics["runs"]) == 1
    assert metrics["runs"][0]["id"] == "swr_1"

    # If since_iso filters it out
    metrics_filtered = query_sqlite_metrics(str(db_file), "2026-07-27T00:00:00Z")
    assert len(metrics_filtered["runs"]) == 0


def test_adapters_discovery_and_extraction(tmp_path: Path) -> None:
    """Test individual source adapters discover and extract correctly."""
    home_mock = tmp_path / "home"
    home_mock.mkdir()

    # 1. Claude Adapter Mock Setup
    claude_dir = home_mock / ".claude"
    claude_dir.mkdir()
    projects_dir = claude_dir / "projects" / "test_proj"
    projects_dir.mkdir(parents=True)
    session_file = projects_dir / "session.jsonl"
    session_file.write_text("Claude transcript event lines", encoding="utf-8")

    # 2. Gemini Adapter Mock Setup
    gemini_dir = home_mock / ".gemini" / "tmp"
    gemini_dir.mkdir(parents=True)
    gem_log = gemini_dir / "gemini_session.log"
    gem_log.write_text("Gemini execution logs", encoding="utf-8")

    # 3. Kimi Adapter Mock Setup
    kimi_dir = home_mock / ".kimi-code"
    kimi_dir.mkdir()
    kimi_index = kimi_dir / "session_index.jsonl"
    kimi_index.write_text(
        json.dumps(
            {"sessionId": "kimi_ses_1", "sessionDir": "~/.kimi-code/sessions/s1", "workDir": "/app"}
        ),
        encoding="utf-8",
    )
    s1_dir = kimi_dir / "sessions" / "s1"
    s1_dir.mkdir(parents=True)
    state_json = s1_dir / "state.json"
    state_json.write_text(
        json.dumps({"title": "Kimi Task", "lastPrompt": "Do work"}), encoding="utf-8"
    )
    wire_dir = s1_dir / "agents" / "main"
    wire_dir.mkdir(parents=True)
    wire_jsonl = wire_dir / "wire.jsonl"
    wire_jsonl.write_text(
        json.dumps(
            {
                "type": "usage",
                "usage": {"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.002},
            }
        )
        + "\n"
        + json.dumps({"role": "agent", "text": "I performed the fix."})
        + "\n",
        encoding="utf-8",
    )

    with (
        patch("pathlib.Path.home", return_value=home_mock),
        patch("os.path.expanduser", lambda x: x.replace("~", str(home_mock))),
    ):
        # Test Claude
        claude_adapter = ClaudeSourceAdapter()
        claude_refs = claude_adapter.discover(window_start_epoch=0)
        assert len(claude_refs) == 1
        claude_digest = claude_adapter.extract(claude_refs[0], byte_cap=1000, token_cap=500)
        assert claude_digest.bytes_read > 0
        assert "Claude transcript" in claude_digest.summary_or_sample

        # Test Gemini
        gemini_adapter = GeminiSourceAdapter()
        gemini_refs = gemini_adapter.discover(window_start_epoch=0)
        assert len(gemini_refs) == 1
        gemini_digest = gemini_adapter.extract(gemini_refs[0], byte_cap=1000, token_cap=500)
        assert "Gemini execution" in gemini_digest.summary_or_sample

        # Test Kimi
        kimi_adapter = KimiSourceAdapter()
        kimi_refs = kimi_adapter.discover(window_start_epoch=0)
        assert len(kimi_refs) == 1
        kimi_digest = kimi_adapter.extract(kimi_refs[0], byte_cap=5000, token_cap=1000)
        assert kimi_digest.bytes_read > 0
        assert "Kimi Task" in kimi_digest.summary_or_sample
        assert "I performed the fix" in kimi_digest.summary_or_sample


def test_harvest_evidence_pipeline(tmp_path: Path) -> None:
    """Verifies the end-to-end harvest pipeline writes valid outputs and records the run."""
    db_file = tmp_path / "omniagentos.db"
    conn = sqlite3.connect(db_file)
    conn.execute(
        "CREATE TABLE reflection_runs (id TEXT PRIMARY KEY, started_at TEXT, finished_at TEXT, status TEXT, sources_read TEXT, bytes_read INTEGER, caps_hit TEXT)"
    )
    conn.commit()
    conn.close()

    # Mock default_db_path and default_vault_dir to our tmp_path
    with (
        patch("omniagentos.reflection.harvest.default_db_path", return_value=str(db_file)),
        patch(
            "omniagentos.reflection.harvest.default_vault_dir", return_value=str(tmp_path / "vault")
        ),
        patch("omniagentos.reflection.harvest._repo_root", return_value=tmp_path),
    ):
        # Create configs
        config_dir = tmp_path / "configs"
        config_dir.mkdir()
        reflection_yaml = config_dir / "reflection.yaml"
        reflection_yaml.write_text(
            """
window_hours: 36
caps:
  per_source_bytes: 1000
  per_source_tokens: 500
  total_tokens: 2000
adapters:
  claude: false
  gemini: false
  kimi: false
  codex: false
  grok: false
""",
            encoding="utf-8",
        )

        evidence = harvest_evidence(date_str="2026-07-26")

        assert isinstance(evidence, ReflectionEvidence)
        assert evidence.date == "2026-07-26"

        # Check output files written
        evidence_json = tmp_path / "var" / "reflection" / "2026-07-26" / "evidence.json"
        digest_md = tmp_path / "var" / "reflection" / "2026-07-26" / "digest.md"

        assert evidence_json.exists()
        assert digest_md.exists()

        loaded_ev = json.loads(evidence_json.read_text(encoding="utf-8"))
        assert loaded_ev["date"] == "2026-07-26"

        # Check sqlite reflection_runs row
        conn = sqlite3.connect(db_file)
        row = conn.execute("SELECT * FROM reflection_runs").fetchone()
        assert row is not None
        assert row[3] == "completed"
        conn.close()
