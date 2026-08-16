"""Conservative Claude-tool classification for bridge sessions.

The ``Bash`` branch delegates to the SINGLE shared shell classifier
(``omniagentos.policy.shell.classify_shell``) that the runner also uses, so the
two guardrail gates can never disagree about a shell command again (AC-policy).
The non-shell tool branches (Read/Write/Edit/WebFetch) live here because they are
specific to the Claude tool surface the Session Bridge sees.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from omniagentos.contracts import ActionClass
from omniagentos.path_containment import inode_relative_parts_anchored
from omniagentos.policy.secrets import (
    tool_input_references_secret,
    tool_input_write_references_secret,
)
from omniagentos.policy.shell import classify_shell

_READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep", "LS", "NotebookRead"})
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
_WRITE_PATH_KEYS: dict[str, tuple[str, ...]] = {
    "Write": ("file_path",),
    "Edit": ("file_path",),
    "MultiEdit": ("file_path",),
    "NotebookEdit": ("notebook_path", "file_path"),
}


def action_hash(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Return the frozen tool-call identity used by hook evaluation and resume."""

    canonical = json.dumps(tool_input, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256((tool_name + canonical).encode()).hexdigest()


def _path_is_within_project(raw_path: object, project_dir: str) -> bool:
    if not isinstance(raw_path, str) or not raw_path.strip() or not project_dir:
        return False
    try:
        project = Path(project_dir).expanduser().resolve(strict=True)
        if not project.is_dir():
            return False
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            candidate = project / candidate
        resolved = candidate.resolve(strict=False)
        return inode_relative_parts_anchored(resolved, project) is not None
    except (OSError, RuntimeError, ValueError):
        return False


def _write_target_references_secret(tool_input: dict[str, Any], project_dir: str) -> bool:
    """True when a Write/Edit target lands INSIDE a REGISTERED secret store.

    WRITE/containment refusal is scoped to the registered secret DIRS (``~/.ssh``,
    ``~/.config/omni``, ``<repo>/var/secrets``, a campaign's secrets dir, ...): a
    write INTO a real credential store still HARD-STOPS, but a write to a merely
    secret-NAMED file OUTSIDE every store (``dashboard/.env.local``, an in-project
    fixture) is allowed -- the earlier "match a secret basename anywhere for writes"
    rule over-refused ~201 legitimate in-project writes. READ protection for those
    distinctive basenames is unchanged (see the Read branch / ``references_secret``).

    Two passes over the SHARED registry: the scoped pass (``project_dir``) catches a
    relative path that resolves into a secret DIRECTORY; the scope-independent pass
    (``None``) catches an ABSOLUTE store path even when the hook has re-classified a
    granted root AS ``project_dir`` (the P3 downgrade seam), so a write into a real
    store cannot be laundered to auto there either.
    """
    return tool_input_write_references_secret(
        tool_input, project_dir
    ) or tool_input_write_references_secret(tool_input, None)


def _webfetch_host(tool_input: dict[str, Any]) -> str | None:
    """Best-effort extraction of the target host from a WebFetch input.

    The url may or may not carry a scheme (``example.test/x`` vs
    ``https://example.test/x``); both resolve to the same host. Returns None when
    no host can be parsed."""
    raw = tool_input.get("url")
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = raw.strip()
    host = urlsplit(candidate).hostname
    if not host:
        # No scheme -> urlsplit sees a bare path. Reparse as a network location.
        host = urlsplit("//" + candidate).hostname
    return host or None


def _webfetch_host_is_local(host: str) -> bool:
    """True for loopback/link-local/private/reserved hosts (SSRF surface).

    Covers localhost, 127.0.0.0/8, ::1, 169.254.0.0/16 (link-local), 10.0.0.0/8,
    192.168.0.0/16, 172.16.0.0/12 and their IPv6 equivalents. A plain external
    hostname (non-IP) is NOT treated as local."""
    name = host.strip().lower().rstrip(".")
    if not name:
        return True
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return (
        ip.is_loopback or ip.is_link_local or ip.is_private or ip.is_reserved or ip.is_unspecified
    )


def _classify_webfetch(tool_input: dict[str, Any]) -> ActionClass:
    """Classify an outbound WebFetch (SEC-003).

    An outbound request is never a plain read: it can exfiltrate a just-read
    secret. A GET to an external host is EXTERNAL_REVERSIBLE (requires approval);
    a non-GET method or a request whose host is loopback/link-local/private is
    CONSEQUENTIAL (SSRF against the local :8485 control plane). A missing or
    unparseable url is ambiguous and fails closed to CONSEQUENTIAL."""
    method = tool_input.get("method", "GET")
    if not (isinstance(method, str) and method.upper() == "GET"):
        return ActionClass.CONSEQUENTIAL
    host = _webfetch_host(tool_input)
    if host is None:
        return ActionClass.CONSEQUENTIAL
    if _webfetch_host_is_local(host):
        return ActionClass.CONSEQUENTIAL
    return ActionClass.EXTERNAL_REVERSIBLE


def classify_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    project_dir: str,
    ssh_key_grant_session_id: str | None = None,
) -> ActionClass:
    """Classify a Claude tool call using a positive allowlist.

    Ambiguous or malformed inputs are consequential. Only known reads and writes
    whose real paths are contained by the project are allowed to classify lower.
    A Bash command is routed to the shared shell classifier (deny-by-default),
    identical to the runner's command gate.
    """

    if not isinstance(tool_name, str) or not isinstance(tool_input, dict):
        return ActionClass.CONSEQUENTIAL
    if tool_name in _READ_ONLY_TOOLS:
        # A "read" is only benign if it is not reading a credential store. The
        # path/file_path/pattern arg is resolved with the SAME resolver the shell
        # classifier uses (shared registry), so a native Read/Grep/Glob/LS of
        # ~/.ssh/id_rsa, ~/.aws/credentials, connections.env, etc. HARD-STOPS
        # instead of auto-executing (AC-policy fix4 BLOCKER 1).
        if tool_input_references_secret(tool_input, project_dir):
            return ActionClass.IRREVERSIBLE
        return ActionClass.READ_ONLY
    if tool_name == "WebFetch":
        return _classify_webfetch(tool_input)
    if tool_name == "Bash":
        # The ONE shared classifier: interpreters, deletes, out-of-scope writes and
        # anything not provably read-only HARD-STOP (irreversible) in AUTO mode.
        return classify_shell(tool_input.get("command"), project_dir, ssh_key_grant_session_id)
    if tool_name in _WRITE_TOOLS:
        # A write INTO a registered secret store (~/.ssh, <repo>/var/secrets, ...)
        # HARD-STOPS even inside the project or a granted root, so it can never
        # downgrade to an auto-approved write -- closing the P3 gap where a
        # granted-root re-classification laundered a store write into
        # INTERNAL_REVERSIBLE. Scoped to the registered DIRS (not a secret basename
        # anywhere), so a legitimate in-project write to a secret-NAMED file is not
        # over-refused; READ protection for those basenames stays in the Read branch.
        if _write_target_references_secret(tool_input, project_dir):
            return ActionClass.IRREVERSIBLE
        keys = _WRITE_PATH_KEYS[tool_name]
        paths = [tool_input.get(key) for key in keys if key in tool_input]
        if len(paths) == 1 and _path_is_within_project(paths[0], project_dir):
            return ActionClass.INTERNAL_REVERSIBLE
        # A write whose real target is OUTSIDE the project's scoped dirs (or whose
        # path is ambiguous) is irreversible -- it can clobber a file the operator
        # never put in scope, so it hard-stops rather than auto-executing.
        return ActionClass.IRREVERSIBLE
    return ActionClass.CONSEQUENTIAL


__all__ = ["action_hash", "classify_tool"]
