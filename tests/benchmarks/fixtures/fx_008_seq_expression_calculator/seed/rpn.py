from __future__ import annotations

from lexer import Token

# TODO: Define ParseError here as a subclass of ValueError


def to_rpn(tokens: list[Token]) -> list[Token]:
    """
    Converts a list of infix tokens to Reverse Polish Notation (RPN) using Shunting-Yard.

    Args:
        tokens: Infix tokens.

    Returns:
        List of tokens in RPN.

    Raises:
        ParseError: For unbalanced parentheses, adjacent numbers, unary operators,
                    or operators without correct operands.
    """
    raise NotImplementedError("to_rpn is not implemented yet.")
