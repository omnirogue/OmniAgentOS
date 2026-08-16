from __future__ import annotations

from pathlib import Path

import pytest

from omniagentos.lease.config import (
    lease_allowed_domains,
    lease_ceilings,
    lease_config,
    lease_enabled,
    lease_enforcing,
    lease_ledger_dir,
    lease_mode,
    lease_net_policy,
    lease_proxy_ports,
    lease_ttl_s,
)


def _write_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str) -> Path:
    """Helper to write a temporary lease config file and update OMNIAGENTOS_LEASE_CONFIG."""
    config_file = tmp_path / "lease.yaml"
    config_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("OMNIAGENTOS_LEASE_CONFIG", str(config_file))
    return config_file


def test_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin default behavior when no environment variables or configuration files are present."""
    monkeypatch.delenv("OMNIAGENTOS_AUTONOMY_LEASE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_LEASE_TTL_S", raising=False)
    monkeypatch.setenv("OMNIAGENTOS_LEASE_CONFIG", str(tmp_path / "nonexistent.yaml"))

    assert lease_mode() == "off"
    assert lease_enabled() is False
    assert lease_enforcing() is False


def test_config_env_override_bidirectional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin that environment variable overrides work in both directions and with boolean-like values."""
    # Scenario A: Config says "enforce", env override to "off"
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: enforce\n")
    monkeypatch.setenv("OMNIAGENTOS_AUTONOMY_LEASE", "off")
    assert lease_mode() == "off"

    # Scenario B: Config says "off", env override to "enforce"
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: off\n")
    monkeypatch.setenv("OMNIAGENTOS_AUTONOMY_LEASE", "enforce")
    assert lease_mode() == "enforce"

    # Scenario C: Env "=1" -> "enforce"
    monkeypatch.setenv("OMNIAGENTOS_AUTONOMY_LEASE", "1")
    assert lease_mode() == "enforce"

    # Scenario D: Env "=0" -> "off"
    monkeypatch.setenv("OMNIAGENTOS_AUTONOMY_LEASE", "0")
    assert lease_mode() == "off"


def test_config_unparseable_env_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin that an unparseable environment override (like a typo) is ignored and the config wins."""
    # Config says "enforce"
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: enforce\n")
    # Env says "enfroce" (typo)
    monkeypatch.setenv("OMNIAGENTOS_AUTONOMY_LEASE", "enfroce")
    assert lease_mode() == "enforce"


def test_config_unquoted_yaml_off_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin that unquoted 'off' in YAML (which PyYAML parses as False) is resolved to off mode."""
    monkeypatch.delenv("OMNIAGENTOS_AUTONOMY_LEASE", raising=False)
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: off\n")
    assert lease_mode() == "off"


def test_config_mode_shadow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin that mode: shadow enables the lease but does not enforce it."""
    monkeypatch.delenv("OMNIAGENTOS_AUTONOMY_LEASE", raising=False)
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  mode: shadow\n")
    assert lease_mode() == "shadow"
    assert lease_enabled() is True
    assert lease_enforcing() is False


def test_config_broken_yaml_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin that a broken YAML config fails closed, yielding empty config and off mode."""
    monkeypatch.delenv("OMNIAGENTOS_AUTONOMY_LEASE", raising=False)
    _write_config(tmp_path, monkeypatch, "key: [unclosed")
    assert lease_config() == {}
    assert lease_mode() == "off"


def test_config_lease_ttl_s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin lease TTL resolution: env override, config value, default 3600.0; garbage is rejected."""
    # Case A: Defaults to 3600.0
    monkeypatch.delenv("OMNIAGENTOS_AUTONOMY_LEASE", raising=False)
    monkeypatch.delenv("OMNIAGENTOS_LEASE_TTL_S", raising=False)
    _write_config(tmp_path, monkeypatch, "autonomy_lease: {}\n")
    assert lease_ttl_s() == 3600.0

    # Case B: Config value 1800.0
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  ttl_seconds: 1800.0\n")
    assert lease_ttl_s() == 1800.0

    # Case C: Env override 900.0
    monkeypatch.setenv("OMNIAGENTOS_LEASE_TTL_S", "900.0")
    assert lease_ttl_s() == 900.0

    # Case D: Garbage/non-positive config value is rejected
    monkeypatch.delenv("OMNIAGENTOS_LEASE_TTL_S", raising=False)
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  ttl_seconds: -100.0\n")
    assert lease_ttl_s() == 3600.0

    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  ttl_seconds: garbage\n")
    assert lease_ttl_s() == 3600.0


def test_config_lease_ceilings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin that lease_ceilings returns exactly four keys and coerces garbage/missing/negative to 0.0."""
    # Case A: empty/missing config
    _write_config(tmp_path, monkeypatch, "autonomy_lease: {}\n")
    ceilings = lease_ceilings()
    assert set(ceilings.keys()) == {"cpu_s", "rss_mb", "max_procs", "wall_s"}
    assert all(v == 0.0 for v in ceilings.values())

    # Case B: valid config
    _write_config(
        tmp_path,
        monkeypatch,
        "autonomy_lease:\n  ceilings:\n    cpu_s: 10.0\n    rss_mb: 256.0\n    max_procs: 50\n    wall_s: 60.0\n",
    )
    assert lease_ceilings() == {"cpu_s": 10.0, "rss_mb": 256.0, "max_procs": 50.0, "wall_s": 60.0}

    # Case C: negative/garbage/boolean is coerced to 0.0
    _write_config(
        tmp_path,
        monkeypatch,
        "autonomy_lease:\n  ceilings:\n    cpu_s: -10.0\n    rss_mb: garbage\n    max_procs: true\n    wall_s: 0.0\n",
    )
    assert lease_ceilings() == {"cpu_s": 0.0, "rss_mb": 0.0, "max_procs": 0.0, "wall_s": 0.0}


def test_config_lease_net_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin that lease_net_policy resolves valid policies and degrades unknown ones to open."""
    # Case A: valid open
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  net:\n    default_policy: open\n")
    assert lease_net_policy() == "open"

    # Case B: valid deny
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  net:\n    default_policy: deny\n")
    assert lease_net_policy() == "deny"

    # Case C: valid proxy
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  net:\n    default_policy: proxy\n")
    assert lease_net_policy() == "proxy"

    # Case D: unknown policy degrades to open
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  net:\n    default_policy: bogus\n")
    assert lease_net_policy() == "open"


def test_config_lease_allowed_domains(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin that lease_allowed_domains strips, lowercases, and de-duplicates domains while preserving order."""
    # Case A: valid list with formatting
    _write_config(
        tmp_path,
        monkeypatch,
        "autonomy_lease:\n  net:\n    allow_domains:\n      - A.Example \n      - b.example\n      - a.example\n",
    )
    assert lease_allowed_domains() == ["a.example", "b.example"]

    # Case B: non-list returns []
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  net:\n    allow_domains: garbage\n")
    assert lease_allowed_domains() == []


def test_config_lease_proxy_ports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin that lease_proxy_ports filters out-of-range ports, de-duplicates, and defaults to (443,)."""
    # Case A: valid ports and duplicates
    _write_config(
        tmp_path,
        monkeypatch,
        "autonomy_lease:\n  net:\n    allow_ports:\n      - 80\n      - 443\n      - 80\n      - 70000\n      - -5\n      - garbage\n",
    )
    assert lease_proxy_ports() == (80, 443)

    # Case B: absent or invalid list defaults to (443,)
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  net:\n    allow_ports: garbage\n")
    assert lease_proxy_ports() == (443,)


def test_config_lease_ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin that lease_ledger_dir honors an explicit ledger_dir string in the config."""
    _write_config(tmp_path, monkeypatch, "autonomy_lease:\n  ledger_dir: /tmp/custom_ledger\n")
    assert lease_ledger_dir() == "/tmp/custom_ledger"
