"""
FROZEN acceptance check for fx_012_indep_three_formatters.
This check file is copied over the workspace AFTER the agent runs to prevent tampering.
It verifies the custom CSV serializer implementation.
"""

from __future__ import annotations

import inspect

from fmt import csvout
from fmt.csvout import quote_field, to_csv


def test_quote_field() -> None:
    # No quoting needed
    assert quote_field("hello") == "hello"
    assert quote_field("123") == "123"
    assert quote_field("hello world") == "hello world"

    # Comma needs quoting
    assert quote_field("hello, world") == '"hello, world"'

    # Quotes need quoting and escaping
    assert quote_field('he"llo') == '"he""llo"'

    # Carriage return or newline needs quoting
    assert quote_field("hello\nworld") == '"hello\nworld"'
    assert quote_field("hello\rworld") == '"hello\rworld"'

    # Leading/trailing spaces (ASCII space ' ')
    assert quote_field(" hello") == '" hello"'
    assert quote_field("hello ") == '"hello "'
    assert quote_field(" hello ") == '" hello "'

    # Tab shouldn't trigger quoting unless it matches another rule
    assert quote_field("\thello") == "\thello"


def test_to_csv_basic() -> None:
    # 2x2 grid
    rows = [["Name", "Age"], ["Alice", "24"], ["Bob, Jr.", "30"]]
    expected = 'Name,Age\r\nAlice,24\r\n"Bob, Jr.",30\r\n'
    assert to_csv(rows) == expected


def test_to_csv_empty_rows() -> None:
    # Empty list of rows
    assert to_csv([]) == ""

    # Row with zero fields
    assert to_csv([[]]) == "\r\n"
    assert to_csv([["a", "b"], [], ["c"]]) == "a,b\r\n\r\nc\r\n"


def test_csv_module_is_not_used() -> None:
    """The csv module is forbidden and quoting must be implemented by hand."""
    src = inspect.getsource(csvout)
    assert "import csv" not in src
    assert "from csv" not in src
    assert "binascii" not in src
