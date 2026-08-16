"""Visible behavior check. Run: python -m pytest check_emitters.py"""

from __future__ import annotations

from emitters import alert_events, run_events, task_events


def test_run_updated() -> None:
    assert run_events.run_updated("run_1", "running") == (
        'event: run.updated\ndata: {"run_id":"run_1","state":"running"}\n\n'
    )


def test_task_updated() -> None:
    assert task_events.task_updated("tsk_1", "done") == (
        'event: task.updated\ndata: {"status":"done","task_id":"tsk_1"}\n\n'
    )


def test_alert_created() -> None:
    assert alert_events.alert_created("alr_1", "high") == (
        'event: alert.created\ndata: {"alert_id":"alr_1","severity":"high"}\n\n'
    )
