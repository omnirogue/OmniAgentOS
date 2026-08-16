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


class BlockBuilder:
    def __init__(self, kind: str, value: str):
        self.kind = kind
        self.value = value
        self.body: list[Block] = []
        self.orelse: list[Block] = []
        self.in_else = False


def build_tree(nodes: list[Node]) -> Block:
    """Build a nested Block tree from a list of scanned Nodes.

    Raises ParseError if block structures are nested incorrectly (e.g., mismatched else/end tags).
    """
    stack: list[BlockBuilder] = [BlockBuilder("root", "")]

    for node in nodes:
        if node.kind == "text":
            leaf = Block("text", node.value, (), ())
            curr = stack[-1]
            if curr.in_else:
                curr.orelse.append(leaf)
            else:
                curr.body.append(leaf)

        elif node.kind == "var":
            leaf = Block("var", node.value, (), ())
            curr = stack[-1]
            if curr.in_else:
                curr.orelse.append(leaf)
            else:
                curr.body.append(leaf)

        elif node.kind == "if":
            builder = BlockBuilder("if", node.value)
            stack.append(builder)

        elif node.kind == "else":
            if len(stack) <= 1:
                raise ParseError("Encountered 'else' tag with no matching 'if'")
            curr = stack[-1]
            if curr.in_else:
                raise ParseError("Encountered multiple 'else' tags for a single 'if'")
            curr.in_else = True

        elif node.kind == "end":
            if len(stack) <= 1:
                raise ParseError("Encountered 'end' tag with no matching 'if'")
            builder = stack.pop()
            block = Block(
                kind=builder.kind,
                value=builder.value,
                body=tuple(builder.body),
                orelse=tuple(builder.orelse),
            )
            curr = stack[-1]
            if curr.in_else:
                curr.orelse.append(block)
            else:
                curr.body.append(block)

    if len(stack) > 1:
        raise ParseError(
            f"Unclosed 'if' block starting with variable/condition '{stack[-1].value}'"
        )

    root_builder = stack[0]
    return Block(kind="root", value="", body=tuple(root_builder.body), orelse=())
