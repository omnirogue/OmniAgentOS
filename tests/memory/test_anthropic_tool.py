"""Tests for the Anthropic custom memory tool client-side handler."""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.memory.anthropic_tool import MetacogMemoryTool
from omniagentos.metacog.service import MetacogService


@pytest.fixture
def metacog_service(tmp_path: Path) -> MetacogService:
    db_path = tmp_path / "metacog.db"
    return MetacogService(db_path=str(db_path))


@pytest.fixture
def memory_tool(metacog_service: MetacogService) -> MetacogMemoryTool:
    return MetacogMemoryTool(service=metacog_service)


def test_path_normalization_and_validation(memory_tool: MetacogMemoryTool) -> None:
    # Valid paths
    assert memory_tool._normalize_and_validate_path("facts.txt") == "/memories/facts.txt"
    assert memory_tool._normalize_and_validate_path("/facts.txt") == "/memories/facts.txt"
    assert memory_tool._normalize_and_validate_path("/memories/facts.txt") == "/memories/facts.txt"
    assert memory_tool._normalize_and_validate_path("memories/facts.txt") == "/memories/facts.txt"

    # Directory traversal prevents
    with pytest.raises(ValueError, match="Directory traversal sequences are forbidden"):
        memory_tool._normalize_and_validate_path("../facts.txt")

    with pytest.raises(ValueError, match="Directory traversal sequences are forbidden"):
        memory_tool._normalize_and_validate_path("/memories/../facts.txt")


def test_create_and_view_memory(memory_tool: MetacogMemoryTool) -> None:
    # Create virtual file
    res = memory_tool.create({"path": "lessons.txt", "file_text": "First line\nSecond line"})
    assert "File created successfully" in res

    # Check that it derives correct type based on filename
    record = memory_tool.store.get_memory(memory_tool._path_to_id("/memories/lessons.txt"))
    assert record is not None
    assert record.type == "lesson"
    assert record.statement == "First line\nSecond line"

    # View virtual file (should include line numbers)
    view_res = memory_tool.view({"path": "lessons.txt"})
    assert view_res == "1: First line\n2: Second line"


def test_directory_listing(memory_tool: MetacogMemoryTool) -> None:
    # Viewing /memories when empty
    empty_listing = memory_tool.view({"path": "/memories"})
    assert "(Empty directory)" in empty_listing

    # Create multiple files
    memory_tool.create({"path": "facts.txt", "file_text": "Some facts"})
    memory_tool.create({"path": "procedures.txt", "file_text": "Some procedure"})

    listing = memory_tool.view({"path": "/memories"})
    assert "- /memories/facts.txt" in listing
    assert "- /memories/procedures.txt" in listing


def test_string_replacement(memory_tool: MetacogMemoryTool) -> None:
    memory_tool.create({"path": "facts.txt", "file_text": "User prefers dark mode."})

    # Successful replace
    res = memory_tool.str_replace(
        {"path": "facts.txt", "old_str": "dark mode", "new_str": "light mode"}
    )
    assert "Successfully replaced text" in res

    # Verify content
    view_res = memory_tool.view({"path": "facts.txt"})
    assert "light mode" in view_res

    # Error: not found
    with pytest.raises(ValueError, match="was not found"):
        memory_tool.str_replace(
            {"path": "facts.txt", "old_str": "dark mode", "new_str": "blue mode"}
        )

    # Error: ambiguous
    memory_tool.create({"path": "ambig.txt", "file_text": "apple apple apple"})
    with pytest.raises(ValueError, match="ambiguous"):
        memory_tool.str_replace({"path": "ambig.txt", "old_str": "apple", "new_str": "banana"})


def test_insert_at_line(memory_tool: MetacogMemoryTool) -> None:
    memory_tool.create({"path": "facts.txt", "file_text": "Line 1\nLine 3"})

    # Insert Line 2 at index 2 (1-based line number 2)
    res = memory_tool.insert({"path": "facts.txt", "line_number": 2, "text": "Line 2"})
    assert "Successfully inserted text" in res

    # Verify view
    view_res = memory_tool.view({"path": "facts.txt"})
    assert view_res == "1: Line 1\n2: Line 2\n3: Line 3"


def test_delete_and_rename_memory(memory_tool: MetacogMemoryTool) -> None:
    memory_tool.create({"path": "facts.txt", "file_text": "Content"})

    # Rename
    res = memory_tool.rename({"path": "facts.txt", "new_path": "rules.txt"})
    assert "Successfully renamed" in res

    # View at old location raises error
    with pytest.raises(FileNotFoundError):
        memory_tool.view({"path": "facts.txt"})

    # View at new location
    assert "1: Content" in memory_tool.view({"path": "rules.txt"})

    # Delete
    del_res = memory_tool.delete({"path": "rules.txt"})
    assert "Successfully deleted" in del_res

    # View raises error now
    with pytest.raises(FileNotFoundError):
        memory_tool.view({"path": "rules.txt"})
