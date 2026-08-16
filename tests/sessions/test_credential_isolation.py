"""Isolation coverage for the three credential surfaces beyond the session token.

These tests fail against the historical import-time ``__file__``-derived paths:
under ``OMNIAGENTOS_SIM_MODE=1`` the hook-token store, the SSH grant store /
server inventory, and the bridge-settings writer all silently pointed at the
operator checkout. They mirror ``tests/sessions/test_token_isolation.py`` and
the landed ``sessions.token`` fail-closed pattern.

CANARY DISCIPLINE: the (a)-shaped tests plant uniquely-named canary files at the
real legacy locations inside this worktree's ``var/`` tree (transient state,
never committed) and remove ONLY what they created. The git-tracked
``vault/servers/inventory.md`` is never written -- only hashed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import stat
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from omniagentos.scope.paths import resolve_into_realm
from omniagentos.sessions import hook_token, install, ssh_keys

_ENV_VARS = ("OMNIAGENTOS_SIM_MODE", "OMNIAGENTOS_VAR_DIR", "OMNIAGENTOS_VAR")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _sim_env(monkeypatch: pytest.MonkeyPatch, var_dir: str) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", var_dir)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)


@contextlib.contextmanager
def _planted_canary(path: Path, content: bytes) -> Iterator[bytes]:
    """Plant a canary at the legacy ``path``; remove ONLY what was created.

    Missing ancestor directories are recorded deepest-first before creation so
    teardown can rmdir exactly the directories this test introduced, leaving the
    worktree's real ``var/`` tree byte-identical to how it was found.
    """
    created_dirs: list[Path] = []
    probe = path.parent
    while not probe.exists():
        created_dirs.append(probe)
        probe = probe.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    assert not path.exists(), f"canary collision at {path}"
    path.write_bytes(content)
    try:
        yield content
    finally:
        path.unlink(missing_ok=True)
        for directory in created_dirs:
            try:
                directory.rmdir()
            except OSError:
                break


def _unique_session_id() -> str:
    return f"sescanary{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# (a) sim + VAR_DIR set: resolve under the campaign var root, legacy untouched
# ---------------------------------------------------------------------------


def test_sim_mode_hook_tokens_live_under_campaign_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    _sim_env(monkeypatch, str(campaign))

    session_id = _unique_session_id()
    legacy_file = hook_token._LEGACY_HOOK_TOKENS_ROOT / f"{session_id}.token"
    canary_value = f"canary-{uuid.uuid4().hex}"
    with _planted_canary(legacy_file, f"{canary_value}\n".encode()):
        canary_hash = _sha(legacy_file)

        value = hook_token.issue_hook_token(session_id)
        path = hook_token.hook_token_path(session_id)
        assert path == campaign / "sessions" / "hook-tokens" / f"{session_id}.token"
        assert resolve_into_realm(str(path), str(campaign)) is not None
        assert path.is_file()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert hook_token.read_hook_token(session_id) == value
        assert hook_token.verify_hook_token(session_id, value) is True
        # Decisive: the legacy file's value must NOT verify -- proof the legacy
        # store was never consulted under sim mode.
        assert hook_token.verify_hook_token(session_id, canary_value) is False
        assert _sha(legacy_file) == canary_hash

        hook_token.revoke_hook_token(session_id)
        assert not path.exists()
        # Revoke targets the campaign store, never the legacy file.
        assert _sha(legacy_file) == canary_hash


def test_sim_mode_ssh_grants_live_under_campaign_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    _sim_env(monkeypatch, str(campaign))

    session_id = _unique_session_id()
    legacy_file = ssh_keys._LEGACY_SSH_KEYS_ROOT / f"{session_id}.grant"
    with _planted_canary(legacy_file, b"canaryhost\n"):
        canary_hash = _sha(legacy_file)

        # Campaign store is empty: the legacy canary grant must NOT be readable.
        assert ssh_keys.read_ssh_key_grant(session_id) == []

        granted = Path(ssh_keys.issue_ssh_key_grant(session_id, ["deploy@campaignhost"]))
        assert granted == campaign / "sessions" / "ssh-keys" / f"{session_id}.grant"
        assert resolve_into_realm(str(granted), str(campaign)) is not None
        assert stat.S_IMODE(granted.stat().st_mode) == 0o600
        assert ssh_keys.read_ssh_key_grant(session_id) == ["deploy@campaignhost"]
        assert _sha(legacy_file) == canary_hash

        ssh_keys.revoke_ssh_key_grant(session_id)
        assert not granted.exists()
        assert _sha(legacy_file) == canary_hash


def test_sim_mode_inventory_reads_campaign_copy_not_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    inventory = campaign / "vault" / "servers" / "inventory.md"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        "## Summary Table\n"
        "\n"
        "| Alias | IP | User | Key | Status | Runs | Sites |\n"
        "|---|---|---|---|---|---|---|\n"
        "| simhost1 | 10.0.0.1 | root | none | active | sim | none |\n",
        encoding="utf-8",
    )
    production = ssh_keys._LEGACY_SERVER_INVENTORY_PATH
    assert production.is_file(), "git-tracked vault/servers/inventory.md must exist"
    production_hash = _sha(production)

    _sim_env(monkeypatch, str(campaign))

    assert ssh_keys._server_inventory_path() == inventory
    sim_hosts = ssh_keys.read_server_inventory_hosts()
    assert sim_hosts == ["simhost1"]
    # Explicit-path calls bypass resolution (mirror of the TOKEN_PATH override).
    production_hosts = ssh_keys.read_server_inventory_hosts(production)
    assert production_hosts, "the real inventory must still parse"
    assert not set(sim_hosts) & set(production_hosts)
    sim_rows = ssh_keys.read_server_inventory()
    assert [row["alias"] for row in sim_rows] == ["simhost1"]
    # Read-only surface, but pin it anyway: production inventory byte-identical.
    assert _sha(production) == production_hash


def test_sim_mode_absent_campaign_inventory_yields_no_hosts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No campaign inventory means NO hosts -- never the production aliases."""
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    _sim_env(monkeypatch, str(campaign))

    assert ssh_keys.read_server_inventory_hosts() == []
    assert ssh_keys.read_server_inventory() == []


def test_sim_mode_bridge_settings_written_under_campaign_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    _sim_env(monkeypatch, str(campaign))

    legacy = install._LEGACY_BRIDGE_SETTINGS_PATH
    with contextlib.ExitStack() as stack:
        if not legacy.exists():
            stack.enter_context(_planted_canary(legacy, b'{"canary": true}\n'))
        legacy_hash = _sha(legacy)

        path = install.bridge_settings_path()
        assert path == campaign / "sessions" / "bridge-settings.json"
        assert resolve_into_realm(str(path), str(campaign)) is not None
        content = json.loads(path.read_text(encoding="utf-8"))
        # T-CODE-003: the sim copy must keep the gate-everything matcher.
        assert [entry["matcher"] for entry in content["hooks"]["PreToolUse"]] == [".*"]
        assert [entry["matcher"] for entry in content["hooks"]["PostToolUse"]] == [".*"]
        assert _sha(legacy) == legacy_hash

        # Reader/writer stay paired: the supervisor resolves through the installer.
        from omniagentos.sessions.supervisor import bridge_settings_path as supervisor_bsp

        assert Path(supervisor_bsp()) == path
        assert _sha(legacy) == legacy_hash


# ---------------------------------------------------------------------------
# (b) sim + VAR_DIR missing/relative/legacy: fail closed, nothing written
# ---------------------------------------------------------------------------


def test_sim_mode_missing_var_dir_raises_per_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    surfaces: list[tuple[type[RuntimeError], Callable[[], object], Path]] = [
        (
            hook_token.HookTokenPathError,
            lambda: hook_token.hook_token_path("ses-x"),
            hook_token._LEGACY_HOOK_TOKENS_ROOT,
        ),
        (
            hook_token.HookTokenPathError,
            lambda: hook_token.issue_hook_token("ses-x"),
            hook_token._LEGACY_HOOK_TOKENS_ROOT,
        ),
        (
            ssh_keys.SshKeyPathError,
            lambda: ssh_keys.ssh_key_grant_path("ses-x"),
            ssh_keys._LEGACY_SSH_KEYS_ROOT,
        ),
        (
            ssh_keys.SshKeyPathError,
            lambda: ssh_keys.issue_ssh_key_grant("ses-x", ["host"]),
            ssh_keys._LEGACY_SSH_KEYS_ROOT,
        ),
        (
            ssh_keys.SshKeyPathError,
            ssh_keys.read_server_inventory_hosts,
            ssh_keys._LEGACY_SERVER_INVENTORY_PATH,
        ),
        (
            install.BridgeSettingsPathError,
            install.bridge_settings_path,
            install._LEGACY_BRIDGE_SETTINGS_PATH,
        ),
    ]
    for error_type, call, legacy in surfaces:
        existed_before = legacy.exists()
        hash_before = _sha(legacy) if legacy.is_file() else None
        with pytest.raises(error_type) as exc_info:
            call()
        message = str(exc_info.value)
        assert "OMNIAGENTOS_SIM_MODE" in message
        assert "OMNIAGENTOS_VAR_DIR" in message or "OMNIAGENTOS_VAR" in message
        # Error message must not present the legacy path as a returned value.
        assert str(legacy) not in message
        # Failed resolution must not create (or rewrite) anything on the legacy path.
        assert legacy.exists() is existed_before
        if hash_before is not None:
            assert _sha(legacy) == hash_before


def test_sim_mode_relative_var_dir_raises_per_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", "relative/campaign/var")
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    with pytest.raises(hook_token.HookTokenPathError) as hook_exc:
        hook_token.hook_token_path("ses-x")
    assert "OMNIAGENTOS_SIM_MODE" in str(hook_exc.value)
    with pytest.raises(ssh_keys.SshKeyPathError) as ssh_exc:
        ssh_keys.ssh_key_grant_path("ses-x")
    assert "OMNIAGENTOS_SIM_MODE" in str(ssh_exc.value)
    with pytest.raises(install.BridgeSettingsPathError) as bridge_exc:
        install.bridge_settings_path()
    assert "OMNIAGENTOS_SIM_MODE" in str(bridge_exc.value)


def test_sim_mode_var_dir_pointing_at_legacy_var_raises_per_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CRITICAL SAFETY: pure resolution only -- nothing is created or read."""
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(hook_token._REPO_ROOT / "var"))
    with pytest.raises(hook_token.HookTokenPathError) as hook_exc:
        hook_token.hook_token_path("ses-x")
    message = str(hook_exc.value)
    assert "OMNIAGENTOS_SIM_MODE" in message
    assert "production" in message.lower() or "legacy" in message.lower()

    with pytest.raises(ssh_keys.SshKeyPathError) as ssh_exc:
        ssh_keys.ssh_key_grant_path("ses-x")
    message = str(ssh_exc.value)
    assert "OMNIAGENTOS_SIM_MODE" in message
    assert "production" in message.lower() or "legacy" in message.lower()

    legacy_bridge = install._LEGACY_BRIDGE_SETTINGS_PATH
    existed_before = legacy_bridge.exists()
    hash_before = _sha(legacy_bridge) if legacy_bridge.is_file() else None
    with pytest.raises(install.BridgeSettingsPathError) as bridge_exc:
        install.bridge_settings_path()
    message = str(bridge_exc.value)
    assert "OMNIAGENTOS_SIM_MODE" in message
    assert "production" in message.lower() or "legacy" in message.lower()
    # The refusal fires BEFORE the write: the production file is untouched.
    assert legacy_bridge.exists() is existed_before
    if hash_before is not None:
        assert _sha(legacy_bridge) == hash_before

    # The inventory's legacy anchor is the repo root itself, not repo/var.
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(ssh_keys._REPO_ROOT))
    with pytest.raises(ssh_keys.SshKeyPathError) as inventory_exc:
        ssh_keys._server_inventory_path()
    message = str(inventory_exc.value)
    assert "OMNIAGENTOS_SIM_MODE" in message
    assert "production" in message.lower() or "legacy" in message.lower()


def test_sim_mode_reader_explicit_path_bypasses_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit ``path`` argument is an intentional caller decision."""
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    inventory = tmp_path / "inventory.md"
    inventory.write_text(
        "## Summary Table\n\n| Alias |\n|---|\n| explicithost |\n", encoding="utf-8"
    )
    assert ssh_keys.read_server_inventory_hosts(inventory) == ["explicithost"]


# ---------------------------------------------------------------------------
# (c) non-sim: byte-identical to the historical __file__-derived resolution
# ---------------------------------------------------------------------------


def test_non_sim_paths_are_exactly_the_file_derived_legacy_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    repo_root = Path(hook_token.__file__).resolve().parents[2]

    assert (
        hook_token.hook_token_path("ses-abc")
        == repo_root / "var" / "sessions" / "hook-tokens" / "ses-abc.token"
    )
    assert (
        ssh_keys.ssh_key_grant_path("ses-abc")
        == repo_root / "var" / "sessions" / "ssh-keys" / "ses-abc.grant"
    )
    assert ssh_keys._server_inventory_path() == repo_root / "vault" / "servers" / "inventory.md"
    assert (
        install._bridge_settings_target() == repo_root / "var" / "sessions" / "bridge-settings.json"
    )


def test_non_sim_ignores_var_dir_env_for_all_three_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike sessions.token, these surfaces never honoured env vars: outside sim
    mode the resolution must stay byte-identical to the historical behaviour even
    with OMNIAGENTOS_VAR_DIR set."""
    monkeypatch.delenv("OMNIAGENTOS_SIM_MODE", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path))

    assert hook_token.hook_token_path("ses-abc") == (
        hook_token._LEGACY_HOOK_TOKENS_ROOT / "ses-abc.token"
    )
    assert ssh_keys.ssh_key_grant_path("ses-abc") == (
        ssh_keys._LEGACY_SSH_KEYS_ROOT / "ses-abc.grant"
    )
    assert ssh_keys._server_inventory_path() == ssh_keys._LEGACY_SERVER_INVENTORY_PATH
    assert install._bridge_settings_target() == install._LEGACY_BRIDGE_SETTINGS_PATH


@pytest.mark.parametrize("value", ["0", "true", "yes", " 1"])
def test_sim_mode_values_other_than_exactly_one_keep_todays_behaviour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Strict parsing of other values belongs to the future simgate, not here."""
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", value)
    monkeypatch.setenv("OMNIAGENTOS_VAR_DIR", str(tmp_path))
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    assert hook_token.hook_token_path("ses-abc") == (
        hook_token._LEGACY_HOOK_TOKENS_ROOT / "ses-abc.token"
    )
    assert ssh_keys.ssh_key_grant_path("ses-abc") == (
        ssh_keys._LEGACY_SSH_KEYS_ROOT / "ses-abc.grant"
    )
    assert ssh_keys._server_inventory_path() == ssh_keys._LEGACY_SERVER_INVENTORY_PATH
    assert install._bridge_settings_target() == install._LEGACY_BRIDGE_SETTINGS_PATH


# ---------------------------------------------------------------------------
# In-process overrides stay first-class (the ~10 monkeypatching test modules)
# ---------------------------------------------------------------------------


def test_in_process_override_wins_even_under_sim_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIAGENTOS_SIM_MODE", "1")
    monkeypatch.delenv("OMNIAGENTOS_VAR_DIR", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_VAR", raising=False)

    monkeypatch.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")
    assert hook_token.hook_token_path("ses-x") == tmp_path / "hook-tokens" / "ses-x.token"

    monkeypatch.setattr(ssh_keys, "SSH_KEYS_ROOT", tmp_path / "ssh-keys")
    assert ssh_keys.ssh_key_grant_path("ses-x") == tmp_path / "ssh-keys" / "ses-x.grant"

    monkeypatch.setattr(ssh_keys, "SERVER_INVENTORY_PATH", tmp_path / "inventory.md")
    assert ssh_keys._server_inventory_path() == tmp_path / "inventory.md"


def test_monkeypatch_teardown_restores_lazy_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    with pytest.MonkeyPatch.context() as context:
        context.setattr(hook_token, "HOOK_TOKENS_ROOT", tmp_path / "hook-tokens")
        context.setattr(ssh_keys, "SSH_KEYS_ROOT", tmp_path / "ssh-keys")
        assert hook_token.hook_token_path("ses-x").parent == tmp_path / "hook-tokens"
        assert ssh_keys.ssh_key_grant_path("ses-x").parent == tmp_path / "ssh-keys"

    assert hook_token.HOOK_TOKENS_ROOT == hook_token._LEGACY_HOOK_TOKENS_ROOT
    assert ssh_keys.SSH_KEYS_ROOT == ssh_keys._LEGACY_SSH_KEYS_ROOT
    assert hook_token.hook_token_path("ses-x").parent == hook_token._LEGACY_HOOK_TOKENS_ROOT
    assert ssh_keys.ssh_key_grant_path("ses-x").parent == ssh_keys._LEGACY_SSH_KEYS_ROOT
