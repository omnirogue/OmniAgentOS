"""
FROZEN acceptance check for fx_008_seq_expression_calculator.
This file is copied in after the agent finishes so the agent cannot weaken it.
"""

from __future__ import annotations

import contextlib
import io
import math

from calc import calculate, main
from evaluate import EvalError, eval_rpn
from lexer import LexError, Token, tokenize
from rpn import ParseError, to_rpn


def test_lexer_valid():
    tokens = tokenize("2.5 + 3 * (4.0 ^ 2)")
    expected = [
        Token("number", "2.5"),
        Token("op", "+"),
        Token("number", "3"),
        Token("op", "*"),
        Token("lparen", "("),
        Token("number", "4.0"),
        Token("op", "^"),
        Token("number", "2"),
        Token("rparen", ")"),
    ]
    assert tokens == expected


def test_lexer_invalid_chars():
    # Test that LexError contains the index
    for text, bad_char, idx in [("2 + a", "a", 4), ("@ 3", "@", 0), ("1.5 + 2#", "#", 7)]:
        try:
            tokenize(text)
            raise AssertionError(f"Expected LexError for {text!r}")
        except LexError as e:
            msg = str(e)
            assert str(idx) in msg, f"Expected 0-based index {idx} in error message: {msg!r}"
            assert bad_char in msg, f"Expected bad char {bad_char!r} in error message: {msg!r}"


def test_lexer_invalid_number_format():
    # .5 is invalid, should fail on '.'
    try:
        tokenize(".5")
        raise AssertionError("Expected LexError for '.5'")
    except LexError as e:
        msg = str(e)
        assert "0" in msg, f"Expected index 0 in error message: {msg!r}"
        assert "." in msg

    # 1. is invalid. tokenize should parse "1" then fail on "." at index 1
    try:
        tokenize("1.")
        raise AssertionError("Expected LexError for '1.'")
    except LexError as e:
        msg = str(e)
        assert "1" in msg, f"Expected index 1 in error message: {msg!r}"
        assert "." in msg


def test_rpn_order():
    tokens = tokenize("2 + 3 * 4")
    rpn = to_rpn(tokens)
    kinds_texts = [(t.kind, t.text) for t in rpn]
    assert kinds_texts == [
        ("number", "2"),
        ("number", "3"),
        ("number", "4"),
        ("op", "*"),
        ("op", "+"),
    ]


def test_rpn_right_associativity():
    tokens = tokenize("2 ^ 3 ^ 2")
    rpn = to_rpn(tokens)
    kinds_texts = [(t.kind, t.text) for t in rpn]
    assert kinds_texts == [
        ("number", "2"),
        ("number", "3"),
        ("number", "2"),
        ("op", "^"),
        ("op", "^"),
    ]
    # 2 ^ (3 ^ 2) = 2 ^ 9 = 512.0
    val = eval_rpn(rpn)
    assert val == 512.0


def test_rpn_parenthesis():
    tokens = tokenize("(2 + 3) * 4")
    rpn = to_rpn(tokens)
    kinds_texts = [(t.kind, t.text) for t in rpn]
    assert kinds_texts == [
        ("number", "2"),
        ("number", "3"),
        ("op", "+"),
        ("number", "4"),
        ("op", "*"),
    ]
    val = eval_rpn(rpn)
    assert val == 20.0


def test_rpn_unbalanced_parentheses():
    for text in ["(2 + 3", "2 + 3)", "((2 + 3) * 4"]:
        try:
            to_rpn(tokenize(text))
            raise AssertionError(f"Expected ParseError for {text!r}")
        except ParseError:
            pass


def test_rpn_adjacent_numbers_and_unary():
    for text in ["2 3", "-3", "+5", "2 + * 3", "2 +", "()", "(2 + )"]:
        try:
            to_rpn(tokenize(text))
            raise AssertionError(f"Expected ParseError for {text!r}")
        except ParseError:
            pass


def test_eval_zero_division():
    for text in ["2 / 0", "2 % 0", "2 / (3 - 3)"]:
        rpn = to_rpn(tokenize(text))
        try:
            eval_rpn(rpn)
            raise AssertionError(f"Expected EvalError for {text!r}")
        except EvalError as e:
            assert "zero" in str(e).lower()


def test_eval_modulo_sign():
    # math.fmod semantics without unary minus
    val1 = calculate("10 % 3")
    assert math.isclose(val1, 1.0)

    val2 = calculate("(0 - 10) % 3")
    assert math.isclose(val2, -1.0)
    # Python's default modulo -10 % 3 would be 2.0. math.fmod(-10, 3) is -1.0.
    # This proves math.fmod was used rather than standard Python % operator.
    assert not math.isclose(val2, 2.0)

    val3 = calculate("(0 - 10) % (0 - 3)")
    assert math.isclose(val3, -1.0)


def test_leading_minus_rejected():
    for expr in ["-3", "2 * -3"]:
        try:
            calculate(expr)
            raise AssertionError(f"Expected ParseError for unary minus in {expr!r}")
        except ParseError:
            pass

    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        code = main(["-3"])
    assert code == 1
    assert f.getvalue().strip().startswith("error: ")


def test_eval_empty_and_overflow():
    try:
        eval_rpn([])
        raise AssertionError("Expected EvalError for empty RPN")
    except EvalError:
        pass

    try:
        calculate("10 ^ 1000")
        raise AssertionError("Expected EvalError for overflow")
    except EvalError:
        pass


def test_calc_calculate():
    assert math.isclose(calculate("2.5 * 4 + 1.5"), 11.5)
    assert math.isclose(calculate("(5 + 2) ^ 2 * 3"), 147.0)


def test_calc_main_success():
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        code = main(["2", "+", "3", "*", "4"])
    assert code == 0
    assert f.getvalue().strip() == "14.0"


def test_calc_main_usage():
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        code = main([])
    assert code == 2
    assert "usage:" in f.getvalue().strip()


def test_calc_main_error():
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        code = main(["2", "+", "a"])
    assert code == 1
    assert "error:" in f.getvalue().strip()
