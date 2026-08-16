from __future__ import annotations

from dataclasses import dataclass


class TemplateError(ValueError):
    """Raised when scan fails due to syntax error inside a tag or unterminated tag."""

    pass


@dataclass(frozen=True)
class Node:
    kind: str  # "text" | "var" | "if" | "else" | "end"
    value: str  # the literal text, or the variable/condition name


def scan(template: str) -> list[Node]:
    """Scan a template string and produce a list of lexical Nodes.

    Raises TemplateError if syntax is invalid or tag is unterminated.
    """
    raise NotImplementedError("Not implemented")
