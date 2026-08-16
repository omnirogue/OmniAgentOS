from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    kind: str  # "number" | "op" | "lparen" | "rparen"
    text: str


# TODO: Define LexError here as a subclass of ValueError


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
    raise NotImplementedError("tokenize is not implemented yet.")
