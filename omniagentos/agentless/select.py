"""Select the winning candidate: majority vote among test-passers, ties broken.

Verification (see :mod:`omniagentos.agentless.verify`) already eliminated the
candidates that fail the test suite; selection only has to pick among the
survivors. When multiple distinct-looking diffs all pass, that's a signal some of
them agree on the SAME fix modulo formatting noise (line numbers, index hashes) —
``normalize_diff`` collapses that noise so the vote clusters correctly, then ties
go to the smallest diff (least invasive) and finally the lowest sample index
(determinism).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from omniagentos.agentless.contracts import VerifiedCandidate

_INDEX_LINE_RE = re.compile(r"^index [0-9a-f]+\.\.[0-9a-f]+.*$", re.MULTILINE)
_HUNK_HEADER_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@.*$", re.MULTILINE)


def normalize_diff(diff: str) -> str:
    """Collapse formatting noise that doesn't change WHAT a diff does: drop
    ``index ..`` lines (blob hashes differ even for identical content changes),
    collapse hunk-header line numbers (``@@ -x,y +a,b @@`` -> ``@@ @@``, since two
    patches doing the same edit can land at different offsets), and rstrip every
    line (trailing whitespace differences)."""
    without_index = _INDEX_LINE_RE.sub("", diff)
    without_hunk_numbers = _HUNK_HEADER_RE.sub("@@ @@", without_index)
    lines = [line.rstrip() for line in without_hunk_numbers.splitlines()]
    # Drop now-empty lines left behind by removed index lines.
    return "\n".join(line for line in lines if line != "")


def _changed_line_count(diff: str) -> int:
    count = 0
    for line in diff.splitlines():
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            count += 1
    return count


def select_candidate(
    candidates: list[VerifiedCandidate],
) -> tuple[VerifiedCandidate | None, str]:
    """Pick the winning candidate among ``candidates`` whose tests passed.

    Majority vote on ``normalize_diff`` clusters; ties broken by fewest changed
    lines, then lowest sample index. Returns (None, reason) when nobody passed,
    with a reason summarizing each candidate's failure mode."""
    passers = [c for c in candidates if c.tests_passed is True]
    if not passers:
        if not candidates:
            return None, "no candidates were generated"
        modes = []
        for c in candidates:
            if not c.applied:
                modes.append(f"#{c.patch.index}: did not apply")
            elif c.tests_passed is False:
                modes.append(f"#{c.patch.index}: tests failed")
            else:
                modes.append(f"#{c.patch.index}: not verified")
        return None, "no candidate passed tests (" + "; ".join(modes) + ")"

    clusters: dict[str, list[VerifiedCandidate]] = defaultdict(list)
    for candidate in passers:
        key = normalize_diff(candidate.patch.diff or "")
        clusters[key].append(candidate)

    vote_counts = Counter({key: len(members) for key, members in clusters.items()})
    max_votes = max(vote_counts.values())
    winning_keys = [key for key, count in vote_counts.items() if count == max_votes]

    tied_candidates: list[VerifiedCandidate] = []
    for key in winning_keys:
        tied_candidates.extend(clusters[key])

    tied_candidates.sort(key=lambda c: (_changed_line_count(c.patch.diff or ""), c.patch.index))
    winner = tied_candidates[0]

    if len(winning_keys) == 1 and max_votes > 1:
        reason = f"majority vote: {max_votes}/{len(passers)} passing candidates agreed"
    elif len(winning_keys) == 1:
        reason = "only one distinct passing candidate"
    else:
        reason = (
            f"tie among {len(winning_keys)} equally-voted clusters "
            f"({max_votes} vote(s) each); broke tie by smallest diff / lowest index"
        )
    return winner, reason
