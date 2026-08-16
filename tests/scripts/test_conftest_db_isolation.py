"""R5: every real pytest entry point rejects inherited runtime DB paths."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omniagentos.contracts import default_db_path

ROOT = Path(__file__).resolve().parents[2]

_PROBE_FLAG = "_OMNI_DB_ISOLATION_PROBE"
_PROBE_FORBIDDEN = "_PROBE_FORBIDDEN_DB"
_PROBE_MARKER = "_PROBE_PYTEST_MARKER"


def _pytest_python() -> tuple[str, dict[str, str]]:
    """Return (python, env) that can import pytest + this package."""
    env = os.environ.copy()
    # The A4 hermetic-lane handshake flag asserts "THIS pytest run is
    # socket-guarded" and is deliberately fail-loud (tests/conftest.py refuses
    # to start when it is set but the testfarm plugin is absent). The child
    # entry points probed here run in the DEFAULT venv (.venv / `uv run`),
    # where testfarm is never installed, so an inherited flag would turn every
    # probe into a handshake refusal when this test itself runs inside
    # `make test-hermetic`. The flag is per-run, not inheritable: strip it.
    env.pop("TESTFARM_HERMETIC", None)
    env.pop("TESTFARM_HERMETIC_ALLOW_NETWORK_ACK", None)
    candidates = [
        ROOT / ".venv" / "bin" / "python",
        Path("/Users/youruser/OmniAgentOS/.venv/bin/python"),
        Path(sys.executable),
    ]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import pytest"],
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:
            env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            return str(candidate), env
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return sys.executable, env


def test_all_pytest_entry_points_isolate_inherited_db(tmp_path: Path) -> None:
    """Run bare pytest, every Make pytest target, and comprehensive for real."""
    if os.environ.get(_PROBE_FLAG) == "1":
        forbidden = Path(os.environ[_PROBE_FORBIDDEN]).resolve()
        current = Path(default_db_path()).resolve()
        env_path = Path(os.environ["OMNIAGENTOS_DB"]).resolve()
        assert current != forbidden, f"default_db_path resolved to forbidden DB: {current}"
        assert env_path != forbidden, f"OMNIAGENTOS_DB still points at forbidden DB: {env_path}"
        return

    live = tmp_path / "live-operator-state.sqlite3"
    live.write_bytes(b"")
    python, env = _pytest_python()
    env["OMNIAGENTOS_DB"] = str(live)
    env[_PROBE_FLAG] = "1"
    target = (
        "tests/scripts/test_conftest_db_isolation.py"
        "::test_all_pytest_entry_points_isolate_inherited_db"
    )
    select = f"{target} -k test_all_pytest_entry_points_isolate_inherited_db -p no:cacheprovider"
    invocations: list[tuple[str, list[str], Path, str | None]] = [
        (
            "bare pytest",
            [
                python,
                "-m",
                "pytest",
                "-q",
                target,
                "-p",
                "no:cacheprovider",
            ],
            live,
            None,
        ),
    ]
    for make_target in (
        "test",
        "smoke",
        "test-perf",
        "test-live",
        "api-contracts",
        "test-coverage-scale",
        "scale-gate",
    ):
        marker = {
            "smoke": "smoke",
            "test-perf": "perf",
            "test-live": "live",
        }.get(make_target)
        invocations.append((f"make {make_target}", ["make", make_target], live, marker))

    comprehensive_db = ROOT / "var" / "comprehensive-test.db"
    invocations.append(
        (
            "scripts/test-comprehensive.sh",
            [
                "bash",
                "scripts/test-comprehensive.sh",
                target,
                "-k",
                "test_all_pytest_entry_points_isolate_inherited_db",
                "-p",
                "no:cacheprovider",
            ],
            comprehensive_db,
            None,
        )
    )

    for name, command, forbidden, marker in invocations:
        child_env = dict(env)
        child_env[_PROBE_FORBIDDEN] = str(forbidden)
        if marker is not None:
            child_env[_PROBE_MARKER] = marker
        if name.startswith("make "):
            child_env["PYTEST_ADDOPTS"] = select
        # This test probes DB ISOLATION, not dependency syncing — but the make
        # targets it spawns run `uv run`, whose implicit project sync REINSTALLS
        # the editable package pointing at THIS process's cwd. Inside a merge
        # gate that cwd is the run's ephemeral scratch; the sync rewrote the
        # SHARED gate venv's _editable_impl_omniagentos.pth to the scratch
        # path, and scratch deletion then broke `import omniagentos` for every
        # later gate (the 2026-08-09 exit-127 storm, four poisonings in one
        # day). The probe must observe the environment, not mutate it.
        child_env["UV_NO_SYNC"] = "1"
        proc = subprocess.run(
            command,
            cwd=str(ROOT),
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, (
            f"{name} isolation probe failed\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )


# Make's marker-filtered targets must collect this same probe. Apply the marker
# only inside that child process; the normal suite sees an ordinary unmarked test.
_dynamic_marker = os.environ.get(_PROBE_MARKER)
if _dynamic_marker:
    test_all_pytest_entry_points_isolate_inherited_db = getattr(pytest.mark, _dynamic_marker)(
        test_all_pytest_entry_points_isolate_inherited_db
    )
