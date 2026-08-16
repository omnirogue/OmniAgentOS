"""Durable import finalization shared by historical imports and benchmark arms.

This module deliberately imports the ledger, vault, and SQLite implementation
only while a caller is finalizing a run.  Importers and dry-run-only wrapper
calls therefore remain free of persistence side effects.
"""

from __future__ import annotations

import importlib
import json
from collections.abc import Callable
from typing import Any, cast

from omniagentos.contracts import (
    IdempotencyReceipt,
    RunManifest,
    RunState,
    Store,
    TaskState,
    default_db_path,
    default_ledger_dir,
    default_vault_dir,
    utc_now_iso,
)
from omniagentos.toolplane.scrub import scrub_optional_text, scrub_run_manifest


def finalize_manifest(
    manifest: RunManifest,
    *,
    task_title: str | None = None,
    task_input: dict[str, Any] | None = None,
    output_text: str | None = None,
    ledger_dir: str | None = None,
    vault_dir: str | None = None,
    db_path: str | None = None,
    write_store: bool = True,
    write_vault: bool = True,
) -> tuple[str, str | None]:
    """Write the three durable projections for one terminal manifest.

    The database row is created first so the run note can render the Store row
    shape.  Ledger and vault imports stay local to this real-finalization path.

    SEAM 2, import half. This function is the ONE entry point for the three
    durable projections of an imported run -- the DB row, the append-only ledger
    JSONL and the vault note -- and its inputs are agent output that never came
    back through the runner's adapter boundary (historical imports, benchmark
    arms). So both carriers are redacted once, here, before any of the three
    writes: ``output_text`` and the manifest's ``extra`` (which also supplies
    ``runs.output_json`` and ``runs.error`` below). The ledger is fsynced and
    append-only; a credential that reaches it cannot be taken back.
    """
    manifest = scrub_run_manifest(manifest)
    output_text = scrub_optional_text(output_text)
    ledger_root = ledger_dir or default_ledger_dir()
    vault_root = vault_dir or default_vault_dir()
    store: Store | None = None
    task_input_json = json.dumps(task_input, sort_keys=True) if task_input is not None else None
    run_row = _run_row(manifest, task_title, output_text)
    if write_store:
        store = _open_store(db_path or default_db_path())
        _ensure_task(store, manifest, task_title, task_input)
        _ensure_run(store, manifest, output_text)
        if output_text is not None:
            store.update_run(
                manifest.run_id,
                {"output_text": output_text, "updated_at": utc_now_iso()},
            )
        saved = store.get_run(manifest.run_id)
        if saved is not None:
            run_row = {**saved, "discipline": manifest.discipline, "task_title": task_title}

    append_manifest = _append_manifest()
    manifest_path = append_manifest(ledger_root, manifest)
    if store is not None:
        store.update_run(
            manifest.run_id,
            {"manifest_path": manifest_path, "updated_at": utc_now_iso()},
        )
        saved = store.get_run(manifest.run_id)
        if saved is not None:
            run_row = {**saved, "discipline": manifest.discipline, "task_title": task_title}

    note_path: str | None = None
    if write_vault:
        render_run_note, write_note = _vault_functions()
        receipts = _receipts(store, manifest)
        artifacts_list = store.get_artifacts(manifest.run_id) if store is not None else None
        relpath, content = render_run_note(
            run_row,
            [],
            manifest_path,
            receipts,
            artifacts_list=artifacts_list,
            task_title=task_title,
            task_input=task_input_json,
        )
        note_path = write_note(vault_root, relpath, content)
        if store is not None:
            store.update_run(
                manifest.run_id,
                {"vault_note_path": note_path, "updated_at": utc_now_iso()},
            )
    return manifest_path, note_path


def _open_store(db_path: str) -> Store:
    db = importlib.import_module("omniagentos.db.store")
    migrate_module = importlib.import_module("omniagentos.db.migrate")
    migrate = cast(Callable[[str], int], migrate_module.migrate)
    store_type = cast(Callable[[str], Store], db.SqliteStore)
    migrate(db_path)
    return store_type(db_path)


def _append_manifest() -> Callable[[str, RunManifest], str]:
    ledger = importlib.import_module("omniagentos.ledger")
    return cast(Callable[[str, RunManifest], str], ledger.append_manifest)


def _vault_functions() -> tuple[
    Callable[..., tuple[str, str]],
    Callable[[str, str, str], str],
]:
    vault = importlib.import_module("omniagentos.vault")
    return (
        cast(
            Callable[..., tuple[str, str]],
            vault.render_run_note,
        ),
        cast(Callable[[str, str, str], str], vault.write_note),
    )


def _ensure_task(
    store: Store,
    manifest: RunManifest,
    task_title: str | None,
    task_input: dict[str, Any] | None,
) -> None:
    if store.get_task(manifest.task_id) is not None:
        return
    now = utc_now_iso()
    discipline = manifest.discipline
    if discipline and not any(row["id"] == discipline for row in store.list_disciplines()):
        store.create_discipline(
            {
                "id": discipline,
                "name": discipline.replace("-", " ").title(),
                "metric_contract": "{}",
                "status": "active",
                "created_at": now,
            }
        )
    store.create_task(
        {
            "id": manifest.task_id,
            "discipline_id": discipline,
            "title": task_title or manifest.task_id,
            "input_json": json.dumps(task_input or {}, sort_keys=True),
            "acceptance_json": "{}",
            "state": _task_state(manifest.state).value,
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )


def _ensure_run(store: Store, manifest: RunManifest, output_text: str | None) -> None:
    if store.get_run(manifest.run_id) is not None:
        return
    now = utc_now_iso()
    usage = manifest.usage
    # RunManifest carries no project_id of its own (frozen contract); the
    # owning task is created by _ensure_task just before this call, so its
    # project_id (set when the task itself is project-scoped, or NULL for
    # unscoped/global) is the only source of truth available here.
    task = store.get_task(manifest.task_id)
    task_project_id = task.get("project_id") if task is not None else None
    store.enqueue_run(
        {
            "id": manifest.run_id,
            "task_id": manifest.task_id,
            "discipline_id": manifest.discipline,
            "project_id": task_project_id,
            "arm": manifest.arm.value if manifest.arm is not None else None,
            "harness": manifest.harness.harness.value,
            "harness_version": manifest.harness.version,
            "env_hash": manifest.harness.env_hash,
            "harness_params": json.dumps(manifest.harness.params, sort_keys=True),
            "agent": manifest.agent,
            "model": manifest.model,
            "state": manifest.state.value,
            "output_text": output_text,
            "output_json": json.dumps(manifest.extra, sort_keys=True),
            "error": _error(manifest),
            "wall_ms": usage.wall_ms if usage is not None else None,
            "turns": usage.turns if usage is not None else None,
            "input_tokens": usage.input_tokens if usage is not None else None,
            "output_tokens": usage.output_tokens if usage is not None else None,
            "cost_usd": usage.cost_usd if usage is not None else None,
            "usage_estimated": int(usage.estimated) if usage is not None else 1,
            "usage_source": usage.source if usage is not None else "imported",
            "trace_id": manifest.trace_id or f"import:{manifest.run_id}",
            "queued_at": manifest.started_at or now,
            "started_at": manifest.started_at,
            "finished_at": manifest.finished_at or now,
            "created_at": now,
            "updated_at": now,
        }
    )


def _run_row(
    manifest: RunManifest, task_title: str | None, output_text: str | None
) -> dict[str, Any]:
    usage = manifest.usage
    return {
        "id": manifest.run_id,
        "task_id": manifest.task_id,
        "task_title": task_title or manifest.task_id,
        "discipline": manifest.discipline,
        "arm": manifest.arm.value if manifest.arm is not None else None,
        "harness": manifest.harness.harness.value,
        "harness_version": manifest.harness.version,
        "env_hash": manifest.harness.env_hash,
        "harness_params": json.dumps(manifest.harness.params, sort_keys=True),
        "agent": manifest.agent,
        "model": manifest.model,
        "state": manifest.state.value,
        "output_text": output_text,
        "wall_ms": usage.wall_ms if usage is not None else None,
        "turns": usage.turns if usage is not None else None,
        "input_tokens": usage.input_tokens if usage is not None else None,
        "output_tokens": usage.output_tokens if usage is not None else None,
        "cost_usd": usage.cost_usd if usage is not None else None,
        "usage_estimated": int(usage.estimated) if usage is not None else 1,
        "usage_source": usage.source if usage is not None else "imported",
        "trace_id": manifest.trace_id or f"import:{manifest.run_id}",
        "queued_at": manifest.started_at,
        "started_at": manifest.started_at,
        "finished_at": manifest.finished_at,
    }


def _receipts(store: Store | None, manifest: RunManifest) -> list[IdempotencyReceipt]:
    if store is None:
        return manifest.receipts
    return [IdempotencyReceipt.model_validate(row) for row in store.idem_for_run(manifest.run_id)]


def _task_state(state: RunState) -> TaskState:
    return {
        RunState.COMPLETED: TaskState.COMPLETED,
        RunState.FAILED: TaskState.FAILED,
        RunState.CANCELLED: TaskState.CANCELLED,
    }[state]


def _error(manifest: RunManifest) -> str | None:
    error = manifest.extra.get("error")
    return str(error) if error else None
