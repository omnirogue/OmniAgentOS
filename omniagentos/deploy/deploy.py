"""Plan an application deployment onto an already-bootstrapped host.

Produces the steps that put code on the box, build it, run it as an
unprivileged systemd service on a loopback port, and put Caddy in front of it
as a reverse proxy for ``domain``. Caddy's automatic HTTPS is left ON — the
certificate is issued by Let's Encrypt on first request, which is why the site
block never contains ``auto_https off``.

Source handling, stated once because it is the one non-obvious contract:

* a URL (``https://…``, ``git@…``, ``ssh://…``) is cloned/pulled ON the remote;
* anything else is treated as a STAGING PATH THAT ALREADY EXISTS ON THE REMOTE
  (uploaded out of band by the file-transfer lane) and is rsync'd into place.

That keeps the invariant every consumer relies on: every command in a plan runs
on the target host and nowhere else.
"""

from __future__ import annotations

from omniagentos.deploy.contracts import (
    CADDY_FILE,
    AppSpec,
    DeployPlan,
    DeployStep,
)

ENV_DIR = "/etc/omniagentos/env"

_URL_PREFIXES = ("http://", "https://", "ssh://", "git://", "git@")


def _is_remote_repo(source: str) -> bool:
    return source.startswith(_URL_PREFIXES) or source.endswith(".git")


def _as_deploy(user: str, payload: str) -> str:
    """Run ``payload`` as the unprivileged deploy user, with a login shell.

    ``payload`` is single-quoted; :class:`AppSpec` refuses single quotes in the
    fields that reach it, so this cannot be broken out of.
    """
    return f"runuser -u {user} -- /bin/bash -lc '{payload}'"


def env_file_path(app: AppSpec) -> str:
    """Where the secrets lane is expected to drop this service's env file."""
    return f"{ENV_DIR}/{app.service_name}.env"


def render_systemd_unit(app: AppSpec) -> str:
    """Render the systemd unit. Runs as the non-root deploy user, never as root."""
    return "\n".join(
        [
            "[Unit]",
            f"Description={app.service_name} (deployed by OmniAgentOS)",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
            f"User={app.deploy_user}",
            f"Group={app.deploy_user}",
            f"WorkingDirectory={app.app_dir}",
            f"Environment=PORT={app.listen_port}",
            "Environment=HOST=127.0.0.1",
            # '-' prefix: a not-yet-provisioned env file must not block the boot.
            f"EnvironmentFile=-{env_file_path(app)}",
            f"ExecStart=/bin/bash -lc '{app.start_cmd}'",
            "Restart=always",
            "RestartSec=3",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=full",
            "ProtectHome=true",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
            "",
        ]
    )


def render_caddy_site(app: AppSpec) -> str:
    """Render the Caddy site block.

    NOTE: no ``auto_https off``, no ``tls internal`` — automatic Let's Encrypt
    issuance for ``domain`` is the point of this block.
    """
    return "\n".join(
        [
            f"{app.domain} {{",
            "\tencode zstd gzip",
            f"\treverse_proxy 127.0.0.1:{app.listen_port}",
            "\tlog {",
            f"\t\toutput file /var/log/caddy/{app.service_name}.log",
            "\t}",
            "}",
            "",
        ]
    )


def _source_step(app: AppSpec) -> DeployStep:
    src = app.repo_url_or_local_path
    if _is_remote_repo(src):
        # `--` ends option parsing and the value is double-quoted, so a source
        # like `--upload-pack=…` can only ever be read as the positional repo,
        # never as a git flag (argument injection). AppSpec ALSO rejects a
        # leading dash; this is the second layer. Double quotes are safe because
        # _SHELL_META already forbids `"`, `$`, backticks and quotes in `src`.
        payload = (
            f"if test -d {app.app_dir}/.git; then "
            f'git -C {app.app_dir} pull --ff-only; '
            f'else git clone -- "{src}" {app.app_dir}; fi'
        )
        return DeployStep(
            step_id="sync-source",
            description=f"Clone or fast-forward {src} into {app.app_dir}",
            command=_as_deploy(app.deploy_user, payload),
        )
    return DeployStep(
        step_id="sync-source",
        description=f"Sync staged source {src} into {app.app_dir}",
        # Same defense for rsync: `--` before the positional source stops a
        # value like `-e ssh:evil` being parsed as an rsync option.
        command=_as_deploy(
            app.deploy_user, f'rsync -a --delete -- "{src}/" {app.app_dir}/'
        ),
    )


def plan_app_deploy(app: AppSpec) -> DeployPlan:
    """Emit the ordered plan that deploys ``app`` and serves it over HTTPS.

    Assumes :func:`omniagentos.deploy.bootstrap.plan_server_bootstrap` already
    ran on the host (Caddy, the runtime, the deploy user and ``/srv/apps`` all
    exist). Re-running is safe: every step is a whole-file write, a
    fast-forward, or a restart.
    """
    steps: list[DeployStep] = [
        DeployStep(
            step_id="app-dir",
            description=f"Create {app.app_dir} owned by {app.deploy_user}",
            command=(
                f"mkdir -p {app.app_dir} && "
                f"chown {app.deploy_user}:{app.deploy_user} {app.app_dir}"
            ),
        ),
        DeployStep(
            step_id="env-dir",
            description="Create the env directory the secrets lane writes into",
            command=(
                f"mkdir -p {ENV_DIR} && "
                f"chown root:{app.deploy_user} {ENV_DIR} && "
                f"chmod 750 {ENV_DIR}"
            ),
        ),
        _source_step(app),
    ]

    if app.build_cmd.strip():
        steps.append(
            DeployStep(
                step_id="build",
                description=f"Build the app: {app.build_cmd}",
                command=_as_deploy(
                    app.deploy_user, f"cd {app.app_dir} && {app.build_cmd}"
                ),
            )
        )

    steps += [
        DeployStep(
            step_id="systemd-unit",
            description=f"Write {app.unit_path} running as {app.deploy_user}",
            command=(
                f"install -m 644 -o root -g root /dev/stdin {app.unit_path} "
                "<<'OMNI_UNIT'\n"
                f"{render_systemd_unit(app)}"
                "OMNI_UNIT"
            ),
        ),
        DeployStep(
            step_id="systemd-reload",
            description="Reload the systemd manager configuration",
            command="systemctl daemon-reload",
        ),
        DeployStep(
            step_id="service-enable",
            description=f"Enable {app.unit_name} at boot",
            command=f"systemctl enable {app.unit_name}",
        ),
        DeployStep(
            step_id="service-restart",
            description=f"(Re)start {app.unit_name} to pick up the new build",
            command=f"systemctl restart {app.unit_name}",
        ),
        DeployStep(
            step_id="service-active",
            description=f"Confirm {app.unit_name} is running",
            command=(
                f"systemctl is-active --quiet {app.unit_name} || "
                f"(journalctl -u {app.unit_name} -n 50 --no-pager; exit 1)"
            ),
            mutating=False,
        ),
        DeployStep(
            step_id="caddy-site",
            description=(
                f"Write the Caddy site block proxying {app.domain} -> "
                f"127.0.0.1:{app.listen_port} (automatic HTTPS)"
            ),
            command=(
                f"install -m 644 -o root -g root /dev/stdin {app.site_path} "
                "<<'OMNI_CADDY_SITE'\n"
                f"{render_caddy_site(app)}"
                "OMNI_CADDY_SITE"
            ),
        ),
        DeployStep(
            step_id="caddy-validate",
            description="Validate the Caddy configuration before reloading it",
            command=f"caddy validate --adapter caddyfile --config {CADDY_FILE}",
            mutating=False,
        ),
        DeployStep(
            step_id="caddy-reload",
            description="Reload Caddy so it serves the new site and issues its certificate",
            # reload-or-restart, not reload: a plain reload errors with "Job type
            # reload is not applicable" when Caddy is stopped/crashed, which would
            # break an idempotent re-deploy after a failed prior run.
            command="systemctl reload-or-restart caddy",
        ),
        DeployStep(
            step_id="health-check",
            description=f"Verify https://{app.domain}/ answers 200",
            command=(
                "code=\"$(curl -sS -o /dev/null -w '%{http_code}' "
                "--retry 12 --retry-delay 5 --retry-all-errors --max-time 30 "
                f"https://{app.domain}/)\" && test \"$code\" = \"200\""
            ),
            mutating=False,
        ),
    ]

    return DeployPlan(
        name=f"deploy:{app.service_name}@{app.domain}",
        target_host=app.domain,
        steps=tuple(steps),
    )


__all__ = [
    "ENV_DIR",
    "env_file_path",
    "plan_app_deploy",
    "render_caddy_site",
    "render_systemd_unit",
]
