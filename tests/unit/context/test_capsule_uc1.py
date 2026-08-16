"""Tests for U-C1: Context Capsule observability and AssembledContext field carrying."""

from unittest.mock import Mock, patch

from omniagentos.context.capsule import (
    REASON_INCLUDED,
    REASON_TRUNCATED_BUDGET,
    CapsuleManifest,
    CapsuleSlice,
    persist_capsule_manifest,
)
from omniagentos.memory.contracts import AssembledContext


class TestPersistCapsuleManifest:
    """Test capsule manifest persistence to events table."""

    def test_persist_manifest_writes_to_events_table(self):
        """Decisive test: manifest persists with correct payload structure."""
        # Create a minimal manifest
        slices = (
            CapsuleSlice(
                name="TASK_CONTRACT",
                kind="contract",
                rank=1,
                digest="abc123",
                bytes=100,
                included=True,
                reason_code=REASON_INCLUDED,
                present_in_brief=True,
            ),
        )
        manifest = CapsuleManifest(
            task_id="task-1",
            run_id="run-1",
            project_id="proj-1",
            contract_version="1",
            repo_sha="sha123",
            preset_digest="preset123",
            brief_digest="brief123",
            brief_bytes=500,
            byte_cap=8000,
            per_slice_cap=2000,
            compression_mode="off",
            slices=slices,
        )

        # Mock store
        store = Mock()
        store.insert_event = Mock(return_value=42)

        # Call persist with mocked context_capsule_mode
        with patch("omniagentos.context.capsule.context_capsule_mode", return_value="shadow"):
            event_id = persist_capsule_manifest(manifest, store=store)

        # Verify store was called
        assert event_id == 42
        store.insert_event.assert_called_once()

        # Verify call arguments
        call_kwargs = store.insert_event.call_args[1]
        assert call_kwargs["type"] == "context.injected"
        assert call_kwargs["actor"] == "capsule"
        assert call_kwargs["action"] == "observe_and_manifest"
        assert call_kwargs["target_type"] == "run"
        assert call_kwargs["target_id"] == "run-1"

        # Verify payload structure
        payload = call_kwargs["payload"]
        assert payload["task_id"] == "task-1"
        assert payload["run_id"] == "run-1"
        assert payload["project_id"] == "proj-1"
        assert "slices" in payload
        assert len(payload["slices"]) == 1
        assert payload["slices"][0]["name"] == "TASK_CONTRACT"
        assert payload["slices"][0]["present_in_brief"] is True

    def test_persist_manifest_mode_off_returns_none(self):
        """When capsule mode is off, persist returns None without calling store."""
        with patch("omniagentos.context.capsule.context_capsule_mode", return_value="off"):
            store = Mock()
            manifest = CapsuleManifest(
                task_id="task-1",
                run_id="run-1",
                project_id="proj-1",
                contract_version="1",
                repo_sha="sha123",
                preset_digest="preset123",
                brief_digest="brief123",
                brief_bytes=500,
                byte_cap=8000,
                per_slice_cap=2000,
                compression_mode="off",
                slices=(),
            )

            event_id = persist_capsule_manifest(manifest, store=store)

            assert event_id is None
            store.insert_event.assert_not_called()

    def test_persist_manifest_store_error_returns_none(self):
        """When store.insert_event raises, persist returns None and logs."""
        manifest = CapsuleManifest(
            task_id="task-1",
            run_id="run-1",
            project_id="proj-1",
            contract_version="1",
            repo_sha="sha123",
            preset_digest="preset123",
            brief_digest="brief123",
            brief_bytes=500,
            byte_cap=8000,
            per_slice_cap=2000,
            compression_mode="off",
            slices=(),
        )
        store = Mock()
        store.insert_event = Mock(side_effect=RuntimeError("DB error"))

        with patch("omniagentos.context.capsule.context_capsule_mode", return_value="shadow"):
            event_id = persist_capsule_manifest(manifest, store=store)

        assert event_id is None


class TestCapsuleCounterfeits:
    """Counterfeit tests to ensure measurement correctness."""

    def test_present_in_brief_must_be_measured_not_asserted(self):
        """Counterfeit: manifest claiming present_in_brief=true for absent content must fail."""
        from omniagentos.context.capsule import CapsuleSource, build_capsule_manifest

        # Create a source that claims to be in the brief but actually isn't
        prompt = "This is the delivered prompt with no special content."
        sources = [
            CapsuleSource(
                name="FICTIONAL_BLOCK",
                kind="fenced",
                content="This content is NOT in the prompt",
                rank=1,
            )
        ]

        # Build manifest with measured present_in_brief
        manifest = build_capsule_manifest(
            prompt=prompt,
            task_id="task-1",
            run_id="run-1",
            project_id="proj-1",
            contract_version="1",
            repo_sha="sha123",
            preset_digest="preset123",
            sources=sources,
            byte_cap=8000,
            per_slice_cap=2000,
            compression_mode="off",
        )

        # Verify that present_in_brief is False (measured, not asserted)
        assert manifest.slices[0].present_in_brief is False
        assert manifest.slices[0].name == "FICTIONAL_BLOCK"

    def test_truncated_flag_measured_not_asserted(self):
        """Counterfeit: truncated=false on over-budget content must fail."""
        from omniagentos.context.capsule import CapsuleSource, build_capsule_manifest

        # Create content larger than per-slice cap
        prompt = "x" * 5000  # 5KB prompt
        large_content = "y" * 3000  # 3KB content (larger than per_slice_cap)
        sources = [
            CapsuleSource(
                name="LARGE_BLOCK",
                kind="fenced",
                content=large_content,
                rank=1,
            )
        ]

        # Build with small per_slice_cap to force truncation
        manifest = build_capsule_manifest(
            prompt=prompt,
            task_id="task-1",
            run_id="run-1",
            project_id="proj-1",
            contract_version="1",
            repo_sha="sha123",
            preset_digest="preset123",
            sources=sources,
            byte_cap=8000,
            per_slice_cap=1000,  # Smaller than content
            compression_mode="off",
        )

        # Verify reason_code indicates truncation when content exceeds per_slice_cap
        # CapsuleSlice encodes truncation in reason_code, not a separate field
        assert manifest.slices[0].reason_code == REASON_TRUNCATED_BUDGET
        assert manifest.slices[0].included is True
        assert manifest.slices[0].bytes == 3000


class TestAssembledContextFields:
    """Test that AssembledContext has all required fields for observability."""

    def test_assembled_context_has_all_required_fields(self):
        """Verify AssembledContext has all six fields for runner metadata."""
        context = AssembledContext(
            block="## SUMMARY\n\nTest summary",
            scope_type="task",
            scope_id="task-1",
            node_turns=3,
            ancestor_summaries=1,
            recalls=2,
            has_summary=True,
            estimated_tokens=150,
            budget_tokens=1200,
            truncated=False,
        )

        # Verify all required fields exist and have correct types
        assert hasattr(context, 'estimated_tokens')
        assert hasattr(context, 'truncated')
        assert hasattr(context, 'node_turns')
        assert hasattr(context, 'ancestor_summaries')
        assert hasattr(context, 'recalls')
        assert hasattr(context, 'has_summary')

        # Verify values are correct type
        assert isinstance(context.estimated_tokens, int)
        assert isinstance(context.truncated, bool)
        assert isinstance(context.node_turns, int)
        assert isinstance(context.ancestor_summaries, int)
        assert isinstance(context.recalls, int)
        assert isinstance(context.has_summary, bool)

        # Verify they can be serialized to dict (for metadata)
        metadata = {
            "estimated_tokens": context.estimated_tokens,
            "truncated": context.truncated,
            "node_turns": context.node_turns,
            "ancestor_summaries": context.ancestor_summaries,
            "recalls": context.recalls,
            "has_summary": context.has_summary,
        }
        assert metadata["estimated_tokens"] == 150
        assert metadata["truncated"] is False
        assert metadata["node_turns"] == 3
        assert metadata["ancestor_summaries"] == 1
        assert metadata["recalls"] == 2
        assert metadata["has_summary"] is True

    def test_metadata_can_record_all_context_fields(self):
        """Test that runner metadata dict can capture all AssembledContext fields."""
        # Create test context with various values
        context = AssembledContext(
            block="test",
            scope_type="task",
            scope_id="task-1",
            node_turns=5,
            ancestor_summaries=2,
            recalls=3,
            has_summary=False,
            estimated_tokens=500,
            budget_tokens=1200,
            truncated=True,
        )

        # Build metadata like runner/core.py does
        metadata = {
            "estimated_tokens": context.estimated_tokens,
            "truncated": context.truncated,
            "node_turns": context.node_turns,
            "ancestor_summaries": context.ancestor_summaries,
            "recalls": context.recalls,
            "has_summary": context.has_summary,
        }

        # Verify all fields are captured
        assert len(metadata) == 6
        assert metadata["estimated_tokens"] == 500
        assert metadata["truncated"] is True
        assert metadata["node_turns"] == 5
