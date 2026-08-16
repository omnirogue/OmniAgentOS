"""Guard tests for scripts/sim-pump.sh's API target resolution.

FIX 2 (sim-isolation follow-up): ``SIM_API`` defaulted to
``http://127.0.0.1:8485`` -- the PRODUCTION control-plane port -- even under
``OMNIAGENTOS_SIM_MODE=1``. That silently pumped simulated swarm dispatches
into the real production API instead of the per-campaign one
``scripts/launch-env.sh`` derives and exports as ``OMNIAGENTOS_API_PORT``.

These tests extract the script's setup preamble (everything up to, but not
including, the ``LOG=`` line -- i.e. before the infinite dispatch loop) and
run it in isolation via ``bash -c``, asserting on the resolved ``$API`` and
``$STATE_ROOT``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIM_PUMP = ROOT / "scripts" / "sim-pump.sh"


def _preamble() -> str:
    lines = SIM_PUMP.read_text(encoding="utf-8").splitlines()
    preamble: list[str] = []
    for line in lines:
        if line.startswith("LOG="):
            break
        preamble.append(line)
    assert preamble, "sim-pump.sh preamble extraction found no LOG= boundary"
    return "\n".join(preamble)


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    script = _preamble() + '\nprintf "API=%s\\nSTATE_ROOT=%s\\n" "$API" "$STATE_ROOT"\n'
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
    )


def _clean_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("OMNIAGENTOS_") or key in ("SIM_API", "OMNIAGENTOS_API_PORT", "REPO"):
            del env[key]
    repo = tmp_path / "repo"
    repo.mkdir()
    env["REPO"] = str(repo)
    return env


def _parsed(stdout: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            data[key] = value
    return data


def test_bash_syntax_check() -> None:
    result = subprocess.run(["bash", "-n", str(SIM_PUMP)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_production_default_is_byte_identical(tmp_path: Path) -> None:
    """Outside sim mode, the historical production default (8485) is unchanged."""
    env = _clean_env(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    data = _parsed(result.stdout)
    assert data["API"] == "http://127.0.0.1:8485"
    assert data["STATE_ROOT"] == str(Path(env["REPO"]) / "var")


def test_production_honours_explicit_sim_api_override(tmp_path: Path) -> None:
    env = _clean_env(tmp_path)
    env["SIM_API"] = "http://127.0.0.1:9999"
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert _parsed(result.stdout)["API"] == "http://127.0.0.1:9999"


def test_sim_mode_never_defaults_to_production_port(tmp_path: Path) -> None:
    """Decisive: sim mode + a per-campaign OMNIAGENTOS_API_PORT must never resolve to 8485."""
    env = _clean_env(tmp_path)
    campaign_var = tmp_path / "campaign-var"
    campaign_var.mkdir()
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_VAR_DIR"] = str(campaign_var)
    env["OMNIAGENTOS_API_PORT"] = "18123"

    result = _run(env)

    assert result.returncode == 0, result.stderr
    data = _parsed(result.stdout)
    assert data["API"] == "http://127.0.0.1:18123"
    assert data["API"] != "http://127.0.0.1:8485"
    assert data["STATE_ROOT"] == str(campaign_var)


def test_sim_mode_honours_explicit_sim_api_over_grok_api_port(tmp_path: Path) -> None:
    env = _clean_env(tmp_path)
    campaign_var = tmp_path / "campaign-var"
    campaign_var.mkdir()
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_VAR_DIR"] = str(campaign_var)
    env["OMNIAGENTOS_API_PORT"] = "18123"
    env["SIM_API"] = "http://127.0.0.1:19999"

    result = _run(env)

    assert result.returncode == 0, result.stderr
    assert _parsed(result.stdout)["API"] == "http://127.0.0.1:19999"


def test_sim_mode_with_neither_sim_api_nor_port_refuses(tmp_path: Path) -> None:
    """Fail closed: sim mode with no way to name the campaign API must refuse, not default to 8485."""
    env = _clean_env(tmp_path)
    campaign_var = tmp_path / "campaign-var"
    campaign_var.mkdir()
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_VAR_DIR"] = str(campaign_var)

    result = _run(env)

    assert result.returncode != 0
    assert "FATAL(sim-isolation)" in result.stderr
    assert "SIM_API" in result.stderr
    assert "OMNIAGENTOS_API_PORT" in result.stderr
    assert "8485" not in _parsed(result.stdout).get("API", "")
