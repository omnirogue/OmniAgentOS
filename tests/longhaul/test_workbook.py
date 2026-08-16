"""Tests for workbook.py: initialization, reading, status parsing, checkpoints."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from omniagentos.longhaul.workbook import (
    append_checkpoint,
    init_workbook,
    read_workbook,
    workbook_status,
    workbook_summary,
)


@pytest.fixture
def tmp_workbook_dir(tmp_path: Path, monkeypatch) -> None:
    """Set working directory to tmp for workbook tests."""
    monkeypatch.chdir(tmp_path)
    yield


class TestInitWorkbook:
    """Workbook initialization."""

    def test_init_workbook_creates_file(self, tmp_workbook_dir) -> None:
        """init_workbook creates WORKBOOK.md with initial sections."""
        path = init_workbook("btk_123", "Test Task", "Fix the bug", "All tests pass")
        assert Path(path).exists()
        content = Path(path).read_text()
        assert "# Test Task" in content
        assert "## Goal" in content
        assert "## Acceptance criteria" in content
        assert "## Status" in content
        assert "WORKING" in content

    def test_init_workbook_path_structure(self, tmp_workbook_dir) -> None:
        """init_workbook creates var/longhaul/<task_id>/WORKBOOK.md path."""
        path = init_workbook("btk_456", "Title", "Brief", "Acceptance")
        expected = Path("var/longhaul/btk_456/WORKBOOK.md")
        assert Path(path) == expected

    def test_init_workbook_includes_sections(self, tmp_workbook_dir) -> None:
        """Workbook includes all required sections."""
        init_workbook("btk_789", "T", "B", "A")
        content = read_workbook("btk_789")
        assert "## Goal" in content
        assert "## Acceptance criteria" in content
        assert "## Plan" in content
        assert "## Progress log" in content
        assert "## Decisions" in content
        assert "## Next steps" in content
        assert "## Status" in content


class TestReadWorkbook:
    """Workbook reading."""

    def test_read_workbook_exists(self, tmp_workbook_dir) -> None:
        """read_workbook returns content when file exists."""
        init_workbook("btk_read", "Title", "Brief", "Acceptance")
        content = read_workbook("btk_read")
        assert content is not None
        assert "## Goal" in content

    def test_read_workbook_missing(self, tmp_workbook_dir) -> None:
        """read_workbook returns None when file missing."""
        result = read_workbook("btk_missing")
        assert result is None


class TestWorkbookStatus:
    """Workbook status parsing."""

    def test_workbook_status_working(self, tmp_workbook_dir) -> None:
        """workbook_status parses WORKING status."""
        init_workbook("btk_ws1", "Title", "Brief", "Acceptance")
        status = workbook_status("btk_ws1")
        assert status == "WORKING"

    def test_workbook_status_blocked(self, tmp_workbook_dir) -> None:
        """workbook_status parses BLOCKED status."""
        path = init_workbook("btk_ws2", "Title", "Brief", "Acceptance")
        # Update status to BLOCKED
        content = Path(path).read_text()
        content = content.replace("## Status\nWORKING", "## Status\nBLOCKED")
        Path(path).write_text(content)
        status = workbook_status("btk_ws2")
        assert status == "BLOCKED"

    def test_workbook_status_done(self, tmp_workbook_dir) -> None:
        """workbook_status parses DONE status."""
        path = init_workbook("btk_ws3", "Title", "Brief", "Acceptance")
        content = Path(path).read_text()
        content = content.replace("## Status\nWORKING", "## Status\nDONE")
        Path(path).write_text(content)
        status = workbook_status("btk_ws3")
        assert status == "DONE"

    def test_workbook_status_missing(self, tmp_workbook_dir) -> None:
        """workbook_status returns None when workbook missing."""
        status = workbook_status("btk_missing")
        assert status is None


class TestWorkbookSummary:
    """Workbook summary generation."""

    def test_workbook_summary_exists(self, tmp_workbook_dir) -> None:
        """workbook_summary returns summary when workbook exists."""
        init_workbook("btk_sum1", "Task", "Brief text here", "Acceptance criteria here")
        summary = workbook_summary("btk_sum1")
        assert summary is not None
        assert "Brief" in summary or "Acceptance" in summary

    def test_workbook_summary_truncates(self, tmp_workbook_dir) -> None:
        """workbook_summary truncates to max_chars."""
        init_workbook("btk_sum2", "Task", "x" * 1000, "y" * 1000)
        summary = workbook_summary("btk_sum2", max_chars=100)
        assert summary is not None
        assert len(summary) <= 110  # Some allowance for formatting

    def test_workbook_summary_missing(self, tmp_workbook_dir) -> None:
        """workbook_summary returns None when workbook missing."""
        summary = workbook_summary("btk_missing")
        assert summary is None


class TestAppendCheckpoint:
    """Checkpoint appending."""

    def test_append_checkpoint_creates_entry(self, tmp_workbook_dir) -> None:
        """append_checkpoint adds checkpoint block to workbook."""
        init_workbook("btk_cp1", "Title", "Brief", "Acceptance")
        append_checkpoint(
            "btk_cp1",
            attempt_seq=1,
            todos_json='["todo1", "todo2"]',
            files_json='["file1.py"]',
            end_reason="completed",
        )
        content = read_workbook("btk_cp1")
        assert "### Checkpoint (attempt 1)" in content
        assert "end_reason: completed" in content
        assert "todo1" in content

    def test_append_checkpoint_missing_workbook(self, tmp_workbook_dir) -> None:
        """append_checkpoint creates minimal workbook if missing."""
        append_checkpoint(
            "btk_cp2",
            attempt_seq=1,
            todos_json="[]",
            files_json="[]",
            end_reason="crashed",
        )
        content = read_workbook("btk_cp2")
        assert content is not None
        assert "### Checkpoint" in content

    def test_append_checkpoint_multiple(self, tmp_workbook_dir) -> None:
        """Multiple checkpoints append sequentially."""
        init_workbook("btk_cp3", "Title", "Brief", "Acceptance")
        append_checkpoint("btk_cp3", 1, "[]", "[]", "usage_limited")
        append_checkpoint("btk_cp3", 2, '["todo"]', "[]", "completed")
        content = read_workbook("btk_cp3")
        assert "### Checkpoint (attempt 1)" in content
        assert "### Checkpoint (attempt 2)" in content
        assert "usage_limited" in content
        assert "completed" in content


def test_longhaul_suite_never_silently_deselected() -> None:
    """Gate longhaul_never_silent_red: modules stay collectable; no silent omission.

    Disposition: longhaul suite remains green; deprecated engine warns but tests
    stay active — never silent red.
    """
    import importlib

    root = Path(__file__).resolve().parent
    paths = sorted(root.glob("test_*.py"))
    expected = {
        "test_api.py",
        "test_concurrency.py",
        "test_engine.py",
        "test_limits.py",
        "test_provider_migration_072.py",
        "test_reliability_findings.py",
        "test_routing.py",
        "test_scope_wiring.py",
        "test_steering.py",
        "test_store.py",
        "test_workbook.py",
    }
    assert {path.name for path in paths} == expected
    for path in paths:
        importlib.import_module(f"tests.longhaul.{path.stem}")

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "skip"
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"
                and isinstance(node.value.value, ast.Name)
                and node.value.value.id == "pytest"
                and not (
                    isinstance(parents.get(node), ast.Call)
                    and parents[node].func is node  # type: ignore[union-attr]
                )
            ):
                pytest.fail(f"{path.name}:{node.lineno} has bare @pytest.mark.skip")
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "pytest":
                if node.func.attr == "skip":
                    assert node.args or any(keyword.arg == "reason" for keyword in node.keywords), (
                        f"{path.name}:{node.lineno} has bare pytest.skip()"
                    )
            mark = node.func.value
            if (
                isinstance(mark, ast.Attribute)
                and mark.attr == "mark"
                and isinstance(mark.value, ast.Name)
                and mark.value.id == "pytest"
                and node.func.attr in {"skip", "skipif"}
            ):
                assert any(keyword.arg == "reason" for keyword in node.keywords), (
                    f"{path.name}:{node.lineno} has {node.func.attr} without a reason"
                )
    # Ensure this package is not using collect_ignore
    conftest = root / "conftest.py"
    if conftest.exists():
        text = conftest.read_text(encoding="utf-8")
        assert "collect_ignore" not in text
