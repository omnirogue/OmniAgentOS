from __future__ import annotations

import math

from lexer import Token


class EvalError(ValueError):
    pass


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
    if not output:
        raise EvalError("Empty expression.")

    stack: list[float] = []

    for tok in output:
        if tok.kind == "number":
            try:
                stack.append(float(tok.text))
            except ValueError as e:
                raise EvalError(f"Invalid number literal: {tok.text}") from e
        elif tok.kind == "op":
            if len(stack) < 2:
                raise EvalError("Malformed RPN: missing operands.")
            b = stack.pop()
            a = stack.pop()
            op = tok.text

            try:
                if op == "+":
                    res = a + b
                elif op == "-":
                    res = a - b
                elif op == "*":
                    res = a * b
                elif op == "/":
                    if b == 0.0:
                        raise EvalError("Division by zero.")
                    res = a / b
                elif op == "%":
                    if b == 0.0:
                        raise EvalError("Modulo by zero.")
                    res = math.fmod(a, b)
                elif op == "^":
                    res = math.pow(a, b)
                else:
                    raise EvalError(f"Unsupported operator '{op}'")

                stack.append(res)
            except (ZeroDivisionError, ValueError, OverflowError) as e:
                if isinstance(e, EvalError):
                    raise e
                raise EvalError(f"Arithmetic error: {e}") from e
        else:
            raise EvalError(f"Unexpected token kind '{tok.kind}' in RPN evaluation.")

    if len(stack) != 1:
        raise EvalError("Malformed RPN: leftover operands.")

    return stack[0]
