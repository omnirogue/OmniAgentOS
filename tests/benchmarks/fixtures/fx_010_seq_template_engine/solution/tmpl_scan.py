from __future__ import annotations

import re
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
    nodes: list[Node] = []
    i = 0
    n = len(template)
    text_buffer: list[str] = []

    # regex to check valid name: [A-Za-z0-9_]+
    name_re = re.compile(r"^[A-Za-z0-9_]+$")

    while i < n:
        if i + 1 < n and template[i] == "{" and template[i + 1] in ("{", "%"):
            # It's a tag! Emit the current text buffer first.
            if text_buffer:
                nodes.append(Node("text", "".join(text_buffer)))
                text_buffer.clear()

            tag_start_char = template[i + 1]  # '{' or '%'
            tag_start_idx = i

            if tag_start_char == "{":
                # Looking for '}}'
                close_idx = template.find("}}", i + 2)
                if close_idx == -1:
                    raise TemplateError(
                        f"Unterminated '{{{{' tag starting at index {tag_start_idx}"
                    )

                content = template[i + 2 : close_idx]
                stripped = content.strip()
                if not name_re.match(stripped):
                    raise TemplateError(
                        f"Invalid variable name '{stripped}' in tag starting at index {tag_start_idx}"
                    )

                nodes.append(Node("var", stripped))
                i = close_idx + 2
            else:
                # Looking for '%}'
                close_idx = template.find("%}", i + 2)
                if close_idx == -1:
                    raise TemplateError(f"Unterminated '{{%' tag starting at index {tag_start_idx}")

                content = template[i + 2 : close_idx]
                stripped = content.strip()

                if stripped == "else":
                    nodes.append(Node("else", ""))
                elif stripped == "end":
                    nodes.append(Node("end", ""))
                elif stripped.startswith("if") and len(stripped) > 2 and stripped[2].isspace():
                    cond = stripped[2:].strip()
                    if not name_re.match(cond):
                        raise TemplateError(
                            f"Invalid condition '{cond}' in 'if' tag starting at index {tag_start_idx}"
                        )
                    nodes.append(Node("if", cond))
                else:
                    raise TemplateError(
                        f"Unknown or invalid control tag '{stripped}' starting at index {tag_start_idx}"
                    )

                i = close_idx + 2
        else:
            text_buffer.append(template[i])
            i += 1

    if text_buffer:
        nodes.append(Node("text", "".join(text_buffer)))

    return nodes
