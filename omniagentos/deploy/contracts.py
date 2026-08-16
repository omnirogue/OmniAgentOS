"""Frozen value types for the deploy planner.

Everything here is a PURE description of work to be done on a REMOTE host. No
step is executed by constructing these objects; execution happens only through
``omniagentos.deploy.executor.execute_plan`` with an injected runner (see that
module's docstring for the policy boundary).

The validators below are a security surface, not a nicety: every field that is
interpolated into a shell command (domain, service name, port, paths) is
constrained to a shape that cannot carry shell metacharacters, so a planner
input can never smuggle a second command into an emitted line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

Runtime = Literal["python", "node"]

# A DNS name: labels of [a-z0-9-] joined by dots, no scheme, no path, no ':'.
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
                        r"(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
# systemd unit stem: what we allow before ".service".
_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
# Unix login name for the unprivileged deploy account.
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
# An apt package atom.
_PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,63}$")

# Commands are run through a shell on the remote host, so anything that could
# terminate one command and start another must never reach an interpolation.
_SHELL_META = set(";&|`$><\n\r\\\"'(){}[]*?!#~")

#: The unprivileged account every app runs as. Never root.
DEPLOY_USER = "deploy"
#: Parent directory for per-service application checkouts.
APPS_ROOT = "/srv/apps"
#: Directory Caddy imports per-site config from (created by the bootstrap plan).
CADDY_SITES_DIR = "/etc/caddy/sites"
#: The main Caddyfile, which does nothing but import CADDY_SITES_DIR.
CADDY_FILE = "/etc/caddy/Caddyfile"


class DeploySpecError(ValueError):
    """A spec field is missing, malformed, or shell-unsafe."""


def _reject_shell_meta(name: str, value: str) -> str:
    """Refuse any value that could break out of the command it is spliced into."""
    if not value:
        raise DeploySpecError(f"{name} must not be empty")
    bad = sorted(_SHELL_META.intersection(value))
    if bad:
        raise DeploySpecError(f"{name} contains shell metacharacters: {''.join(bad)!r}")
    return value


def _reject_leading_dash(name: str, value: str) -> str:
    """Refuse a value that begins with '-'.

    A leading dash lets a value be parsed as a command OPTION instead of the
    positional argument it is spliced in as (e.g. ``--upload-pack=…`` to git,
    ``-e ssh:…`` to rsync). Emitted commands also guard this with ``--``, but a
    value beginning with ``-`` is never a legitimate host/user/path/package, so
    it is rejected outright as the first line of defense.
    """
    if value.startswith("-"):
        raise DeploySpecError(f"{name} must not begin with '-' (argument-injection guard): {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class ServerSpec:
    """A freshly provisioned host to be made ready to serve apps over HTTPS."""

    host: str
    ssh_user: str
    ssh_key_ref: str
    """An OPAQUE REFERENCE to a key (vault id / agent alias) — never key material."""
    runtime: Runtime = "python"
    packages: tuple[str, ...] = ()
    """Extra apt packages beyond the runtime's own baseline."""
    deploy_user: str = DEPLOY_USER

    def __post_init__(self) -> None:
        _reject_shell_meta("host", self.host)
        _reject_shell_meta("ssh_user", self.ssh_user)
        _reject_shell_meta("ssh_key_ref", self.ssh_key_ref)
        # host and ssh_user are handed to the SSH runner as ssh arguments, where a
        # leading dash (e.g. `-oProxyCommand=…`) is an option-injection vector.
        _reject_leading_dash("host", self.host)
        _reject_leading_dash("ssh_user", self.ssh_user)
        # ssh_key_ref may be handed to ssh as `-i <ref>`; a leading dash lets it
        # be reparsed as an ssh option (e.g. `-oProxyCommand=…`).
        _reject_leading_dash("ssh_key_ref", self.ssh_key_ref)
        if self.runtime not in ("python", "node"):
            raise DeploySpecError(f"runtime must be 'python' or 'node', got {self.runtime!r}")
        if not _USER_RE.match(self.deploy_user):
            raise DeploySpecError(f"deploy_user is not a valid unix login: {self.deploy_user!r}")
        if self.deploy_user == "root":
            raise DeploySpecError("deploy_user must not be root")
        for pkg in self.packages:
            if not _PACKAGE_RE.match(pkg):
                raise DeploySpecError(f"not a valid apt package name: {pkg!r}")
        if "PRIVATE KEY" in self.ssh_key_ref:
            raise DeploySpecError("ssh_key_ref must be a reference, not key material")


@dataclass(frozen=True, slots=True)
class AppSpec:
    """One application: where it comes from, how it builds, how it is served."""

    repo_url_or_local_path: str
    domain: str
    service_name: str
    listen_port: int
    build_cmd: str
    start_cmd: str
    env_ref: str = ""
    """An OPAQUE REFERENCE to a secrets bundle. Resolved to a remote
    EnvironmentFile path by the deployer; never inlined secret VALUES."""
    deploy_user: str = DEPLOY_USER

    def __post_init__(self) -> None:
        _reject_shell_meta("repo_url_or_local_path", self.repo_url_or_local_path)
        _reject_leading_dash("repo_url_or_local_path", self.repo_url_or_local_path)
        if not _DOMAIN_RE.match(self.domain):
            raise DeploySpecError(f"domain must be a bare DNS name, got {self.domain!r}")
        if not _SERVICE_RE.match(self.service_name):
            raise DeploySpecError(f"service_name is not a valid unit stem: {self.service_name!r}")
        if not isinstance(self.listen_port, int) or isinstance(self.listen_port, bool):
            raise DeploySpecError("listen_port must be an int")
        # 80/443 belong to Caddy; the app always sits behind the proxy.
        if not (1024 <= self.listen_port <= 65535):
            raise DeploySpecError(
                f"listen_port must be an unprivileged port 1024-65535, got {self.listen_port}"
            )
        if not self.start_cmd.strip():
            raise DeploySpecError("start_cmd must not be empty")
        if "\n" in self.start_cmd or "\n" in self.build_cmd:
            raise DeploySpecError("build_cmd/start_cmd must be single-line commands")
        # Both are spliced into a single-quoted context (systemd ExecStart, and
        # `bash -lc '...'` under runuser), so a single quote would break out of it.
        if "'" in self.start_cmd or "'" in self.build_cmd:
            raise DeploySpecError(
                "build_cmd/start_cmd must not contain a single quote; they are spliced "
                "into a single-quoted shell/systemd context"
            )
        # env_ref is an OPTIONAL opaque reference the deployer resolves to a
        # remote EnvironmentFile path; when present it can reach a command, so it
        # gets the same shell-meta + leading-dash guards as every other ref.
        if self.env_ref:
            _reject_shell_meta("env_ref", self.env_ref)
            _reject_leading_dash("env_ref", self.env_ref)
        if not _USER_RE.match(self.deploy_user):
            raise DeploySpecError(f"deploy_user is not a valid unix login: {self.deploy_user!r}")
        if self.deploy_user == "root":
            raise DeploySpecError("deploy_user must not be root")

    @property
    def app_dir(self) -> str:
        """Remote checkout directory for this service."""
        return f"{APPS_ROOT}/{self.service_name}"

    @property
    def unit_name(self) -> str:
        return f"{self.service_name}.service"

    @property
    def unit_path(self) -> str:
        return f"/etc/systemd/system/{self.unit_name}"

    @property
    def site_path(self) -> str:
        return f"{CADDY_SITES_DIR}/{self.domain}.caddy"


@dataclass(frozen=True, slots=True)
class DeployStep:
    """One remote command, with a stable id and a human-readable description."""

    step_id: str
    description: str
    command: str
    mutating: bool = True
    """False for pure probes (health checks, validations) that change no state."""

    def __post_init__(self) -> None:
        if not self.step_id:
            raise DeploySpecError("step_id must not be empty")
        if not self.description:
            raise DeploySpecError("description must not be empty")
        if not self.command.strip():
            raise DeploySpecError("command must not be empty")


@dataclass(frozen=True, slots=True)
class DeployPlan:
    """An ordered, immutable list of remote steps. Pure data — never self-executing."""

    name: str
    target_host: str
    steps: tuple[DeployStep, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for step in self.steps:
            if step.step_id in seen:
                raise DeploySpecError(f"duplicate step_id in plan: {step.step_id!r}")
            seen.add(step.step_id)

    def __len__(self) -> int:
        return len(self.steps)

    @property
    def step_ids(self) -> tuple[str, ...]:
        return tuple(step.step_id for step in self.steps)

    def step(self, step_id: str) -> DeployStep:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(step_id)

    def to_script(self) -> str:
        """Render the plan as one idempotent bash script (for review / dry-run).

        The script is what a human approves before the grant is issued; it is
        never run by this library.
        """
        lines = [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            f"# plan: {self.name}",
            f"# target: {self.target_host}",
        ]
        for step in self.steps:
            lines.append("")
            lines.append(f"# [{step.step_id}] {step.description}")
            lines.append(step.command)
        return "\n".join(lines) + "\n"


@dataclass(frozen=True, slots=True)
class RunResult:
    """What an injected runner returns for one command."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


__all__ = [
    "APPS_ROOT",
    "CADDY_FILE",
    "CADDY_SITES_DIR",
    "DEPLOY_USER",
    "AppSpec",
    "DeployPlan",
    "DeploySpecError",
    "DeployStep",
    "RunResult",
    "Runtime",
    "ServerSpec",
]
