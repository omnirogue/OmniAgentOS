from __future__ import annotations

from mydiff import Op, diff_lines
from mypatch import apply_ops, invert_ops


def verify(a: list[str], b: list[str]) -> bool:
    """
    Returns True if the diff roundtrip and its inverse both hold successfully.
    Returns False if any PatchError or other error occurs during the process.
    """
    try:
        ops = diff_lines(a, b)
        if apply_ops(a, ops) != b:
            return False
        if apply_ops(b, invert_ops(ops)) != a:
            return False
        return True
    except Exception:
        return False


def stats(ops: list[Op]) -> dict[str, int]:
    """
    Returns a dictionary of counts for each operation tag.
    Format: {"same": count, "removed": count, "added": count}
    """
    same = 0
    removed = 0
    added = 0
    for tag, _ in ops:
        if tag == "=":
            same += 1
        elif tag == "-":
            removed += 1
        elif tag == "+":
            added += 1
    return {"same": same, "removed": removed, "added": added}


def summarize(a: list[str], b: list[str]) -> str:
    """
    Generates a summary string of the format: "+{added} -{removed} ={same}"
    representing the diff from sequence a to sequence b.
    """
    ops = diff_lines(a, b)
    s = stats(ops)
    return f"+{s['added']} -{s['removed']} ={s['same']}"
