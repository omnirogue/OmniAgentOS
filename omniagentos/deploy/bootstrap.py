"""Plan the one-time bootstrap of a freshly provisioned host.

The plan is IDEMPOTENT by construction: every mutating command is either
guarded by a probe (``command -v caddy >/dev/null 2>&1 || ...``) or is a
whole-file write / a naturally repeat-safe operation (``mkdir -p``, ``chown``,
``ufw allow``). Re-running the plan on an already-bootstrapped host is a no-op
and must never fail — that is what makes it safe for an agent to re-drive after
a partial failure.

Nothing here executes: it returns a :class:`DeployPlan` of remote commands.
"""

from __future__ import annotations

from omniagentos.deploy.contracts import (
    APPS_ROOT,
    CADDY_FILE,
    CADDY_SITES_DIR,
    DeployPlan,
    DeployStep,
    ServerSpec,
)

# Always present before anything else: TLS roots, a fetcher, the firewall, and
# the two tools the app-deploy plan needs to move code onto the box.
BASE_PACKAGES: tuple[str, ...] = (
    "ca-certificates",
    "curl",
    "gnupg",
    "ufw",
    "git",
    "rsync",
)

RUNTIME_PACKAGES: dict[str, tuple[str, ...]] = {
    "python": ("python3", "python3-venv", "python3-pip"),
    "node": ("nodejs",),
}

_CADDY_KEYRING = "/usr/share/keyrings/caddy-stable-archive-keyring.gpg"
_CADDY_LIST = "/etc/apt/sources.list.d/caddy-stable.list"
_NODE_KEYRING = "/usr/share/keyrings/nodesource.gpg"
_NODE_LIST = "/etc/apt/sources.list.d/nodesource.list"
_APT_INSTALL = "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends"


def _install_if_missing(packages: tuple[str, ...]) -> str:
    """apt install, guarded so an already-installed set is a no-op."""
    joined = " ".join(packages)
    return f"dpkg -s {joined} >/dev/null 2>&1 || ({_APT_INSTALL} {joined})"


def _caddyfile_body() -> str:
    """The root Caddyfile does nothing but import per-site blocks.

    Deliberately contains NO ``auto_https off``: automatic Let's Encrypt
    issuance is the entire reason Caddy is the proxy here.
    """
    return f"import {CADDY_SITES_DIR}/*.caddy\n"


def _runtime_steps(spec: ServerSpec) -> list[DeployStep]:
    if spec.runtime == "python":
        return [
            DeployStep(
                step_id="runtime-python",
                description="Install the Python 3 runtime, venv and pip",
                command=_install_if_missing(RUNTIME_PACKAGES["python"]),
            )
        ]
    # Node: Debian's own nodejs is too old for most apps, so use NodeSource.
    return [
        DeployStep(
            step_id="node-repo-key",
            description="Install the NodeSource apt signing key",
            command=(
                f"test -f {_NODE_KEYRING} || "
                "(curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key "
                f"| gpg --dearmor -o {_NODE_KEYRING})"
            ),
        ),
        DeployStep(
            step_id="node-repo-list",
            description="Register the NodeSource apt repository",
            command=(
                f"test -f {_NODE_LIST} || "
                f"(printf '%s\\n' 'deb [signed-by={_NODE_KEYRING}] "
                "https://deb.nodesource.com/node_22.x nodistro main' "
                f"> {_NODE_LIST} && apt-get update -qq)"
            ),
        ),
        DeployStep(
            step_id="runtime-node",
            description="Install the Node.js runtime",
            command=(
                "command -v node >/dev/null 2>&1 || "
                f"({_APT_INSTALL} {' '.join(RUNTIME_PACKAGES['node'])})"
            ),
        ),
    ]


def plan_server_bootstrap(spec: ServerSpec) -> DeployPlan:
    """Emit the idempotent plan that makes a fresh host able to serve HTTPS apps.

    Order matters and is load-bearing: SSH is allowed through the firewall
    BEFORE ``ufw`` is enabled (otherwise the bootstrap locks itself out), and
    the app directory exists before any deploy plan runs against it.
    """
    steps: list[DeployStep] = [
        DeployStep(
            step_id="apt-update",
            description="Refresh the apt package index",
            command="apt-get update -qq",
        ),
        DeployStep(
            step_id="base-packages",
            description=f"Install base packages: {', '.join(BASE_PACKAGES)}",
            command=_install_if_missing(BASE_PACKAGES),
        ),
        DeployStep(
            step_id="caddy-repo-key",
            description="Install the Caddy apt signing key",
            command=(
                f"test -f {_CADDY_KEYRING} || "
                "(curl -fsSL "
                "'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' "
                f"| gpg --dearmor -o {_CADDY_KEYRING})"
            ),
        ),
        DeployStep(
            step_id="caddy-repo-list",
            description="Register the Caddy stable apt repository",
            command=(
                f"test -f {_CADDY_LIST} || "
                f"(printf '%s\\n' 'deb [signed-by={_CADDY_KEYRING}] "
                "https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main' "
                f"> {_CADDY_LIST} && apt-get update -qq)"
            ),
        ),
        DeployStep(
            step_id="install-caddy",
            description="Install Caddy (provides automatic Let's Encrypt HTTPS)",
            command=f"command -v caddy >/dev/null 2>&1 || ({_APT_INSTALL} caddy)",
        ),
        DeployStep(
            step_id="caddy-sites-dir",
            description="Create the per-site Caddy config directory",
            command=f"mkdir -p {CADDY_SITES_DIR}",
        ),
        DeployStep(
            step_id="caddy-log-dir",
            description="Create the Caddy log directory site blocks write into",
            command="mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy",
        ),
        DeployStep(
            step_id="caddy-root-config",
            description="Write the root Caddyfile that imports per-site blocks",
            command=(
                f"grep -qF 'import {CADDY_SITES_DIR}/*.caddy' {CADDY_FILE} 2>/dev/null || "
                f"cat > {CADDY_FILE} <<'OMNI_CADDYFILE'\n"
                f"{_caddyfile_body()}"
                "OMNI_CADDYFILE"
            ),
        ),
        *_runtime_steps(spec),
    ]

    if spec.packages:
        steps.append(
            DeployStep(
                step_id="extra-packages",
                description=f"Install requested packages: {', '.join(spec.packages)}",
                command=_install_if_missing(tuple(spec.packages)),
            )
        )

    steps += [
        DeployStep(
            step_id="deploy-user",
            description=f"Create the unprivileged {spec.deploy_user} account",
            command=(
                f"id -u {spec.deploy_user} >/dev/null 2>&1 || "
                f"useradd --system --create-home --home-dir /home/{spec.deploy_user} "
                f"--shell /usr/sbin/nologin {spec.deploy_user}"
            ),
        ),
        DeployStep(
            step_id="apps-root",
            description=f"Create {APPS_ROOT} owned by {spec.deploy_user}",
            command=(
                f"mkdir -p {APPS_ROOT} && "
                f"chown {spec.deploy_user}:{spec.deploy_user} {APPS_ROOT} && "
                f"chmod 755 {APPS_ROOT}"
            ),
        ),
        # SSH first: enabling ufw before allowing SSH bricks the box.
        DeployStep(
            step_id="firewall-ssh",
            description="Allow SSH through the firewall (before enabling it)",
            command="ufw allow OpenSSH",
        ),
        DeployStep(
            step_id="firewall-http",
            description="Allow HTTP/80 (ACME http-01 challenge + redirect to HTTPS)",
            command="ufw allow 80/tcp",
        ),
        DeployStep(
            step_id="firewall-https",
            description="Allow HTTPS/443",
            command="ufw allow 443/tcp",
        ),
        DeployStep(
            step_id="firewall-enable",
            description="Enable the firewall if it is not already active",
            command="ufw status | grep -q 'Status: active' || ufw --force enable",
        ),
        DeployStep(
            step_id="caddy-enable",
            description="Enable the Caddy service at boot",
            command="systemctl is-enabled --quiet caddy || systemctl enable caddy",
        ),
        DeployStep(
            step_id="caddy-start",
            description="Start Caddy if it is not already running",
            command="systemctl is-active --quiet caddy || systemctl start caddy",
        ),
        DeployStep(
            step_id="verify-bootstrap",
            description="Verify Caddy and the runtime are present and healthy",
            command=(
                "caddy version && systemctl is-active --quiet caddy && "
                + ("python3 --version" if spec.runtime == "python" else "node --version")
            ),
            mutating=False,
        ),
    ]

    return DeployPlan(
        name=f"bootstrap:{spec.host}",
        target_host=spec.host,
        steps=tuple(steps),
    )


__all__ = ["BASE_PACKAGES", "RUNTIME_PACKAGES", "plan_server_bootstrap"]
