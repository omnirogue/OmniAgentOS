"""The bootstrap plan must be idempotent, ordered safely, and HTTPS-capable."""

from __future__ import annotations

import pytest

from omniagentos.deploy.bootstrap import plan_server_bootstrap
from omniagentos.deploy.contracts import CADDY_SITES_DIR, ServerSpec
from tests.deploy.idempotence import find_unguarded, mutating_lines


def test_every_mutating_line_is_guarded(server: ServerSpec) -> None:
    script = plan_server_bootstrap(server).to_script()
    # Sanity: the checker must actually be looking at real work.
    assert len(mutating_lines(script)) >= 8
    violations = find_unguarded(script)
    assert violations == [], "\n".join(f"L{v.line_no}: {v.line}" for v in violations)


def test_node_bootstrap_is_also_idempotent(node_server: ServerSpec) -> None:
    script = plan_server_bootstrap(node_server).to_script()
    assert find_unguarded(script) == []
    assert "node" in script


def test_installs_caddy_for_automatic_https(server: ServerSpec) -> None:
    plan = plan_server_bootstrap(server)
    install = plan.step("install-caddy")
    assert "command -v caddy" in install.command
    assert "||" in install.command
    script = plan.to_script()
    # Automatic HTTPS is the point of using Caddy — never turn it off.
    assert "auto_https off" not in script
    assert CADDY_SITES_DIR in script


def test_runtime_is_selected_by_spec() -> None:
    py = plan_server_bootstrap(
        ServerSpec(host="h1", ssh_user="root", ssh_key_ref="ref", runtime="python")
    )
    node = plan_server_bootstrap(
        ServerSpec(host="h1", ssh_user="root", ssh_key_ref="ref", runtime="node")
    )
    assert "runtime-python" in py.step_ids
    assert "runtime-node" not in py.step_ids
    assert "runtime-node" in node.step_ids
    assert "python3-venv" not in node.to_script()


def test_deploy_user_is_created_unprivileged_and_guarded(server: ServerSpec) -> None:
    step = plan_server_bootstrap(server).step("deploy-user")
    assert step.command.startswith("id -u deploy")
    assert "useradd --system" in step.command
    assert "--shell /usr/sbin/nologin" in step.command


def test_firewall_allows_ssh_before_enabling(server: ServerSpec) -> None:
    ids = plan_server_bootstrap(server).step_ids
    assert ids.index("firewall-ssh") < ids.index("firewall-enable")
    assert ids.index("firewall-http") < ids.index("firewall-enable")
    assert ids.index("firewall-https") < ids.index("firewall-enable")


def test_firewall_opens_80_and_443(server: ServerSpec) -> None:
    plan = plan_server_bootstrap(server)
    assert plan.step("firewall-http").command == "ufw allow 80/tcp"
    assert plan.step("firewall-https").command == "ufw allow 443/tcp"


def test_extra_packages_are_installed_when_requested(server: ServerSpec) -> None:
    plan = plan_server_bootstrap(server)
    assert "sqlite3" in plan.step("extra-packages").command
    bare = plan_server_bootstrap(
        ServerSpec(host="h", ssh_user="root", ssh_key_ref="ref")
    )
    assert "extra-packages" not in bare.step_ids


def test_plan_is_pure_and_repeatable(server: ServerSpec) -> None:
    assert plan_server_bootstrap(server) == plan_server_bootstrap(server)


def test_script_has_strict_bash_header(server: ServerSpec) -> None:
    script = plan_server_bootstrap(server).to_script()
    assert script.startswith("#!/usr/bin/env bash\nset -euo pipefail\n")


@pytest.mark.parametrize("step_id", ["apt-update", "install-caddy", "deploy-user", "apps-root"])
def test_core_steps_present(server: ServerSpec, step_id: str) -> None:
    assert step_id in plan_server_bootstrap(server).step_ids
