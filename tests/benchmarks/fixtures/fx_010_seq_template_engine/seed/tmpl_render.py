from __future__ import annotations

from tmpl_parse import Block


class RenderError(KeyError):
    """Raised when rendering a template fails due to missing context variable."""

    pass


def render(tree: Block, context: dict[str, object]) -> str:
    """Render a Block tree given a context dictionary.

    Raises RenderError if a variable (node.kind == 'var') is missing from context.
    Missing condition variables (node.kind == 'if') default to False.
    """
    raise NotImplementedError("Not implemented")
