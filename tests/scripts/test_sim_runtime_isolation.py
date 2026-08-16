"""Tests for simulation runtime isolation (S1) features."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def get_clean_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Delete every key starting with OMNIAGENTOS_
    for k in list(env.keys()):
        if k.startswith("OMNIAGENTOS_"):
            del env[k]
    # Set OMNIAGENTOS_PYTHON
    env["OMNIAGENTOS_PYTHON"] = sys.executable
    # Create fake HOME
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(fake_home)
    # Sibling product ~/OmniAgentOS
    sibling = fake_home / "OmniAgentOS"
    sibling.mkdir(parents=True, exist_ok=True)
    return env


def test_bash_syntax_check() -> None:
    """bash -n both scripts in one test."""
    for script in ["scripts/launch-env.sh", "scripts/launch-supervised.sh"]:
        path = ROOT / script
        res = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert res.returncode == 0, f"Syntax check failed for {script}: {res.stderr}"


def test_launcher_containment_is_shared_not_embedded() -> None:
    """Migration tripwire: the launcher consults the one Python primitive."""
    source = (ROOT / "scripts/launch-supervised.sh").read_text(encoding="utf-8")
    function = source.split("_path_inside() {", 1)[1].split("\n}", 1)[0]
    assert "-m omniagentos.path_containment" in function
    assert "samestat" not in function
    assert "startswith" not in function


def test_sim_mode_with_no_campaign(tmp_path: Path) -> None:
    """1. sim mode with no campaign -> returncode != 0, stderr mentions campaign."""
    env = get_clean_env(tmp_path)
    env["OMNIAGENTOS_SIM_ROOT"] = str(tmp_path / "simulations")
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "campaign" in res.stderr.lower()


def test_campaign_with_missing_value(tmp_path: Path) -> None:
    """2. --campaign with a missing value -> non-zero."""
    env = get_clean_env(tmp_path)
    env["OMNIAGENTOS_SIM_ROOT"] = str(tmp_path / "simulations")
    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "--campaign"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 2


@pytest.mark.parametrize(
    "inherited_var",
    [
        "OMNIAGENTOS_DB",
        "OMNIAGENTOS_VAR",
        "OMNIAGENTOS_VAR_DIR",
        "OMNIAGENTOS_LEDGER_DIR",
        "OMNIAGENTOS_VAULT_DIR",
        "OMNIAGENTOS_TRANSCRIPTS_ROOT",
    ],
)
def test_refuse_inherited_variables(tmp_path: Path, inherited_var: str) -> None:
    """3. sim mode + campaign + that var preset -> non-zero AND stderr contains variable, 'inherited', campaign root."""
    env = get_clean_env(tmp_path)
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_SIM_CAMPAIGN"] = "campaign123"

    sim_root = tmp_path / "simulations"
    env["OMNIAGENTOS_SIM_ROOT"] = str(sim_root)
    expected_root = sim_root / "campaign123"

    # Preset the inherited variable
    env[inherited_var] = "/some/inherited/path"

    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert inherited_var in res.stderr
    assert "inherited" in res.stderr.lower()
    assert str(expected_root) in res.stderr


def test_positive_path_preflight(tmp_path: Path) -> None:
    """4. positive path: preflight with OMNIAGENTOS_SIM_ROOT=<tmp> + campaign -> rc 0, and all resolve under campaign."""
    env = get_clean_env(tmp_path)
    sim_root = tmp_path / "simulations"
    env["OMNIAGENTOS_SIM_ROOT"] = str(sim_root)
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_SIM_CAMPAIGN"] = "my-campaign-12"

    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0

    # Parse stdout and assert all resolve under campaign_root
    lines = res.stdout.strip().split("\n")
    data = {}
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v

    campaign_root = sim_root / "my-campaign-12"
    assert data["OMNIAGENTOS_SIM_MODE"] == "1"
    assert data["OMNIAGENTOS_SIM_CAMPAIGN"] == "my-campaign-12"

    for k in [
        "OMNIAGENTOS_VAR_DIR",
        "OMNIAGENTOS_VAR",
        "OMNIAGENTOS_DB",
        "OMNIAGENTOS_LEDGER_DIR",
        "OMNIAGENTOS_VAULT_DIR",
    ]:
        val_path = Path(data[k])
        assert campaign_root in val_path.parents or val_path == campaign_root


def test_campaign_root_inside_source_tree(tmp_path: Path) -> None:
    """5. campaign root inside the repo source tree -> non-zero, stderr mentions source-controlled."""
    env = get_clean_env(tmp_path)
    env["OMNIAGENTOS_SIM_ROOT"] = str(ROOT / "omniagentos")
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_SIM_CAMPAIGN"] = "campaign123"

    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "source-controlled" in res.stderr


def test_campaign_root_under_sibling_product(tmp_path: Path) -> None:
    """6. campaign root under the fake $HOME/OmniAgentOS -> non-zero, stderr mentions sibling product."""
    env = get_clean_env(tmp_path)
    fake_sibling = Path(env["HOME"]) / "OmniAgentOS"
    env["OMNIAGENTOS_SIM_ROOT"] = str(fake_sibling)
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_SIM_CAMPAIGN"] = "campaign123"

    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "sibling product" in res.stderr


@pytest.mark.parametrize(
    "unsafe_id",
    [
        "../escape",
        "a/b",
        "..",
        ".",
        "-start",
    ],
)
def test_unsafe_campaign_ids(tmp_path: Path, unsafe_id: str) -> None:
    """7. traversal / unsafe campaign ids -> non-zero."""
    env = get_clean_env(tmp_path)
    env["OMNIAGENTOS_SIM_ROOT"] = str(tmp_path / "simulations")
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_SIM_CAMPAIGN"] = unsafe_id

    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "invalid campaign id" in res.stderr.lower()


def test_inherited_marker_cannot_bypass_refusal(tmp_path: Path) -> None:
    """8. inherited OMNIAGENTOS_LAUNCH_ENV_LOADED=1 must NOT bypass the inherited-DB refusal -> still non-zero."""
    env = get_clean_env(tmp_path)
    env["OMNIAGENTOS_SIM_ROOT"] = str(tmp_path / "simulations")
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_SIM_CAMPAIGN"] = "camp1"
    env["OMNIAGENTOS_LAUNCH_ENV_LOADED"] = "1"
    env["OMNIAGENTOS_SIM_ENV_LOADED"] = "1"
    env["OMNIAGENTOS_DB"] = "/some/inherited/db.db"

    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "OMNIAGENTOS_DB" in res.stderr
    assert "inherited" in res.stderr


def test_refuse_inherited_pid_file(tmp_path: Path) -> None:
    """Proves an inherited OMNIAGENTOS_SUPERVISOR_PID_FILE is refused in sim mode."""
    env = get_clean_env(tmp_path)
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_SIM_CAMPAIGN"] = "campaign123"

    sim_root = tmp_path / "simulations"
    env["OMNIAGENTOS_SIM_ROOT"] = str(sim_root)
    expected_root = sim_root / "campaign123"

    # Preset the inherited variable
    env["OMNIAGENTOS_SUPERVISOR_PID_FILE"] = "/some/inherited/path/supervisor.json"

    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert "OMNIAGENTOS_SUPERVISOR_PID_FILE" in res.stderr
    assert "inherited" in res.stderr
    assert str(expected_root) in res.stderr


def test_production_mode_regression(tmp_path: Path) -> None:
    """9. production mode preflight -> rc 0 and a preset OMNIAGENTOS_DB is still honoured."""
    env = get_clean_env(tmp_path)
    preset_db = "/tmp/my_preset_db.sqlite3"
    env["OMNIAGENTOS_DB"] = preset_db

    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0

    lines = res.stdout.strip().split("\n")
    data = {}
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v

    assert Path(data["OMNIAGENTOS_DB"]).resolve() == Path(preset_db).resolve()
    assert data["OMNIAGENTOS_SIM_MODE"] == ""
    assert data["OMNIAGENTOS_SIM_CAMPAIGN"] == ""


def test_campaign_flag_equivalence(tmp_path: Path) -> None:
    """10. --campaign <id> flag is equivalent to OMNIAGENTOS_SIM_MODE=1 OMNIAGENTOS_SIM_CAMPAIGN=<id>."""
    env = get_clean_env(tmp_path)
    sim_root = tmp_path / "simulations"
    env["OMNIAGENTOS_SIM_ROOT"] = str(sim_root)

    res = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/launch-supervised.sh"),
            "--campaign",
            "mycam123",
            "preflight",
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    lines = res.stdout.strip().split("\n")
    data = {}
    for line in lines:
        if "=" in line:
            k, v = line.split("=", 1)
            data[k] = v

    assert data["OMNIAGENTOS_SIM_MODE"] == "1"
    assert data["OMNIAGENTOS_SIM_CAMPAIGN"] == "mycam123"


def test_hermetic_proof(tmp_path: Path) -> None:
    """11. hermetic proof: after a refused sim run, the campaign root directory was NOT created."""
    env = get_clean_env(tmp_path)
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_SIM_CAMPAIGN"] = "never_created"
    env["OMNIAGENTOS_DB"] = "/some/inherited/db.sqlite3"

    sim_root = tmp_path / "simulations"
    env["OMNIAGENTOS_SIM_ROOT"] = str(sim_root)

    assert not sim_root.exists()

    # This test's campaign path in the REAL repo tree (not sim_root, which is
    # tmp_path-based): the launcher must never create a campaign directory
    # under ROOT/var/simulations for this refused run.
    real_campaign_dir = ROOT / "var" / "simulations" / "never_created"
    assert not real_campaign_dir.exists()

    # Snapshot real repo DB file status
    db_path = ROOT / "var/runtime/state.sqlite3"
    db_exists_before = db_path.exists()
    db_mtime_before = db_path.stat().st_mtime if db_exists_before else None

    res = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode != 0
    assert not sim_root.exists()

    # NOTE: this used to snapshot the whole of ROOT/var (os.listdir before vs.
    # after) and assert byte-for-byte equality. ROOT/var is a shared mutable
    # directory: under pytest-xdist (and on the merge gate, alongside other
    # concurrently-running processes) other workers create/remove entries
    # under it independently of this test, so that snapshot asserted global
    # process quiescence rather than anything this SUT actually promises, and
    # flaked on unrelated concurrent activity (e.g. a sibling worker creating
    # var/simulations mid-test). What this SUT actually promises is that a
    # refused sim run does not create ITS OWN campaign directory, which is
    # what real_campaign_dir asserts above using the campaign id
    # ("never_created") unique to this test.
    assert not real_campaign_dir.exists()

    # Assert that real repo DB was not created/modified
    db_exists_after = db_path.exists()
    assert db_exists_before == db_exists_after
    if db_exists_before:
        assert db_path.stat().st_mtime == db_mtime_before
