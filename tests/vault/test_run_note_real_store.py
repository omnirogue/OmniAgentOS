"""Exercise ``render_run_note`` with real SqliteStore rows, not test doubles."""

from __future__ import annotations

from pathlib import Path

from omniagentos.db.migrate import migrate
from omniagentos.db.store import SqliteStore
from omniagentos.vault import render_run_note


def test_render_run_note_with_real_store_and_artifacts(tmp_path: Path) -> None:
    """A real run row needs related task and artifact rows to render completely."""
    db_path = tmp_path / "test.db"
    migrate(str(db_path))
    store = SqliteStore(str(db_path))

    store.create_task(
        {
            "id": "tsk_realtest001",
            "discipline_id": "code-changes",
            "title": "Fix the vault note rendering",
            "input_json": '{"task":"demonstrate artifacts and output"}',
            "acceptance_json": "{}",
            "state": "submitted",
            "risk": "low",
            "created_at": "2026-07-11T14:00:00Z",
            "updated_at": "2026-07-11T14:00:00Z",
        }
    )
    run_dict = {
        "id": "run_realtest001",
        "task_id": "tsk_realtest001",
        "discipline_id": "code-changes",
        "harness": "cli-claude",
        "model": "claude-opus",
        "state": "completed",
        "queued_at": "2026-07-11T14:00:00Z",
        "started_at": "2026-07-11T14:00:05Z",
        "finished_at": "2026-07-11T14:05:00Z",
        "output_text": "The vault note now renders correctly!",
        "output_json": '{"status":"success"}',
        "trace_id": "trace-real001",
        "created_at": "2026-07-11T14:00:00Z",
        "updated_at": "2026-07-11T14:05:00Z",
    }
    store.enqueue_run(run_dict)
    assert store.upsert_step(
        "run_realtest001",
        1,
        {
            "name": "analyze",
            "status": "completed",
            "started_at": "2026-07-11T14:00:05Z",
            "finished_at": "2026-07-11T14:02:00Z",
        },
    )
    store.add_artifact(
        {
            "id": "art_realtest001",
            "run_id": "run_realtest001",
            "type": "file",
            "uri": "s3://bucket/vault-note-fix.patch",
            "created_at": "2026-07-11T14:04:00Z",
        }
    )

    task = store.get_task("tsk_realtest001")
    run = store.get_run("run_realtest001")
    assert task is not None
    assert run is not None
    relpath, content = render_run_note(
        run,
        store.get_steps("run_realtest001"),
        "ledger/test.jsonl",
        [],
        artifacts_list=store.get_artifacts("run_realtest001"),
        task_title=str(task["title"]),
        task_input=str(task["input_json"]),
    )

    assert "Fix the vault note rendering" in content
    assert "[[code-changes]]" in content
    assert "analyze" in content
    assert "s3://bucket/vault-note-fix.patch" in content
    assert "The vault note now renders correctly!" in content
    assert "success" in content
    assert "demonstrate artifacts and output" in content
    assert "2026/07/" in relpath


def test_render_run_note_path_uses_finished_at_not_render_time() -> None:
    """A rerender after a month boundary retains the completed run's month."""
    import omniagentos.vault.run_note as run_note_module

    run = {
        "id": "run_boundary",
        "task_id": "tsk_boundary",
        "finished_at": "2026-07-31T23:50:00Z",
        "created_at": "2026-07-31T23:40:00Z",
    }
    original_utc_now_iso = run_note_module.utc_now_iso
    run_note_module.utc_now_iso = lambda: "2026-08-01T00:05:00Z"
    try:
        relpath, _ = render_run_note(run, [], "ledger/x.jsonl", [])
        assert "2026/07/" in relpath
    finally:
        run_note_module.utc_now_iso = original_utc_now_iso
