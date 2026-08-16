from __future__ import annotations

from evaluate import EvalError, eval_rpn
from lexer import LexError, tokenize
from rpn import ParseError, to_rpn


def calculate(source: str) -> float:
    """
    Runs the entire pipeline: tokenize, to_rpn, and eval_rpn.

    Args:
        source: Infix expression.

    Returns:
        The float result of the expression.
    """
    tokens = tokenize(source)
    rpn_tokens = to_rpn(tokens)
    return eval_rpn(rpn_tokens)


def main(argv: list[str]) -> int:
    """
    Command line entry point.

    Args:
        argv: Command line arguments without the program name.

    Returns:
        Exit code (0 for success, 1 for errors, 2 for usage).
    """
    if not argv:
        print("usage: calc.py <expression>")
        return 2

    expression = " ".join(argv)
    try:
        result = calculate(expression)
        print(result)
        return 0
    except (LexError, ParseError, EvalError) as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    import sys

    sys.exit(main(sys.argv[1:]))
