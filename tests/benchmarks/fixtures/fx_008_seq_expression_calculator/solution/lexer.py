from __future__ import annotations

import re
from dataclasses import dataclass


class LexError(ValueError):
    pass


@dataclass(frozen=True)
class Token:
    kind: str  # "number" | "op" | "lparen" | "rparen"
    text: str


def tokenize(source: str) -> list[Token]:
    """
    Tokenizes the input expression into a list of Tokens.

    Args:
        source: The infix expression string to tokenize.

    Returns:
        A list of Token objects.

    Raises:
        LexError: If an invalid character is encountered.
    """
    tokens: list[Token] = []
    i = 0
    n = len(source)
    while i < n:
        c = source[i]
        if c in (" ", "\t"):
            i += 1
            continue
        if c in ("+", "-", "*", "/", "%", "^"):
            tokens.append(Token("op", c))
            i += 1
            continue
        if c == "(":
            tokens.append(Token("lparen", c))
            i += 1
            continue
        if c == ")":
            tokens.append(Token("rparen", c))
            i += 1
            continue

        if c.isdigit():
            # Match number starting at the exact index
            match = re.match(r"^\d+(?:\.\d+)?", source[i:])
            if match:
                text = match.group(0)
                tokens.append(Token("number", text))
                i += len(text)
                continue

        raise LexError(f"Lexing error: Invalid character '{c}' at index {i}")
    return tokens
