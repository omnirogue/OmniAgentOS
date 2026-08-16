"""Guard tests for scripts/launch-env.sh."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scripts.process_supervisor import build_process_specs

ROOT = Path(__file__).resolve().parents[2]
LAUNCH_ENV = ROOT / "scripts" / "launch-env.sh"

# Primary product entry points that must source launch-env.sh.
REQUIRED_SOURCES = (
    ROOT / "scripts" / "launch-supervised.sh",
    ROOT / "scripts" / "certify-omniagentos.sh",
    ROOT / "scripts" / "test-comprehensive.sh",
    ROOT / "Makefile",
)


def test_launch_env_file_exists() -> None:
    assert LAUNCH_ENV.is_file()
    # bash -n syntax check
    subprocess.run(["bash", "-n", str(LAUNCH_ENV)], check=True)


def test_three_db_map_exists() -> None:
    path = ROOT / "docs" / "operations" / "three-db-map.md"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "OMNIAGENTOS_DB" in text
    assert "three" in text.lower() or "Three" in text


def test_sourcing_twice_is_noop() -> None:
    script = f"""
set -euo pipefail
. "{LAUNCH_ENV}"
first="$OMNIAGENTOS_DB"
. "{LAUNCH_ENV}"
second="$OMNIAGENTOS_DB"
test "$first" = "$second"
test "${{OMNIAGENTOS_LAUNCH_ENV_LOADED}}" = "1"
echo OK
"""
    out = subprocess.check_output(["bash", "-c", script], text=True)
    assert "OK" in out


def test_preset_db_survives_sourcing() -> None:
    preset = "/tmp/custom-omni-test.db"
    script = f"""
set -euo pipefail
export OMNIAGENTOS_DB="{preset}"
. "{LAUNCH_ENV}"
test "$OMNIAGENTOS_DB" = "{preset}"
echo OK
"""
    env = os.environ.copy()
    env["OMNIAGENTOS_DB"] = preset
    out = subprocess.check_output(["bash", "-c", script], env=env, text=True)
    assert "OK" in out


def test_entry_points_source_launch_env() -> None:
    missing: list[str] = []
    for path in REQUIRED_SOURCES:
        text = path.read_text(encoding="utf-8")
        if "launch-env.sh" not in text:
            missing.append(str(path.relative_to(ROOT)))
    assert missing == [], f"entry points missing launch-env source: {missing}"


def test_comprehensive_never_inherits_launch_env_db() -> None:
    """BLOCKER regression: comprehensive suite pins isolated test DB unconditionally."""
    script = ROOT / "scripts" / "test-comprehensive.sh"
    text = script.read_text(encoding="utf-8")
    assert 'export OMNIAGENTOS_DB="$ROOT/var/comprehensive-test.db"' in text
    assert "OMNIAGENTOS_DB:-" not in text  # no fallback to inherited/live DB

    # Runtime: even with a live launch-env path preset, comprehensive must override.
    live = "/tmp/launch-env-live-should-not-win.db"
    probe = f"""
set -euo pipefail
export OMNIAGENTOS_DB="{live}"
# Source the same preamble as test-comprehensive (launch-env then force pin).
ROOT="{ROOT}"
. "$ROOT/scripts/launch-env.sh"
export OMNIAGENTOS_DB="$ROOT/var/comprehensive-test.db"
test "$OMNIAGENTOS_DB" = "$ROOT/var/comprehensive-test.db"
test "$OMNIAGENTOS_DB" != "{live}"
echo OK
"""
    out = subprocess.check_output(["bash", "-c", probe], text=True)
    assert "OK" in out


def test_makefile_migrate_sources_launch_env() -> None:
    """MAJOR: migrate must source launch-env so it targets the same DB as api/runner."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    # The migrate recipe must source launch-env before invoking the migrator.
    assert "migrate:" in makefile
    migrate_block = makefile.split("migrate:", 1)[1].split("\n\n", 1)[0]
    assert "launch-env.sh" in migrate_block


# Pre-unset idiom (mirrors scripts/launch-supervised.sh): an inherited
# OMNIAGENTOS_SIM_ENV_LOADED=1 makes launch-env.sh skip its sim block entirely
# and fail open to production paths, so every harness entry point must unset it
# (SIM_MODE-guarded) before sourcing launch-env.sh.
SIM_PRE_UNSET = "unset OMNIAGENTOS_LAUNCH_ENV_LOADED OMNIAGENTOS_SIM_ENV_LOADED"


def _preamble_through_launch_env_source(script: Path) -> str:
    lines = script.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.lstrip().startswith(".") and "launch-env.sh" in line:
            return "\n".join(lines[: i + 1])
    raise AssertionError(f"{script} does not source launch-env.sh")


def test_harness_entry_points_pre_unset_inherited_sim_flag(tmp_path: Path) -> None:
    """Inherited OMNIAGENTOS_SIM_ENV_LOADED=1 must not fail-open to production paths."""
    # Makefile shape: each recipe line is its own shell, so the pre-unset must
    # share a logical line (via backslash continuation) with the source.
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    logical: list[str] = []
    for raw in makefile.splitlines():
        if logical and logical[-1].endswith("\\"):
            logical[-1] = logical[-1][:-1] + raw
        else:
            logical.append(raw)
    sourcing = [
        line
        for line in logical
        if "scripts/launch-env.sh" in line and not line.lstrip().startswith("#")
    ]
    assert sourcing, "Makefile no longer sources launch-env.sh"
    for line in sourcing:
        before_source = line.split("launch-env.sh", 1)[0]
        assert SIM_PRE_UNSET in before_source, (
            f"Makefile recipe sources launch-env.sh without the sim pre-unset "
            f"on the same shell line: {line!r}"
        )

    # Shell scripts: run each file's actual preamble (through the launch-env
    # source) with the fail-open env and assert campaign paths still win.
    env = os.environ.copy()
    for key in (
        "OMNIAGENTOS_DB",
        "OMNIAGENTOS_VAR",
        "OMNIAGENTOS_VAR_DIR",
        "OMNIAGENTOS_LEDGER_DIR",
        "OMNIAGENTOS_VAULT_DIR",
        "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
        "OMNIAGENTOS_SIM_ROOT",
        "OMNIAGENTOS_SWARM_DIR",
        "OMNIAGENTOS_MEMORIES_DIR",
        "OMNIAGENTOS_TRANSCRIPTS_ROOT",
    ):
        env.pop(key, None)
    env["OMNIAGENTOS_SIM_MODE"] = "1"
    env["OMNIAGENTOS_SIM_CAMPAIGN"] = "simguard"
    sim_root = Path(env["HOME"]) / "OmniAgentOS-sims"
    env["OMNIAGENTOS_LAUNCH_ENV_LOADED"] = "1"
    env["OMNIAGENTOS_SIM_ENV_LOADED"] = "1"  # the measured fail-open inheritance
    for name in ("certify-omniagentos.sh", "test-comprehensive.sh"):
        script = ROOT / "scripts" / name
        preamble = _preamble_through_launch_env_source(script)
        assert SIM_PRE_UNSET in preamble, f"{name} lacks the sim pre-unset ahead of the source"
        probe = (
            preamble
            + f"""
test "$OMNIAGENTOS_SIM_ENV_LOADED" = "1"
test "$OMNIAGENTOS_DB" = "{sim_root}/simguard/state.sqlite3"
echo OK
"""
        )
        out = subprocess.check_output(["bash", "-c", probe, str(script)], env=env, text=True)
        assert "OK" in out, f"{name} preamble failed to defeat inherited sim flag"


@pytest.mark.parametrize("bad_value", ["true", "yes", "0"])
def test_sim_mode_misspelling_is_fatal_and_quoted(bad_value: str) -> None:
    env = os.environ.copy()
    env["OMNIAGENTOS_SIM_MODE"] = bad_value
    result = subprocess.run(
        ["bash", "-c", f'. "{LAUNCH_ENV}"'],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert f"'{bad_value}'" in result.stderr
    assert "set it exactly to '1'" in result.stderr

    launcher_result = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert launcher_result.returncode != 0
    assert f"'{bad_value}'" in launcher_result.stderr


@pytest.mark.parametrize(
    "poisoned_var",
    [
        "OMNIAGENTOS_SIM_ROOT",
        "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
        "OMNIAGENTOS_SWARM_DIR",
        "OMNIAGENTOS_MEMORIES_DIR",
        "OMNIAGENTOS_TRANSCRIPTS_ROOT",
    ],
)
def test_sim_state_path_inheritance_is_fatal(poisoned_var: str, tmp_path: Path) -> None:
    env = os.environ.copy()
    for key in (
        "OMNIAGENTOS_DB",
        "OMNIAGENTOS_VAR",
        "OMNIAGENTOS_VAR_DIR",
        "OMNIAGENTOS_LEDGER_DIR",
        "OMNIAGENTOS_VAULT_DIR",
        "OMNIAGENTOS_SIM_ROOT",
        "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
        "OMNIAGENTOS_SWARM_DIR",
        "OMNIAGENTOS_MEMORIES_DIR",
        "OMNIAGENTOS_TRANSCRIPTS_ROOT",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "OMNIAGENTOS_SIM_MODE": "1",
            "OMNIAGENTOS_SIM_CAMPAIGN": "poisoned",
            poisoned_var: str(tmp_path / "outside"),
        }
    )
    Path(env["HOME"]).mkdir()
    result = subprocess.run(
        ["bash", "-c", f'. "{LAUNCH_ENV}"'],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert poisoned_var in result.stderr
    assert "unset" in result.stderr


def test_transcripts_root_inheritance_is_fatal_with_explicit_campaign_root(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    for key in (
        "OMNIAGENTOS_DB",
        "OMNIAGENTOS_VAR",
        "OMNIAGENTOS_VAR_DIR",
        "OMNIAGENTOS_LEDGER_DIR",
        "OMNIAGENTOS_VAULT_DIR",
        "OMNIAGENTOS_TRANSCRIPTS_ROOT",
        "OMNIAGENTOS_SIM_ROOT",
        "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
        "OMNIAGENTOS_SIM_ENV_LOADED",
        "OMNIAGENTOS_LAUNCH_ENV_LOADED",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "OMNIAGENTOS_SIM_MODE": "1",
            "OMNIAGENTOS_SIM_CAMPAIGN": "X",
            "OMNIAGENTOS_SIM_CAMPAIGN_ROOT": "/sim/X",
            "OMNIAGENTOS_TRANSCRIPTS_ROOT": "/production/var/transcripts",
        }
    )
    Path(env["HOME"]).mkdir()

    result = subprocess.run(
        ["bash", "-c", f'. "{LAUNCH_ENV}"'],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "FATAL(sim-isolation)" in result.stderr
    # The launcher refuses every inherited state-path override; the campaign
    # root appears first in its deterministic refusal loop for this exact
    # ambient setup. The parametrized test above isolates transcript-root
    # refusal and asserts that variable specifically.
    assert "OMNIAGENTOS_SIM_CAMPAIGN_ROOT" in result.stderr
    assert "inherited" in result.stderr.lower()


def _preflight_data(env: dict[str, str], *args: str) -> dict[str, str]:
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/launch-supervised.sh"), *args, "preflight"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return {
        key: value
        for line in result.stdout.splitlines()
        if "=" in line
        for key, value in [line.split("=", 1)]
    }


def _worker_campaign(name: str) -> str:
    """Keep simulation-port cache keys disjoint across xdist workers.

    A simulated campaign retains its ports in its campaign-root cache.  Static
    campaign names therefore make independently running test workers reuse the
    same port pair if their simulation roots overlap.  The test's campaign
    identity is otherwise irrelevant, so scope it to the worker that owns this
    invocation while retaining a stable name for the repeat assertion below.
    """
    return f"{name}-{os.environ.get('PYTEST_XDIST_WORKER', 'master')}"


def test_coherent_sim_env_gets_isolated_ports_and_no_production_bases(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "OMNIAGENTOS_PYTHON": sys.executable,
            "CLAUDE_CONFIG_DIR": str(tmp_path / "custom-claude"),
        }
    )
    Path(env["HOME"]).mkdir()
    first_campaign = _worker_campaign("campaign-one")
    second_campaign = _worker_campaign("campaign-two")
    first = _preflight_data(env, "--simulate", "--campaign", first_campaign)
    second = _preflight_data(env, "--simulate", "--campaign", second_campaign)
    repeat = _preflight_data(env, "--simulate", "--campaign", first_campaign)

    assert first["OMNIAGENTOS_API_PORT"] not in {"8485", "3003"}
    assert first["OMNIAGENTOS_DASH_PORT"] not in {"8485", "3003"}
    assert (first["OMNIAGENTOS_API_PORT"], first["OMNIAGENTOS_DASH_PORT"]) != (
        second["OMNIAGENTOS_API_PORT"],
        second["OMNIAGENTOS_DASH_PORT"],
    )
    assert (first["OMNIAGENTOS_API_PORT"], first["OMNIAGENTOS_DASH_PORT"]) == (
        repeat["OMNIAGENTOS_API_PORT"],
        repeat["OMNIAGENTOS_DASH_PORT"],
    )
    assert first["OMNIAGENTOS_PROJECT_BASES"] == ""
    assert "OmniAgentOS-sims" in first["OMNIAGENTOS_SIM_CAMPAIGN_ROOT"]
    assert first["CLAUDE_CONFIG_DIR"] == str(tmp_path / "custom-claude")
    assert str(ROOT) not in first["OMNIAGENTOS_SIM_CAMPAIGN_ROOT"]


def test_sim_ports_use_nonproduction_defaults_unless_explicitly_exported(
    tmp_path: Path,
) -> None:
    env = os.environ.copy()
    for key in ("OMNIAGENTOS_API_PORT", "OMNIAGENTOS_DASH_PORT", "OMNIAGENTOS_SIM_PORTS_EXPLICIT"):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "OMNIAGENTOS_PYTHON": sys.executable,
        }
    )
    Path(env["HOME"]).mkdir()

    derived = _preflight_data(
        env, "--simulate", "--campaign", _worker_campaign("port-defaults")
    )
    assert derived["OMNIAGENTOS_API_PORT"] != "8485"
    assert derived["OMNIAGENTOS_DASH_PORT"] != "3003"

    env.update({"OMNIAGENTOS_API_PORT": "8485", "OMNIAGENTOS_DASH_PORT": "3003"})
    explicit = _preflight_data(
        env, "--simulate", "--campaign", _worker_campaign("port-explicit")
    )
    assert explicit["OMNIAGENTOS_API_PORT"] == "8485"
    assert explicit["OMNIAGENTOS_DASH_PORT"] == "3003"


def test_sim_preflight_omits_unset_claude_config_dir(tmp_path: Path) -> None:
    env = os.environ.copy()
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "OMNIAGENTOS_PYTHON": sys.executable,
        }
    )
    Path(env["HOME"]).mkdir()

    preflight = _preflight_data(
        env, "--simulate", "--campaign", _worker_campaign("no-claude-config")
    )
    assert "CLAUDE_CONFIG_DIR" not in preflight


def test_sim_launcher_refuses_in_repo_campaign_root(tmp_path: Path) -> None:
    env = os.environ.copy()
    for key in (
        "OMNIAGENTOS_DB",
        "OMNIAGENTOS_VAR",
        "OMNIAGENTOS_VAR_DIR",
        "OMNIAGENTOS_LEDGER_DIR",
        "OMNIAGENTOS_VAULT_DIR",
        "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
        "OMNIAGENTOS_SWARM_DIR",
        "OMNIAGENTOS_MEMORIES_DIR",
        "OMNIAGENTOS_SIM_ENV_LOADED",
        "OMNIAGENTOS_SIM_ENV_NONCE",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "OMNIAGENTOS_PYTHON": sys.executable,
            "OMNIAGENTOS_SIM_ROOT": str(ROOT / "var" / "simulations"),
        }
    )
    Path(env["HOME"]).mkdir()

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/launch-supervised.sh"),
            "--simulate",
            "--campaign",
            "in-repo-campaign",
            "preflight",
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "runtime state path is source-controlled" in result.stderr
    assert "migrate the campaign" in result.stderr


def test_nonce_reentry_requires_matching_same_shell_nonce(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = f'''
set -euo pipefail
export HOME="{home}"
export OMNIAGENTOS_SIM_MODE=1 OMNIAGENTOS_SIM_CAMPAIGN=nonce-test
unset OMNIAGENTOS_DB OMNIAGENTOS_VAR OMNIAGENTOS_VAR_DIR OMNIAGENTOS_LEDGER_DIR OMNIAGENTOS_VAULT_DIR
. "{LAUNCH_ENV}"
first="$OMNIAGENTOS_SIM_ENV_NONCE"
test -n "$first"
. "{LAUNCH_ENV}"
test "$OMNIAGENTOS_SIM_ENV_NONCE" = "$first"
printf 'OK\\n'
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "OK"


def test_inherited_sim_loaded_without_matching_nonce_is_fatal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = f'''
set -euo pipefail
export HOME="{home}"
export OMNIAGENTOS_SIM_MODE=1 OMNIAGENTOS_SIM_CAMPAIGN=missing-nonce
export OMNIAGENTOS_SIM_ENV_LOADED=1
unset OMNIAGENTOS_SIM_ENV_NONCE _omni_sim_launcher_nonce
. "{LAUNCH_ENV}"
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode != 0
    assert "without the matching launcher nonce" in result.stderr


def test_inherited_sim_loaded_with_forged_nonce_is_fatal(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    script = f'''
set -euo pipefail
export HOME="{home}"
export OMNIAGENTOS_SIM_MODE=1 OMNIAGENTOS_SIM_CAMPAIGN=forged-nonce
export OMNIAGENTOS_SIM_ENV_LOADED=1 OMNIAGENTOS_SIM_ENV_NONCE=forged-nonce
unset _omni_sim_launcher_nonce
. "{LAUNCH_ENV}"
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    assert result.returncode != 0
    assert "without the matching launcher nonce" in result.stderr


def test_supervisor_process_specs_reenter_simulation_explicitly(
    monkeypatch, tmp_path: Path
) -> None:
    launcher = tmp_path / "launch.sh"
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_SIM_CAMPAIGN", "spec-campaign")
    specs = build_process_specs(tmp_path / "repo", launcher, tmp_path / "runtime")

    for spec in specs:
        assert spec.command == (
            str(launcher),
            "--simulate",
            "--campaign",
            "spec-campaign",
            spec.name,
        )

    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE")
    monkeypatch.delenv("OMNIAGENTOS_SIM_CAMPAIGN")
    production_specs = build_process_specs(tmp_path / "repo", launcher, tmp_path / "runtime")
    assert all("--simulate" not in spec.command for spec in production_specs)
    assert all("--campaign" not in spec.command for spec in production_specs)


def test_child_style_launcher_reentry_rederives_campaign_paths(tmp_path: Path) -> None:
    probe = tmp_path / "api-env.txt"
    stub_python = tmp_path / "python"
    stub_python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-m" ] && [ "$2" = "uvicorn" ]; then\n'
        '  printf "%s\\n" "$OMNIAGENTOS_VAR_DIR" "$OMNIAGENTOS_VAR" "$OMNIAGENTOS_DB" \
"$OMNIAGENTOS_LEDGER_DIR" "$OMNIAGENTOS_VAULT_DIR" > "$WP2_PROBE"\n'
        "  exit 0\n"
        "fi\n"
        f'exec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    stub_python.chmod(0o755)
    sim_root = tmp_path / "simulations"
    campaign = "test-reentry-001"
    env = os.environ.copy()
    for key in (
        "OMNIAGENTOS_DB",
        "OMNIAGENTOS_VAR",
        "OMNIAGENTOS_VAR_DIR",
        "OMNIAGENTOS_LEDGER_DIR",
        "OMNIAGENTOS_VAULT_DIR",
        "OMNIAGENTOS_SIM_CAMPAIGN_ROOT",
        "OMNIAGENTOS_SWARM_DIR",
        "OMNIAGENTOS_MEMORIES_DIR",
    ):
        env.pop(key, None)
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "OMNIAGENTOS_PYTHON": str(stub_python),
            "OMNIAGENTOS_SIM_ROOT": str(sim_root),
            "OMNIAGENTOS_SIM_MODE": "1",
            "OMNIAGENTOS_SIM_CAMPAIGN": campaign,
            "OMNIAGENTOS_SIM_ENV_LOADED": "1",
            "OMNIAGENTOS_SIM_ENV_NONCE": "stale-parent-nonce",
            "OMNIAGENTOS_DB": str(tmp_path / "production.sqlite3"),
            "WP2_PROBE": str(probe),
        }
    )
    Path(env["HOME"]).mkdir()

    result = subprocess.run(
        [
            "bash",
            str(ROOT / "scripts/launch-supervised.sh"),
            "--simulate",
            "--campaign",
            campaign,
            "api",
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    campaign_root = sim_root / campaign
    assert campaign_root.is_dir()
    for raw_path in probe.read_text(encoding="utf-8").splitlines():
        path = Path(raw_path)
        assert path == campaign_root or campaign_root in path.parents


def test_production_launch_env_exports_are_byte_identical_to_base() -> None:
    """The production environment may only change by a deliberate re-pin.

    The anchor SHA is the last commit whose launch-env exported exactly the set
    below. Moving it is the review event: whoever adds or removes an export
    re-pins this SHA in the SAME commit and names the change in the message, so
    an accidental environment drift stays a red and a deliberate one is on the
    record.

    Anchor history:
      f1451d75 -> cc9d94b3  adds OMNIAGENTOS_CONTEXT_CAPSULE (U-C1 promoted the
                            context capsule to production shadow; the flag has to
                            reach child processes or the shadow never runs).
      cc9d94b3 -> 087d530c  adds OMNIAGENTOS_BUDGET_ENFORCEMENT=block (autonomy
                            audit C3: enforcement was unset on the live daemons,
                            so every policy-gated budget control logged and
                            proceeded instead of blocking; the flag has to reach
                            child processes or no cap can ever trip).
      087d530c -> 511dfe90  adds OMNIAGENTOS_QUICK_PROJECT_ROUTING_MODE=shadow
                            (511dfe90's own message: "default ... on the launch
                            path"; a deliberate export the landing commit forgot
                            to re-pin here, which is exactly the accidental-drift
                            shape this test exists to catch — it went red on
                            main from 511dfe90 until this re-pin. Re-pinning to
                            511dfe90 rather than a later synthetic commit because
                            it is the last commit that actually touched
                            scripts/launch-env.sh; `git log 511dfe90..main --
                            scripts/launch-env.sh` is empty, and 511dfe90's copy
                            of the file diffs byte-identical to the one on disk
                            here, so the anchor and the file agree by
                            construction, not by coincidence).
      511dfe90 -> e30862c3e adds OMNIAGENTOS_MEMORY_HYBRID=1 (hybrid context
                            assembly — sentence-grain history retrieval leg,
                            temporal stamps, abstention guard, reserved
                            packing — promoted to the launch path; memcert v2
                            lane feat/memory-hybrid-memcert2-0813, certified
                            by tests/memcert/test_sufficiency.py. Deliberate:
                            the flag must reach child processes or production
                            briefs silently revert to the v1 recency-only
                            assembler. e30862c3e is the commit that touched
                            scripts/launch-env.sh; `git log e30862c3e..HEAD --
                            scripts/launch-env.sh` is empty on this branch.)
    """
    if not (ROOT / ".git").exists():
        pytest.skip(
            "no git history in this checkout: the anchor SHA e30862c3e above pins "
            "a commit in the private estate's continuous history, and this "
            "repository is a scrubbed, single point-in-time public-release export "
            "that neither carries that estate's history (the anchor SHA is foreign "
            "to whatever history this export ends up with) nor, right now, has any "
            "git history of its own at all (`git show <sha>:path` fails with 'not "
            "a git repository' here — see tests/docs/test_phase1_plan_reconciliation.py "
            "for the same shape of gap). Forcing a diff against an unreachable "
            "commit would not honestly prove no-drift; it would just move the "
            "favourable-absence hazard this test exists to prevent from 'nobody "
            "checked' to 'checked against a SHA that cannot resolve'. Re-enable "
            "once this checkout has its own git history to diff against."
        )
    base_source = subprocess.check_output(
        ["git", "show", "e30862c3e:scripts/launch-env.sh"],
        cwd=ROOT,
        text=True,
    )

    # Both sources are sourced from the SAME on-disk path so the two runs derive
    # identical repo-relative values and differ only where the scripts differ.
    #
    # It is a real file and not the old `. /dev/stdin` spelling because a pipe on
    # macOS holds 16 KiB: launch-env.sh passed that size, so bash silently
    # stopped sourcing partway and the probe reported "every export vanished"
    # as though the environment had drifted. A file has no such ceiling, and it
    # is also how every launcher actually sources this script.
    with tempfile.TemporaryDirectory() as tmpdir:
        fake_root = Path(tmpdir) / "OmniAgentOS"
        (fake_root / "scripts").mkdir(parents=True)
        script = fake_root / "scripts" / "launch-env.sh"

        def resolved_exports(source: str) -> str:
            script.write_text(source, encoding="utf-8")
            clean_env = {"HOME": "/private/tmp/wp2-clean-home", "PATH": os.environ["PATH"]}
            probe = f'. "{script}"; export -p | sort'
            result = subprocess.run(
                ["bash", "-c", probe],
                cwd=ROOT,
                env=clean_env,
                capture_output=True,
                text=True,
                check=True,
            )
            return result.stdout

        base_exports = resolved_exports(base_source)
        current_exports = resolved_exports(LAUNCH_ENV.read_text())

    # Guard the guard: a probe that resolves nothing would compare two empty
    # environments and pass no matter what drifted.
    assert "OMNIAGENTOS_VAR_DIR=" in base_exports, base_exports

    # A bare `assert base_exports == current_exports` on two ~3.4 KB strings
    # gets prefix-elided by pytest ("Skipping N identical leading characters
    # in diff, use -v to show"), which cuts the offending line mid-token and
    # was misread as byte-level capture corruption (finding sha256:caefcb75,
    # answered by measurement in sha256:e69c2c6c: 0/440 reproductions across
    # both a sequential run and a 12-way concurrent run). Compare by line SET first so the message names
    # the added/removed export in full, including its `declare -x ` prefix,
    # and states the remedy (re-pin the anchor SHA above). Do not "simplify"
    # this back to a bare assert — that is exactly the readability defect
    # this test exists to avoid re-introducing.
    base_lines = set(base_exports.splitlines())
    current_lines = set(current_exports.splitlines())
    added = sorted(current_lines - base_lines)
    removed = sorted(base_lines - current_lines)
    assert not added and not removed, (
        "production launch-env exports drifted from the pinned anchor "
        "e30862c3e. If this is deliberate, re-pin the anchor SHA in "
        "scripts/launch-env.sh's history reference above IN THE SAME COMMIT "
        "that changes the export.\n"
        f"added (present now, absent at anchor): {added}\n"
        f"removed (present at anchor, absent now): {removed}"
    )

    # Backstop: line-set equality alone would pass on an ordering-only or
    # duplicate-line difference that leaves both sets equal but the byte
    # strings unequal. Keep the byte-exact assert last so the test's
    # red/green behaviour is unchanged from before this message rewrite.
    assert base_exports == current_exports
