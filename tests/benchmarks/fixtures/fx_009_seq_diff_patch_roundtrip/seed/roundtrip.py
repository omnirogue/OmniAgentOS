from __future__ import annotations

from mydiff import Op


def verify(a: list[str], b: list[str]) -> bool:
    """
    Returns True if the diff roundtrip and its inverse both hold successfully.
    Returns False if any PatchError or other error occurs during the process.
    """
    raise NotImplementedError


def stats(ops: list[Op]) -> dict[str, int]:
    """
    Returns a dictionary of counts for each operation tag.
    Format: {"same": count, "removed": count, "added": count}
    """
    raise NotImplementedError


def summarize(a: list[str], b: list[str]) -> str:
    """
    Generates a summary string of the format: "+{added} -{removed} ={same}"
    representing the diff from sequence a to sequence b.
    """
    raise NotImplementedError
