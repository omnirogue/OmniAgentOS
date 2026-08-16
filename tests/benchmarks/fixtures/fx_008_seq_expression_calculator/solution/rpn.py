from __future__ import annotations

from lexer import Token


class ParseError(ValueError):
    pass


PRECEDENCE = {
    "^": 3,
    "*": 2,
    "/": 2,
    "%": 2,
    "+": 1,
    "-": 1,
}

RIGHT_ASSOC = {
    "^": True,
    "*": False,
    "/": False,
    "%": False,
    "+": False,
    "-": False,
}


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
    if not tokens:
        return []

    # Check structural validity
    expect_value = True
    paren_depth = 0
    for tok in tokens:
        if tok.kind == "number":
            if not expect_value:
                raise ParseError("Unexpected number; expected an operator or parenthesis.")
            expect_value = False
        elif tok.kind == "op":
            if expect_value:
                raise ParseError(f"Unexpected operator '{tok.text}' without a left operand/value.")
            expect_value = True
        elif tok.kind == "lparen":
            if not expect_value:
                raise ParseError("Unexpected '('; expected an operator.")
            paren_depth += 1
            expect_value = True
        elif tok.kind == "rparen":
            if expect_value:
                raise ParseError("Unexpected ')'; expected a value.")
            paren_depth -= 1
            if paren_depth < 0:
                raise ParseError("Mismatched ')' (no matching opening parenthesis).")
            expect_value = False
        else:
            raise ParseError(f"Unknown token kind '{tok.kind}'")

    if expect_value:
        raise ParseError("Expression ended prematurely; expected a value.")
    if paren_depth != 0:
        raise ParseError("Mismatched '('; not all parentheses were closed.")

    # Shunting-Yard translation
    output_queue: list[Token] = []
    operator_stack: list[Token] = []

    for tok in tokens:
        if tok.kind == "number":
            output_queue.append(tok)
        elif tok.kind == "op":
            o1 = tok.text
            while operator_stack:
                o2_tok = operator_stack[-1]
                if o2_tok.kind != "op":
                    break
                o2 = o2_tok.text
                p1 = PRECEDENCE[o1]
                p2 = PRECEDENCE[o2]
                if (not RIGHT_ASSOC[o1] and p1 <= p2) or (RIGHT_ASSOC[o1] and p1 < p2):
                    output_queue.append(operator_stack.pop())
                else:
                    break
            operator_stack.append(tok)
        elif tok.kind == "lparen":
            operator_stack.append(tok)
        elif tok.kind == "rparen":
            while operator_stack and operator_stack[-1].kind != "lparen":
                output_queue.append(operator_stack.pop())
            if not operator_stack:
                raise ParseError("Mismatched parentheses (extra closing parenthesis).")
            operator_stack.pop()  # pop lparen

    while operator_stack:
        tok = operator_stack.pop()
        if tok.kind in ("lparen", "rparen"):
            raise ParseError("Mismatched parentheses.")
        output_queue.append(tok)

    return output_queue
