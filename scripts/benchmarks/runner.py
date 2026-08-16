"""Execute one benchmark fixture against one named arm and score it.

Sequence per run — the ordering is the design:

    materialize seed/  →  snapshot  →  ARM RUNS  →  snapshot  →  diff/canary/
    transcript  →  overlay accept/  →  frozen acceptance check  →  record

The second snapshot is taken BEFORE the acceptance overlay, so every path in
the diff is attributable to the arm and nothing the harness itself writes can
be mistaken for an agent edit. The overlay lands after, so an arm that edited a
visible check has that edit reverted before grading (and still recorded as an
undeclared modification).

Arm dispatch reuses the existing ladder verbatim:
``omniagentos.harnesses.bench.runner.resolve_arm_adapter`` for identity and
``omniagentos.harnesses.bench.b0.run_b0_arm`` for b0 itself. Identity fields
(arm, harness, harness_version, env_hash, harness_params) come from that
package's own ``build_manifest``, so a benchmark run and a bench-ladder run
describe themselves identically.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast

from omniagentos.contracts import (
    AgentInput,
    AgentResult,
    AgentUsage,
    Arm,
    BudgetSpec,
    HarnessType,
    ResultStatus,
    new_id,
    utc_now_iso,
)
from omniagentos.harnesses.bench.runner import build_manifest, resolve_arm_adapter
from scripts.benchmarks import observe
from scripts.benchmarks.fixtures import (
    REPO_ROOT,
    Fixture,
    apply_acceptance_files,
    apply_solution,
    materialize,
)

# arm key → (Arm | None, HarnessType). The first three mirror
# omniagentos.harnesses.bench.runner._ARM_TABLE (private there). The rest are
# SYNTHETIC arms: they call no model, carry no contracts.Arm, and exist to
# bracket the real arms — `oracle` applies the reference solution (the ceiling
# any arm could reach) and `noop` does nothing (the floor). A chart with only
# agent arms on it has no scale.
#
# `grok` is a REAL arm (synthetic=False, live adapter, live cost) that carries
# no contracts.Arm member: Arm is frozen at b0/b1/champion and a benchmark must
# not widen a shared contract enum. HarnessType.CLI_GROK already exists and is
# the adapter registry key, so identity is exact where it matters.
ARM_TABLE: dict[str, tuple[Arm | None, HarnessType]] = {
    "b0": (Arm.B0, HarnessType.CLI_CLAUDE),
    "b1": (Arm.B1, HarnessType.MINI_SWE),
    "champion": (Arm.CHAMPION, HarnessType.FUSION),
    "grok": (None, HarnessType.CLI_GROK),
    "mock": (None, HarnessType.MOCK),
    "oracle": (None, HarnessType.MOCK),
    "noop": (None, HarnessType.MOCK),
}
SYNTHETIC_ARMS = frozenset({"mock", "oracle", "noop"})

# Per-arm default model, applied only when the caller passed no --model override.
# Keeps `--arm grok` reproducible and makes the stored record say which model ran
# instead of NULL; configs/modelintel.yaml pins grok-4.5 as the baseline model.
ARM_DEFAULT_MODELS: dict[str, str] = {"grok": "grok-4.5"}

DEFAULT_WORKSPACE_ROOT = REPO_ROOT / "var" / "benchmarks" / "workspaces"
_ACCEPTANCE_TAIL_CHARS = 4000
_TRANSCRIPT_SCAN_BYTES = 8_000_000


def render_prompt(fixture: Fixture, workspace: Path) -> str:
    """The task text handed to the arm. Identical for every arm, by construction."""
    return (
        f"You are working in the directory {workspace}.\n"
        "It is a small self-contained project. Complete the task below, then stop.\n"
        "Do not ask follow-up questions; there is nobody to answer them.\n\n"
        f"TASK:\n{fixture.prompt}\n"
    )


def _build_agent_input(
    *,
    fixture: Fixture,
    workspace: Path,
    arm: str,
    run_id: str,
    replicate: int,
    model: str | None,
    capture_id: str,
) -> AgentInput:
    return AgentInput(
        run_id=run_id,
        task_id=fixture.id,
        prompt=render_prompt(fixture, workspace),
        working_dir=str(workspace),
        model=model,
        budget=BudgetSpec(wall_ms_max=fixture.timeout_ms, max_turns=fixture.max_turns),
        metadata={
            "arm": arm,
            "replicate": replicate,
            "capture_id": capture_id,
            "bench_fixture": fixture.id,
            # The arm must be able to edit its workspace or every fixture fails
            # for the wrong reason. This grants the workspace as an OS-sandbox
            # write root; it does NOT grant anything outside it.
            "sandbox": {"level": "workspace_write"},
            # DELIBERATELY NOT SET: "cli_workspace_write_elevated".
            # adapters/common.py:603 _unattended_elevated reads that key as "a
            # human explicitly elevated this unattended sub-CLI step", which makes
            # _refuse_unattended_launch return None -- disabling the floor that
            # refuses to launch an unattended agent when sandbox_available() is
            # False (non-macOS host, missing sandbox-exec, failed self-test).
            # A benchmark must never be the thing that bypasses a safety floor:
            # if the sandbox is unavailable, the correct outcome is a refused
            # launch recorded as such, not an unsandboxed arm scored as valid.
        },
    )


def _run_synthetic(arm: str, agent_input: AgentInput, fixture: Fixture) -> AgentResult:
    """Control arms. No model, no network, no cost — a fixed edit or none at all."""
    started = time.monotonic()
    applied: list[str] = []
    if arm == "oracle":
        applied = apply_solution(fixture, str(agent_input.working_dir))
    wall_ms = max(1, int((time.monotonic() - started) * 1000))
    return AgentResult(
        status=ResultStatus.OK,
        output_text=f"synthetic arm {arm}: applied {applied}",
        usage=AgentUsage(
            wall_ms=wall_ms,
            turns=0,
            input_tokens=0,
            output_tokens=0,
            cost_usd=0.0,
            estimated=False,
            source="imported",
        ),
    )


def invoke_arm(arm: str, agent_input: AgentInput, *, adapter: Any, fixture: Fixture) -> AgentResult:
    """Run the arm. b0 goes through the existing ``run_b0_arm`` unchanged."""
    if arm in ("oracle", "noop"):
        return _run_synthetic(arm, agent_input, fixture)
    if arm == "b0":
        from omniagentos.harnesses.bench.b0 import run_b0_arm

        return run_b0_arm(agent_input)
    return adapter.run(agent_input)


def run_acceptance(fixture: Fixture, workspace: Path) -> dict[str, Any]:
    """Overlay the frozen checks and run them. Return code 0 == task success."""
    overlaid = apply_acceptance_files(fixture, workspace)
    command = [
        sys.executable,
        "-m",
        "pytest",
        *fixture.acceptance_paths,
        "-q",
        "-p",
        "no:cacheprovider",
        "--rootdir",
        str(workspace),
    ]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", str(workspace)),
        "PYTHONPATH": str(workspace),
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
    }
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=fixture.acceptance_timeout_s,
            env=env,
            check=False,
        )
        returncode = completed.returncode
        output = (completed.stdout or "") + (completed.stderr or "")
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        returncode = -1
        output = f"acceptance timed out after {fixture.acceptance_timeout_s}s\n{exc}"
        timed_out = True
    duration_ms = int((time.monotonic() - started) * 1000)
    return {
        "returncode": returncode,
        "passed": returncode == 0,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "overlaid_paths": overlaid,
        "command": command[1:],
        "tail": output[-_ACCEPTANCE_TAIL_CHARS:],
    }


def _read_transcript_text(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        with path.open(encoding="utf-8", errors="ignore") as handle:
            return handle.read(_TRANSCRIPT_SCAN_BYTES)
    except OSError:
        return ""


def run_fixture(
    fixture: Fixture,
    arm: str,
    *,
    capture_id: str,
    workspace_root: str | Path = DEFAULT_WORKSPACE_ROOT,
    replicate: int = 0,
    model: str | None = None,
    adapter: Any | None = None,
    invoke: Any | None = None,
) -> dict[str, Any]:
    """Run one fixture against one arm and return the full scored record.

    ``adapter``/``invoke`` are injection seams for the harness's own tests; the
    real capture path leaves both None and resolves the arm normally.
    """
    key = arm.lower().strip()
    if key not in ARM_TABLE:
        raise KeyError(f"unknown arm {arm!r}; known: {sorted(ARM_TABLE)}")
    arm_enum, harness_type = ARM_TABLE[key]
    model = model or ARM_DEFAULT_MODELS.get(key)

    workspace = Path(workspace_root) / capture_id / f"{fixture.id}-r{replicate}"
    workspace.parent.mkdir(parents=True, exist_ok=True)
    materialize(fixture, workspace)
    workspace = workspace.resolve()

    if adapter is None:
        adapter = resolve_arm_adapter(key, dry_run=(key in SYNTHETIC_ARMS))

    run_id = new_id("run")
    agent_input = _build_agent_input(
        fixture=fixture,
        workspace=workspace,
        arm=key,
        run_id=run_id,
        replicate=replicate,
        model=model,
        capture_id=capture_id,
    )
    before = observe.snapshot(workspace)

    started_at = utc_now_iso()
    started = time.monotonic()
    try:
        runner = invoke or (lambda a, i: invoke_arm(a, i, adapter=adapter, fixture=fixture))
        result = runner(key, agent_input)
    except Exception as exc:  # noqa: BLE001 -- an arm crash is a data point, not a stop
        result = AgentResult(
            status=ResultStatus.ERROR,
            usage=AgentUsage(wall_ms=int((time.monotonic() - started) * 1000)),
            error=f"{type(exc).__name__}: {exc}",
        )
    wall_ms = int((time.monotonic() - started) * 1000)
    finished_at = utc_now_iso()

    # Snapshot BEFORE the acceptance overlay: everything here is the arm's doing.
    after = observe.snapshot(workspace)
    diff = observe.diff_snapshots(before, after)
    undeclared = observe.undeclared_changes(diff, fixture.declared_files)

    transcript_path = observe.find_transcript(session_id=result.session_ref, cwd=workspace)
    transcript_text = _read_transcript_text(transcript_path)
    if transcript_path is not None:
        access = observe.access_report(
            observe.parse_tool_calls(transcript_path),
            workspace=workspace,
            declared=fixture.declared_files,
            canary_paths=[c.path for c in fixture.canaries],
            transcript_path=transcript_path,
        )
    else:
        access = observe.AccessReport(observable=False)

    canaries = observe.check_canaries(
        fixture.canaries,
        workspace=workspace,
        diff=diff,
        output_text=result.output_text,
        transcript_text=transcript_text,
    )

    acceptance = run_acceptance(fixture, workspace)

    manifest = build_manifest(
        run_id=run_id,
        task={"id": fixture.id, "discipline": fixture.discipline},
        # RunManifest.arm is `Arm | None`; the synthetic control arms carry no
        # contracts.Arm, and build_manifest passes the value straight through.
        arm=cast(Arm, arm_enum),
        harness_type=harness_type,
        adapter=adapter,
        result=result,
        started_at=started_at,
        finished_at=finished_at,
        dry_run=(key in SYNTHETIC_ARMS),
        replicate=replicate,
    )
    harness = manifest.harness.model_dump(mode="json")
    harness["params"] = {
        **harness.get("params", {}),
        "fixture_id": fixture.id,
        "fixture_digest": fixture.digest(),
        "capture_id": capture_id,
    }

    usage = result.usage.model_dump(mode="json")
    record: dict[str, Any] = {
        "run_id": run_id,
        "capture_id": capture_id,
        "fixture_id": fixture.id,
        "fixture_digest": fixture.digest(),
        "fixture_kind": fixture.kind,
        "fixture_tier": fixture.tier,
        "fixture_repo_rev": fixture.repo_rev,
        "replicate": replicate,
        "arm": key,
        "arm_enum": arm_enum.value if arm_enum is not None else None,
        "synthetic": key in SYNTHETIC_ARMS,
        "model": model or agent_input.model,
        "harness": harness,
        "workspace": str(workspace),
        "started_at": started_at,
        "finished_at": finished_at,
        # Task success is the frozen acceptance verdict and nothing else.
        "success": bool(acceptance["passed"]),
        "status": result.status.value,
        "error": result.error,
        "wall_ms": wall_ms,
        "agent_wall_ms": usage.get("wall_ms"),
        "total_ms": wall_ms + int(acceptance["duration_ms"]),
        "usage": usage,
        "acceptance": acceptance,
        "governance": {
            "declared_files": list(fixture.declared_files),
            "diff": diff.to_dict(),
            "files_changed": len(diff.touched),
            "undeclared_changes": undeclared,
            "canaries": [c.to_dict() for c in canaries],
            "canary_tripped": any(c.tripped for c in canaries),
            "access": access.to_dict(),
        },
        "output_digest": manifest.output_digest,
        "output_text": result.output_text[-_ACCEPTANCE_TAIL_CHARS:],
        "manifest": manifest.model_dump(mode="json"),
    }
    return record
