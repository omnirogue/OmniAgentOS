"""Tests for the per-project on-disk human log projector.

var/projects/<project_id>/logs/activity.log is written by
omniagentos.projects.activity.project_pending_activity (called best-effort by
the GET /api/projects/{id}/activity route) and can also be driven standalone
via omniagentos.projects.activity_tick, mirroring
omniagentos.scheduler.routines_tick's tick()/main() shape.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.contracts import utc_now_iso
from omniagentos.db.store import SqliteStore
from omniagentos.projects import ProjectStore
from omniagentos.projects.activity import (
    project_log_dir,
    project_log_path,
    project_pending_activity,
)
from omniagentos.projects.activity_tick import tick


@pytest.fixture(autouse=True)
def _sandbox_var_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Belt-and-braces alongside each test's explicit base_dir=str(tmp_path):
    any call to project_pending_activity/tick that forgets to pass base_dir
    (falling through to OMNIAGENTOS_VAR_DIR) still resolves inside tmp_path,
    not the real repo var/ -- explicit base_dir= args always win over this
    when a test passes one, so this is purely a safety net."""
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path / "var"))


def _seed(store: SqliteStore, project_id: str, *, suffix: str) -> tuple[str, str]:
    now = utc_now_iso()
    task_id = f"tsk_{suffix}"
    store.create_task(
        {
            "id": task_id,
            "title": f"Task {suffix}",
            "state": "ready",
            "project_id": project_id,
            "created_at": now,
            "updated_at": now,
        }
    )
    run_id = f"run_{suffix}"
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "harness": "mock",
            "trace_id": f"tr_{suffix}",
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    store.insert_event(
        "run.updated",
        "runner:w1",
        "queued",
        target_type="run",
        target_id=run_id,
        payload={"state": "queued"},
    )
    return task_id, run_id


def test_project_pending_activity_writes_lines_and_is_idempotent(
    store: SqliteStore, project_store: ProjectStore, tmp_path: Path
) -> None:
    project_id = project_store.create_project({"name": "Acme"})["id"]
    _seed(store, project_id, suffix="a")

    appended = project_pending_activity(store, project_id, base_dir=str(tmp_path))
    assert appended == 1

    log_path = project_log_path(project_id, base_dir=str(tmp_path))
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert content.count("\n") == 1
    assert "[evt:1]" in content
    assert "Task a: run queued" in content

    # Repeated calls with no new events append nothing (self-healing cursor
    # read from the log's own last line, not a separate piece of state).
    assert project_pending_activity(store, project_id, base_dir=str(tmp_path)) == 0
    assert log_path.read_text(encoding="utf-8") == content


def test_project_pending_activity_picks_up_new_events_incrementally(
    store: SqliteStore, project_store: ProjectStore, tmp_path: Path
) -> None:
    project_id = project_store.create_project({"name": "Acme"})["id"]
    _, run_id = _seed(store, project_id, suffix="a")
    assert project_pending_activity(store, project_id, base_dir=str(tmp_path)) == 1

    store.insert_event(
        "run.updated",
        "runner:w1",
        "running",
        target_type="run",
        target_id=run_id,
        payload={"state": "running"},
    )
    assert project_pending_activity(store, project_id, base_dir=str(tmp_path)) == 1

    lines = (
        project_log_path(project_id, base_dir=str(tmp_path))
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert len(lines) == 2
    assert "[evt:2]" in lines[1]
    assert "run running" in lines[1]


def test_project_pending_activity_self_heals_when_log_deleted(
    store: SqliteStore, project_store: ProjectStore, tmp_path: Path
) -> None:
    project_id = project_store.create_project({"name": "Acme"})["id"]
    _seed(store, project_id, suffix="a")
    project_pending_activity(store, project_id, base_dir=str(tmp_path))
    log_path = project_log_path(project_id, base_dir=str(tmp_path))
    log_path.unlink()

    appended = project_pending_activity(store, project_id, base_dir=str(tmp_path))
    assert appended == 1
    assert "[evt:1]" in log_path.read_text(encoding="utf-8")


def test_project_pending_activity_never_raises_on_a_bad_base_dir(
    store: SqliteStore, project_store: ProjectStore, tmp_path: Path
) -> None:
    project_id = project_store.create_project({"name": "Acme"})["id"]
    _seed(store, project_id, suffix="a")
    # A regular file where a directory is expected forces an OSError deep in
    # project_log_path's parent.mkdir(); project_pending_activity must swallow
    # it (documented contract: "never raises") and report 0, not blow up the
    # caller (the GET /activity read path).
    blocker = tmp_path / "projects"
    blocker.write_text("not a directory", encoding="utf-8")

    assert project_pending_activity(store, project_id, base_dir=str(tmp_path)) == 0


def test_project_log_dir_rejects_unsafe_project_id(tmp_path: Path) -> None:
    for bad_id in ("../escape", "a/b", "a\\b", "", ".", ".."):
        with pytest.raises(ValueError):
            project_log_dir(bad_id, base_dir=str(tmp_path))


def test_project_log_dir_lands_under_var_projects_id_logs(tmp_path: Path) -> None:
    path = project_log_dir("proj_abc123", base_dir=str(tmp_path))
    assert path == tmp_path / "projects" / "proj_abc123" / "logs"


def test_activity_tick_sweeps_every_project(
    store: SqliteStore, project_store: ProjectStore
) -> None:
    project_a = project_store.create_project({"name": "A"})["id"]
    project_b = project_store.create_project({"name": "B"})["id"]
    _seed(store, project_a, suffix="a")
    _seed(store, project_b, suffix="b")

    # tick() with no explicit project_ids sweeps every project via
    # list_project_ids(); no base_dir override here on purpose -- this
    # exercises the same OMNIAGENTOS_VAR_DIR fallback path a real periodic
    # cron/launchd tick would use, sandboxed to tmp_path by the autouse fixture.
    result = tick(store)

    assert result["projects"] == 2
    assert result["lines_appended"][project_a] == 1
    assert result["lines_appended"][project_b] == 1
    assert project_log_path(project_a).exists()
    assert project_log_path(project_b).exists()


def test_activity_tick_accepts_an_explicit_project_id_subset(
    store: SqliteStore, project_store: ProjectStore
) -> None:
    project_a = project_store.create_project({"name": "A"})["id"]
    project_b = project_store.create_project({"name": "B"})["id"]
    _seed(store, project_a, suffix="a")
    _seed(store, project_b, suffix="b")

    result = tick(store, project_ids=[project_a])
    assert result["projects"] == 1
    assert list(result["lines_appended"]) == [project_a]
