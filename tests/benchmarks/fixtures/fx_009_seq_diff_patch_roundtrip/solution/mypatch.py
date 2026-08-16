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
    a_idx = 0
    result = []
    for op_idx, op in enumerate(ops):
        if not isinstance(op, tuple) or len(op) != 2:
            raise PatchError(f"Invalid op format at index {op_idx}")
        tag, line = op
        if tag == "=":
            if a_idx >= len(a):
                raise PatchError(f"Unexpected end of input at op index {op_idx}")
            if a[a_idx] != line:
                raise PatchError(
                    f"Mismatch at op index {op_idx}: expected {repr(line)}, got {repr(a[a_idx])}"
                )
            result.append(line)
            a_idx += 1
        elif tag == "-":
            if a_idx >= len(a):
                raise PatchError(f"Unexpected end of input at op index {op_idx}")
            if a[a_idx] != line:
                raise PatchError(
                    f"Mismatch at op index {op_idx}: expected {repr(line)}, got {repr(a[a_idx])}"
                )
            a_idx += 1
        elif tag == "+":
            result.append(line)
        else:
            raise PatchError(f"Unknown tag {repr(tag)} at op index {op_idx}")

    if a_idx < len(a):
        raise PatchError(f"Leftover input lines at end of ops (index {len(ops)})")

    return result


def invert_ops(ops: list[Op]) -> list[Op]:
    """
    Inverts the given operations list.
    Swaps '-' and '+', keeping '='.
    """
    inv = []
    for tag, line in ops:
        if tag == "=":
            inv.append(("=", line))
        elif tag == "-":
            inv.append(("+", line))
        elif tag == "+":
            inv.append(("-", line))
        else:
            raise PatchError(f"Unknown tag in invert_ops: {repr(tag)}")
    return inv
