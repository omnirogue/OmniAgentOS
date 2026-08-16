from __future__ import annotations

from lexer import Token

# TODO: Define EvalError here as a subclass of ValueError


def eval_rpn(output: list[Token]) -> float:
    """
    Evaluates a list of RPN tokens and returns the float result.

    Args:
        output: RPN tokens.

    Returns:
        The evaluated float result.

    Raises:
        EvalError: For division/modulo by zero, malformed RPN streams,
                   or empty token list.
    """
    raise NotImplementedError("eval_rpn is not implemented yet.")
