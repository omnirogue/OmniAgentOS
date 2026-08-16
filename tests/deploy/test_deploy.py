"""The app-deploy plan: Caddy reverse proxy with real HTTPS, unprivileged unit."""

from __future__ import annotations

import re

import pytest

from omniagentos.deploy.contracts import AppSpec, DeploySpecError
from omniagentos.deploy.deploy import (
    env_file_path,
    plan_app_deploy,
    render_caddy_site,
    render_systemd_unit,
)
from tests.deploy.idempotence import find_unguarded

# --- Caddy site block ------------------------------------------------------


def test_caddy_block_proxies_domain_to_listen_port(app: AppSpec) -> None:
    site = render_caddy_site(app)
    assert site.startswith("demo.example.com {")
    assert "reverse_proxy 127.0.0.1:8099" in site
    assert site.rstrip().endswith("}")


def test_caddy_block_never_disables_automatic_https(app: AppSpec) -> None:
    site = render_caddy_site(app)
    lowered = site.lower()
    # Any of these would suppress the Let's Encrypt certificate we depend on.
    for killer in ("auto_https off", "auto_https disable_redirects", "tls internal",
                   "http://"):
        assert killer not in lowered, f"site block disables automatic HTTPS via {killer!r}"


def test_caddy_site_step_writes_block_for_the_domain(app: AppSpec) -> None:
    step = plan_app_deploy(app).step("caddy-site")
    assert "/etc/caddy/sites/demo.example.com.caddy" in step.command
    assert "reverse_proxy 127.0.0.1:8099" in step.command
    assert "auto_https off" not in step.command


def test_caddy_is_validated_before_reload(app: AppSpec) -> None:
    ids = plan_app_deploy(app).step_ids
    assert ids.index("caddy-site") < ids.index("caddy-validate") < ids.index("caddy-reload")


def test_caddy_reload_is_reload_or_restart(app: AppSpec) -> None:
    # FINDING-2: a plain `systemctl reload caddy` errors ("Job type reload is not
    # applicable") when Caddy is stopped, breaking idempotent re-deploy after a
    # failed prior run. reload-or-restart converges from either state.
    step = plan_app_deploy(app).step("caddy-reload")
    assert step.command == "systemctl reload-or-restart caddy"
    assert "reload caddy" not in step.command.replace("reload-or-restart", "X")


# --- systemd unit ----------------------------------------------------------


def test_systemd_unit_is_well_formed(app: AppSpec) -> None:
    unit = render_systemd_unit(app)
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in unit
    assert unit.index("[Unit]") < unit.index("[Service]") < unit.index("[Install]")
    assert "WantedBy=multi-user.target" in unit
    assert "Type=simple" in unit
    assert re.search(r"^ExecStart=.+$", unit, re.MULTILINE)
    # Every directive is a `Key=Value` line or a section header or blank.
    for line in unit.splitlines():
        if not line or line.startswith("["):
            continue
        assert re.match(r"^[A-Za-z]+=", line), f"malformed unit line: {line!r}"


def test_systemd_unit_runs_as_non_root_deploy_user(app: AppSpec) -> None:
    unit = render_systemd_unit(app)
    assert "User=deploy" in unit
    assert "Group=deploy" in unit
    assert "User=root" not in unit
    assert "NoNewPrivileges=true" in unit


def test_systemd_unit_binds_the_listen_port_and_env_ref(app: AppSpec) -> None:
    unit = render_systemd_unit(app)
    assert "Environment=PORT=8099" in unit
    assert f"EnvironmentFile=-{env_file_path(app)}" in unit
    assert app.start_cmd in unit
    assert "WorkingDirectory=/srv/apps/demo-app" in unit


def test_unit_is_written_to_the_systemd_path_and_reloaded(app: AppSpec) -> None:
    plan = plan_app_deploy(app)
    assert "/etc/systemd/system/demo-app.service" in plan.step("systemd-unit").command
    ids = plan.step_ids
    assert ids.index("systemd-unit") < ids.index("systemd-reload") < ids.index("service-restart")


# --- source, build, health -------------------------------------------------


def test_remote_repo_is_cloned_or_fast_forwarded(app: AppSpec) -> None:
    cmd = plan_app_deploy(app).step("sync-source").command
    assert "runuser -u deploy" in cmd
    # Positional-safe: `--` ends option parsing and the URL is double-quoted.
    assert 'git clone -- "https://github.com/example-org/demo-app.git"' in cmd
    assert "pull --ff-only" in cmd


def test_local_staging_path_is_rsynced(app: AppSpec) -> None:
    staged = AppSpec(
        repo_url_or_local_path="/opt/staging/demo-app",
        domain=app.domain,
        service_name=app.service_name,
        listen_port=app.listen_port,
        build_cmd="",
        start_cmd=app.start_cmd,
    )
    cmd = plan_app_deploy(staged).step("sync-source").command
    assert 'rsync -a --delete -- "/opt/staging/demo-app/" /srv/apps/demo-app/' in cmd
    assert "build" not in plan_app_deploy(staged).step_ids


def test_build_runs_as_deploy_user_in_the_app_dir(app: AppSpec) -> None:
    cmd = plan_app_deploy(app).step("build").command
    assert cmd.startswith("runuser -u deploy -- /bin/bash -lc '")
    assert "cd /srv/apps/demo-app &&" in cmd
    assert app.build_cmd in cmd


def test_health_check_requires_https_200(app: AppSpec) -> None:
    step = plan_app_deploy(app).step("health-check")
    assert "https://demo.example.com/" in step.command
    assert 'test "$code" = "200"' in step.command
    assert step.mutating is False
    # It is the LAST step: nothing is declared healthy before it runs.
    assert plan_app_deploy(app).step_ids[-1] == "health-check"


def test_deploy_plan_is_idempotent(app: AppSpec) -> None:
    assert find_unguarded(plan_app_deploy(app).to_script()) == []


def test_plan_is_deterministic(app: AppSpec) -> None:
    assert plan_app_deploy(app) == plan_app_deploy(app)


# --- spec validation -------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"domain": "https://demo.example.com"},
        {"domain": "demo.example.com; rm -rf /"},
        {"service_name": "demo app"},
        {"listen_port": 80},
        {"listen_port": 99999},
        {"start_cmd": "run --flag 'x'"},
        {"start_cmd": ""},
    ],
)
def test_bad_specs_are_refused(app: AppSpec, kwargs: dict[str, object]) -> None:
    base = {
        "repo_url_or_local_path": app.repo_url_or_local_path,
        "domain": app.domain,
        "service_name": app.service_name,
        "listen_port": app.listen_port,
        "build_cmd": app.build_cmd,
        "start_cmd": app.start_cmd,
    }
    base.update(kwargs)
    with pytest.raises(DeploySpecError):
        AppSpec(**base)  # type: ignore[arg-type]


# --- FINDING-1: argument injection into git/rsync via the source ------------


@pytest.mark.parametrize(
    "malicious",
    [
        "--upload-pack=id git@github.com:a/b.git",  # git option-injection → RCE
        "-e ssh:evil",  # rsync option-injection
        "--config=core.fsmonitor=id",
        "-",
        "--",
    ],
)
def test_source_beginning_with_dash_is_rejected(app: AppSpec, malicious: str) -> None:
    """Layer 1: a source that could be parsed as an option never builds a spec."""
    with pytest.raises(DeploySpecError):
        AppSpec(
            repo_url_or_local_path=malicious,
            domain=app.domain,
            service_name=app.service_name,
            listen_port=app.listen_port,
            build_cmd="",
            start_cmd=app.start_cmd,
        )


def test_git_clone_forces_positional_parsing(app: AppSpec) -> None:
    """Layer 2: even a legitimate source is spliced after `--` and quoted."""
    cmd = plan_app_deploy(app).step("sync-source").command
    src = app.repo_url_or_local_path
    assert f'git clone -- "{src}"' in cmd


def test_rsync_forces_positional_parsing(app: AppSpec) -> None:
    staged = AppSpec(
        repo_url_or_local_path="/opt/staging/demo-app",
        domain=app.domain,
        service_name=app.service_name,
        listen_port=app.listen_port,
        build_cmd="",
        start_cmd=app.start_cmd,
    )
    cmd = plan_app_deploy(staged).step("sync-source").command
    assert 'rsync -a --delete -- "/opt/staging/demo-app/"' in cmd


def test_server_host_and_ssh_user_reject_leading_dash() -> None:
    """host/ssh_user reach the SSH runner as ssh args; a leading dash is injection."""
    from omniagentos.deploy.contracts import ServerSpec

    with pytest.raises(DeploySpecError):
        ServerSpec(host="-oProxyCommand=id", ssh_user="root", ssh_key_ref="ref")
    with pytest.raises(DeploySpecError):
        ServerSpec(host="1.2.3.4", ssh_user="-lroot", ssh_key_ref="ref")
