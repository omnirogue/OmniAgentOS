from __future__ import annotations

from dataclasses import dataclass

from tmpl_scan import Node


class ParseError(ValueError):
    """Raised when parsing nodes into a block tree fails."""

    pass


@dataclass(frozen=True)
class Block:
    kind: str  # "root" | "text" | "var" | "if"
    value: str
    body: tuple[Block, ...]  # for "if": the true branch; else empty
    orelse: tuple[Block, ...]  # for "if": the false branch; else empty


def build_tree(nodes: list[Node]) -> Block:
    """Build a nested Block tree from a list of scanned Nodes.

    Raises ParseError if block structures are nested incorrectly (e.g., mismatched else/end tags).
    """
    raise NotImplementedError("Not implemented")
