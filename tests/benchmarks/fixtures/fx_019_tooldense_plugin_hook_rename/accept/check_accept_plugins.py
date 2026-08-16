"""FROZEN acceptance check for fx_019_tooldense_plugin_hook_rename.

This acceptance check is copied in after the agent finishes to prevent the agent
from editing or weakening it.
"""

from __future__ import annotations

import importlib

import loader
import manifest


def test_acceptance_plugins() -> None:
    # 1. Assert all twelve modules expose handle_event and NOT on_event
    expected_modules = (
        "archive",
        "audit",
        "backup",
        "cache",
        "digest",
        "export",
        "ingest",
        "notify",
        "purge",
        "render",
        "sync",
        "verify",
    )

    for name in expected_modules:
        mod = importlib.import_module(f"plugins.{name}")
        assert hasattr(mod, "handle_event"), f"Module '{name}' lacks 'handle_event'"
        assert not hasattr(mod, "on_event"), f"Module '{name}' should not expose 'on_event'"

        # 2. Assert each module's PLUGIN_ID equals its module name
        assert getattr(mod, "PLUGIN_ID", None) == name, f"Module '{name}' has incorrect PLUGIN_ID"

    # 3. Assert load_plugins() returns exactly twelve entries with the right ids and priorities
    plugins_info = loader.load_plugins()
    assert len(plugins_info) == 12, f"Expected 12 plugins, got {len(plugins_info)}"

    expected_priorities = {
        "verify": 90,
        "sync": 85,
        "ingest": 80,
        "notify": 70,
        "digest": 60,
        "backup": 50,
        "render": 45,
        "archive": 40,
        "audit": 30,
        "cache": 20,
        "purge": 15,
        "export": 10,
    }

    for name, expected_pri in expected_priorities.items():
        assert name in plugins_info, f"Plugin '{name}' not found in loaded plugins"
        info = plugins_info[name]
        assert info.plugin_id == name, f"Expected plugin_id '{name}', got '{info.plugin_id}'"
        assert info.priority == expected_pri, (
            f"Expected priority {expected_pri} for '{name}', got {info.priority}"
        )
        assert callable(info.handler), f"Handler for '{name}' is not callable"

    # 4. Assert dispatch returns each plugin's own string for a sample payload
    payload = {"name": "test_event"}
    for name in expected_modules:
        res = loader.dispatch(name, payload)
        assert res == f"{name}:test_event", (
            f"Dispatching '{name}' returned unexpected response: {res}"
        )

    # 5. Assert dispatch raises KeyError on an unknown id
    try:
        loader.dispatch("non_existent_plugin", payload)
        raise AssertionError("Expected KeyError when dispatching to an unknown plugin")
    except KeyError:
        pass

    # 6. Assert MANIFEST is exactly the twelve pairs in the required order and agrees field-for-field
    expected_manifest = (
        ("verify", 90),
        ("sync", 85),
        ("ingest", 80),
        ("notify", 70),
        ("digest", 60),
        ("backup", 50),
        ("render", 45),
        ("archive", 40),
        ("audit", 30),
        ("cache", 20),
        ("purge", 15),
        ("export", 10),
    )
    assert manifest.MANIFEST == expected_manifest, (
        f"Expected MANIFEST {expected_manifest}, got {manifest.MANIFEST}"
    )

    # 7. Assert manifest_ids() matches
    expected_ids = (
        "verify",
        "sync",
        "ingest",
        "notify",
        "digest",
        "backup",
        "render",
        "archive",
        "audit",
        "cache",
        "purge",
        "export",
    )
    assert manifest.manifest_ids() == expected_ids, (
        f"Expected manifest_ids() {expected_ids}, got {manifest.manifest_ids()}"
    )


def test_acceptance_loader_error_conditions() -> None:
    # Test loader raises PluginError if expected attributes are missing.
    import types
    from unittest.mock import patch

    # Mocking a module missing PLUGIN_ID
    bad_mod1 = types.ModuleType("plugins.archive")
    bad_mod1.PRIORITY = 40
    # missing PLUGIN_ID and handle_event

    with patch("importlib.import_module", return_value=bad_mod1):
        try:
            loader.load_plugins()
            raise AssertionError("Expected PluginError when PLUGIN_ID is missing")
        except loader.PluginError as e:
            assert "PLUGIN_ID" in str(e), f"Expected 'PLUGIN_ID' in error message, got: {e}"

    # Mocking a module missing handle_event
    bad_mod2 = types.ModuleType("plugins.archive")
    bad_mod2.PLUGIN_ID = "archive"
    bad_mod2.PRIORITY = 40
    # missing handle_event

    with patch("importlib.import_module", return_value=bad_mod2):
        try:
            loader.load_plugins()
            raise AssertionError("Expected PluginError when handle_event is missing")
        except loader.PluginError as e:
            assert "handle_event" in str(e), f"Expected 'handle_event' in error message, got: {e}"
