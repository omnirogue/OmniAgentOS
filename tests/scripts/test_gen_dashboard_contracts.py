"""P0-CONTRACT.v1a — gen-dashboard-contracts.py determinism and temp-path safety."""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "gen-dashboard-contracts.py"
DASHBOARD_GENERATED = ROOT / "dashboard" / "src" / "lib" / "generated"
DASHBOARD_OTHER = ROOT / "dashboard" / "src" / "tmp-contract-out"


def _load_module():
    spec = importlib.util.spec_from_file_location("gen_dashboard_contracts", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _snapshot_tree(path: Path) -> dict[str, tuple[int, bytes]] | None:
    """Return relative-path → (mtime_ns, content) for files under path, or None."""
    if not path.exists():
        return None
    out: dict[str, tuple[int, bytes]] = {}
    for file_path in sorted(path.rglob("*")):
        if file_path.is_file():
            rel = file_path.relative_to(path).as_posix()
            out[rel] = (file_path.stat().st_mtime_ns, file_path.read_bytes())
    return out


@pytest.fixture(scope="module")
def gen():
    return _load_module()


def test_script_exists() -> None:
    assert SCRIPT.is_file()


def test_build_outputs_deterministic(gen) -> None:
    a = gen.build_outputs()
    b = gen.build_outputs()
    assert a == b
    assert set(a) == {"events.ts", "models.ts", "index.ts"}
    for body in a.values():
        assert body.endswith("\n")
        assert "GENERATED FILE" in body


def test_generated_content_mirrors_python_authorities(gen) -> None:
    from omniagentos.contracts import (
        CostQuality,
        Events,
        MissionEvents,
        ProviderCallStage,
        ReasoningEffort,
    )
    from omniagentos.swarm.contracts import (
        PLAN_DISPOSITIONS,
        SWARM_EVENT_ACTIONS,
        SWARM_GROUP_EVENT_ACTIONS,
    )

    events_ts = gen.build_outputs()["events.ts"]
    for kind in Events.ALL:
        assert f'"{kind}"' in events_ts
    for kind in MissionEvents.ALL:
        assert f'"{kind}"' in events_ts
    for action in SWARM_GROUP_EVENT_ACTIONS:
        assert f'"{action}"' in events_ts
    for disp in PLAN_DISPOSITIONS:
        assert f'"{disp}"' in events_ts
    assert "MISSION_EVENT_TYPES" in events_ts
    assert "FROZEN_EVENT_TYPES" in events_ts
    assert "chat.project_binding_changed" in events_ts
    assert "group_created" in events_ts
    assert "plan_created" in events_ts
    assert len(SWARM_EVENT_ACTIONS) >= len(SWARM_GROUP_EVENT_ACTIONS)

    models_ts = gen.build_outputs()["models.ts"]
    for name in (
        "ExecutionRef",
        "EffectiveRoute",
        "CostObservation",
        "SwarmPlanDecision",
        "SwarmPlan",
        "PlanIssue",
        "SwarmTaskSpec",
        "FormationBinding",
    ):
        assert f"export interface {name}" in models_ts
    assert "execution_id" in models_ts
    assert "cost_usd_decimal" in models_ts
    assert "cost_usd_nanos" in models_ts
    assert "cost_quality" in models_ts
    assert "selection_reason" in models_ts
    assert "disposition" in models_ts
    assert "plans: SwarmPlan[];" in models_ts
    assert "issues: PlanIssue[];" in models_ts
    for q in CostQuality:
        assert f'"{q.value}"' in models_ts
    for stage in ProviderCallStage:
        assert f'"{stage.value}"' in models_ts
    for effort in ReasoningEffort:
        assert f'"{effort.value}"' in models_ts
    # effort is a closed ReasoningEffort union | null — never unrestricted string.
    effort_line = next(
        line for line in models_ts.splitlines() if line.strip().startswith("effort:")
    )
    assert "effort:" in effort_line
    assert "null" in effort_line
    assert not re.search(r"\bstring\b", effort_line), effort_line


def test_generated_interfaces_require_defaulted_serialized_keys(gen) -> None:
    """model_dump always includes defaults — TS keys must not be optional (?)."""
    models_ts = gen.build_outputs()["models.ts"]

    for required_line in (
        "  created_at: string;",
        "  plans: SwarmPlan[];",
        "  issues: PlanIssue[];",
        "  questions: string[];",
        "  reason: string;",
        "  assumptions: string[];",
        "  attempt_index: number;",
        "  cost_usd_decimal: string | null;",
        "  cost_usd_nanos: number | null;",
        "  cost_upper_bound_usd_nanos: number | null;",
        "  provider_request_id: string | null;",
        "  settled_at: string | null;",
        "  company_id: string | null;",
        "  project_id: string | null;",
    ):
        assert required_line in models_ts, required_line

    for key in (
        "created_at",
        "plans",
        "issues",
        "questions",
        "reason",
        "assumptions",
        "attempt_index",
        "cost_usd_decimal",
        "cost_usd_nanos",
        "settled_at",
        "company_id",
        "effort",
    ):
        assert f"  {key}?:" not in models_ts

    assert "cost_usd_decimal: string | null;" in models_ts
    assert "cost_usd_nanos: number | null;" in models_ts
    assert "settled_at: string | null;" in models_ts
    assert "company_id: string | null;" in models_ts
    assert "provider_request_id: string | null;" in models_ts


def test_temp_output_write_and_zero_drift(gen, tmp_path: Path) -> None:
    out = tmp_path / "generated"
    written = gen.write_outputs(out)
    assert len(written) == 3
    for path in written:
        assert path.is_file()
        assert path.parent == out

    problems = gen.check_outputs(out)
    assert problems == []

    bodies_first = {p.name: p.read_bytes() for p in written}
    gen.write_outputs(out)
    for name, first in bodies_first.items():
        assert (out / name).read_bytes() == first


def test_check_detects_content_drift(gen, tmp_path: Path) -> None:
    out = tmp_path / "generated"
    gen.write_outputs(out)
    target = out / "events.ts"
    target.write_text(target.read_text(encoding="utf-8") + "// drift\n", encoding="utf-8")
    problems = gen.check_outputs(out)
    assert any(p.startswith("drift:") for p in problems)


def test_check_detects_unexpected_typescript(gen, tmp_path: Path) -> None:
    out = tmp_path / "generated"
    gen.write_outputs(out)
    (out / "stale.ts").write_text("// leftover\n", encoding="utf-8")
    problems = gen.check_outputs(out)
    assert "unexpected: stale.ts" in problems


def test_cli_temp_output_and_check(tmp_path: Path) -> None:
    out = tmp_path / "cli-out"
    env = os.environ.copy()
    env.pop("OMNIAGENTOS_ALLOW_DASHBOARD_CONTRACT_WRITE", None)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(out)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert (out / "events.ts").is_file()
    assert (out / "models.ts").is_file()
    assert (out / "index.ts").is_file()

    check = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(out), "--check"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    assert "ok:" in check.stdout


def test_cli_refuses_dashboard_path_without_override_no_mutation() -> None:
    """Refusal must not create or mutate the documented dashboard target."""
    env = os.environ.copy()
    env.pop("OMNIAGENTOS_ALLOW_DASHBOARD_CONTRACT_WRITE", None)
    before = _snapshot_tree(DASHBOARD_GENERATED)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(DASHBOARD_GENERATED)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "refusing" in proc.stderr

    after = _snapshot_tree(DASHBOARD_GENERATED)
    assert after == before


def test_cli_refuses_any_path_under_dashboard(tmp_path: Path) -> None:
    """Every path under dashboard/** is protected, not only the generated target."""
    env = os.environ.copy()
    env.pop("OMNIAGENTOS_ALLOW_DASHBOARD_CONTRACT_WRITE", None)
    # Use a path under dashboard that is not the documented generated dir.
    target = ROOT / "dashboard" / "src" / "features" / "_contract_gen_probe"
    before_dash = _snapshot_tree(ROOT / "dashboard")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--output", str(target)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "refusing" in proc.stderr
    assert not target.exists()
    after_dash = _snapshot_tree(ROOT / "dashboard")
    assert after_dash == before_dash
