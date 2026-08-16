from __future__ import annotations


def calculate(source: str) -> float:
    """
    Runs the entire pipeline: tokenize, to_rpn, and eval_rpn.

    Args:
        source: Infix expression.

    Returns:
        The float result of the expression.
    """
    raise NotImplementedError("calculate is not implemented yet.")


def main(argv: list[str]) -> int:
    """
    Command line entry point.

    Args:
        argv: Command line arguments without the program name.

    Returns:
        Exit code (0 for success, 1 for errors, 2 for usage).
    """
    raise NotImplementedError("main is not implemented yet.")
