"""SHIP DARK — proof that this package changes nothing until someone wires it.

The claim being proved has three parts, and each needs a different kind of test:

1. **Structural inertness.** No module outside ``omniagentos/workmodes`` imports
   it, so there is no code path through any existing lane that can reach it.
   ``test_no_production_module_imports_workmodes`` is a source scan, which is the
   only way to prove a NEGATIVE about reachability; a runtime test can only show
   that one path did not reach it.

2. **Gate inertness.** With no environment variable and no config section, the
   enforcement gate is ``off`` and the one function that can refuse a write does
   not refuse it.

3. **Side-effect inertness.** Importing the package, and resolving/inferring/
   splitting with it, creates no directory, writes no file and opens no
   database. Only ``provision_roots``, ``write_manifest`` and ``promote`` touch
   the filesystem, and each has to be called by name.

Plus the behavioural identity that matters to the planner: ``split_units`` over a
code-only plan returns the same units, in the same order, with the same
dependency tuples — byte-for-byte unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omniagentos.contracts import TaskMode
from omniagentos.workmodes import modes
from omniagentos.workmodes.artificer import enforce_writes, plan_artificer
from omniagentos.workmodes.modes import (
    WorkUnit,
    infer_task_mode,
    resolve_roots,
    split_units,
    work_modes_enabled,
    work_modes_enforcing,
    work_modes_mode,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = REPO_ROOT / "omniagentos"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """No gate env var, and a config file that cannot accidentally enable it."""
    monkeypatch.delenv(modes.WORK_MODES_ENV, raising=False)
    monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(REPO_ROOT / "configs" / "nope.yaml"))


# --- 1. structural inertness ----------------------------------------------


# Grok deliberately wires workmodes at these call sites (toolplane artifact
# validation, post-attempt assess/grade, revise API). Anything NEW must be
# added here intentionally — this is the SHIP-DARK tripwire, not a ban.
_WORKMODES_ALLOWED_CALL_SITES = frozenset(
    {
        "omniagentos/toolplane/tools.py",
        "omniagentos/execution/assess.py",
        "omniagentos/api/routes/grok_ops.py",
        # Lane F (MCP/ops, merged 2026-07-27) deliberately wired work-mode
        # validators into the MCP reach and verify paths; lane C2's merge
        # hardening reads them from the scheduler. Extending the allowlist is
        # what this tripwire asks for — deleting the gate is not.
        "omniagentos/toolplane/workmodes_bridge.py",
        "omniagentos/toolplane/mcp_client.py",
        "omniagentos/swarm/scheduler.py",
    }
)


def test_no_production_module_imports_workmodes() -> None:
    """Only the allowlisted Grok call sites may reference workmodes."""
    offenders: list[str] = []
    for path in PACKAGE.rglob("*.py"):
        if "workmodes" in path.parts:
            continue
        if "workmodes" in path.read_text(encoding="utf-8", errors="replace"):
            rel = str(path.relative_to(REPO_ROOT))
            if rel not in _WORKMODES_ALLOWED_CALL_SITES:
                offenders.append(rel)
    assert offenders == [], (
        "workmodes is referenced by production code outside the allowlist: "
        f"{offenders}. This test is the SHIP-DARK tripwire — when a lane is "
        "deliberately wired to work modes, add the path to "
        "_WORKMODES_ALLOWED_CALL_SITES rather than deleting the gate."
    )


def test_workmodes_imports_no_lane_at_module_scope() -> None:
    """Layering: above scope/contracts, below every lane.

    ``intake.fastlane`` is the one lane dependency and it is imported INSIDE
    ``target_location_for``; at module scope it would drag the whole intake
    service (and its DB imports) into anything that touches a path helper.
    """
    lanes = ("runner", "swarm", "longhaul", "orchestrator", "dispatch", "db")
    for path in (PACKAGE / "workmodes").glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if line[:1] in (" ", "\t"):  # indented == lazy import inside a function
                continue
            for lane in lanes:
                assert f"omniagentos.{lane}" not in stripped, f"{path.name}: {stripped}"
            assert "omniagentos.intake" not in stripped, f"{path.name}: {stripped}"


# --- 2. gate inertness -----------------------------------------------------


def test_gate_defaults_to_off() -> None:
    assert work_modes_mode() == "off"
    assert work_modes_enabled() is False
    assert work_modes_enforcing() is False


def test_guard_does_not_refuse_when_the_gate_is_off(tmp_path: Path) -> None:
    """The one function that can refuse a write does not, by default."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    plan = plan_artificer(
        TaskMode.CONTENT, "tsk_1", base=str(tmp_path / "var"), repo_root=str(repo)
    )
    verdict = enforce_writes([str(repo / "src" / "app.py")], plan)  # would violate
    assert verdict.ok is False  # recorded...
    assert verdict.gate == "off"  # ...and not acted on


def test_a_typo_in_the_gate_never_turns_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(modes.WORK_MODES_ENV, "enfroce")
    assert work_modes_mode() == "off"


def test_the_gate_can_be_forced_off_from_a_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(modes.WORK_MODES_ENV, "off")
    assert work_modes_mode() == "off"
    monkeypatch.setenv(modes.WORK_MODES_ENV, "1")
    assert work_modes_mode() == "enforce"


def test_a_broken_config_file_fails_closed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    broken = tmp_path / "parallelism.yaml"
    broken.write_text("work_modes: [this is not a mapping\n", encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(broken))
    assert work_modes_mode() == "off"


def test_config_can_enable_it_and_env_still_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "parallelism.yaml"
    config.write_text("work_modes:\n  mode: enforce\n", encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_PARALLELISM_CONFIG", str(config))
    assert work_modes_mode() == "enforce"
    monkeypatch.setenv(modes.WORK_MODES_ENV, "off")
    assert work_modes_mode() == "off"


# --- 3. side-effect inertness ---------------------------------------------


def test_importing_the_package_touches_nothing(tmp_path: Path) -> None:
    """A fresh interpreter imports it and creates no var/ tree and no db file."""
    var = tmp_path / "var"
    env = {
        **os.environ,
        "OMNIAGENTOS_VAR_DIR": str(var),
        "OMNIAGENTOS_DB": str(tmp_path / "db.sqlite"),
        "PYTHONPATH": str(REPO_ROOT),
    }
    completed = subprocess.run(
        [sys.executable, "-c", "import omniagentos.workmodes as w; print(w.work_modes_mode())"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "off"
    assert not var.exists()
    assert not (tmp_path / "db.sqlite").exists()


def test_resolution_inference_and_splitting_are_pure(tmp_path: Path) -> None:
    var = tmp_path / "var"
    resolve_roots(TaskMode.INTAKE_PROCESSING, "tsk_1", base=str(var))
    infer_task_mode("write a report and a video script")
    split_units((WorkUnit(key="a", repo_paths=("src/a.py",)),))
    plan_artificer(TaskMode.REPORT, "tsk_1", base=str(var), repo_root=str(tmp_path / "repo"))
    assert not var.exists()


def test_split_units_is_identity_over_a_code_only_plan() -> None:
    """Byte-for-byte unchanged: the property the planner depends on."""
    units = tuple(
        WorkUnit(key=f"t{i}", repo_paths=(f"src/{i}.py",), depends_on=(f"t{i - 1}",) if i else ())
        for i in range(6)
    )
    result = split_units(units)
    assert result.units == units
    assert result.changed is False
    assert all(a is b for a, b in zip(result.units, units, strict=True))


def test_declaring_only_artifacts_or_only_repo_paths_is_never_split() -> None:
    for unit in (
        WorkUnit(key="a", mode=TaskMode.REPORT, artifact_outputs=("r.md",)),
        WorkUnit(key="b", repo_paths=("src/b.py",)),
    ):
        assert split_units((unit,)).changed is False
