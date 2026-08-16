from __future__ import annotations

from tmpl_parse import Block


class RenderError(KeyError):
    """Raised when rendering a template fails due to missing context variable."""

    def __init__(self, message: str, name: str | None = None):
        super().__init__(message)
        self.message = message
        self.name = name

    def __str__(self) -> str:
        return self.message


def render(tree: Block, context: dict[str, object]) -> str:
    """Render a Block tree given a context dictionary.

    Raises RenderError if a variable (node.kind == 'var') is missing from context.
    Missing condition variables (node.kind == 'if') default to False.
    """
    parts: list[str] = []

    def walk(node: Block) -> None:
        if node.kind == "root":
            for child in node.body:
                walk(child)
        elif node.kind == "text":
            parts.append(node.value)
        elif node.kind == "var":
            if node.value not in context:
                raise RenderError(
                    f"Variable '{node.value}' is missing from context", name=node.value
                )
            parts.append(str(context[node.value]))
        elif node.kind == "if":
            cond_val = bool(context.get(node.value, False))
            if cond_val:
                for child in node.body:
                    walk(child)
            else:
                for child in node.orelse:
                    walk(child)

    walk(tree)
    return "".join(parts)
