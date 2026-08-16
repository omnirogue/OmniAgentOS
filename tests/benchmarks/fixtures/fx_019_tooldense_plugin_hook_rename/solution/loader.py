"""Plugin discovery and dispatch layer."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# Explicit list of the twelve plugin modules
PLUGIN_MODULES = (
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


class PluginError(RuntimeError):
    """Raised when a plugin is invalid."""


@dataclass(frozen=True)
class PluginInfo:
    """Metadata about a registered plugin."""

    plugin_id: str
    priority: int
    handler: Callable[[dict[str, str]], str]


def load_plugins() -> dict[str, PluginInfo]:
    """Load plugins from the explicitly defined module list.

    Expects 'PLUGIN_ID' and 'handle_event' attributes in each plugin module.
    Raises PluginError naming the offending module if either attribute is missing.
    """
    import importlib

    plugins: dict[str, PluginInfo] = {}
    for name in PLUGIN_MODULES:
        try:
            mod = importlib.import_module(f"plugins.{name}")
            plugin_id = getattr(mod, "PLUGIN_ID", None)
            handler = getattr(mod, "handle_event", None)
            if plugin_id is None:
                raise PluginError(f"Plugin module '{name}' lacks 'PLUGIN_ID' attribute.")
            if handler is None:
                raise PluginError(f"Plugin module '{name}' lacks 'handle_event' attribute.")
            priority = getattr(mod, "PRIORITY", 0)
            plugins[plugin_id] = PluginInfo(
                plugin_id=plugin_id,
                priority=priority,
                handler=handler,
            )
        except ImportError as e:
            raise PluginError(f"Failed to import plugin module '{name}': {e}") from e
    return plugins


def dispatch(name: str, payload: dict[str, str]) -> str:
    """Dispatch an event to the named plugin."""
    plugins = load_plugins()
    if name not in plugins:
        raise KeyError(f"Unknown plugin id: {name}")
    return plugins[name].handler(payload)
