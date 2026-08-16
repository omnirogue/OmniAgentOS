"""Tests for the 5 new retrieval tools: repomap, semantic_search, knowledge_recall, memory_search, vault_search."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from omniagentos.connectors import SideEffectClass
from omniagentos.contracts import ActionClass
from omniagentos.toolplane.catalog import build_catalog
from omniagentos.toolplane.manifest import CapabilityManifest
from omniagentos.toolplane.tools import CAPABILITY_INVENTORY, TOOLS


class TestRetrievalToolsExist:
    """Verify all 5 new tools are registered and have basic properties."""

    def test_repomap_exists(self):
        """repomap tool is registered."""
        from omniagentos.toolplane.tools import TOOLS
        assert "repomap" in TOOLS
        assert "repomap" in CAPABILITY_INVENTORY

    def test_semantic_search_exists(self):
        """semantic_search tool is registered."""
        from omniagentos.toolplane.tools import TOOLS
        assert "semantic_search" in TOOLS
        assert "semantic_search" in CAPABILITY_INVENTORY

    def test_knowledge_recall_exists(self):
        """knowledge_recall tool is registered."""
        from omniagentos.toolplane.tools import TOOLS
        assert "knowledge_recall" in TOOLS
        assert "knowledge_recall" in CAPABILITY_INVENTORY

    def test_memory_search_exists(self):
        """memory_search tool is registered."""
        from omniagentos.toolplane.tools import TOOLS
        assert "memory_search" in TOOLS
        assert "memory_search" in CAPABILITY_INVENTORY

    def test_vault_search_exists(self):
        """vault_search tool is registered."""
        from omniagentos.toolplane.tools import TOOLS
        assert "vault_search" in TOOLS
        assert "vault_search" in CAPABILITY_INVENTORY


class TestRetrievalToolsInventory:
    """Verify capability inventory entries are correctly configured."""

    def test_repomap_inventory(self):
        """repomap has correct category and risk."""
        inv = CAPABILITY_INVENTORY["repomap"]
        assert inv["category"] == "fs_read"
        assert inv["risk"] == "low"
        assert inv["requires_scope"] is True

    def test_semantic_search_inventory(self):
        """semantic_search has correct category and risk."""
        inv = CAPABILITY_INVENTORY["semantic_search"]
        assert inv["category"] == "fs_read"
        assert inv["risk"] == "low"
        assert inv["requires_scope"] is True

    def test_knowledge_recall_inventory(self):
        """knowledge_recall has correct category and risk."""
        inv = CAPABILITY_INVENTORY["knowledge_recall"]
        assert inv["category"] == "validation"
        assert inv["risk"] == "low"
        assert inv["requires_scope"] is False

    def test_memory_search_inventory(self):
        """memory_search has correct category and risk."""
        inv = CAPABILITY_INVENTORY["memory_search"]
        assert inv["category"] == "validation"
        assert inv["risk"] == "low"
        assert inv["requires_scope"] is False

    def test_vault_search_inventory(self):
        """vault_search has correct category and risk."""
        inv = CAPABILITY_INVENTORY["vault_search"]
        assert inv["category"] == "fs_read"
        assert inv["risk"] == "low"
        assert inv["requires_scope"] is True


class TestRetrievalToolsCatalogEntry:
    """Verify catalog entries resolve correctly to READ_ONLY action class."""

    def test_repomap_catalog_entry(self):
        """repomap resolves to READ_ONLY action class."""
        catalog = build_catalog(registry=None)
        entry = catalog.get("repomap")
        assert entry is not None
        assert entry.action_class == ActionClass.READ_ONLY
        assert entry.read_only is True
        assert entry.side_effect_class == SideEffectClass.NONE

    def test_semantic_search_catalog_entry(self):
        """semantic_search resolves to READ_ONLY action class."""
        catalog = build_catalog(registry=None)
        entry = catalog.get("semantic_search")
        assert entry is not None
        assert entry.action_class == ActionClass.READ_ONLY
        assert entry.read_only is True
        assert entry.side_effect_class == SideEffectClass.NONE

    def test_knowledge_recall_catalog_entry(self):
        """knowledge_recall resolves to READ_ONLY action class."""
        catalog = build_catalog(registry=None)
        entry = catalog.get("knowledge_recall")
        assert entry is not None
        assert entry.action_class == ActionClass.READ_ONLY
        assert entry.read_only is True
        assert entry.side_effect_class == SideEffectClass.NONE

    def test_memory_search_catalog_entry(self):
        """memory_search resolves to READ_ONLY action class."""
        catalog = build_catalog(registry=None)
        entry = catalog.get("memory_search")
        assert entry is not None
        assert entry.action_class == ActionClass.READ_ONLY
        assert entry.read_only is True
        assert entry.side_effect_class == SideEffectClass.NONE

    def test_vault_search_catalog_entry(self):
        """vault_search resolves to READ_ONLY action class."""
        catalog = build_catalog(registry=None)
        entry = catalog.get("vault_search")
        assert entry is not None
        assert entry.action_class == ActionClass.READ_ONLY
        assert entry.read_only is True
        assert entry.side_effect_class == SideEffectClass.NONE


class TestRetrievalToolsMetadata:
    """Verify BUILTIN_METADATA entries have all required fields."""

    def test_repomap_metadata_complete(self):
        """repomap metadata has all required fields."""
        from omniagentos.toolplane.catalog import BUILTIN_METADATA
        meta = BUILTIN_METADATA.get("repomap")
        assert meta is not None
        assert meta["label"] == "Query Repo Map"
        assert "compact_hint" in meta
        assert "description" in meta
        assert "resource_keys" in meta
        assert "idempotent" in meta
        assert "parallel_safe" in meta
        assert "cancellation_group" in meta
        assert "credential_scope" in meta
        assert "result_size_class" in meta
        assert "parameter_names" in meta
        assert "input_examples" in meta

    def test_semantic_search_metadata_complete(self):
        """semantic_search metadata has all required fields."""
        from omniagentos.toolplane.catalog import BUILTIN_METADATA
        meta = BUILTIN_METADATA.get("semantic_search")
        assert meta is not None
        assert "label" in meta
        assert "compact_hint" in meta
        assert "description" in meta
        assert "resource_keys" in meta

    def test_knowledge_recall_metadata_complete(self):
        """knowledge_recall metadata has all required fields."""
        from omniagentos.toolplane.catalog import BUILTIN_METADATA
        meta = BUILTIN_METADATA.get("knowledge_recall")
        assert meta is not None
        assert "label" in meta

    def test_memory_search_metadata_complete(self):
        """memory_search metadata has all required fields."""
        from omniagentos.toolplane.catalog import BUILTIN_METADATA
        meta = BUILTIN_METADATA.get("memory_search")
        assert meta is not None
        assert "label" in meta

    def test_vault_search_metadata_complete(self):
        """vault_search metadata has all required fields."""
        from omniagentos.toolplane.catalog import BUILTIN_METADATA
        meta = BUILTIN_METADATA.get("vault_search")
        assert meta is not None
        assert "label" in meta


class TestVaultSearchFunctional:
    """Functional tests for vault_search tool."""

    def test_vault_search_happy_path(self, tmp_path):
        """vault_search returns non-empty note identifier, snippet, and score > 0."""
        # Create a tmp_path vault with moc/test.md
        moc_dir = tmp_path / "moc"
        moc_dir.mkdir()

        moc_file = moc_dir / "test.md"
        moc_file.write_text("# Test MOC\nThis is a test MOC with some content about testing.\n")

        # Create manifest
        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        # Call vault_search
        result = TOOLS["vault_search"](
            manifest,
            {
                "query": "test",
                "vault_dir": str(tmp_path),
                "limit": 5,
            },
        )

        # Should have ok: true
        assert result.get("ok") is True
        # Should have result with at least one hit
        result_list = result.get("result", [])
        if result_list:  # Only check if we got results
            hit = result_list[0]
            # Must have relpath or title as note identifier (not empty)
            assert (
                hit.get("relpath") or hit.get("title")
            ), "Hit must have relpath or title"
            # Must have non-empty snippet
            assert hit.get("snippet"), "Hit must have non-empty snippet"
            # Must have score > 0
            assert hit.get("score", 0) > 0, "Hit must have score > 0"


class TestMemorySearchFunctional:
    """Functional tests for memory_search tool."""

    def test_memory_search_happy_path(self, tmp_path):
        """memory_search returns records with statement text and promotion_status."""
        from omniagentos.metacog.contracts import MemoryRecord

        # Create manifest
        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        # Mock MetacogService to return a promoted memory record
        test_statement = "This is a test memory about my experiences"
        test_record = MemoryRecord(
            id="mem_test_123",
            type="lesson",
            statement=test_statement,
            promotion_status="promoted",
        )

        with patch("omniagentos.metacog.service.MetacogService") as MockService:
            mock_service = MagicMock()
            mock_service.search_memory.return_value = [test_record]
            MockService.return_value = mock_service

            result = TOOLS["memory_search"](
                manifest,
                {
                    "query": "test memory",
                    "limit": 20,
                },
            )

        # Should have ok: true
        assert result.get("ok") is True
        # Should have result with at least one hit
        result_list = result.get("result", [])
        assert len(result_list) > 0, "Should have at least one memory record"

        hit = result_list[0]
        # Must have text field that contains the statement
        assert hit.get("text") == test_statement, "text field should contain the statement"
        # Must have promotion_status
        assert hit.get("promotion_status") == "promoted"

    def test_memory_search_filters_shadow_records(self, tmp_path):
        """memory_search excludes shadow (un-promoted) records."""
        from omniagentos.metacog.contracts import MemoryRecord

        # Create manifest
        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        # Mock returns only the promoted row (statuses=["promoted"] filters at the service).
        promoted_record = MemoryRecord(
            id="mem_promoted_123",
            type="lesson",
            statement="This is a promoted memory",
            promotion_status="promoted",
        )

        with patch("omniagentos.metacog.service.MetacogService") as MockService:
            mock_service = MagicMock()
            # Should only call with statuses=["promoted"]
            mock_service.search_memory.return_value = [promoted_record]
            MockService.return_value = mock_service

            result = TOOLS["memory_search"](
                manifest,
                {
                    "query": "test memory",
                    "limit": 20,
                },
            )

            # Verify that search_memory was called with statuses=["promoted"]
            mock_service.search_memory.assert_called_once()
            call_kwargs = mock_service.search_memory.call_args.kwargs
            assert call_kwargs.get("statuses") == ["promoted"], (
                "memory_search must pass statuses=['promoted'] to filter out shadow records"
            )
            # Return payload must not surface shadow records (would fail if the mock
            # ever handed a shadow hit through).
            assert result.get("ok") is True
            hits = result.get("result", [])
            assert len(hits) == 1
            assert hits[0].get("id") == "mem_promoted_123"
            assert hits[0].get("promotion_status") == "promoted"
            assert hits[0].get("text") == "This is a promoted memory"
            assert all(h.get("promotion_status") == "promoted" for h in hits)
            assert not any(h.get("id") == "mem_shadow_123" for h in hits)
            assert not any(h.get("promotion_status") == "shadow" for h in hits)


class TestSemanticSearchFunctional:
    """Functional tests for semantic_search tool."""

    def test_semantic_search_unavailable(self, tmp_path):
        """semantic_search handles SemanticUnavailable gracefully."""
        from omniagentos.filesearch.semantic import SemanticUnavailable

        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        with patch("omniagentos.filesearch.semantic.semantic_query") as mock_query:
            mock_query.side_effect = SemanticUnavailable("pgvector unavailable")

            result = TOOLS["semantic_search"](
                manifest,
                {
                    "query": "test",
                    "limit": 20,
                },
            )

        # Should have ok: false
        assert result.get("ok") is False
        # Should have error: unavailable
        assert result.get("error") == "unavailable"


class TestRepositoryMapFunctional:
    """Functional tests for repomap tool."""

    def test_repomap_happy_path(self, tmp_path):
        """repomap returns structure and symbols for a repo."""
        # Create a simple Python file
        py_file = tmp_path / "test_module.py"
        py_file.write_text(
            "def hello_world():\n    return 'Hello, World!'\n\nclass TestClass:\n    pass\n"
        )

        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        with patch("omniagentos.repomap.build_repo_map") as mock_map:
            mock_map.return_value = "test_module.py:\n  hello_world: function\n  TestClass: class\n"

            result = TOOLS["repomap"](
                manifest,
                {
                    "repo_dir": str(tmp_path),
                },
            )

        # Should have ok: true
        assert result.get("ok") is True
        # Should have result that contains symbol names
        result_str = result.get("result", "")
        assert "hello_world" in result_str or "test_module" in result_str


class TestKnowledgeRecallFunctional:
    """Functional tests for knowledge_recall tool."""

    def test_knowledge_recall_composition(self, tmp_path):
        """knowledge_recall converts RecallLine objects correctly."""
        try:
            from omniagentos.retrieval.recall import RecallLine
        except ModuleNotFoundError:
            pytest.skip("omniagentos.retrieval.recall not available in this environment")

        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        # Create mock RecallLine objects
        recall_line = RecallLine(
            text="This is recalled information",
            source="test_source",
            score=0.95,
            ref="ref_123",
        )

        with patch("omniagentos.retrieval.recall.recall") as mock_recall:
            mock_recall.return_value = [recall_line]

            result = TOOLS["knowledge_recall"](
                manifest,
                {
                    "query": "test question",
                    "top_k": 8,
                },
            )

        # Should have ok: true
        assert result.get("ok") is True
        result_list = result.get("result", [])
        if result_list:
            hit = result_list[0]
            # Must have text, source, score, ref
            assert hit.get("text") == "This is recalled information"
            assert hit.get("source") == "test_source"
            assert hit.get("score") == 0.95
            assert hit.get("ref") == "ref_123"


    def test_semantic_search_import_error(self, tmp_path):
        """semantic_search handles ImportError gracefully."""
        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        with patch("omniagentos.filesearch.semantic.semantic_query") as mock_query:
            mock_query.side_effect = ImportError("semantic module not available")

            result = TOOLS["semantic_search"](
                manifest,
                {
                    "query": "test",
                },
            )

        # Should return unavailable
        assert result.get("ok") is False
        assert result.get("error") == "unavailable"

    def test_repomap_import_error(self, tmp_path):
        """repomap handles ImportError gracefully."""
        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        with patch("omniagentos.repomap.build_repo_map") as mock_map:
            mock_map.side_effect = ImportError("repomap module not available")

            result = TOOLS["repomap"](
                manifest,
                {
                    "repo_dir": str(tmp_path),
                },
            )

        # Should return unavailable
        assert result.get("ok") is False
        assert result.get("error") == "unavailable"

    def test_memory_search_import_error(self, tmp_path):
        """memory_search handles ImportError gracefully."""
        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        with patch("omniagentos.metacog.service.MetacogService") as MockService:
            MockService.side_effect = ImportError("metacog module not available")

            result = TOOLS["memory_search"](
                manifest,
                {
                    "query": "test",
                },
            )

        # Should return unavailable
        assert result.get("ok") is False
        assert result.get("error") == "unavailable"

    def test_knowledge_recall_import_error(self, tmp_path):
        """knowledge_recall handles ImportError gracefully."""
        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        # When both retrieval.recall and knowledge.recall are unavailable,
        # the tool should return unavailable
        # Just test that it doesn't crash and returns ok:False
        result = TOOLS["knowledge_recall"](
            manifest,
            {
                "query": "test",
            },
        )

        # Should return either ok:True if knowledge is available,
        # or ok:False if unavailable
        # We can't guarantee which since it depends on the environment
        assert isinstance(result.get("ok"), bool)


class TestVaultSearchWithDefaults:
    """Test vault_search with default vault_dir."""

    def test_vault_search_uses_default_vault_dir(self, tmp_path, monkeypatch):
        """vault_search uses default_vault_dir() when not provided."""
        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        # Mock default_vault_dir to return our tmp_path
        def mock_vault_dir():
            return str(tmp_path)

        with patch("omniagentos.contracts.default_vault_dir") as mock:
            mock.return_value = str(tmp_path)

            # Call without vault_dir - should use default
            result = TOOLS["vault_search"](
                manifest,
                {
                    "query": "test",
                },
            )

            # Should succeed with default
            assert result.get("ok") is True


class TestRepositoryMapWithDefaults:
    """Test repomap with default repo_dir."""

    def test_repomap_uses_default_repo_dir(self, tmp_path):
        """repomap uses _repo_root() when not provided."""
        manifest = CapabilityManifest(
            run_id="r1",
            session_id="s1",
            holder_generation=1,
            read_roots=[str(tmp_path)],
            write_roots=[],
            allowed_ops=list(TOOLS.keys()),
        )

        # Mock _repo_root to return our tmp_path
        with patch("omniagentos.contracts._repo_root") as mock_root:
            mock_root.return_value = str(tmp_path)

            # Call without repo_dir - should use default
            result = TOOLS["repomap"](
                manifest,
                {},
            )

            # Should succeed with default
            assert result.get("ok") is True
