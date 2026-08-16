"""Tier2 live probe: ``python -m omniagentos.runner --once`` executes a real
cheap-CLI run (gemini flash via the gemini CLI adapter) to a terminal state.

The plan/run seeding mirrors tests/knowledge/test_e2e_real.py::_create_run; the
model pin follows tests/swarm/test_live_all_providers.py PROVIDER_MODELS
(gemini -> a cheap flash model). The sandbox is REAL — nothing pins
wrap_available — because this is a live spawn.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

REPO = Path(__file__).resolve().parents[3]

# Cheap model where the gemini CLI accepts -m (PROVIDER_MODELS idiom).
_GEMINI_MODEL = "gemini-2.5-flash"
# Stay well under the 180s global pytest timeout: internal subprocess cap.
_SUBPROCESS_TIMEOUT_S = 140


def _load_gemini_env(env: dict[str, str]) -> None:
    """Forward ~/.gemini/.env keys (API key) without clobbering presets."""
    gemini_env = Path.home() / ".gemini" / ".env"
    if not gemini_env.exists():
        return
    for line in gemini_env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def test_runner_once_completes_cheap_cli_run(fh_budget, fh_subprocess_env) -> None:
    if shutil.which("gemini") is None:
        pytest.skip("gemini CLI binary not on PATH — cannot spawn a live runner attempt")
    if not (Path.home() / ".gemini").is_dir():
        pytest.skip("gemini CLI auth dir ~/.gemini absent — cannot spawn a live runner attempt")
    fh_budget.require_headroom(cli=True)

    from omniagentos.contracts import (
        ActionClass,
        HarnessType,
        RunState,
        TaskState,
        new_id,
        utc_now_iso,
    )
    from omniagentos.db.migrate import migrate
    from omniagentos.db.store import SqliteStore

    db_path = fh_subprocess_env["OMNIAGENTOS_DB"]
    migrate(db_path)
    store = SqliteStore(db_path)

    now = utc_now_iso()
    task_id = new_id("tsk")
    run_id = new_id("run")
    prompt = "Reply with exactly this text and nothing else: FH-TIER2-RUNNER-OK"
    store.create_task(
        {
            "id": task_id,
            "discipline_id": "code-changes",
            "title": "fh tier2 runner probe",
            "input_json": json.dumps({"prompt": prompt}),
            "acceptance_json": "{}",
            "state": TaskState.QUEUED.value,
            "risk": "low",
            "created_at": now,
            "updated_at": now,
        }
    )
    store.enqueue_run(
        {
            "id": run_id,
            "task_id": task_id,
            "discipline_id": "code-changes",
            "agent": "fh-tier2-agent",
            "harness": HarnessType.CLI_GEMINI.value,
            "state": RunState.QUEUED.value,
            "plan_json": json.dumps(
                [
                    {
                        "name": "agent",
                        "kind": "agent",
                        "action_class": ActionClass.SANDBOXED_CREATION.value,
                        "params": {
                            "adapter": HarnessType.CLI_GEMINI.value,
                            "prompt": prompt,
                            "model": _GEMINI_MODEL,
                        },
                    }
                ]
            ),
            "budget_json": json.dumps({"wall_ms_max": 90_000, "max_turns": 1}),
            "trace_id": f"trace-{run_id}",
            "queued_at": now,
            "created_at": now,
            "updated_at": now,
        }
    )

    env = dict(fh_subprocess_env)
    _load_gemini_env(env)
    # The runner's per-run workspace base defaults to the PRODUCT var/runs
    # (runner/core.py:_default_workspace_base) — pin it inside the isolated var.
    workspace = Path(env["OMNIAGENTOS_VAR_DIR"]) / "runs"
    # The runner resolves working_dir to <workspace>/<run_id> without creating
    # it for an agent step (only _run_workspace mkdirs); pre-create it so the
    # CLI adapter's cwd exists.
    (workspace / run_id).mkdir(parents=True, exist_ok=True)
    env["OMNIAGENTOS_WORKSPACE_DIR"] = str(workspace)

    fh_budget.record_cli_call()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "omniagentos.runner", "--once"],
            env=env,
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        pytest.skip(
            f"runner --once exceeded the internal {_SUBPROCESS_TIMEOUT_S}s cap for a "
            f"live gemini CLI attempt (global pytest timeout protection)"
        )

    run_row = store.get_run(run_id)
    assert run_row is not None
    state = str(run_row.get("state"))
    tail = (proc.stderr or proc.stdout or "")[-800:]
    assert state == RunState.COMPLETED.value, (
        f"run reached {state!r}, not completed (rc={proc.returncode}); "
        f"error={run_row.get('error')!r}; runner output tail: {tail}"
    )

    steps = store.get_steps(run_id)
    agent_steps = [s for s in steps if str(s.get("name")) == "agent"]
    assert agent_steps, f"no agent step recorded for {run_id}: {steps}"
    output = json.loads(agent_steps[-1]["result_json"] or "{}")
    usage = output.get("usage") or {}
    # tokens/cost recorded: the gemini CLI reports exact token counts; cost may
    # legitimately be unreported for a subscription CLI — assert presence/shape,
    # never a specific value.
    assert isinstance(usage.get("input_tokens"), int) and usage["input_tokens"] > 0, usage
    assert isinstance(usage.get("output_tokens"), int) and usage["output_tokens"] > 0, usage
    assert "cost_usd" in usage, f"cost_usd field missing from recorded usage: {usage}"
    assert "FH-TIER2-RUNNER-OK" in str(output.get("output_text", "")), output.get("output_text")
