"""Test that all installers properly source launch-env.sh.

This is a regression test for the 2026-08-01 launchd durability fix:
NO launchd job ever sourced scripts/launch-env.sh, making OMNIAGENTOS_GATE_WORKSPACE
and OMNIAGENTOS_API_PORT INERT in every daemon until the plist exec chain included
`. <REPO_ROOT>/scripts/launch-env.sh;` after the connections.env sourcing.
"""

from __future__ import annotations

from pathlib import Path

# All installers that render plists and source connections.env
INSTALLERS_TO_CHECK = [
    "scripts/archi-morning/install-archi-morning.sh",
    "scripts/backlog-executor/install.sh",
    "scripts/gates/install-agent-watchdog.sh",
    "scripts/gates/install-planner-canary.sh",
    "scripts/golden-suite/install-golden-suite.sh",  # This execs golden-suite.sh
    "scripts/health-sentinel/install.sh",
    "scripts/hygiene/install-hygiene.sh",
    "scripts/scheduler/install-bank-balances.sh",
    "scripts/scheduler/install-banking.sh",
    "scripts/scheduler/install-comms.sh",
    "scripts/scheduler/install-comms-slack.sh",  # renders BOTH hybrid halves
    "scripts/scheduler/install-feature-health.sh",
    "scripts/scheduler/install-modelintel.sh",  # Mentions but doesn't source
    "scripts/scheduler/install-revenue.sh",
    "scripts/scheduler/install-routines.sh",
    "scripts/scheduler/install-steward.sh",
    "scripts/swarm/install-swarm-optimizer.sh",
]

# Additional job scripts that source connections.env
JOB_SCRIPTS_TO_CHECK = [
    "scripts/golden-suite/golden-suite.sh",
]


def test_all_installers_and_scripts_source_launch_env_sh() -> None:
    """All installers/scripts that source connections.env must also source launch-env.sh."""
    failures = []

    all_paths = INSTALLERS_TO_CHECK + JOB_SCRIPTS_TO_CHECK

    for path_str in all_paths:
        p = Path(path_str)
        if not p.exists():
            continue

        content = p.read_text()

        # Skip if file only mentions connections.env (e.g., in comments)
        # but doesn't actually source it
        if '. "$HOME/.config/omni/connections.env"' not in content and \
           'connections.env' in content:
            # Only mentions it, doesn't source
            continue

        # If it sources connections.env, it must also source launch-env.sh
        if "connections.env" in content and "launch-env.sh" not in content:
            failures.append(
                f"{path_str}: sources connections.env but not launch-env.sh"
            )

    if failures:
        raise AssertionError(
            "Installers/scripts must source launch-env.sh after connections.env:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


def test_launch_env_sh_sourced_after_connections_env() -> None:
    """launch-env.sh should be sourced after connections.env (not before)."""
    failures = []

    all_paths = INSTALLERS_TO_CHECK + JOB_SCRIPTS_TO_CHECK

    for path_str in all_paths:
        p = Path(path_str)
        if not p.exists():
            continue

        content = p.read_text()

        # Find positions of connections.env and launch-env.sh
        connections_pos = content.find("connections.env")
        launch_env_pos = content.find("launch-env.sh")

        if connections_pos == -1 or launch_env_pos == -1:
            continue

        if launch_env_pos < connections_pos:
            failures.append(
                f"{path_str}: launch-env.sh sourced BEFORE connections.env"
            )

    if failures:
        raise AssertionError(
            "launch-env.sh must be sourced AFTER connections.env:\n"
            + "\n".join(f"  - {f}" for f in failures)
        )


def test_launch_env_sh_exists_and_is_idempotent() -> None:
    """The launch-env.sh file must exist and be idempotent when sourced."""
    launch_env = Path("scripts/launch-env.sh")
    assert launch_env.exists(), "scripts/launch-env.sh must exist"

    content = launch_env.read_text()
    # Check for idempotency guard
    assert "OMNIAGENTOS_LAUNCH_ENV_LOADED" in content, \
        "launch-env.sh must have idempotency guard to prevent re-sourcing issues"


if __name__ == "__main__":
    test_all_installers_and_scripts_source_launch_env_sh()
    test_launch_env_sh_sourced_after_connections_env()
    test_launch_env_sh_exists_and_is_idempotent()
    print("✓ All launch-env.sh tests passed!")
