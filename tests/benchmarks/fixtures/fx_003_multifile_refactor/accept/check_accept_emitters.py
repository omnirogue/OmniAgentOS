"""FROZEN acceptance check for fx_003_multifile_refactor.

Three independent gates, all deterministic:
  1. behavior is byte-for-byte identical to the seed;
  2. the shared helper exists with the required name/shape;
  3. the duplication is actually gone (structural, AST-level).
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from emitters import alert_events, frame, run_events, task_events

_REFACTORED = ("run_events", "task_events", "alert_events")


def test_behavior_run_events_unchanged() -> None:
    assert run_events.run_updated("run_1", "running") == (
        'event: run.updated\ndata: {"run_id":"run_1","state":"running"}\n\n'
    )
    assert run_events.run_failed("run_2", "boom") == (
        'event: run.updated\ndata: {"error":"boom","run_id":"run_2","state":"failed"}\n\n'
    )


def test_behavior_task_events_unchanged() -> None:
    assert task_events.task_updated("tsk_1", "done") == (
        'event: task.updated\ndata: {"status":"done","task_id":"tsk_1"}\n\n'
    )
    assert task_events.task_assigned("tsk_2", "wkr_9") == (
        'event: task.updated\ndata: {"task_id":"tsk_2","worker_id":"wkr_9"}\n\n'
    )


def test_behavior_alert_events_unchanged() -> None:
    assert alert_events.alert_created("alr_1", "high") == (
        'event: alert.created\ndata: {"alert_id":"alr_1","severity":"high"}\n\n'
    )


def test_shared_helper_exists_and_formats() -> None:
    assert hasattr(frame, "format_frame"), "emitters/frame.py must expose format_frame"
    out = frame.format_frame("run.updated", {"run_id": "r", "state": "s"})
    assert out == 'event: run.updated\ndata: {"run_id":"r","state":"s"}\n\n'


@pytest.mark.parametrize("module_name", _REFACTORED)
def test_duplication_removed(module_name: str) -> None:
    module = importlib.import_module(f"emitters.{module_name}")
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "json" not in imported, f"emitters/{module_name}.py still imports json"

    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert "dumps" not in attrs, f"emitters/{module_name}.py still serializes inline"


@pytest.mark.parametrize("module_name", _REFACTORED)
def test_modules_use_the_shared_helper(module_name: str) -> None:
    module = importlib.import_module(f"emitters.{module_name}")
    source = Path(str(module.__file__)).read_text(encoding="utf-8")
    assert "format_frame" in source, (
        f"emitters/{module_name}.py does not call the shared format_frame"
    )


def test_signatures_preserved() -> None:
    import inspect

    assert list(inspect.signature(run_events.run_updated).parameters) == ["run_id", "state"]
    assert list(inspect.signature(run_events.run_failed).parameters) == ["run_id", "error"]
    assert list(inspect.signature(task_events.task_updated).parameters) == ["task_id", "status"]
    assert list(inspect.signature(task_events.task_assigned).parameters) == [
        "task_id",
        "worker_id",
    ]
    assert list(inspect.signature(alert_events.alert_created).parameters) == [
        "alert_id",
        "severity",
    ]
