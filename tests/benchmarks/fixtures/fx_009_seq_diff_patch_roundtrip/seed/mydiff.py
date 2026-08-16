from __future__ import annotations

# Type alias: Op represents a single operation of the edit script.
# The first element is the tag ('=', '-', or '+').
# The second element is the string line content.
Op = tuple[str, str]


def lcs_length(a: list[str], b: list[str]) -> int:
    """
    Computes the length of the Longest Common Subsequence (LCS)
    of two lists of lines using dynamic programming.
    """
    raise NotImplementedError


def diff_lines(a: list[str], b: list[str]) -> list[Op]:
    """
    Generates a deterministic, minimal edit script mapping sequence a to sequence b.
    Satisfies: len(deletions) + len(insertions) == len(a) + len(b) - 2 * lcs_length(a, b).

    Tie-breaking:
    - Prefer matching lines ('=') when they match.
    - When there is a choice between deletion ('-') and insertion ('+'),
      the deletion is placed before the insertion in the final left-to-right order.
    """
    raise NotImplementedError
