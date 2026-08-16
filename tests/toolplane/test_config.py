"""Tests for toolplane configuration system.

Verifies:
- Default mode is 'off' with no env and no config file
- Env shadow/enforce/off each resolve
- Env truthy '1' -> 'enforce'; falsy '0' -> 'off'
- An unparseable env value falls through to the config/default
- A YAML file with mode: shadow is honoured, and env off overrides it back to off
- Unquoted mode: off in YAML resolves to 'off'
- Broken YAML degrades to defaults
- core_tools() caps at 5 and drops non-strings and blanks
- max_definitions() / concurrency_budget() reject invalid integers
- The shipped configs/toolplane.yaml parses and yields exactly the hard-coded defaults
"""

from __future__ import annotations

import pytest

from omniagentos.toolplane.config import (
    DEFAULT_CONCURRENCY_BUDGET,
    DEFAULT_CORE_TOOLS,
    DEFAULT_MAX_DEFINITIONS,
    DEFAULT_SMALL_CATALOG_TOKENS,
    DEFAULT_SMALL_CATALOG_TOOLS,
    TOOL_CATALOG_ENV,
    TOOL_SCHEDULER_ENV,
    concurrency_budget,
    core_tools,
    max_definitions,
    small_catalog_max_tokens,
    small_catalog_max_tools,
    tool_catalog_enabled,
    tool_catalog_enforcing,
    tool_catalog_mode,
    tool_scheduler_enabled,
    tool_scheduler_enforcing,
    tool_scheduler_mode,
    toolplane_config_path,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure environment is completely clean from any toolplane config."""
    monkeypatch.delenv(TOOL_CATALOG_ENV, raising=False)
    monkeypatch.delenv(TOOL_SCHEDULER_ENV, raising=False)
    monkeypatch.delenv("OMNIAGENTOS_TOOLPLANE_CONFIG", raising=False)


class TestToolplaneConfigDefaults:
    """Test defaults when no configuration is present."""

    def test_defaults_no_config(self, monkeypatch, tmp_path):
        """No env and nonexistent config file path -> default modes are off."""
        nonexistent = tmp_path / "nonexistent.yaml"
        monkeypatch.setenv("OMNIAGENTOS_TOOLPLANE_CONFIG", str(nonexistent))

        assert tool_catalog_mode() == "off"
        assert not tool_catalog_enabled()
        assert not tool_catalog_enforcing()

        assert tool_scheduler_mode() == "off"
        assert not tool_scheduler_enabled()
        assert not tool_scheduler_enforcing()

        assert core_tools() == DEFAULT_CORE_TOOLS
        assert max_definitions() == DEFAULT_MAX_DEFINITIONS
        assert small_catalog_max_tools() == DEFAULT_SMALL_CATALOG_TOOLS
        assert small_catalog_max_tokens() == DEFAULT_SMALL_CATALOG_TOKENS
        assert concurrency_budget() == DEFAULT_CONCURRENCY_BUDGET


class TestToolplaneConfigEnvOverrides:
    """Test environment variable overrides."""

    def test_env_modes_resolve(self, monkeypatch):
        """Env shadow/enforce/off resolve correctly."""
        for mode in ("off", "shadow", "enforce"):
            monkeypatch.setenv(TOOL_CATALOG_ENV, mode)
            assert tool_catalog_mode() == mode

            monkeypatch.setenv(TOOL_SCHEDULER_ENV, mode)
            assert tool_scheduler_mode() == mode

    def test_env_boolean_truthy_falsy(self, monkeypatch):
        """Env truthy/falsy maps to enforce/off."""
        for truthy in ("1", "true", "YES", "on"):
            monkeypatch.setenv(TOOL_CATALOG_ENV, truthy)
            assert tool_catalog_mode() == "enforce"
            assert tool_catalog_enabled()
            assert tool_catalog_enforcing()

        for falsy in ("0", "false", "NO", "off"):
            monkeypatch.setenv(TOOL_CATALOG_ENV, falsy)
            assert tool_catalog_mode() == "off"
            assert not tool_catalog_enabled()

    def test_unparseable_env_ignored(self, monkeypatch, tmp_path):
        """An unparseable env value falls back to config/default."""
        # Write a config file with 'shadow'
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("tool_catalog:\n  mode: shadow\n", encoding="utf-8")
        monkeypatch.setenv("OMNIAGENTOS_TOOLPLANE_CONFIG", str(cfg_file))

        monkeypatch.setenv(TOOL_CATALOG_ENV, "enfroce")
        # Should ignore 'enfroce' and use config 'shadow'
        assert tool_catalog_mode() == "shadow"


class TestToolplaneConfigYamlResolution:
    """Test resolution with YAML configuration files."""

    def test_config_file_honoured(self, monkeypatch, tmp_path):
        """A YAML file with mode: shadow is honoured, and env off overrides it."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "tool_catalog:\n  mode: shadow\n  max_definitions: 42\n", encoding="utf-8"
        )
        monkeypatch.setenv("OMNIAGENTOS_TOOLPLANE_CONFIG", str(cfg_file))

        assert tool_catalog_mode() == "shadow"
        assert max_definitions() == 42

        # Env override back to off
        monkeypatch.setenv(TOOL_CATALOG_ENV, "off")
        assert tool_catalog_mode() == "off"

    def test_unquoted_mode_off_yaml(self, monkeypatch, tmp_path):
        """Unquoted mode: off (parsed as False by PyYAML) resolves to 'off'."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("tool_catalog:\n  mode: off\n", encoding="utf-8")
        monkeypatch.setenv("OMNIAGENTOS_TOOLPLANE_CONFIG", str(cfg_file))

        assert tool_catalog_mode() == "off"

    def test_broken_yaml_degrades_gracefully(self, monkeypatch, tmp_path):
        """Broken YAML fails safe and degrades to defaults."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("tool_catalog: [unclosed list", encoding="utf-8")
        monkeypatch.setenv("OMNIAGENTOS_TOOLPLANE_CONFIG", str(cfg_file))

        assert tool_catalog_mode() == "off"
        assert max_definitions() == DEFAULT_MAX_DEFINITIONS


class TestToolplaneConfigSpecificKnobs:
    """Test coercion and constraints on specific knobs."""

    def test_core_tools_coercion(self, monkeypatch, tmp_path):
        """core_tools() caps at 5 and drops non-strings/blanks."""
        cfg_file = tmp_path / "config.yaml"
        content = """
tool_catalog:
  core_tools:
    - "read_file"
    - ""
    - 123
    - "  write_file  "
    - "search_files"
    - "hash_file"
    - "list_files"
    - "extra_tool_beyond_limit"
"""
        cfg_file.write_text(content, encoding="utf-8")
        monkeypatch.setenv("OMNIAGENTOS_TOOLPLANE_CONFIG", str(cfg_file))

        tools = core_tools()
        # Should strip and keep only non-empty strings, capped at 5
        assert len(tools) == 5
        assert tools == ("read_file", "write_file", "search_files", "hash_file", "list_files")

    def test_integer_knobs_coercion(self, monkeypatch, tmp_path):
        """max_definitions and concurrency_budget reject non-positives/bools/junk."""
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text(
            "tool_catalog:\n  max_definitions: -5\n  small_catalog_max_tools: true\n"
            "tool_scheduler:\n  concurrency_budget: 0\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("OMNIAGENTOS_TOOLPLANE_CONFIG", str(cfg_file))

        assert max_definitions() == DEFAULT_MAX_DEFINITIONS
        assert small_catalog_max_tools() == DEFAULT_SMALL_CATALOG_TOOLS
        assert concurrency_budget() == DEFAULT_CONCURRENCY_BUDGET


class TestShippedToolplaneYaml:
    """Test that the actual shipped configs/toolplane.yaml is correct."""

    def test_shipped_yaml_matches_defaults(self, monkeypatch):
        """The shipped file parses and yields exactly the hard-coded defaults."""
        monkeypatch.delenv("OMNIAGENTOS_TOOLPLANE_CONFIG", raising=False)
        path = toolplane_config_path()
        assert path.exists()

        assert tool_catalog_mode() == "off"
        assert tool_scheduler_mode() == "off"
        assert core_tools() == ()
        assert max_definitions() == 5
        assert small_catalog_max_tools() == 10
        assert small_catalog_max_tokens() == 10000
        assert concurrency_budget() == 4
