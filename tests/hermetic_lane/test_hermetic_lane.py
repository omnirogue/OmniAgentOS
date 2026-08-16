"""Durable proof of the A4 hermetic lane's isolation boundary and handshake.

The lane's guarantees previously lived only in narrative (commit message /
TESTING.md). These tests pin them down so a regression fails a suite instead of
an argument:

  * handshake — flag-unset fast-return, missing-plugin refusal, disabled-plugin
    refusal, allow-network double-acknowledgement, `-q`-proof stderr banner;
  * isolation boundary — `scripts/hermetic-venv-guard.sh` refuses `.venv`,
    symlinked `.venv-hermetic`, and non-directory shapes; the Makefile refuses
    `HERMETIC_VENV` overrides at parse time;
  * enforcement — with the guard active (hermetic lane only), outbound DNS and
    TCP raise ``NetworkBlockedError`` and the ``live`` frame disables blocking.

Every test here runs in BOTH lanes. The venv-dependent halves select
themselves: subprocess tests that need the testfarm plugin skip in the default
venv, and the end-to-end missing-plugin refusal runs ONLY in the default venv
(where testfarm is genuinely absent). Nothing below opens a real network
connection in either lane: probe targets are RFC 2606 ``.invalid`` names and
RFC 5737 TEST-NET-1 addresses, and are only contacted when the guard is active.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import types
from pathlib import Path

import pytest

from tests.conftest import _require_testfarm_guard

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_SCRIPT = REPO_ROOT / "scripts" / "hermetic-venv-guard.sh"
_TESTFARM_AVAILABLE = importlib.util.find_spec("testfarm") is not None
_THIS_FILE_REL = "tests/hermetic_lane/test_hermetic_lane.py"


# --- stubs ---------------------------------------------------------------------


class _StubPluginManager:
    def __init__(self, has_plugin: bool) -> None:
        self._has_plugin = has_plugin

    def hasplugin(self, name: str) -> bool:  # pragma: no cover - trivial
        return self._has_plugin


class _StubConfig:
    """Just enough pytest.Config surface for _require_testfarm_guard."""

    def __init__(self, *, has_plugin: bool = True, allow_network: bool = False) -> None:
        self.pluginmanager = _StubPluginManager(has_plugin)
        self._allow_network = allow_network

    def getoption(self, name: str, default: object = None) -> object:
        if name == "--testfarm-allow-network":
            return self._allow_network
        return default


def _inject_fake_testfarm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import testfarm.harness.plugin`` succeed regardless of venv."""
    testfarm = types.ModuleType("testfarm")
    harness = types.ModuleType("testfarm.harness")
    plugin = types.ModuleType("testfarm.harness.plugin")
    testfarm.harness = harness  # type: ignore[attr-defined]
    harness.plugin = plugin  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "testfarm", testfarm)
    monkeypatch.setitem(sys.modules, "testfarm.harness", harness)
    monkeypatch.setitem(sys.modules, "testfarm.harness.plugin", plugin)


# --- handshake unit coverage (both lanes) --------------------------------------


def test_flag_unset_returns_before_touching_config_or_testfarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No flag -> immediate return. config=None proves nothing else is touched."""
    monkeypatch.delenv("TESTFARM_HERMETIC", raising=False)
    assert _require_testfarm_guard(None) is None  # type: ignore[arg-type]


def test_missing_plugin_refuses_with_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TESTFARM_HERMETIC", "1")
    # None sentinel forces ImportError even where testfarm IS installed, so this
    # exercises the same refusal in the hermetic venv as in the default one.
    monkeypatch.setitem(sys.modules, "testfarm.harness.plugin", None)
    with pytest.raises(pytest.UsageError, match="not importable"):
        _require_testfarm_guard(_StubConfig())  # type: ignore[arg-type]


def test_disabled_plugin_refuses_with_usage_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TESTFARM_HERMETIC", "1")
    _inject_fake_testfarm(monkeypatch)
    with pytest.raises(pytest.UsageError, match="refuses to run unguarded"):
        _require_testfarm_guard(_StubConfig(has_plugin=False))  # type: ignore[arg-type]


def test_allow_network_without_ack_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TESTFARM_HERMETIC", "1")
    monkeypatch.delenv("TESTFARM_HERMETIC_ALLOW_NETWORK_ACK", raising=False)
    _inject_fake_testfarm(monkeypatch)
    with pytest.raises(pytest.UsageError, match="TESTFARM_HERMETIC_ALLOW_NETWORK_ACK"):
        _require_testfarm_guard(_StubConfig(allow_network=True))  # type: ignore[arg-type]


def test_allow_network_with_ack_banners_loudly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TESTFARM_HERMETIC", "1")
    monkeypatch.setenv("TESTFARM_HERMETIC_ALLOW_NETWORK_ACK", "1")
    _inject_fake_testfarm(monkeypatch)
    _require_testfarm_guard(_StubConfig(allow_network=True))  # type: ignore[arg-type]
    assert "ALLOW-NETWORK" in capsys.readouterr().err


def test_guarded_mode_banners_blocked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TESTFARM_HERMETIC", "1")
    _inject_fake_testfarm(monkeypatch)
    _require_testfarm_guard(_StubConfig(allow_network=False))  # type: ignore[arg-type]
    assert "network blocked" in capsys.readouterr().err


# --- isolation boundary: venv guard script + Makefile pin (both lanes) ---------


def _run_guard(arg: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(GUARD_SCRIPT), arg],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def test_guard_script_refuses_dot_venv(tmp_path: Path) -> None:
    proc = _run_guard(".venv", tmp_path)
    assert proc.returncode == 2
    assert "refuses venv path" in proc.stderr


def test_guard_script_refuses_symlinked_hermetic_venv(tmp_path: Path) -> None:
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv-hermetic").symlink_to(tmp_path / ".venv")
    proc = _run_guard(".venv-hermetic", tmp_path)
    assert proc.returncode == 2
    assert "symlink" in proc.stderr


def test_guard_script_refuses_non_directory(tmp_path: Path) -> None:
    (tmp_path / ".venv-hermetic").write_text("not a venv\n", encoding="utf-8")
    proc = _run_guard(".venv-hermetic", tmp_path)
    assert proc.returncode == 2
    assert "not a directory" in proc.stderr


def test_guard_script_accepts_absent_and_real_directory(tmp_path: Path) -> None:
    assert _run_guard(".venv-hermetic", tmp_path).returncode == 0
    (tmp_path / ".venv-hermetic").mkdir()
    assert _run_guard(".venv-hermetic", tmp_path).returncode == 0


def test_makefile_refuses_hermetic_venv_override() -> None:
    """`make test-hermetic HERMETIC_VENV=.venv` must die at parse time."""
    proc = subprocess.run(
        ["make", "-n", "test-hermetic", "HERMETIC_VENV=.venv"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "not overridable" in proc.stdout + proc.stderr


def test_makefile_dry_run_still_parses_without_override() -> None:
    proc = subprocess.run(
        ["make", "-n", "test-hermetic"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "uv sync --locked" in proc.stdout


# --- end-to-end handshake through the real conftest (venv-dependent) -----------


def _run_pytest_subprocess(
    *extra_args: str, env_overrides: dict[str, str | None]
) -> subprocess.CompletedProcess[str]:
    """Collect-only pytest run against THIS file through the real root conftest."""
    env = os.environ.copy()
    for noise in ("PYTEST_ADDOPTS", "PYTEST_DISABLE_PLUGIN_AUTOLOAD"):
        env.pop(noise, None)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--collect-only",
            "-p",
            "no:cacheprovider",
            _THIS_FILE_REL,
            *extra_args,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


@pytest.mark.skipif(
    _TESTFARM_AVAILABLE, reason="default-venv-only: needs testfarm genuinely absent"
)
def test_end_to_end_missing_plugin_refusal_in_default_venv() -> None:
    """TESTFARM_HERMETIC=1 in the default venv refuses instead of running unguarded."""
    proc = _run_pytest_subprocess(
        env_overrides={
            "TESTFARM_HERMETIC": "1",
            "TESTFARM_HERMETIC_ALLOW_NETWORK_ACK": None,
        }
    )
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "not importable" in proc.stdout + proc.stderr


@pytest.mark.skipif(
    not _TESTFARM_AVAILABLE, reason="hermetic-venv-only: needs the testfarm plugin"
)
def test_end_to_end_guarded_banner_survives_quiet_mode() -> None:
    proc = _run_pytest_subprocess(
        env_overrides={
            "TESTFARM_HERMETIC": "1",
            "TESTFARM_HERMETIC_ALLOW_NETWORK_ACK": None,
        }
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "testfarm hermetic lane: network blocked" in proc.stderr


@pytest.mark.skipif(
    not _TESTFARM_AVAILABLE, reason="hermetic-venv-only: needs the testfarm plugin"
)
def test_end_to_end_allow_network_without_ack_refuses() -> None:
    proc = _run_pytest_subprocess(
        "--testfarm-allow-network",
        env_overrides={
            "TESTFARM_HERMETIC": "1",
            "TESTFARM_HERMETIC_ALLOW_NETWORK_ACK": None,
        },
    )
    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "TESTFARM_HERMETIC_ALLOW_NETWORK_ACK" in proc.stdout + proc.stderr


@pytest.mark.skipif(
    not _TESTFARM_AVAILABLE, reason="hermetic-venv-only: needs the testfarm plugin"
)
def test_end_to_end_allow_network_with_ack_is_visibly_unguarded() -> None:
    proc = _run_pytest_subprocess(
        "--testfarm-allow-network",
        env_overrides={
            "TESTFARM_HERMETIC": "1",
            "TESTFARM_HERMETIC_ALLOW_NETWORK_ACK": "1",
        },
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALLOW-NETWORK" in proc.stderr


# --- enforcement probes (only meaningful with the guard active) ----------------

_GUARD_ACTIVE = os.environ.get("TESTFARM_HERMETIC") == "1"


@pytest.mark.skipif(not _GUARD_ACTIVE, reason="hermetic lane only: guard not active")
def test_guard_blocks_dns_and_tcp_connect() -> None:
    from testfarm.harness import network
    from testfarm.harness.network import NetworkBlockedError

    assert network.is_blocking_active()
    with pytest.raises(NetworkBlockedError):
        socket.getaddrinfo("testfarm-hermetic-probe.invalid", 80)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkBlockedError):
            sock.connect(("192.0.2.1", 9))  # RFC 5737 TEST-NET-1: never routable
    finally:
        sock.close()


@pytest.mark.skipif(not _GUARD_ACTIVE, reason="hermetic lane only: guard not active")
def test_live_frame_disables_blocking_and_restores() -> None:
    from testfarm.harness import network

    assert network.is_blocking_active()
    network.begin_test("hermetic-lane-probe::live", live=True)
    try:
        assert not network.is_blocking_active()
    finally:
        network.end_test()
    assert network.is_blocking_active()
