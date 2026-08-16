"""B5 — global pause halts claiming / parks mid-flight runs; unpause requeues."""

from __future__ import annotations

import pytest

from tests.smoke.helpers import (
    SmokeError,
    Workspace,
    create_run,
    create_task,
    get_run,
    mock_agent_step,
    poll_until,
    run_id_of,
    run_state,
    set_pause,
    start_api,
    start_runner,
    start_system,
    task_id_of,
    wait_http_ok,
    wait_run_state,
    wait_run_terminal,
)


@pytest.mark.smoke
@pytest.mark.s19b_live_restart
def test_pause_keeps_queued_run_unclaimed(
    smoke_workspace: Workspace,
    managed_procs: list,
) -> None:
    """With pause ON, a newly queued run stays QUEUED (runner does not claim)."""
    ws = smoke_workspace
    logs = ws.root / "logs"

    # Start runner first so it is alive but must honor pause.
    runner = start_runner(ws, poll_ms=200, log_path=logs / "runner.log")
    managed_procs.append(runner)
    api, port = start_api(ws, log_path=logs / "api.log")
    managed_procs.append(api)
    base = f"http://127.0.0.1:{port}"
    wait_http_ok(base)

    import httpx

    with httpx.Client(base_url=base, timeout=10.0) as client:
        set_pause(client, True, reason="smoke-pause-queued")

        task = create_task(client, title="pause-queued-smoke")
        task_id = task_id_of(task)
        plan = [
            mock_agent_step("step-a", reply="a", usage={"input_tokens": 2, "output_tokens": 1}),
            mock_agent_step("step-b", reply="b", usage={"input_tokens": 2, "output_tokens": 1}),
        ]
        run = create_run(client, task_id, harness="mock", plan=plan, prompt="paused queue")
        rid = run_id_of(run)

        # Hold pause for several runner poll cycles; state must remain queued.
        def _still_queued() -> str | None:
            payload = get_run(client, rid)
            state = run_state(payload)
            if state != "queued":
                raise SmokeError(f"run left queued while paused: state={state}")
            return state

        # Positive observation window: several successful "still queued" samples.
        samples = 0

        def _enough_samples() -> bool:
            nonlocal samples
            _still_queued()
            samples += 1
            return samples >= 5

        poll_until(_enough_samples, timeout_s=8.0, start_s=0.15, max_s=0.5, desc="stay queued")

        # Unpause → runner claims → completes.
        set_pause(client, False, reason="smoke-unpause-queued")
        final = wait_run_terminal(client, rid, timeout_s=60.0)
        assert run_state(final) == "completed", final


@pytest.mark.smoke
@pytest.mark.s19b_live_restart
def test_pause_mid_flight_parks_and_resumes(
    smoke_workspace: Workspace,
    managed_procs: list,
) -> None:
    """In-flight run parks PAUSED between steps; unpause → requeue → completed.

    Also verifies no further agent steps complete while paused (proxy for
    'no adapter calls while paused' — MockAdapter has no shared call counter,
    so we assert step statuses / events).
    """
    ws = smoke_workspace
    system = start_system(ws, poll_ms=150, logs_dir=ws.root / "logs")
    managed_procs.extend([system.api, system.runner])

    import httpx

    with httpx.Client(base_url=system.base_url, timeout=10.0) as client:
        # Multi-step plan: first step is fast, second is long so we can pause
        # after step-1 completes and before step-2 finishes.
        plan = [
            mock_agent_step(
                "step-1",
                reply="one",
                usage={"input_tokens": 3, "output_tokens": 2, "turns": 1},
            ),
            mock_agent_step(
                "step-2",
                reply="two",
                delay_ms=15_000,
                usage={"input_tokens": 3, "output_tokens": 2, "turns": 1},
            ),
            mock_agent_step(
                "step-3",
                reply="three",
                usage={"input_tokens": 3, "output_tokens": 2, "turns": 1},
            ),
        ]
        task = create_task(client, title="pause-midflight-smoke")
        task_id = task_id_of(task)
        run = create_run(client, task_id, harness="mock", plan=plan, prompt="midflight")
        rid = run_id_of(run)

        # Wait until step-1 is completed (run is mid-flight).
        def _step1_done() -> bool:
            steps = ws.steps_for(rid)
            for step in steps:
                if step["name"] == "step-1" and step["status"] in {"completed", "skipped"}:
                    return True
            # Fallback: run has left queued.
            row = ws.run_row(rid)
            return bool(row and row["state"] in {"running", "paused"})

        poll_until(_step1_done, timeout_s=30.0, desc="step-1 completed")

        set_pause(client, True, reason="smoke-pause-midflight")

        # Runner must park before next step boundary → PAUSED (or stay without
        # completing step-3). Wait for PAUSED state.
        paused_payload = wait_run_state(client, rid, {"paused"}, timeout_s=45.0)
        assert run_state(paused_payload) == "paused"

        completed_before = {
            s["name"] for s in ws.steps_for(rid) if s["status"] in {"completed", "skipped"}
        }
        # While paused, no additional steps should complete.
        samples = 0

        def _no_progress() -> bool:
            nonlocal samples
            payload = get_run(client, rid)
            assert run_state(payload) == "paused", payload
            now = {s["name"] for s in ws.steps_for(rid) if s["status"] in {"completed", "skipped"}}
            if now - completed_before:
                raise SmokeError(
                    f"steps completed while paused: before={completed_before} now={now}"
                )
            samples += 1
            return samples >= 4

        poll_until(_no_progress, timeout_s=6.0, start_s=0.2, max_s=0.5, desc="no progress paused")

        # Unpause: runner owns PAUSED→QUEUED (D-008), then runs to completion.
        set_pause(client, False, reason="smoke-unpause-midflight")

        # Observe requeue (QUEUED) at least once if timing allows — not required
        # if the runner claims immediately after requeue within one poll.
        def _left_paused() -> dict | None:
            payload = get_run(client, rid)
            state = run_state(payload)
            if state != "paused":
                return payload
            return None

        poll_until(_left_paused, timeout_s=30.0, desc="leave paused after unpause")

        final = wait_run_terminal(client, rid, timeout_s=60.0)
        assert run_state(final) == "completed", final

        # All three agent steps should have completed eventually.
        final_steps = {s["name"]: s["status"] for s in ws.steps_for(rid)}
        for name in ("step-1", "step-2", "step-3"):
            assert final_steps.get(name) in {"completed", "skipped"}, final_steps
