from __future__ import annotations

Op = tuple[str, str]


def lcs_length(a: list[str], b: list[str]) -> int:
    """
    Computes the length of the Longest Common Subsequence (LCS)
    of two lists of lines using dynamic programming.
    """
    M, N = len(a), len(b)
    dp = [[0] * (N + 1) for _ in range(M + 1)]
    for i in range(1, M + 1):
        for j in range(1, N + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])
    return dp[M][N]


def diff_lines(a: list[str], b: list[str]) -> list[Op]:
    """
    Generates a deterministic, minimal edit script mapping sequence a to sequence b.
    Satisfies: len(deletions) + len(insertions) == len(a) + len(b) - 2 * lcs_length(a, b).

    Tie-breaking:
    - Prefer matching lines ('=') when they match.
    - When there is a choice between deletion ('-') and insertion ('+'),
      the deletion is placed before the insertion in the final left-to-right order.
    """
    M, N = len(a), len(b)
    dp = [[0] * (N + 1) for _ in range(M + 1)]
    for i in range(1, M + 1):
        for j in range(1, N + 1):
            if a[i - 1] == b[j - 1]:
                dp[i][j] = dp[i - 1][j - 1] + 1
            else:
                dp[i][j] = max(dp[i - 1][j], dp[i][j - 1])

    # Backtrack to build the edit script.
    ops = []
    i, j = M, N
    while i > 0 or j > 0:
        if i > 0 and j > 0 and a[i - 1] == b[j - 1]:
            ops.append(("=", a[i - 1]))
            i -= 1
            j -= 1
        elif j > 0 and (i == 0 or dp[i][j - 1] >= dp[i - 1][j]):
            # Move to (i, j-1) -> insertion in b.
            # In reverse backtracking, we add it. In the final list, it comes after the deletion.
            ops.append(("+", b[j - 1]))
            j -= 1
        else:
            # Move to (i-1, j) -> deletion from a.
            ops.append(("-", a[i - 1]))
            i -= 1

    ops.reverse()
    return ops
