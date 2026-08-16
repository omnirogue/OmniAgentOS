"""Per-session allowlists for narrowly scoped production SSH credentials.

The grant is deliberately metadata, not a private key: one 0600 file records the
exact ``[user@]host`` destinations a live session may reach.  The sandbox uses it
to re-open only the matching identities from ``~/.ssh/config``; the shell policy
uses the same file to reject destinations outside the session's grant.

The store is separate from ``var/secrets`` and is deny-read by default in every
sandbox profile.  Only the owning session's literal grant file is re-opened.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from omniagentos.path_containment import inode_relative_parts
from omniagentos.sessions.token import _nearest_existing

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SSH_KEYS_LEAF = ("sessions", "ssh-keys")
_INVENTORY_LEAF = ("vault", "servers", "inventory.md")
#: Historical repo-root-relative paths. Each is BOTH the non-sim default AND the
#: sentinel meaning "the matching module-level name has not been overridden".
_LEGACY_SSH_KEYS_ROOT: Path = _REPO_ROOT.joinpath("var", *_SSH_KEYS_LEAF)
_LEGACY_SERVER_INVENTORY_PATH: Path = _REPO_ROOT.joinpath(*_INVENTORY_LEAF)

# Monkeypatchable defaults, resolved per call (matching the hook-token convention).
SSH_KEYS_ROOT: Path = _LEGACY_SSH_KEYS_ROOT
SERVER_INVENTORY_PATH: Path = _LEGACY_SERVER_INVENTORY_PATH


class SshKeyPathError(RuntimeError):
    """The SSH grant store or server inventory path cannot be resolved safely."""


def _is_legacy_path(candidate: Path, legacy: Path) -> bool | None:
    """Is ``candidate`` the production path ``legacy``? ``None`` means unknown.

    Mirrors ``sessions.token._is_legacy_token_path`` (see its docstring for why a
    string compare is unsound): identity is (ancestor directory by inode) AND (the
    exact remaining path components), so absent leaves never need to exist.
    """
    if _nearest_existing(candidate) is None:
        return None
    anchored = _nearest_existing(legacy)
    if anchored is None:
        return None
    anchor, legacy_parts = anchored
    parts = inode_relative_parts(candidate, anchor)
    if parts is None:
        return False
    return parts == legacy_parts


def _sim_var_root() -> Path | None:
    """The campaign var root under ``OMNIAGENTOS_SIM_MODE=1``, else ``None``.

    Mirrors the fail-closed sim resolution of ``sessions.token.token_path``: only
    the exact value ``"1"`` engages it (strict parsing of other values belongs to
    the future simgate), and a missing or relative var root raises rather than
    falling back to the production grant store or server inventory.
    """
    if os.environ.get("OMNIAGENTOS_SIM_MODE") != "1":
        return None
    raw = (os.environ.get("OMNIAGENTOS_VAR_DIR") or os.environ.get("OMNIAGENTOS_VAR") or "").strip()
    if not raw:
        raise SshKeyPathError(
            "OMNIAGENTOS_SIM_MODE=1 requires OMNIAGENTOS_VAR_DIR (or "
            "OMNIAGENTOS_VAR) to be set to an absolute campaign var root; "
            "refusing to fall back to the production SSH grant store and "
            "server inventory"
        )
    if not Path(raw).is_absolute():
        raise SshKeyPathError(
            "OMNIAGENTOS_SIM_MODE=1 requires OMNIAGENTOS_VAR_DIR (or "
            "OMNIAGENTOS_VAR) to be an absolute path "
            f"(got {raw!r}); refusing to fall back to the production SSH "
            "grant store and server inventory"
        )
    return Path(raw)


def _ssh_keys_root() -> Path:
    """The active grant store root, resolved fresh on every call.

    Outside sim mode this is byte-identical to the historical resolution: the
    ``__file__``-derived ``SSH_KEYS_ROOT`` (env vars deliberately ignored).
    """
    current = SSH_KEYS_ROOT
    if Path(current) != _LEGACY_SSH_KEYS_ROOT:
        # Explicit in-process override (tests, deliberate callers). Honour it
        # with no further checks -- even under sim mode.
        return Path(current)
    var_root = _sim_var_root()
    if var_root is None:
        return _LEGACY_SSH_KEYS_ROOT
    candidate = var_root.joinpath(*_SSH_KEYS_LEAF)
    # Fail closed: True (it IS production) and None (cannot tell) both refuse.
    if _is_legacy_path(candidate, _LEGACY_SSH_KEYS_ROOT) is not False:
        raise SshKeyPathError(
            "OMNIAGENTOS_SIM_MODE=1 refuses to use the production SSH grant "
            "store: OMNIAGENTOS_VAR_DIR / OMNIAGENTOS_VAR resolved to the "
            f"legacy path {_LEGACY_SSH_KEYS_ROOT}"
        )
    return candidate


def _server_inventory_path() -> Path:
    """The active server inventory, resolved fresh on every call.

    Under sim mode the inventory lives INSIDE the campaign var root (a campaign
    plants its own ``vault/servers/inventory.md`` there, or the readers fail
    closed to no hosts) so production SSH aliases can never seed sim grants.
    """
    current = SERVER_INVENTORY_PATH
    if Path(current) != _LEGACY_SERVER_INVENTORY_PATH:
        # Explicit in-process override (tests, deliberate callers). Honour it
        # with no further checks -- even under sim mode.
        return Path(current)
    var_root = _sim_var_root()
    if var_root is None:
        return _LEGACY_SERVER_INVENTORY_PATH
    candidate = var_root.joinpath(*_INVENTORY_LEAF)
    # Fail closed: True (it IS production) and None (cannot tell) both refuse.
    if _is_legacy_path(candidate, _LEGACY_SERVER_INVENTORY_PATH) is not False:
        raise SshKeyPathError(
            "OMNIAGENTOS_SIM_MODE=1 refuses to use the production server "
            "inventory: OMNIAGENTOS_VAR_DIR / OMNIAGENTOS_VAR resolved to the "
            f"legacy path {_LEGACY_SERVER_INVENTORY_PATH}"
        )
    return candidate


_HOST_ENTRY = re.compile(r"^(?:[A-Za-z0-9._-]+@)?(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])$")


def _safe_name(session_id: str) -> str:
    """Return a filename-safe session id, matching the hook-token convention."""
    cleaned = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    if not cleaned:
        raise ValueError("session_id must contain at least one safe character")
    return cleaned


def _validated_hosts(server_hosts: list[str]) -> list[str]:
    """Validate exact host entries and preserve their first-seen order."""
    result: list[str] = []
    for raw in server_hosts:
        host = raw.strip()
        if not host or not _HOST_ENTRY.fullmatch(host):
            raise ValueError(f"invalid SSH grant host: {raw!r}")
        if host not in result:
            result.append(host)
    return result


def ssh_key_grant_path(session_id: str) -> Path:
    """Path to ``session_id``'s SSH destination grant."""
    return _ssh_keys_root() / f"{_safe_name(session_id)}.grant"


def read_server_inventory_hosts(path: Path | None = None) -> list[str]:
    """Read exact aliases from the inventory's Summary Table, or fail closed.

    Free-form IPs and prose elsewhere in the document are deliberately ignored.
    Only the first column beneath ``## Summary Table`` is accepted, and every
    value must pass the same exact-host validation used by grant issuance.
    """
    source = path or _server_inventory_path()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    in_summary = False
    hosts: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "## Summary Table":
            in_summary = True
            continue
        if in_summary and stripped.startswith("## "):
            break
        if not in_summary or not stripped.startswith("|"):
            continue
        columns = [column.strip().strip("`") for column in stripped.strip("|").split("|")]
        if not columns or columns[0].casefold() == "alias" or set(columns[0]) <= {"-", ":"}:
            continue
        try:
            validated = _validated_hosts([columns[0]])
        except ValueError:
            return []
        if validated[0] not in hosts:
            hosts.append(validated[0])
    return hosts


def _parse_table(lines: list[str], heading: str) -> list[list[str]]:
    """Return the rows of the first markdown table found under ``heading``.

    A "row" is every ``|``-delimited line after the heading, up to (but not
    including) the next ``## `` heading, minus the header row and the
    ``|---|---|`` separator row. Malformed rows are skipped rather than
    raising -- this feeder is display-only (see ``read_server_inventory``);
    the fail-closed contract belongs to ``read_server_inventory_hosts`` alone.
    """
    rows: list[list[str]] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == heading:
            in_section = True
            continue
        if in_section and stripped.startswith("## "):
            break
        if not in_section or not stripped.startswith("|"):
            continue
        columns = [column.strip().strip("`") for column in stripped.strip("|").split("|")]
        if not columns or set(columns[0]) <= {"-", ":"}:
            continue
        if columns[0].casefold() in {"alias", "alias / destination", "host"}:
            continue
        rows.append(columns)
    return rows


def read_server_inventory(path: Path | None = None) -> list[dict[str, str]]:
    """Read the full server inventory (Summary Table + ephemeral + legacy sections).

    Unlike ``read_server_inventory_hosts`` (SSH-grant-eligible aliases only, fail-
    closed to ``[]``), this is a display feeder for the dashboard's Servers section:
    it best-effort parses every table in the document and tags each row with its
    section so the UI can group ACTIVE / EPHEMERAL / LEGACY-STALE. A malformed row
    here is skipped, never treated as a security failure -- nothing here is used to
    authorize an SSH destination.
    """
    source = path or _server_inventory_path()
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    servers: list[dict[str, str]] = []

    for columns in _parse_table(lines, "## Summary Table"):
        if len(columns) < 7:
            continue
        alias, ip, user, key, status, runs, sites = columns[:7]
        servers.append(
            {
                "alias": alias,
                "host": ip,
                "user": user,
                "key": key,
                "status": status,
                "purpose": runs,
                "sites": sites,
            }
        )

    for columns in _parse_table(lines, "## Ephemeral Servers"):
        if len(columns) < 6:
            continue
        alias, host, user, key, status, runs = columns[:6]
        servers.append(
            {
                "alias": alias,
                "host": host,
                "user": user,
                "key": key,
                "status": status,
                "purpose": runs,
                "sites": "",
            }
        )

    # The "Legacy / Stale Servers" section holds two differently-shaped tables:
    # one for boxes with no ~/.ssh/config alias at all (5 columns), one for boxes
    # that have an alias but are unreachable/unused (7 columns, same shape as the
    # Summary Table). _parse_table returns both intermixed; disambiguate by width.
    for columns in _parse_table(lines, "## Legacy / Stale Servers"):
        if len(columns) == 5:
            host, user, key, status, notes = columns[:5]
            servers.append(
                {
                    "alias": host,
                    "host": host,
                    "user": user,
                    "key": key,
                    "status": status,
                    "purpose": notes,
                    "sites": "",
                }
            )
        elif len(columns) >= 7:
            alias, ip, user, key, status, runs, sites = columns[:7]
            servers.append(
                {
                    "alias": alias,
                    "host": ip,
                    "user": user,
                    "key": key,
                    "status": status,
                    "purpose": runs,
                    "sites": sites,
                }
            )

    return servers


def issue_ssh_key_grant(session_id: str, server_hosts: list[str]) -> str:
    """Create or replace a 0600 grant and return its path as a string.

    Validation happens before the file is touched, so a malformed entry cannot
    leave a partially widened grant behind.
    """
    hosts = _validated_hosts(server_hosts)
    path = ssh_key_grant_path(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        if hosts:
            handle.write("\n".join(hosts) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)


def read_ssh_key_grant(session_id: str) -> list[str]:
    """Read validated allowed destinations, or return an empty list if unavailable."""
    try:
        lines = ssh_key_grant_path(session_id).read_text(encoding="utf-8").splitlines()
        return _validated_hosts([line for line in lines if line.strip()])
    except (OSError, ValueError):
        return []


def revoke_ssh_key_grant(session_id: str) -> None:
    """Best-effort, idempotent deletion on session terminalization."""
    try:
        ssh_key_grant_path(session_id).unlink(missing_ok=True)
    except OSError:
        pass


__all__ = [
    "SSH_KEYS_ROOT",
    "SERVER_INVENTORY_PATH",
    "SshKeyPathError",
    "issue_ssh_key_grant",
    "read_server_inventory",
    "read_server_inventory_hosts",
    "read_ssh_key_grant",
    "revoke_ssh_key_grant",
    "ssh_key_grant_path",
]
