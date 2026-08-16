"""Per-workflow instance modules — each registers ONE loop instance's tools.

The runtime ships no tools on purpose: a runtime that owns tools owns
credentials. W1..W5 each add a module here exposing ``register(ctx) -> None``,
which builds ``LoopTool``s over existing callables (the connector broker library
path, ``steward.notify``, stdlib) and registers them on ``ctx.tools``.

``worker.load_instance_tools`` refuses any module outside this package, so a
routine row can never name an arbitrary import path.
"""
