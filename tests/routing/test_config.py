"""Tests for omniagentos.routing.config -- configs/accounts.yaml loading and
glob-based account discovery. Entirely offline: no network, no real CLI, no
credentials read (config_dir is only ever treated as an opaque path)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from omniagentos.routing.config import (
    Account,
    config_path,
    load_accounts_config,
)


def _write_config(tmp_path: Path, data: dict[str, Any]) -> Path:
    path = tmp_path / "accounts.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


class TestConfigPath:
    def test_default_config_path_is_repo_configs_accounts_yaml(self) -> None:
        path = config_path()
        assert path.name == "accounts.yaml"
        assert path.parent.name == "configs"
        assert path.is_file()

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        override = tmp_path / "custom-accounts.yaml"
        monkeypatch.setenv("OMNIAGENTOS_ACCOUNTS_CONFIG", str(override))
        assert config_path() == override


class TestLoadShippedConfig:
    """Sanity-check the real configs/accounts.yaml this repo ships, without
    depending on how many EXTRA accounts this specific machine's
    ~/.claude-account-* glob happens to discover (that varies by machine)."""

    def test_claude_provider_has_the_two_static_accounts(self) -> None:
        config = load_accounts_config()
        assert "claude" in config.providers
        claude = config.providers["claude"]
        ids = {a.id for a in claude.accounts}
        assert {"account-1", "account-2"}.issubset(ids)

        account_1 = next(a for a in claude.accounts if a.id == "account-1")
        assert account_1.resolved_config_dir() == str(Path("~/.claude").expanduser())
        account_2 = next(a for a in claude.accounts if a.id == "account-2")
        assert account_2.resolved_config_dir() == str(Path("~/.claude-account-2").expanduser())

    def test_multi_provider_accounts_are_configured(self) -> None:
        """Grok product ships live multi-provider account dirs (not Claude-only)."""
        config = load_accounts_config()
        assert {a.id for a in config.providers["codex"].accounts} >= {"codex-default"}
        assert {a.id for a in config.providers["grok"].accounts}
        # Optional providers may be present for full multi-model swarm.
        for name in ("gemini", "kimi"):
            if name in config.providers:
                assert config.providers[name].accounts

    def test_never_exposes_anything_but_a_path(self) -> None:
        # Defensive: the Account model has exactly the documented fields --
        # nothing that could hold a credential/token by accident.
        assert set(Account.model_fields) == {
            "id",
            "config_dir",
            "enabled",
            "priority",
            "max_inflight",
            "cooldown_seconds",
        }


class TestAccountModel:
    def test_defaults(self) -> None:
        account = Account(id="a", config_dir="~/.claude")
        assert account.enabled is True
        assert account.priority == 0
        assert account.max_inflight is None
        assert account.cooldown_seconds is None

    def test_resolved_config_dir_expands_tilde(self) -> None:
        account = Account(id="a", config_dir="~/.claude-test")
        assert account.resolved_config_dir() == str(Path.home() / ".claude-test")

    def test_extra_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Account.model_validate({"id": "a", "config_dir": "~/.claude", "bogus": True})


class TestDiscovery:
    def test_discover_glob_skips_unreadable_candidate(self, tmp_path: Path) -> None:
        denied = tmp_path / ".claude-account-denied"
        allowed = tmp_path / ".claude-account-allowed"
        denied.mkdir()
        allowed.mkdir()
        config_file = _write_config(
            tmp_path,
            {
                "providers": {
                    "claude": {
                        "accounts": [],
                        "discover_glob": str(tmp_path / ".claude-account-*"),
                    }
                }
            },
        )
        real_is_dir = Path.is_dir

        def _is_dir(path: Path) -> bool:
            if path == denied:
                raise PermissionError("sandbox denied account directory")
            return real_is_dir(path)

        with patch.object(Path, "is_dir", _is_dir):
            config = load_accounts_config(config_file)

        assert {a.id for a in config.providers["claude"].accounts} == {
            "claude-account-allowed"
        }

    def test_discover_glob_adds_new_accounts(self, tmp_path: Path) -> None:
        (tmp_path / ".claude-account-5").mkdir()
        (tmp_path / ".claude-account-6").mkdir()
        (tmp_path / "not-matching").mkdir()
        config_file = _write_config(
            tmp_path,
            {
                "providers": {
                    "claude": {
                        "cooldown_seconds": 60,
                        "accounts": [],
                        "discover_glob": str(tmp_path / ".claude-account-*"),
                    }
                }
            },
        )
        config = load_accounts_config(config_file)
        ids = {a.id for a in config.providers["claude"].accounts}
        assert ids == {"claude-account-5", "claude-account-6"}

    def test_discover_glob_dedupes_static_entry_by_resolved_path(self, tmp_path: Path) -> None:
        account_dir = tmp_path / ".claude-account-7"
        account_dir.mkdir()
        config_file = _write_config(
            tmp_path,
            {
                "providers": {
                    "claude": {
                        "cooldown_seconds": 60,
                        "accounts": [{"id": "account-2", "config_dir": str(account_dir)}],
                        "discover_glob": str(tmp_path / ".claude-account-*"),
                    }
                }
            },
        )
        config = load_accounts_config(config_file)
        ids = [a.id for a in config.providers["claude"].accounts]
        # Same directory as the static "account-2" -- no duplicate pool entry.
        assert ids == ["account-2"]

    def test_discover_glob_skips_non_directories(self, tmp_path: Path) -> None:
        (tmp_path / ".claude-account-file").write_text("not a dir", encoding="utf-8")
        config_file = _write_config(
            tmp_path,
            {
                "providers": {
                    "claude": {"accounts": [], "discover_glob": str(tmp_path / ".claude-account-*")}
                }
            },
        )
        config = load_accounts_config(config_file)
        assert config.providers["claude"].accounts == []

    def test_no_discover_glob_means_static_only(self, tmp_path: Path) -> None:
        config_file = _write_config(
            tmp_path,
            {"providers": {"claude": {"accounts": [{"id": "solo", "config_dir": "~/.claude"}]}}},
        )
        config = load_accounts_config(config_file)
        assert [a.id for a in config.providers["claude"].accounts] == ["solo"]

    def test_discovered_id_never_collides_with_a_different_static_id(self, tmp_path: Path) -> None:
        # "account-1" is statically mapped to a DIFFERENT dir than the one the
        # glob would discover as id "claude-account-1" -- both must survive,
        # since they are legitimately different accounts.
        (tmp_path / ".claude-account-1").mkdir()
        static_dir = tmp_path / "primary-claude-dir"
        static_dir.mkdir()
        config_file = _write_config(
            tmp_path,
            {
                "providers": {
                    "claude": {
                        "accounts": [{"id": "account-1", "config_dir": str(static_dir)}],
                        "discover_glob": str(tmp_path / ".claude-account-*"),
                    }
                }
            },
        )
        config = load_accounts_config(config_file)
        ids = {a.id for a in config.providers["claude"].accounts}
        assert ids == {"account-1", "claude-account-1"}
        by_id = {a.id: a for a in config.providers["claude"].accounts}
        assert by_id["account-1"].resolved_config_dir() == str(static_dir)
        assert by_id["claude-account-1"].resolved_config_dir() == str(
            tmp_path / ".claude-account-1"
        )


class TestValidation:
    def test_duplicate_id_across_providers_raises(self, tmp_path: Path) -> None:
        config_file = _write_config(
            tmp_path,
            {
                "providers": {
                    "claude": {"accounts": [{"id": "dup", "config_dir": "~/.claude"}]},
                    "codex": {"accounts": [{"id": "dup", "config_dir": "~/.codex"}]},
                }
            },
        )
        with pytest.raises(ValidationError):
            load_accounts_config(config_file)

    def test_duplicate_id_within_same_provider_raises(self, tmp_path: Path) -> None:
        config_file = _write_config(
            tmp_path,
            {
                "providers": {
                    "claude": {
                        "accounts": [
                            {"id": "dup", "config_dir": "~/.claude"},
                            {"id": "dup", "config_dir": "~/.claude-2"},
                        ]
                    }
                }
            },
        )
        with pytest.raises(ValidationError):
            load_accounts_config(config_file)

    def test_provider_default_cooldown_is_60(self, tmp_path: Path) -> None:
        config_file = _write_config(tmp_path, {"providers": {"claude": {"accounts": []}}})
        config = load_accounts_config(config_file)
        assert config.providers["claude"].cooldown_seconds == 60

    def test_missing_providers_key_yields_empty_config(self, tmp_path: Path) -> None:
        config_file = _write_config(tmp_path, {})
        config = load_accounts_config(config_file)
        assert config.providers == {}
