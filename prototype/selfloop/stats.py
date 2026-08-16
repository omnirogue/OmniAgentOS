"""Four pure functions. Together they are the entire honest-evidence layer.

Nothing here touches a clock, a store or a context, so each is independently
testable and each is independently *replaceable*: this is the file a developer
edits to change how the loop weighs evidence, and it is deliberately small
enough that changing it is a decision rather than an excavation.
"""

from __future__ import annotations

import math
import re

#: Lowercase alphanumeric runs. Punctuation, case and whitespace are noise for
#: the purpose these tokens serve (deciding whether two failure reports are the
#: same failure), and keeping them would make ``Timeout:`` and ``timeout``
#: different tokens.
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def wilson_lower_bound(wins: float, n: float, z: float = 1.96) -> float:
    """Lower bound of the Wilson score interval for the proportion ``wins / n``.

    Conservative at small ``n`` — it will not endorse something off two lucky
    wins in two tries — and converges to the raw rate as ``n`` grows. Closed
    form (Wilson 1927)::

        p_hat  = wins / n
        center = p_hat + z**2 / (2n)
        margin = z * sqrt(p_hat*(1 - p_hat)/n + z**2/(4n**2))
        lower  = (center - margin) / (1 + z**2/n)

    ``n <= 0`` returns ``0.0``. No evidence means no confidence, and it is never
    a basis to promote anything.

    **Where this may and may not be used.** This bound is computed from
    POST-injection attribution counters (``helped`` / ``used``), so it is an
    input to recall ranking and to regression retirement, and it must never be
    an input to promotion admission. The reason is arithmetic, not taste: those
    counters are only written after a lesson has been promoted and injected, so
    at first promotion ``used == 0``, this returns ``0.0``, and any threshold
    above zero makes promotion unsatisfiable. That is not a hypothetical — it is
    the precise shape of the bug that left the source system with 207 staged
    candidates and zero promotions, forever, behind a gate that was correctly
    wired and mathematically always closed.
    """
    if n <= 0:
        return 0.0
    p_hat = wins / n
    z2 = z * z
    denominator = 1 + z2 / n
    center = p_hat + z2 / (2 * n)
    margin = z * math.sqrt((p_hat * (1 - p_hat) / n) + (z2 / (4 * n * n)))
    return (center - margin) / denominator


def decay_weight(age_days: float, full_days: float = 7.0, zero_days: float = 14.0) -> float:
    """Linear age weight: full up to *full_days*, down to zero at *zero_days*.

    Negative ages clamp to FULL weight. A sample stamped slightly in the future
    is a clock that skewed, not a sample from the future, and letting skew
    un-count a fresh observation is a failure mode that only ever shows up in
    production — the machine that produced the record and the machine that reads
    it are rarely in perfect agreement.

    ``zero_days <= full_days`` degenerates to a step at *full_days* rather than
    dividing by zero: a caller who asks for no decay window gets no decay window.
    """
    if zero_days <= full_days:
        return 1.0 if age_days <= full_days else 0.0
    if age_days <= full_days:
        return 1.0
    if age_days >= zero_days:
        return 0.0
    return (zero_days - age_days) / (zero_days - full_days)


def normalise_tokens(text: str) -> frozenset[str]:
    """Lowercase alphanumeric tokens of *text*. Empty string gives the empty set.

    A set, not a list: repetition carries no information about whether two
    failure reports describe the same failure, and counting it would let one
    verbose stack trace dominate a comparison.
    """
    return frozenset(_TOKEN_RE.findall(text.lower()))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two token sets.

    Two EMPTY sets score ``1.0``. That is the mathematically correct answer —
    identical emptiness is identical — but it places an obligation on the caller
    that is worth stating plainly here, because getting it wrong is how a
    clustering pass produces a lesson whose claim is the empty string: an
    all-empty cluster must be REJECTED by the clustering code, never emitted.

    The second obligation is bigger. Raw token similarity conflates unrelated
    failures, because ``error``, ``failed``, ``in`` and ``line`` appear in
    everything, and at a threshold of 0.3 they form one enormous trash cluster
    whose lesson is an amalgamation of contradictory fixes. Similarity is
    therefore only ever run WITHIN a ``(scope, failure_tag)`` partition; a group
    with no shared structured tag is not a cluster, however similar its words.
    """
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


__all__ = ["decay_weight", "jaccard", "normalise_tokens", "wilson_lower_bound"]
