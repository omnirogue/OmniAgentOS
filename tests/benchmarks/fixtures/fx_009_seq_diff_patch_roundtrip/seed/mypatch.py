from __future__ import annotations

from mydiff import Op


class PatchError(ValueError):
    """Raised when an edit script cannot be applied correctly."""

    pass


def apply_ops(a: list[str], ops: list[Op]) -> list[str]:
    """
    Applies the operations to the sequence 'a', returning the patched sequence.
    Raises PatchError if:
    - An operation expects a line from 'a' that doesn't match or doesn't exist.
    - There are remaining unconsumed lines in 'a' after all ops are applied.
    - An unknown operation tag is encountered.
    The exception message must contain the 0-based index of the offending op.
    """
    raise NotImplementedError


def invert_ops(ops: list[Op]) -> list[Op]:
    """
    Inverts the given operations list.
    Swaps '-' and '+', keeping '='.
    """
    raise NotImplementedError
