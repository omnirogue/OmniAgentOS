"""B2 — restart/resume holds idempotency under kill -9 mid-effect."""

from __future__ import annotations

import json

import pytest

from tests.smoke.helpers import (
    EFFECT_TOOLS,
    SmokeError,
    Workspace,
    canonical_append_receipt_step,
    count_receipt_lines,
    kill9,
    kill_process,
    poll_until,
    start_runner,
    wait_db_run_state,
)


@pytest.mark.smoke
@pytest.mark.s19b_live_restart
def test_restart_resume_append_file_exactly_once(
    smoke_workspace: Workspace,
    managed_procs: list,
) -> None:
    """Kill -9 mid effect step; restart; run completes with one receipt line.

    Uses the CANONICAL append_file+probe step from contracts/statemachine.md so a
    crash between effect and idem_complete can still resolve via probe and must
    not double-append.
    """
    ws = smoke_workspace
    try:
        from omniagentos.contracts import new_id
    except ImportError as exc:  # pragma: no cover
        pytest.skip(f"contracts.new_id unavailable: {exc}")

    run_id = new_id("run")
    line = f"effect-{run_id}"
    # Effects are confined to the runner-assigned workspace <workspace_dir>/<run_id>
    # (council R2 governance); the relative effect path resolves under it.
    receipt_path = ws.workspace_dir / run_id / "var" / "smoke" / "receipt.txt"
    plan = [canonical_append_receipt_step(run_id)]
    task_id, actual_run_id = _enqueue_with_fixed_run_id(ws, run_id=run_id, plan=plan)
    assert actual_run_id == run_id
    assert task_id.startswith("tsk_")

    logs = ws.root / "logs"
    runner = start_runner(
        ws,
        poll_ms=100,
        once=False,
        worker_id="smoke-restart-1",
        log_path=logs / "runner1.log",
    )
    managed_procs.append(runner)

    def _mid_effect() -> bool:
        steps = ws.steps_for(run_id)
        if not steps:
            if receipt_path.is_file() and line in receipt_path.read_text(encoding="utf-8"):
                row = ws.run_row(run_id)
                if row is not None and row["state"] not in {"completed", "failed", "cancelled"}:
                    return True
            return False
        for step in steps:
            if step["name"] != "append-receipt":
                continue
            status = step["status"]
            if status == "started" and count_receipt_lines(receipt_path, line) >= 1:
                # ``started`` is durable before the external effect begins.
                # Killing on that state alone can therefore land before the
                # append, which is not the crash window this probe promises.
                # Wait until the effect itself is observable while the
                # idempotency receipt is still incomplete.
                return True
            # Tight race: effect finished before we observed STARTED — still kill
            # and prove resume does not double-apply.
            if status in {"completed", "skipped"} and count_receipt_lines(receipt_path, line) >= 1:
                return True
        return False

    try:
        poll_until(_mid_effect, timeout_s=45.0, start_s=0.01, max_s=0.2, desc="mid-effect")
    except SmokeError as exc:
        steps = [dict(s) for s in ws.steps_for(run_id)]
        row = ws.run_row(run_id)
        raise SmokeError(
            f"never observed mid-effect for {run_id}; run={dict(row) if row else None}; "
            f"steps={steps}; receipt_exists={receipt_path.exists()}"
        ) from exc

    kill9(runner)
    managed_procs.remove(runner)

    runner2 = start_runner(
        ws,
        poll_ms=100,
        once=False,
        worker_id="smoke-restart-2",
        log_path=logs / "runner2.log",
    )
    managed_procs.append(runner2)

    final = wait_db_run_state(ws, run_id, {"completed"}, timeout_s=60.0)
    assert final["state"] == "completed", f"expected completed, got {final['state']!r}"

    assert receipt_path.is_file(), f"missing receipt file {receipt_path}"
    hits = count_receipt_lines(receipt_path, line)
    body = receipt_path.read_text(encoding="utf-8")
    assert hits == 1, f"expected exactly one '{line}' in receipt, found {hits}:\n{body}"

    idems = ws.idem_for(run_id)
    matching = [r for r in idems if r["step_name"] == "append-receipt"]
    assert len(matching) == 1, f"expected 1 idempotency row, got {matching!r}"
    assert matching[0]["result_json"] is not None, "idempotency receipt incomplete"

    kill_process(runner2)


def _enqueue_with_fixed_run_id(
    workspace: Workspace,
    *,
    run_id: str,
    plan: list[dict],
    title: str = "smoke-restart-resume",
) -> tuple[str, str]:
    """Enqueue a run with a pre-allocated run_id so the effect line is known a priori."""
    try:
        from omniagentos.contracts import new_id, utc_now_iso
        from omniagentos.db.store import SqliteStore
    except ImportError as exc:  # pragma: no cover
        raise SmokeError(f"store unavailable: {exc}") from exc

    store = SqliteStore(str(workspace.db_path))
    now = utc_now_iso()
    task_id = new_id("tsk")
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "research-briefs",
            "title": title,
            "input_json": json.dumps({"prompt": "restart smoke", "tools_allowed": EFFECT_TOOLS}),
            "acceptance_json": "{}",
            "state": "ready",
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "discipline_id": "research-briefs",
            "arm": None,
            "harness": "mock",
            "harness_version": "",
            "env_hash": "",
            "harness_params": "{}",
            "agent": "mock",
            "model": None,
            "attempt": 1,
            "state": "queued",
            "worker_id": None,
            "plan_json": json.dumps(plan),
            "budget_json": "{}",
            "cancel_requested": 0,
            "trace_id": run_id,
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )
    try:
        store.update_task_state(task_id, "queued", expect=["ready", "draft"])
    except Exception:
        store.update_task_state(task_id, "queued")
    return task_id, run_id
