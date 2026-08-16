"""Pure, dry-runnable planning of server bootstrap + app deploy with automatic HTTPS.

Three layers, all import-safe and side-effect free:

1. ``bootstrap.plan_server_bootstrap`` — make a fresh host serve HTTPS (Caddy).
2. ``deploy.plan_app_deploy`` — put an app behind Caddy as a systemd service.
3. ``executor.execute_plan`` — run a plan through an INJECTED runner only.

Nothing in this package opens a socket, shells out, or imports an SSH library.
Live execution belongs to the SSH policy lane behind a consequential grant; see
``omniagentos.deploy.executor``'s module docstring.
"""

from omniagentos.deploy.bootstrap import plan_server_bootstrap
from omniagentos.deploy.contracts import (
    AppSpec,
    DeployPlan,
    DeploySpecError,
    DeployStep,
    RunResult,
    ServerSpec,
)
from omniagentos.deploy.deploy import plan_app_deploy, render_caddy_site, render_systemd_unit
from omniagentos.deploy.executor import ExecReport, StepReport, dry_run_runner, execute_plan

__all__ = [
    "AppSpec",
    "DeployPlan",
    "DeploySpecError",
    "DeployStep",
    "ExecReport",
    "RunResult",
    "ServerSpec",
    "StepReport",
    "dry_run_runner",
    "execute_plan",
    "plan_app_deploy",
    "plan_server_bootstrap",
    "render_caddy_site",
    "render_systemd_unit",
]
