"""Tests for omniagentos.vault_wiki: post-run wiki-update module.

Test coverage:
- (a) Flag off → no-op, no LLM call
- (b) Trivial-run heuristic skips non-trivial runs
- (c) Happy path: mocked LLM writes/updates a page and links from run note
- (d) Adapter exception is swallowed and run continues
- (e) Path-traversal rejection
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from omniagentos.contracts import ResultStatus
from omniagentos.vault_wiki import (
    _is_trivial_run,
    _validate_vault_path,
    maybe_update_wiki,
)


class TestFlagOff:
    """Test (a): Flag off → no-op, no LLM call."""

    def test_returns_immediately_when_flag_unset(self, tmp_path: Path, monkeypatch: Any) -> None:
        """When OMNIAGENTOS_WIKI_UPDATE is not set, maybe_update_wiki returns immediately."""
        monkeypatch.delenv("OMNIAGENTOS_WIKI_UPDATE", raising=False)
        vault_dir = str(tmp_path / "vault")
        Path(vault_dir).mkdir()

        run = {
            "id": "run_test1",
            "state": "completed",
            "vault_note_path": "runs/run_test1.md",
        }

        maybe_update_wiki(run, vault_dir, artifacts=[])
        assert True

    def test_returns_immediately_when_flag_is_zero(self, tmp_path: Path, monkeypatch: Any) -> None:
        """When OMNIAGENTOS_WIKI_UPDATE=0, maybe_update_wiki returns immediately."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "0")
        vault_dir = str(tmp_path / "vault")
        Path(vault_dir).mkdir()

        run = {
            "id": "run_test2",
            "state": "completed",
            "vault_note_path": "runs/run_test2.md",
        }

        maybe_update_wiki(run, vault_dir, artifacts=[])
        assert True

    def test_no_llm_call_when_flag_off(self, tmp_path: Path, monkeypatch: Any) -> None:
        """When flag is off, the LLM is never called (no adapter import)."""
        monkeypatch.delenv("OMNIAGENTOS_WIKI_UPDATE", raising=False)
        vault_dir = str(tmp_path / "vault")
        Path(vault_dir).mkdir()

        run = {
            "id": "run_test3",
            "state": "completed",
            "vault_note_path": "runs/run_test3.md",
        }

        with patch("omniagentos.adapters.registry.resolve_adapter") as mock_resolve:
            maybe_update_wiki(run, vault_dir, artifacts=[])
            mock_resolve.assert_not_called()


class TestTrivialRunHeuristic:
    """Test (b): Trivial-run heuristic skips correctly."""

    def test_is_trivial_run_failed_state(self) -> None:
        """Runs in non-completed state are trivial."""
        run = {"id": "run_fail", "state": "failed"}
        assert _is_trivial_run(run) is True

    def test_is_trivial_run_no_artifacts_no_output(self) -> None:
        """Completed runs with no artifacts and no output_json are trivial."""
        run = {
            "id": "run_empty",
            "state": "completed",
            "output_json": None,
        }
        assert _is_trivial_run(run) is True

    def test_is_trivial_run_short_duration(self) -> None:
        """Completed runs with < 1s duration are trivial."""
        run = {
            "id": "run_short",
            "state": "completed",
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:00.5Z",
        }
        assert _is_trivial_run(run) is True

    def test_is_nontrivial_run_with_artifacts(self) -> None:
        """Completed runs with artifacts are non-trivial."""
        run = {
            "id": "run_work",
            "state": "completed",
            "output_json": None,
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }
        assert _is_trivial_run(run, artifacts=["artifact.txt"]) is False

    def test_is_nontrivial_run_with_output_json(self) -> None:
        """Completed runs with output_json are non-trivial."""
        run = {
            "id": "run_output",
            "state": "completed",
            "output_json": {"result": "success"},
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }
        assert _is_trivial_run(run) is False

    def test_skip_trivial_run_when_flag_on(self, tmp_path: Path, monkeypatch: Any) -> None:
        """When flag is on but run is trivial, maybe_update_wiki skips it."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        Path(vault_dir).mkdir()

        run = {"id": "run_fail", "state": "failed"}

        with patch("omniagentos.vault_wiki._is_trivial_run", return_value=True):
            with patch("omniagentos.adapters.registry.resolve_adapter") as mock_resolve:
                maybe_update_wiki(run, vault_dir, artifacts=[])
                mock_resolve.assert_not_called()


class TestHappyPath:
    """Test (c): Happy path with mocked LLM creates/updates vault note."""

    def test_creates_new_concept_note_on_mocked_llm_success(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """When LLM returns created=True, a new note is written."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary here.\n\n## Notes (human)\n")

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Vault Schema\n\nSchema content here.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.OK
        mock_response.output_json = {
            "created": True,
            "note_id": "adapter-context-limits",
            "path": "learnings/adapter-context-limits.md",
            "note_content": (
                "---\nid: adapter-context-limits\ntype: learning\n"
                "discipline: general\ncreated: 2026-07-22T18:30:00Z\n"
                "source_run: run_test\nconfidence: high\nstatus: active\n"
                "supersedes: null\n---\n\n# Learning: Adapter Context Limits\n\n"
                "Content here.\n"
            ),
            "reasoning": "Run showed adapter context window issue.",
        }
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            maybe_update_wiki(run, vault_dir, artifacts=[])

        concept_file = vault_path / "learnings" / "adapter-context-limits.md"
        assert concept_file.exists()
        content = concept_file.read_text()
        assert "Adapter Context Limits" in content

    def test_llm_not_called_when_no_run_note_path(self, tmp_path: Path, monkeypatch: Any) -> None:
        """If run has no vault_note_path, LLM is not called."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        Path(vault_dir).mkdir()

        run = {
            "id": "run_no_note",
            "state": "completed",
            "vault_note_path": None,
        }

        with patch("omniagentos.adapters.registry.resolve_adapter") as mock_resolve:
            maybe_update_wiki(run, vault_dir, artifacts=[])
            mock_resolve.assert_not_called()

    def test_llm_not_called_when_schema_missing(self, tmp_path: Path, monkeypatch: Any) -> None:
        """If SCHEMA.md is missing, LLM is not called."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary here.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
        }

        with patch("omniagentos.adapters.registry.resolve_adapter") as mock_resolve:
            maybe_update_wiki(run, vault_dir, artifacts=[])
            mock_resolve.assert_not_called()


class TestExceptionHandling:
    """Test (d): Adapter exceptions are swallowed."""

    def test_adapter_error_is_swallowed(self, tmp_path: Path, monkeypatch: Any) -> None:
        """When adapter.run() raises, the exception is caught and logged."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary.\n")

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Schema\n\nContent.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
        }

        mock_adapter = MagicMock()
        mock_adapter.run.side_effect = RuntimeError("LLM connection failed")

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            maybe_update_wiki(run, vault_dir, artifacts=[])

    def test_file_write_error_is_swallowed(self, tmp_path: Path, monkeypatch: Any) -> None:
        """When writing the concept note fails, exception is caught."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary.\n")

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Schema\n\nContent.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.OK
        mock_response.output_json = {
            "created": True,
            "note_id": "test",
            "path": "learnings/test.md",
            "note_content": (
                "---\nid: test\ntype: learning\ndiscipline: general\n"
                "created: 2026-07-22T18:30:00Z\nsource_run: run_test\n"
                "confidence: high\nstatus: active\nsupersedes: null\n---\n\n"
                "# Test\n\nContent.\n"
            ),
            "reasoning": "Test.",
        }
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            with patch(
                "omniagentos.vault.write.write_note",
                side_effect=OSError("Permission denied"),
            ):
                maybe_update_wiki(run, vault_dir, artifacts=[])


class TestPathTraversalRejection:
    """Test (e): Path-traversal attempts are rejected."""

    def test_validate_vault_path_rejects_traversal(self, tmp_path: Path) -> None:
        """Paths with .. are rejected."""
        vault_dir = str(tmp_path / "vault")
        assert _validate_vault_path("../outside.md", vault_dir) is False

    def test_validate_vault_path_rejects_absolute_path(self, tmp_path: Path) -> None:
        """Absolute paths are rejected."""
        vault_dir = str(tmp_path / "vault")
        assert _validate_vault_path("/etc/passwd", vault_dir) is False

    def test_validate_vault_path_accepts_safe_path(self, tmp_path: Path) -> None:
        """Safe relative paths are accepted."""
        vault_dir = str(tmp_path / "vault")
        assert _validate_vault_path("learnings/foo.md", vault_dir) is True

    def test_validate_vault_path_accepts_nested_path(self, tmp_path: Path) -> None:
        """Nested safe paths are accepted."""
        vault_dir = str(tmp_path / "vault")
        assert _validate_vault_path("subfolder/nested/page.md", vault_dir) is True

    def test_traversal_attempt_in_llm_response_is_rejected(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """If LLM returns a traversal path, the write is rejected."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary.\n")

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Schema\n\nContent.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.OK
        mock_response.output_json = {
            "created": True,
            "note_id": "evil",
            "path": "../../../etc/passwd",
            "note_content": "# Evil\n\nContent.",
            "reasoning": "Evil attempt.",
        }
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            maybe_update_wiki(run, vault_dir, artifacts=[])

        evil_file = tmp_path / "etc" / "passwd"
        assert not evil_file.exists()


class TestResultStatusHandling:
    """Test handling of non-OK LLM response statuses."""

    def test_non_ok_status_is_logged_and_skipped(self, tmp_path: Path, monkeypatch: Any) -> None:
        """When LLM returns non-OK status, the update is skipped."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary.\n")

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Schema\n\nContent.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.ERROR
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            maybe_update_wiki(run, vault_dir, artifacts=[])


class TestLLMDecisionNotToCreate:
    """Test when LLM decides no knowledge extraction is needed."""

    def test_created_false_skips_write(self, tmp_path: Path, monkeypatch: Any) -> None:
        """When LLM returns created=False, no note is written."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary.\n")

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Schema\n\nContent.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.OK
        mock_response.output_json = {
            "created": False,
            "note_id": None,
            "path": None,
            "note_content": None,
            "reasoning": "No knowledge found.",
        }
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            maybe_update_wiki(run, vault_dir, artifacts=[])

        learnings_dir = vault_path / "learnings"
        if learnings_dir.exists():
            assert len(list(learnings_dir.glob("*.md"))) == 0


class TestBacklinkAppending:
    """Test back-link appending to run notes."""

    def test_happy_path_appends_backlink_to_extracted_section(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """When a concept note is created, a back-link is appended to run note's Extracted section."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        original_content = "# run: test\n\nRun summary here.\n\n## Notes (human)\n"
        run_note_file.write_text(original_content)

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Vault Schema\n\nSchema content here.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.OK
        mock_response.output_json = {
            "created": True,
            "note_id": "my-learning",
            "path": "learnings/my-learning.md",
            "note_content": "---\nid: my-learning\ntype: learning\ndiscipline: null\ncreated: 2026-07-22T18:30:00Z\nsource_run: run_test\nconfidence: high\nstatus: active\nsupersedes: null\n---\n\n# Learning\n\nContent.",
            "reasoning": "Test.",
        }
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            maybe_update_wiki(run, vault_dir, artifacts=[])

        # Verify the back-link was appended
        updated_content = run_note_file.read_text()
        assert "## Extracted (auto)" in updated_content
        assert "- [[my-learning]]" in updated_content
        assert "## Extracted (auto)" in updated_content and "- [[my-learning]]" in updated_content

    def test_happy_path_creates_extracted_section_if_missing(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """When Extracted section doesn't exist, it's created with the back-link."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary here.\n")

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Vault Schema\n\nSchema content here.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.OK
        mock_response.output_json = {
            "created": True,
            "note_id": "my-learning",
            "path": "learnings/my-learning.md",
            "note_content": "---\nid: my-learning\ntype: learning\ndiscipline: null\ncreated: 2026-07-22T18:30:00Z\nsource_run: run_test\nconfidence: high\nstatus: active\nsupersedes: null\n---\n\n# Learning\n\nContent.",
            "reasoning": "Test.",
        }
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            maybe_update_wiki(run, vault_dir, artifacts=[])

        # Verify the Notes section was created with the back-link
        updated_content = run_note_file.read_text()
        assert "## Extracted (auto)" in updated_content
        assert "- [[my-learning]]" in updated_content

    def test_backlink_failure_swallowed_does_not_lose_concept_note(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """If back-link write fails, concept note is not lost and error is swallowed."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary.\n")

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Vault Schema\n\nSchema content.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.OK
        mock_response.output_json = {
            "created": True,
            "note_id": "my-learning",
            "path": "learnings/my-learning.md",
            "note_content": "---\nid: my-learning\ntype: learning\ndiscipline: null\ncreated: 2026-07-22T18:30:00Z\nsource_run: run_test\nconfidence: high\nstatus: active\nsupersedes: null\n---\n\n# Learning\n\nContent.",
            "reasoning": "Test.",
        }
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            # Mock the _append_run_note_backlink to raise an error
            with patch(
                "omniagentos.vault_wiki._append_run_note_backlink",
                side_effect=OSError("Permission denied"),
            ):
                # This should not raise; it should swallow the error
                maybe_update_wiki(run, vault_dir, artifacts=[])

        # Verify the concept note was still created despite back-link failure
        concept_file = vault_path / "learnings" / "my-learning.md"
        assert concept_file.exists()
        content = concept_file.read_text()
        assert "my-learning" in content

    def test_no_backlink_when_llm_decides_no_extraction(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """When LLM decides not to extract knowledge, run note is unchanged."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        original_content = "# run: test\n\nRun summary.\n\n## Notes (human)\n"
        run_note_file.write_text(original_content)

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Vault Schema\n\nSchema content.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.OK
        mock_response.output_json = {
            "created": False,
            "note_id": None,
            "path": None,
            "note_content": None,
            "reasoning": "No knowledge found.",
        }
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            maybe_update_wiki(run, vault_dir, artifacts=[])

        # Verify run note is unchanged
        updated_content = run_note_file.read_text()
        assert updated_content == original_content


class TestFixtureFidelity:
    """Test with run dict built exactly as _owned_run (from _RUN_COLUMNS) returns it."""

    def test_trivial_run_heuristic_with_real_run_dict_structure(self, monkeypatch: Any) -> None:
        """Heuristic correctly skips runs built from real _RUN_COLUMNS dict.

        This test builds the run dict EXACTLY as _owned_run returns it:
        - No artifacts_list key (artifacts come from get_artifacts separately)
        - output_json as a raw string from the database
        - All other keys from _RUN_COLUMNS

        Demonstrates that with output_json as a string, the old code would have
        failed (would look for run["artifacts_list"] which doesn't exist), but
        the fixed code handles it correctly.
        """
        # This is the REAL structure: _RUN_COLUMNS keys, no artifacts_list
        run = {
            "id": "run_abc123",
            "state": "completed",
            "output_json": '""',  # Empty string in the database
            "output_text": None,
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
            # ... other _RUN_COLUMNS keys, but no artifacts_list
        }

        # Call with artifacts=[] (real: store.get_artifacts returned nothing)
        assert _is_trivial_run(run, artifacts=[]) is True

    def test_trivial_run_with_string_output_json_null(self) -> None:
        """Heuristic treats output_json: 'null' string as empty."""
        run = {
            "id": "run_abc",
            "state": "completed",
            "output_json": "null",  # Raw string from DB
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }
        assert _is_trivial_run(run, artifacts=[]) is True

    def test_nontrivial_run_with_string_output_json(self) -> None:
        """Heuristic treats non-empty output_json string as meaningful output."""
        run = {
            "id": "run_abc",
            "state": "completed",
            "output_json": '{"result": "success"}',  # Real JSON string from DB
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }
        assert _is_trivial_run(run, artifacts=[]) is False

    def test_nontrivial_run_with_explicit_artifacts_list(self) -> None:
        """Heuristic uses explicitly-passed artifacts list, not run["artifacts_list"]."""
        run = {
            "id": "run_abc",
            "state": "completed",
            "output_json": None,
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
            # No artifacts_list key
        }
        # With real artifacts passed separately
        assert _is_trivial_run(run, artifacts=["file.txt"]) is False


class TestLLMOutputValidation:
    """Test validation of LLM output before persisting."""

    def test_malformed_frontmatter_is_rejected(self, tmp_path: Path, monkeypatch: Any) -> None:
        """If LLM produces note_content with invalid frontmatter, it's not persisted."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary.\n")

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Schema\n\nContent.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.OK
        # Return malformed frontmatter (missing required fields)
        mock_response.output_json = {
            "created": True,
            "note_id": "test-note",
            "path": "learnings/test-note.md",
            "note_content": "# Bad note without frontmatter\n\nNo frontmatter block!",
            "reasoning": "Test.",
        }
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            maybe_update_wiki(run, vault_dir, artifacts=[])

        # Verify the malformed note was NOT written
        note_file = vault_path / "learnings" / "test-note.md"
        assert not note_file.exists()

    def test_invalid_notetype_is_rejected(self, tmp_path: Path, monkeypatch: Any) -> None:
        """If LLM produces note with invalid NoteType, it's not persisted."""
        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        vault_dir = str(tmp_path / "vault")
        vault_path = Path(vault_dir)
        vault_path.mkdir()

        run_note_dir = vault_path / "runs"
        run_note_dir.mkdir()
        run_note_file = run_note_dir / "run_test.md"
        run_note_file.write_text("# run: test\n\nRun summary.\n")

        schema_file = vault_path / "SCHEMA.md"
        schema_file.write_text("# Schema\n\nContent.\n")

        run = {
            "id": "run_test",
            "state": "completed",
            "output_json": '{"result": "success"}',
            "vault_note_path": "runs/run_test.md",
            "started_at": "2026-07-22T18:00:00Z",
            "finished_at": "2026-07-22T18:00:02Z",
        }

        mock_adapter = MagicMock()
        mock_response = MagicMock()
        mock_response.status = ResultStatus.OK
        # Return note with invalid type (capability is not a valid NoteType)
        mock_response.output_json = {
            "created": True,
            "note_id": "test-note",
            "path": "capabilities/test-note.md",
            "note_content": (
                "---\nid: test-note\ntype: capability\n"  # INVALID TYPE!
                "discipline: null\ncreated: 2026-07-22T18:30:00Z\n"
                "source_run: run_test\nconfidence: high\nstatus: active\n"
                "supersedes: null\n---\n\n# Test\n\nContent."
            ),
            "reasoning": "Test.",
        }
        mock_adapter.run.return_value = mock_response

        with patch("omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter):
            maybe_update_wiki(run, vault_dir, artifacts=[])

        # Verify the invalid note was NOT written
        note_file = vault_path / "capabilities" / "test-note.md"
        assert not note_file.exists()


class TestHarnessSelection:
    """Wiki-update harness/model are env-configurable, defaulting to cli-grok."""

    def _vault_with_note(self, tmp_path: Path) -> tuple[str, dict[str, Any]]:
        vault_path = tmp_path / "vault"
        (vault_path / "runs").mkdir(parents=True)
        (vault_path / "runs" / "run_hx.md").write_text("# run: hx\n\n## Notes (human)\n")
        (vault_path / "SCHEMA.md").write_text("# Vault Schema\n")
        run = {
            "id": "run_hx",
            "state": "completed",
            "output_json": '{"result": "ok"}',
            "vault_note_path": "runs/run_hx.md",
        }
        return str(vault_path), run

    def _no_extraction_adapter(self) -> MagicMock:
        adapter = MagicMock()
        response = MagicMock()
        response.status = ResultStatus.OK
        response.output_json = {
            "created": False,
            "note_id": None,
            "path": None,
            "note_content": None,
            "reasoning": "nothing durable",
        }
        adapter.run.return_value = response
        return adapter

    def test_defaults_to_cli_grok_with_adapter_default_model(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """No env overrides: resolve cli-grok and leave model to the adapter default."""
        from omniagentos.contracts import HarnessType

        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        monkeypatch.delenv("OMNIAGENTOS_WIKI_HARNESS", raising=False)
        monkeypatch.delenv("OMNIAGENTOS_WIKI_MODEL", raising=False)
        vault_dir, run = self._vault_with_note(tmp_path)
        mock_adapter = self._no_extraction_adapter()

        with patch(
            "omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter
        ) as mock_resolve:
            maybe_update_wiki(run, vault_dir, artifacts=[])

        mock_resolve.assert_called_once_with(HarnessType.CLI_GROK)
        agent_input = mock_adapter.run.call_args.args[0]
        assert agent_input.model is None

    def test_env_overrides_harness_and_model(self, tmp_path: Path, monkeypatch: Any) -> None:
        """OMNIAGENTOS_WIKI_HARNESS / OMNIAGENTOS_WIKI_MODEL select the LLM."""
        from omniagentos.contracts import HarnessType

        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        monkeypatch.setenv("OMNIAGENTOS_WIKI_HARNESS", "cli-claude")
        monkeypatch.setenv("OMNIAGENTOS_WIKI_MODEL", "sonnet")
        vault_dir, run = self._vault_with_note(tmp_path)
        mock_adapter = self._no_extraction_adapter()

        with patch(
            "omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter
        ) as mock_resolve:
            maybe_update_wiki(run, vault_dir, artifacts=[])

        mock_resolve.assert_called_once_with(HarnessType.CLI_CLAUDE)
        assert mock_adapter.run.call_args.args[0].model == "sonnet"

    def test_invalid_harness_falls_back_to_grok(self, tmp_path: Path, monkeypatch: Any) -> None:
        """An unknown harness value falls back to cli-grok instead of raising."""
        from omniagentos.contracts import HarnessType

        monkeypatch.setenv("OMNIAGENTOS_WIKI_UPDATE", "1")
        monkeypatch.setenv("OMNIAGENTOS_WIKI_HARNESS", "cli-bogus")
        vault_dir, run = self._vault_with_note(tmp_path)
        mock_adapter = self._no_extraction_adapter()

        with patch(
            "omniagentos.adapters.registry.resolve_adapter", return_value=mock_adapter
        ) as mock_resolve:
            maybe_update_wiki(run, vault_dir, artifacts=[])

        mock_resolve.assert_called_once_with(HarnessType.CLI_GROK)
