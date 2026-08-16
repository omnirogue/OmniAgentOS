"""B3 — budget exhaustion and cancellation (incl. parked approvals)."""

from __future__ import annotations

import pytest

from tests.smoke.helpers import (
    EFFECT_TOOLS,
    SmokeError,
    Workspace,
    approval_gated_effect_step,
    cancel_run,
    create_run,
    create_task,
    get_run,
    mock_agent_step,
    poll_until,
    run_id_of,
    run_state,
    start_system,
    task_id_of,
    wait_run_state,
    wait_run_terminal,
)


@pytest.mark.smoke
@pytest.mark.s19b_live_restart
def test_budget_exhaustion_stops_further_steps(
    smoke_workspace: Workspace,
    managed_procs: list,
) -> None:
    """Early step burns tokens; subsequent agent steps never run; error=budget_exceeded.

    Blocking is opt-in since 2026-07-24 (budgets are advisory by default, so an
    overshoot never strands a half-done plan). This smoke pins the escape hatch
    end-to-end through a REAL api+runner: with enforcement ON the run still fails
    budget_exceeded and the later step never executes.
    """
    ws = smoke_workspace
    # The runner is a real SUBPROCESS: monkeypatch.setenv would not reach it, so the
    # posture must ride in the workspace env the process is spawned with. This is
    # the same requirement production has -- launchd jobs need it exported too.
    ws.env["OMNIAGENTOS_BUDGET_ENFORCEMENT"] = "block"
    system = start_system(ws, poll_ms=150, logs_dir=ws.root / "logs")
    managed_procs.extend([system.api, system.runner])

    import httpx

    # BudgetSpec field is tokens_max (not max_tokens) per contracts.BudgetSpec.
    budget = {"tokens_max": 10}
    plan = [
        mock_agent_step(
            "burn-tokens",
            reply="burn",
            usage={"input_tokens": 8, "output_tokens": 5, "turns": 1},
        ),
        mock_agent_step(
            "should-not-run",
            reply="nope",
            usage={"input_tokens": 1, "output_tokens": 1, "turns": 1},
        ),
        mock_agent_step(
            "also-should-not-run",
            reply="nope2",
            usage={"input_tokens": 1, "output_tokens": 1, "turns": 1},
        ),
    ]

    with httpx.Client(base_url=system.base_url, timeout=10.0) as client:
        task = create_task(client, title="budget-exhaust-smoke")
        run = create_run(
            client,
            task_id_of(task),
            harness="mock",
            plan=plan,
            budget=budget,
            prompt="budget test",
        )
        rid = run_id_of(run)
        final = wait_run_terminal(client, rid, timeout_s=60.0)

        assert run_state(final) == "failed", final
        # Prefer HTTP payload error, fall back to DB row.
        err = final.get("error")
        if err is None and isinstance(final.get("run"), dict):
            err = final["run"].get("error")
        if err is None:
            row = ws.run_row(rid)
            err = row["error"] if row is not None else None
        assert err == "budget_exceeded", f"expected budget_exceeded, got {err!r} payload={final}"

        # No further adapter steps after exhaustion: only burn-tokens may complete.
        step_rows = ws.steps_for(rid)
        by_name = {s["name"]: s["status"] for s in step_rows}
        assert by_name.get("should-not-run") not in {"completed", "started", "skipped"}, by_name
        assert by_name.get("also-should-not-run") not in {"completed", "started", "skipped"}, (
            by_name
        )


@pytest.mark.smoke
@pytest.mark.s19b_live_restart
def test_cancel_during_step_ends_cancelled(
    smoke_workspace: Workspace,
    managed_procs: list,
) -> None:
    """Cancel while a long agent step is STARTED → run CANCELLED; compensations run."""
    ws = smoke_workspace
    system = start_system(ws, poll_ms=100, logs_dir=ws.root / "logs")
    managed_procs.extend([system.api, system.runner])

    import httpx

    # First step records a side effect + declares compensate; second step delays
    # so we can cancel mid-flight.
    # Effects confine to the runner-assigned workspace (council R2); working_dir is
    # no longer honored, so the relative paths resolve under <workspace>/<run_id>.
    compensate = {
        "effect": "append_file",
        "path": "var/smoke/compensate.txt",
        "line": "compensated",
        "probe": "grep -q compensated var/smoke/compensate.txt",
    }
    plan = [
        {
            "name": "do-work",
            "kind": "effect",
            "action_class": "internal_reversible",
            "params": {
                "effect": "append_file",
                "path": "var/smoke/work.txt",
                "line": "done",
                "probe": "grep -q done var/smoke/work.txt",
                "compensate": compensate,
            },
        },
        mock_agent_step(
            "slow-agent",
            reply="slow",
            delay_ms=30_000,
            usage={"input_tokens": 2, "output_tokens": 2, "turns": 1},
        ),
        mock_agent_step(
            "after-cancel",
            reply="should-not",
            usage={"input_tokens": 1, "output_tokens": 1, "turns": 1},
        ),
    ]

    with httpx.Client(base_url=system.base_url, timeout=10.0) as client:
        task = create_task(client, title="cancel-mid-step-smoke", tools_allowed=EFFECT_TOOLS)
        run = create_run(
            client,
            task_id_of(task),
            harness="mock",
            plan=plan,
            prompt="cancel mid step",
        )
        rid = run_id_of(run)

        def _slow_started() -> bool:
            for step in ws.steps_for(rid):
                if step["name"] == "slow-agent" and step["status"] == "started":
                    return True
            return False

        poll_until(
            _slow_started, timeout_s=45.0, start_s=0.05, max_s=0.5, desc="slow-agent STARTED"
        )

        cancel_run(client, rid)
        final = wait_run_terminal(client, rid, timeout_s=60.0)
        assert run_state(final) == "cancelled", final

        # Compensations for completed steps that declared them. Effects are confined
        # to the runner-assigned workspace <workspace_dir>/<run_id> (council R2).
        work = ws.workspace_dir / rid / "var" / "smoke" / "work.txt"
        comp = ws.workspace_dir / rid / "var" / "smoke" / "compensate.txt"
        assert work.is_file() and "done" in work.read_text(encoding="utf-8")

        def _compensated() -> bool:
            steps = ws.steps_for(rid)
            for step in steps:
                if step["name"] == "do-work" and step["status"] == "compensated":
                    return True
            # Fallback: compensate effect landed even if status naming differs.
            return comp.is_file() and "compensated" in comp.read_text(encoding="utf-8")

        try:
            poll_until(_compensated, timeout_s=20.0, desc="compensation executed")
        except SmokeError:
            steps = [dict(s) for s in ws.steps_for(rid)]
            raise AssertionError(f"compensation not observed; steps={steps}") from None

        # After-cancel step must not complete.
        by_name = {s["name"]: s["status"] for s in ws.steps_for(rid)}
        assert by_name.get("after-cancel") not in {"completed", "skipped"}, by_name


@pytest.mark.smoke
@pytest.mark.s19b_live_restart
def test_cancel_while_awaiting_approval_voids_approval(
    smoke_workspace: Workspace,
    managed_procs: list,
) -> None:
    """Cancel while parked on approval → CANCELLED + approval EXPIRED voided note."""
    ws = smoke_workspace
    system = start_system(ws, poll_ms=150, logs_dir=ws.root / "logs")
    managed_procs.extend([system.api, system.runner])

    import httpx

    plan = [
        mock_agent_step(
            "pre",
            reply="pre",
            usage={"input_tokens": 2, "output_tokens": 1, "turns": 1},
        ),
        approval_gated_effect_step("needs-approval", effect="noop"),
        mock_agent_step(
            "post",
            reply="post",
            usage={"input_tokens": 1, "output_tokens": 1, "turns": 1},
        ),
    ]

    with httpx.Client(base_url=system.base_url, timeout=10.0) as client:
        task = create_task(client, title="cancel-awaiting-approval-smoke")
        run = create_run(
            client,
            task_id_of(task),
            harness="mock",
            plan=plan,
            prompt="cancel parked",
        )
        rid = run_id_of(run)

        parked = wait_run_state(client, rid, {"awaiting_approval"}, timeout_s=45.0)
        assert run_state(parked) == "awaiting_approval"

        # Approval must be pending.
        def _pending_approval() -> dict | None:
            rows = ws.approvals_for(rid)
            for row in rows:
                if row["state"] == "pending":
                    return dict(row)
            payload = get_run(client, rid)
            for appr in payload.get("approvals") or []:
                if isinstance(appr, dict) and appr.get("state") == "pending":
                    return appr
            return None

        pending = poll_until(_pending_approval, timeout_s=15.0, desc="pending approval")
        assert pending is not None

        cancel_run(client, rid)
        final = wait_run_terminal(client, rid, timeout_s=45.0)
        assert run_state(final) == "cancelled", final

        def _voided() -> dict | None:
            for row in ws.approvals_for(rid):
                if row["state"] == "expired":
                    note = row["decision_note"] or ""
                    if "voided: run cancelled" in note:
                        return dict(row)
            payload = get_run(client, rid)
            for appr in payload.get("approvals") or []:
                if not isinstance(appr, dict):
                    continue
                if appr.get("state") == "expired" and "voided: run cancelled" in (
                    appr.get("decision_note") or ""
                ):
                    return appr
            return None

        voided = poll_until(_voided, timeout_s=20.0, desc="approval voided")
        assert voided["state"] == "expired"
        assert voided.get("decision_note") == "voided: run cancelled" or (
            "voided: run cancelled" in (voided.get("decision_note") or "")
        )
