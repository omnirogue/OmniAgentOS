"""Coverage for the server inventory readers in ``omniagentos.sessions.ssh_keys``.

Two readers exist over the same document, with deliberately different failure modes:

* ``read_server_inventory_hosts`` is the SSH-grant-eligible allowlist. It reads ONLY
  the ``## Summary Table`` and fails closed to ``[]`` on any malformed row -- a bug
  here zeros every session's SSH grant, so these tests are adversarial.
* ``read_server_inventory`` is a display-only feeder for the dashboard's Servers
  section. It best-effort parses every table in the document (Summary Table +
  Ephemeral + Legacy/Stale) and is never used to authorize anything.
"""

from __future__ import annotations

from pathlib import Path

from omniagentos.sessions import ssh_keys

# The alias set that MUST still be present once the fleet's real inventory.md is
# updated -- the active fleet documented before this change. Losing any of these
# would silently zero SSH grants for sessions that expect to reach them.
EXISTING_ACTIVE_ALIASES = [
    "initech-roi-calculator",
    "acmeuniapp",
    "acmeuni",
    "acmeunistudio",
    "initech-crmnew",
    "acmeuniunlimited.com",
    "initechapp.com",
    "acmeuni-claude",
    "agentproacademy",
]


def test_real_inventory_summary_table_still_parses() -> None:
    """The live vault/servers/inventory.md must still parse under the strict reader.

    This is the regression this test exists to catch: a malformed row in the real
    document silently zeroes every session's SSH grant (fail-closed to ``[]``).
    """
    hosts = ssh_keys.read_server_inventory_hosts()
    assert hosts, "the real inventory must yield at least one SSH-grant-eligible alias"
    for alias in EXISTING_ACTIVE_ALIASES:
        assert alias in hosts, f"{alias} dropped out of the Summary Table"


def test_real_inventory_hosts_excludes_ephemeral_and_legacy_sections() -> None:
    """Ephemeral (RunPod pods) and legacy/stale boxes must never become SSH grants."""
    hosts = ssh_keys.read_server_inventory_hosts()
    for excluded in (
        "RunPod LipForcing pod",
        "RunPod Qwen3.5-122B pod",
        "legacy-site-a",
        "tryacmeuni",
    ):
        assert excluded not in hosts


def test_real_inventory_full_reader_returns_all_sections() -> None:
    """The display feeder sees the active fleet, ephemeral, and legacy/stale rows."""
    servers = ssh_keys.read_server_inventory()
    aliases = {row["alias"] for row in servers}
    for alias in EXISTING_ACTIVE_ALIASES:
        assert alias in aliases

    statuses = {row["alias"]: row["status"] for row in servers}
    assert statuses["initech-roi-calculator"].startswith("ACTIVE")
    assert any(s.startswith("EPHEMERAL") for s in statuses.values())
    assert any(s.startswith("LEGACY-STALE") for s in statuses.values())

    # Never key material -- only a filename/path.
    for row in servers:
        assert row["key"].startswith("~/.ssh/")

    # Every row is fully shaped.
    for row in servers:
        assert set(row) == {"alias", "host", "user", "key", "status", "purpose", "sites"}


def test_full_reader_skips_malformed_rows_without_raising(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.md"
    inventory.write_text(
        """# Server Inventory

## Summary Table

| Alias | IP | User | Key | Status | Runs | Sites |
|---|---|---|---|---|---|---|
| prod-a | 192.0.2.10 | root | ~/.ssh/example_a.pem | ACTIVE | web app | prod-a.com |
| too-short-row | only-two-columns |

## Ephemeral Servers

| Alias / Destination | Host | User | Key | Status | Runs |
|---|---|---|---|---|---|---|
| pod-1 | 203.0.113.5:2222 | root | ~/.ssh/runpod_codex_ed25519 | EPHEMERAL | gpu job |

## Legacy / Stale Servers

**No alias:**

| Host | User | Key | Status | Notes |
|---|---|---|---|---|
| 203.0.113.9 | root | ~/.ssh/example_a.pem | LEGACY-STALE | unknown |
""",
        encoding="utf-8",
    )
    servers = ssh_keys.read_server_inventory(inventory)
    aliases = [row["alias"] for row in servers]
    assert "prod-a" in aliases
    assert "too-short-row" not in aliases  # malformed row skipped, not raised
    assert "pod-1" in aliases
    assert "203.0.113.9" in aliases


def test_full_reader_returns_empty_list_for_missing_file(tmp_path: Path) -> None:
    assert ssh_keys.read_server_inventory(tmp_path / "missing.md") == []
