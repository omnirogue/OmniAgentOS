"""Parse integration-branch review verdicts (fail-closed).

The intended consumers are the COMBINED-QUEUE T5 rewrites of
``scripts/integrate.sh`` and ``scripts/fleet-ledger.py``, which have not
landed. Until a production caller exists, the parsing surface
(``_parse_verdict``, ``_reviewer_lineage_ok``) is PRIVATE: the reachability
gate refuses unwired public symbols, ``devtasks/REACHABILITY-EXEMPT.txt``
rightly bans "will be wired later" as a reason, and a public capability
nothing invokes is this repo's signature defect. T5 drops the underscores in
the same change that wires the real caller. The behaviour stays pinned by
``tests/integration/test_verdicts.py`` and
``tests/scripts/test_verdict_artifact_integrity.py`` meanwhile.

Unparseable text is NEVER treated as approval — that is the far-side rule
applied to verdict grammar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from omniagentos.formation.lineage import UnknownModelLineageError, lineage_for_model
from omniagentos.integration.config import IntegrationConfig

Decision = Literal["approve", "approve_with_notes", "reject", "failed", "unparseable"]

# Ported EXACTLY from scripts/fleet-ledger.py:~line 100 so transition behavior
# (prose_fallback=true) matches the legacy shell ledger.
_LEGACY_ANTHROPIC_PROSE_MARKERS: tuple[str, ...] = (
    "opus",
    "claude",
    "sonnet",
    "fable",
    "haiku",
)

_TRAILER_REVIEWER = re.compile(
    r"(?im)^\s*Reviewer-Model\s*:\s*(.+?)\s*$"
)
_TRAILER_CANDIDATE = re.compile(
    r"(?im)^\s*Candidate-Sha\s*:\s*(.+?)\s*$"
)

# Exact decision tokens after case-fold + whitespace strip. Prefix matching is
# forbidden: "APPROVED" / "APPROVE pending fixes" must stay unparseable.
_EXACT_DECISIONS: dict[str, Decision] = {
    "APPROVE": "approve",
    "APPROVE WITH NOTES": "approve_with_notes",
    "REJECT": "reject",
}


# Decoration this parser strips off the key. `_` is here because the shell grammar
# accepts it as emphasis: leaving it out did not make `_VERDICT: REJECT_` strict, it made
# the REFUSAL INVISIBLE — see _decision_from_verdict_line.
_STRIPPED_DECORATION = "*#_"

# A line that still INTENDS a verdict but wears decoration this parser does not strip:
# a list marker (`- VERDICT: REJECT`, `1. VERDICT: REJECT`) or any other non-alphanumeric
# ornament. Such a line must be UNPARSEABLE, never "not a verdict line" — see the
# favourable-absence note in _decision_from_verdict_line.
#
# Two prefixes are deliberately NOT matched, because they mean "this text is not mine":
# leading WHITESPACE (indented/fenced) and `>` (blockquote). Both are excluded by the
# shell grammar for the same reason, so excluding them here keeps the two agreeing.
_DECORATED_VERDICT_RE = re.compile(r"(?i)^(?![>\s])[^0-9A-Za-z>]*(?:[0-9]+[.)][^0-9A-Za-z>]*)?VERDICT")


@dataclass(frozen=True)
class ParsedVerdict:
    decision: Decision
    reviewer_model: str | None
    candidate_sha: str | None
    # Full source text from _parse_verdict so _reviewer_lineage_ok(v, cfg) can
    # run the contracted two-argument prose fallback without a third arg.
    source_text: str = ""


def _strip_markdown_prefix(line: str) -> str:
    """Drop leading markdown emphasis / heading markers.

    Leading WHITESPACE is not stripped, and that is the point: an indented line
    is quoted or fenced material, not this file's decision. Accepting it made
    this parser approve an input ``scripts/lib/verdict-grammar.sh`` refuses,
    which is the direction that matters — the strict parser must never be the
    permissive one. No verdict artifact in the live corpus is indented.
    """
    if line[:1].isspace():
        return ""
    return line.lstrip(_STRIPPED_DECORATION).strip()


def _decision_from_verdict_line(line: str) -> Decision | None:
    """Return a Decision if ``line`` is a VERDICT: line, else None (not a verdict line).

    Markdown decorations around the key (``**VERDICT:**``, ``# VERDICT:``) are
    stripped the same way ``scripts/fleet-ledger.py`` / ``integrate.sh`` do —
    leading ``*#_`` and any residual ``*`` immediately around the colon.

    Decision tokens are matched EXACTLY (case-insensitive, surrounding
    whitespace ok). Only APPROVE / APPROVE WITH NOTES / REJECT are accepted;
    anything else — including APPROVED, prefix extensions, FAILED — is
    ``unparseable``.

    RETURNING None IS A DECISION, AND IT WAS FAIL-OPEN. ``_parse_verdict``
    filters None out of ``seen``, so a None here does not make the file
    unparseable — it makes the line DISAPPEAR, and an approval elsewhere then
    wins unopposed. That is favourable absence inside the parser written to
    eliminate favourable absence: ``_`` was not in the stripped set, so
    ``_VERDICT: REJECT`` returned None and a file carrying it above
    ``VERDICT: APPROVE`` parsed as ``approve`` while the shell grammar refused
    it — the exact direction the bridging invariant forbids. Contrast the
    healthy twin ``VERDICT = REJECT``, which reaches ``unparseable`` and
    correctly poisons the whole file.

    So None is now reserved for lines that are NOT ABOUT a verdict at all.
    Any line that names VERDICT under decoration this parser does not
    recognise is ``unparseable`` — visible, and never approval.
    """
    cleaned = _strip_markdown_prefix(line)
    # Require VERDICT as the start of the cleaned line (case-insensitive).
    if not cleaned.upper().startswith("VERDICT"):
        if _DECORATED_VERDICT_RE.match(line):
            # It says VERDICT and this parser cannot read it. Say so.
            return "unparseable"
        return None
    rest = cleaned[len("VERDICT") :]
    # Tolerate ``VERDICT:**`` / ``VERDICT**:`` markdown emphasis around the colon.
    rest = rest.lstrip("*").lstrip()
    if not rest.startswith(":"):
        # "first line beginning VERDICT:" — colon is required.
        return "unparseable"
    payload = rest[1:].lstrip("*").strip()
    if not payload:
        return "unparseable"
    # Exact comparison only — startswith would fail-open on "APPROVED" etc.
    return _EXACT_DECISIONS.get(payload.upper(), "unparseable")


def _first_trailer(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if match is None:
        return None
    value = match.group(1).strip()
    return value or None


def _parse_verdict(text: str) -> ParsedVerdict:
    """Parse a reviewer's free-text verdict into a structured decision.

    EVERY line that begins with ``VERDICT:`` (after stripping markdown
    decoration) is read, and the least favourable one decides. Recognised
    tokens (case-insensitive, exact match after surrounding-whitespace strip):

    * ``APPROVE``
    * ``APPROVE WITH NOTES``
    * ``REJECT``

    Anything else (including ``APPROVED``, ``APPROVE pending fixes``,
    ``FAILED``), or a missing VERDICT line, is ``unparseable``. Unparseable
    is never approval.

    PRECEDENCE, and why it is not "the first line wins": a decorative title is
    a line too. ``# Verdict: Approve`` over a body of ``VERDICT: REJECT``
    parsed as ``approve`` here, the same defect that let the shell gate merge a
    rejected lane (MGI-001). So a ``reject`` anywhere outranks everything, and
    verdict lines that DISAGREE are ``unparseable`` rather than whichever came
    first — a file that cannot make up its mind has not established a claim.
    This mirrors the refusal-first precedence in
    ``scripts/lib/verdict-grammar.sh``. The two are deliberately NOT
    equivalent — that grammar is operational and must recognise
    ``VERDICT: FAILED`` and ``APPROVE-WITH-NOTES``, which are counterfeits
    here — but neither may ever approve what the other refuses, and
    ``tests/scripts/test_verdict_artifact_integrity.py`` pins that direction.

    Trailers ``Reviewer-Model:`` and ``Candidate-Sha:`` are scanned anywhere
    in the text. The full source is retained on ``ParsedVerdict.source_text``
    so ``_reviewer_lineage_ok(v, cfg)`` can apply prose fallback without a
    third argument.
    """
    source = str(text or "")
    seen: list[Decision] = [
        parsed
        for parsed in (_decision_from_verdict_line(line) for line in source.splitlines())
        if parsed is not None
    ]
    if not seen:
        decision: Decision = "unparseable"
    elif "reject" in seen:
        decision = "reject"
    elif len(set(seen)) == 1:
        decision = seen[0]
    else:
        decision = "unparseable"

    reviewer_model = _first_trailer(_TRAILER_REVIEWER, source)
    candidate_sha = _first_trailer(_TRAILER_CANDIDATE, source)
    return ParsedVerdict(
        decision=decision,
        reviewer_model=reviewer_model,
        candidate_sha=candidate_sha,
        source_text=source,
    )


def prose_names_anthropic(text: str) -> bool:
    """Legacy fleet-ledger anthropic-name heuristic (exact substring set)."""
    lower = str(text or "").lower()
    return any(marker in lower for marker in _LEGACY_ANTHROPIC_PROSE_MARKERS)


def _reviewer_lineage_ok(v: ParsedVerdict, cfg: IntegrationConfig) -> bool:
    """True when the verdict's reviewer satisfies ``cfg.reviewer_lineage_required``.

    * Trailer ``Reviewer-Model:`` wins: its lineage must equal the required
      lineage (unknown models fail closed).
    * If the trailer is missing and ``cfg.prose_fallback`` is True, fall back
      to the fleet-ledger prose substring set against ``v.source_text``.
    * If the trailer is missing and ``prose_fallback`` is False, refuse.
    """
    trailer = (v.reviewer_model or "").strip()
    if trailer:
        try:
            return lineage_for_model(trailer) == cfg.reviewer_lineage_required
        except UnknownModelLineageError:
            return False
    if not cfg.prose_fallback:
        return False
    # Prose heuristic is the anthropic-name set from fleet-ledger; it only
    # satisfies a required lineage of anthropic.
    if cfg.reviewer_lineage_required != "anthropic":
        return False
    return prose_names_anthropic(v.source_text)


__all__ = [
    "Decision",
    "ParsedVerdict",
    "prose_names_anthropic",
    "_LEGACY_ANTHROPIC_PROSE_MARKERS",
]
