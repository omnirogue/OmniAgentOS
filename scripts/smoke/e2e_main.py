#!/usr/bin/env python3
"""A6 end-to-end happy path (+ failing-run task projection).

Bootstraps a tmp workspace, starts API + runner as separate processes, drives:

  create task → queue mock run with approval-gated step → park awaiting_approval
  → approve → completed → exactly one ledger line + vault note cross-refs
  → task projects to completed

Also verifies a failing mock run projects the task to failed (D-002).

Exit 0 on success; nonzero with a clear message on any assertion failure.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import traceback
from pathlib import Path

# Allow `python scripts/smoke/e2e_main.py` from worktree root without install tricks.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from tests.smoke.helpers import (  # noqa: E402
    SmokeError,
    Workspace,
    approval_gated_effect_step,
    create_run,
    create_task,
    decide_approval,
    find_vault_notes,
    get_run,
    get_task,
    mock_agent_step,
    poll_until,
    read_ledger_manifests,
    run_id_of,
    run_state,
    start_system,
    task_id_of,
    wait_run_state,
    wait_run_terminal,
)


def _fail(msg: str, code: int = 1) -> int:
    print(f"E2E FAIL: {msg}", file=sys.stderr)
    return code


def _ok(msg: str) -> None:
    print(f"E2E OK: {msg}")


def _pending_approval(client, workspace: Workspace, run_id: str) -> dict | None:
    payload = get_run(client, run_id)
    for appr in payload.get("approvals") or []:
        if isinstance(appr, dict) and appr.get("state") == "pending":
            return appr
    for row in workspace.approvals_for(run_id):
        if row["state"] == "pending":
            return dict(row)
    return None


def _run_happy_path(client, workspace: Workspace) -> str:
    plan = [
        mock_agent_step(
            "draft",
            reply="draft-output",
            usage={"input_tokens": 4, "output_tokens": 6, "turns": 1, "cost_usd": 0.0},
        ),
        # No action_class override: the helper's default is the irreversible
        # hard-stop class — since the AC-policy flip, it is the ONLY class that
        # parks under AUTO mode. The old external_reversible override predated
        # that flip and auto-executed, so the run completed without ever
        # parking and this smoke could not pass.
        approval_gated_effect_step(
            "gated-publish",
            effect="noop",
        ),
        mock_agent_step(
            "finalize-agent",
            reply="done",
            usage={"input_tokens": 2, "output_tokens": 2, "turns": 1},
        ),
    ]
    task = create_task(
        client,
        title="e2e-happy-path",
        discipline_id="research-briefs",
        input_payload={"prompt": "e2e happy path", "tools_allowed": []},
    )
    task_id = task_id_of(task)
    run = create_run(
        client,
        task_id,
        harness="mock",
        plan=plan,
        prompt="e2e happy path",
    )
    run_id = run_id_of(run)
    _ok(f"created task={task_id} run={run_id}")

    parked = wait_run_state(client, run_id, {"awaiting_approval"}, timeout_s=60.0)
    assert run_state(parked) == "awaiting_approval", parked
    _ok("run parked in awaiting_approval")

    approval = poll_until(
        lambda: _pending_approval(client, workspace, run_id),
        timeout_s=20.0,
        desc="pending approval row",
    )
    approval_id = approval["id"]
    decide_approval(client, approval_id, decision="approved", note="e2e approve")
    _ok(f"approved {approval_id}")

    final = wait_run_terminal(client, run_id, timeout_s=90.0)
    state = run_state(final)
    if state != "completed":
        raise SmokeError(f"happy path expected completed, got {state}: {final}")
    _ok("run completed")

    # Task projection (D-002): latest run completed → task completed.
    def _task_completed() -> dict | None:
        task_payload = get_task(client, task_id)
        tstate = task_payload.get("state")
        if tstate == "completed":
            return task_payload
        # Fallback to DB if API nests differently.
        row = workspace.task_row(task_id)
        if row is not None and row["state"] == "completed":
            return dict(row)
        return None

    poll_until(_task_completed, timeout_s=20.0, desc="task projects to completed")
    _ok("task projected to completed")

    # Finalization (ledger manifest, then vault note) happens just AFTER the run
    # flips terminal, so poll for both to appear before asserting on them — else
    # the gate is flaky under load (council OPS-001).
    def _finalized() -> bool:
        ms = read_ledger_manifests(workspace.ledger_dir, run_id=run_id)
        if not ms:
            return False
        return bool(
            find_vault_notes(workspace.vault_dir, run_id, task_id)
            or find_vault_notes(workspace.vault_dir, run_id, "e2e-happy-path")
            or list(workspace.vault_dir.rglob(f"*{run_id}*.md"))
        )

    poll_until(_finalized, timeout_s=30.0, desc="finalization (ledger line + vault note)")

    # Exactly ONE ledger JSONL line for the run (state=completed, usage + receipts).
    manifests = read_ledger_manifests(workspace.ledger_dir, run_id=run_id)
    if len(manifests) != 1:
        raise SmokeError(f"expected exactly 1 ledger line for {run_id}, got {len(manifests)}")
    manifest = manifests[0]
    m_state = getattr(manifest, "state", None) or (
        manifest.get("state") if isinstance(manifest, dict) else None
    )
    m_state_s = str(getattr(m_state, "value", m_state))
    if m_state_s != "completed":
        raise SmokeError(f"ledger state expected completed, got {m_state_s!r}")
    usage = (
        getattr(manifest, "usage", None)
        if not isinstance(manifest, dict)
        else manifest.get("usage")
    )
    if usage is None:
        raise SmokeError("ledger manifest missing usage")
    receipts = (
        getattr(manifest, "receipts", None)
        if not isinstance(manifest, dict)
        else manifest.get("receipts")
    )
    if receipts is None:
        raise SmokeError("ledger manifest missing receipts field")
    _ok("ledger has exactly one completed manifest with usage + receipts")

    # Vault note cross-references the run AND the task.
    notes = find_vault_notes(workspace.vault_dir, run_id, task_id)
    if not notes:
        # Title is plain text on the note; also accept run id + task title.
        notes = find_vault_notes(workspace.vault_dir, run_id, "e2e-happy-path")
    if not notes:
        # Last resort: path contains run id under runs/.
        candidates = list(workspace.vault_dir.rglob(f"*{run_id}*.md"))
        if candidates:
            notes = candidates
    if not notes:
        raise SmokeError(
            f"no vault note referencing run={run_id} and task={task_id} under {workspace.vault_dir}"
        )
    note_path = notes[0]
    if not note_path.is_file() or note_path.stat().st_size <= 0:
        raise SmokeError(f"vault note missing or empty: {note_path}")
    text = note_path.read_text(encoding="utf-8")
    if run_id not in text:
        raise SmokeError(f"vault note does not mention run_id: {note_path}")
    if task_id not in text and "e2e-happy-path" not in text:
        raise SmokeError(f"vault note does not cross-reference task: {note_path}")
    _ok(f"vault note ok: {note_path.relative_to(workspace.vault_dir)}")

    # Confirm ledger file path(s) exist and are readable.
    ledger_files = list(workspace.ledger_dir.glob("runs-*.jsonl"))
    if not ledger_files:
        raise SmokeError(f"no ledger JSONL files under {workspace.ledger_dir}")
    for path in ledger_files:
        path.read_text(encoding="utf-8")
    _ok(f"ledger files readable: {[p.name for p in ledger_files]}")

    return run_id


def _run_failing_projection(client, workspace: Workspace) -> str:
    """Failing mock run must project the task to failed."""
    plan = [
        mock_agent_step(
            "will-fail",
            reply="x",
            fail=True,
            usage={"input_tokens": 2, "output_tokens": 1, "turns": 1},
        ),
    ]
    task = create_task(client, title="e2e-failing-run", discipline_id="research-briefs")
    task_id = task_id_of(task)
    run = create_run(
        client,
        task_id,
        harness="mock",
        plan=plan,
        prompt="fail please",
    )
    run_id = run_id_of(run)
    final = wait_run_terminal(client, run_id, timeout_s=60.0)
    if run_state(final) != "failed":
        raise SmokeError(f"failing run expected failed, got {run_state(final)}: {final}")

    def _task_failed() -> bool:
        payload = get_task(client, task_id)
        if payload.get("state") == "failed":
            return True
        row = workspace.task_row(task_id)
        return bool(row and row["state"] == "failed")

    poll_until(_task_failed, timeout_s=20.0, desc="task projects to failed")
    _ok(f"failing run {run_id} projected task {task_id} → failed")
    return run_id


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="omniagentos-e2e-"))
    system = None
    try:
        try:
            workspace = Workspace.create(tmp)
        except SmokeError as exc:
            return _fail(str(exc))

        try:
            system = start_system(workspace, poll_ms=500, logs_dir=tmp / "logs")
        except Exception as exc:
            return _fail(f"failed to start api/runner: {exc}")

        import httpx

        with httpx.Client(base_url=system.base_url, timeout=15.0) as client:
            _run_happy_path(client, workspace)
            _run_failing_projection(client, workspace)

        print("E2E PASS: happy path + failing projection verified")
        return 0
    except SmokeError as exc:
        return _fail(str(exc))
    except Exception as exc:  # noqa: BLE001 — top-level e2e guard
        traceback.print_exc()
        return _fail(f"unhandled error: {exc}")
    finally:
        if system is not None:
            system.close()
        else:
            # Best-effort if partial start.
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
