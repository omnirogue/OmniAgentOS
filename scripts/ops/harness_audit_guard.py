#!/usr/bin/env python3
"""harness_audit_guard.py — retest guard for the external-harness-audit record.

Companion to ``docs/operations/external-harness-audit.md``. Given a mechanism
description or a handful of keywords, greps
``devtasks/harness-audits/RECORD.md`` for prior dispositions and prints a
report: has this mechanism (or something close to it) already been
adjudicated, and — most importantly — was it already rejected, with what
evidence?

This is a LOOKUP tool, not a merge gate by default: the exit code is ``0``
once the record has been read and a report printed, whether or not any match
was found (a clean "no prior disposition" report is a valid, useful answer).
It exits ``2`` on a usage/read error (record file missing or undecodable,
empty query, or a query that reduces to zero searchable keywords) and, with
``--strict``, on any PARSE warning (a malformed bullet, an unbalanced fenced
code block) — a warning means the record was not read completely, and
``--strict`` is for callers (CI, the audit runbook) that need that to be
loud rather than merely visible.

Record format (see the file's own header for the authoritative spec): each
entry is a bullet — ``-`` or ``*``, optionally indented, immediately followed
by ``**<mechanism name>**`` (any trailing text after the closing ``**`` is
ignored) — followed by indented ``  - key: value`` metadata lines
(``disposition``, ``north_star``, ``evidence``, ``source``) until the next
entry, a heading, or a blank line ends that field's wrap. A field's value may
continue onto following indented prose lines.

Matching: every query keyword must be at least 3 characters (shorter terms
can be forced through by quoting them, e.g. ``"AI"``) and not a stopword.
A keyword hitting the entry's MECHANISM NAME scores 2; hitting only its other
fields (evidence/source/north_star) scores 1 — a title hit is stronger
evidence of relevance than an incidental word buried in prose. Results sort
by score descending; a D-reject only breaks a tie with a non-reject at the
same score, it never outranks a higher-scoring non-reject. The "ALREADY
REJECTED" banner requires at least one D-reject match with score >= 2 or a
direct mechanism-name hit — a single word that only brushes a reject's
evidence text is not enough to sound the alarm.

Usage
-----
    python scripts/ops/harness_audit_guard.py "sandbox fallback wrap_command"
    python scripts/ops/harness_audit_guard.py "cordis dependency injection" --json
    python scripts/ops/harness_audit_guard.py "some new mechanism" --record path/to/RECORD.md
    python scripts/ops/harness_audit_guard.py "..." --strict   # exit 2 if the record didn't parse cleanly
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RECORD = REPO_ROOT / "devtasks" / "harness-audits" / "RECORD.md"

#: Matches a batch heading, e.g. "## Batch: deepseek-harness (2026-08-13)".
_BATCH_RE = re.compile(r"^##\s+Batch:\s*(.+)$")
#: Matches an entry's mechanism-name line: a bullet (-/*), optional leading
#: indent, then a bold span immediately after the marker. Trailing content
#: after the closing "**" (a period, "(superseded)", ...) is deliberately NOT
#: required to be empty — it is ignored, not rejected.
_ENTRY_RE = re.compile(r"^\s*[-*]\s+\*\*(.+?)\*\*")
#: A "- **Label:** value" batch-header metadata bullet (e.g. "**Rubric:**",
#: "**Upstream audited:**") — recognized, deliberate prose, NOT a mechanism
#: entry. Checked before ``_ENTRY_RE`` so these never become bogus entries.
_METADATA_LABEL_RE = re.compile(r"^\s*[-*]\s+\*\*[^*\n]+:\*\*")
#: Anything that LOOKS like it is trying to be a mechanism bullet (starts a
#: bold span right after a bullet marker) but does not fully match
#: ``_ENTRY_RE`` (e.g. an unclosed "**") — this must never silently become
#: part of the previous entry.
_BULLET_LIKE_RE = re.compile(r"^\s*[-*]\s+\*\*")
#: Matches an indented "  - key: value" metadata line under an entry.
_FIELD_RE = re.compile(r"^\s+-\s+([a-z_]+):\s*(.*)$")

#: Words too common to carry any matching signal on their own.
_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "for",
    "and",
    "or",
    "to",
    "on",
    "in",
    "with",
    "as",
    "is",
    "at",
    "vs",
    "not",
    "into",
    "this",
    "that",
    "its",
    "per",
    "via",
    "no",
}

#: A query keyword must be at least this long UNLESS it was explicitly quoted
#: (see ``_keywords``) — short tokens ("DI", "no") carry almost no matching
#: signal and are the easiest way to accidentally match everything.
_MIN_KEYWORD_LEN = 3

_DISPOSITION_CODES = {
    "A-integrate": "A",
    "B-prototype": "B",
    "C-watch": "C",
    "D-reject": "D",
}


@dataclass
class Entry:
    mechanism: str
    batch: str
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def disposition(self) -> str:
        return self.fields.get("disposition", "")

    @property
    def searchable_text(self) -> str:
        return " ".join([self.mechanism, *self.fields.values()])


def parse_record(text: str, warnings: list[str] | None = None) -> list[Entry]:
    """Parse the append-only record into a flat list of entries.

    ``warnings``, if passed, is a caller-owned list that this function APPENDS
    parse warnings to in place (an unparsed bullet, an unbalanced fenced code
    block) — pass one in to see them; the return value is always just the
    entries, so ordinary callers that do not care about warnings do not need
    to unpack anything.

    Tolerant by design where the input is ordinary prose (blank lines, plain
    paragraphs) but NOT tolerant of ambiguous bullets — a line that looks
    like a mechanism bullet but fails to parse as one closes whatever entry
    was open (so its fields can never be mistaken for the previous entry's)
    and is reported as a warning rather than silently dropped or silently
    absorbed. Two more accommodations:

    * Fenced code blocks (```` ``` ````) are skipped, so the format spec's
      own illustrative ``- **<mechanism name>**`` example (inside a fence,
      near the top of the file) is never parsed as a real entry. If the file
      contains an ODD number of ``` markers (an unclosed fence — a realistic
      authoring slip), fence-skipping is disabled for the WHOLE parse instead
      of silently discarding everything from the unclosed fence to EOF, and a
      warning is recorded either way.
    * A metadata field's value may wrap onto following indented prose lines;
      a blank line, a literal ``---`` horizontal rule, a new entry, or a
      heading all end the wrap (a horizontal rule must never become part of
      a field's value).
    """
    lines = text.splitlines()
    if warnings is None:
        warnings = []

    fence_count = sum(1 for ln in lines if ln.strip().startswith("```"))
    skip_fences = True
    if fence_count % 2 == 1:
        warnings.append(
            f"unbalanced fenced code block: {fence_count} ``` markers (odd count) — "
            "fence-skipping disabled for this parse so content after the unclosed "
            "fence is not silently dropped"
        )
        skip_fences = False

    entries: list[Entry] = []
    current_batch = "(unbatched)"
    current: Entry | None = None
    current_field: str | None = None
    in_fence = False

    for lineno, raw_line in enumerate(lines, start=1):
        if skip_fences and raw_line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if skip_fences and in_fence:
            continue

        batch_match = _BATCH_RE.match(raw_line)
        if batch_match:
            current_batch = batch_match.group(1).strip()
            current = None
            current_field = None
            continue

        if _METADATA_LABEL_RE.match(raw_line):
            # e.g. "- **Rubric:** ..." in a batch header — recognized prose,
            # not a mechanism entry. Ends whatever entry was open (it is a
            # new top-level bullet) but is otherwise silently ignored.
            current = None
            current_field = None
            continue

        entry_match = _ENTRY_RE.match(raw_line)
        if entry_match:
            current = Entry(mechanism=entry_match.group(1).strip(), batch=current_batch)
            entries.append(current)
            current_field = None
            continue

        if _BULLET_LIKE_RE.match(raw_line):
            # Opens a bold span right after a bullet marker but never closes
            # it (or is otherwise malformed) — never let this be mistaken for
            # a continuation of the entry above it.
            warnings.append(f"unparsed bullet at line {lineno}: {raw_line.strip()!r}")
            current = None
            current_field = None
            continue

        if current is None:
            continue

        field_match = _FIELD_RE.match(raw_line)
        if field_match:
            key, value = field_match.group(1), field_match.group(2).strip()
            current.fields[key] = value
            current_field = key
            continue

        stripped = raw_line.strip()
        if stripped == "" or stripped == "---":
            # Blank line or a markdown horizontal rule: ends the current
            # field's wrap, but neither closes the entry by itself — only a
            # new bullet/heading does that.
            current_field = None
            continue

        if raw_line.startswith("##"):
            current = None
            current_field = None
            continue

        # Indented prose continuing the most recently seen field's value.
        if current_field is not None:
            current.fields[current_field] = f"{current.fields[current_field]} {stripped}"

    return entries


def parse_census(entries: list[Entry], warnings: list[str]) -> str:
    """One line converting silent parse failures into something visible."""
    counts: dict[str, int] = {}
    for e in entries:
        code = _DISPOSITION_CODES.get(e.disposition, "?")
        counts[code] = counts.get(code, 0) + 1
    order = [c for c in ("A", "B", "C", "D") if c in counts]
    order += sorted(c for c in counts if c not in {"A", "B", "C", "D"})
    breakdown = "/".join(f"{c}{counts[c]}" for c in order)
    unparsed = sum(1 for w in warnings if w.startswith("unparsed bullet"))
    return f"entries={len(entries)} ({breakdown}) unparsed_bullets={unparsed}"


#: A `"quoted phrase"` in the query forces its tokens through regardless of
#: the minimum-length filter (but they still pass through unchanged, i.e.
#: lowercased substring tokens — quoting is not a regex/exact-phrase feature).
_QUOTED_RE = re.compile(r'"([^"]+)"')


def _keywords(query: str) -> list[str]:
    forced: list[str] = []
    for qmatch in _QUOTED_RE.finditer(query):
        forced.extend(re.findall(r"[a-zA-Z0-9_./:-]+", qmatch.group(1).lower()))
    remainder = _QUOTED_RE.sub(" ", query)
    tokens = re.findall(r"[a-zA-Z0-9_./:-]+", remainder.lower())
    generic = [t for t in tokens if t not in _STOPWORDS and len(t) >= _MIN_KEYWORD_LEN]

    seen: set[str] = set()
    result: list[str] = []
    for t in forced + generic:
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


@dataclass
class Match:
    entry: Entry
    score: int
    matched_keywords: list[str]
    #: True if at least one matched keyword hit the mechanism NAME (not just
    #: an evidence/source/north_star field) — the strong-relevance signal.
    name_hit: bool


def search(entries: list[Entry], query: str) -> list[Match]:
    """Rank entries by weighted keyword overlap with the query.

    A keyword hitting the entry's mechanism name scores 2 (per keyword); a
    keyword hitting only the entry's other fields scores 1. Results sort by
    score descending; among entries tied on score, a D-reject sorts ahead of
    a non-reject (never the other way — a tie-break, not a primary key), then
    alphabetically for determinism.
    """
    keywords = _keywords(query)
    matches: list[Match] = []
    for entry in entries:
        name_l = entry.mechanism.lower()
        fields_l = " ".join(entry.fields.values()).lower()
        hit: list[str] = []
        score = 0
        name_hit = False
        for kw in keywords:
            in_name = kw in name_l
            in_fields = kw in fields_l
            if not (in_name or in_fields):
                continue
            hit.append(kw)
            if in_name:
                score += 2
                name_hit = True
            else:
                score += 1
        if hit:
            matches.append(Match(entry=entry, score=score, matched_keywords=hit, name_hit=name_hit))

    def sort_key(m: Match) -> tuple[int, int, str]:
        is_reject = 0 if m.entry.disposition == "D-reject" else 1
        return (-m.score, is_reject, m.entry.mechanism.lower())

    return sorted(matches, key=sort_key)


def render_report(
    query: str,
    matches: list[Match],
    record_path: Path,
    census_line: str | None = None,
) -> str:
    lines = [f'harness_audit_guard: query="{query}" record={record_path}']
    if census_line:
        lines.append(census_line)
    if not matches:
        lines.append(
            "no prior adjudicated disposition found in this record. This record holds "
            "only the final adjudicated set per batch — a mechanism absent here can be "
            "genuinely unexamined, OR it can have been scored by a seat but not "
            "adjudicated into this file. Check that batch's own audit_appendix.md / "
            "all_lane_results.json (the fuller per-seat record) before concluding it "
            "was never looked at."
        )
        return "\n".join(lines)

    rejects = [m for m in matches if m.entry.disposition == "D-reject"]
    # A reject only sounds the alarm on real signal: a fairly strong overlap
    # (score >= 2) or a direct hit on the mechanism's own name — one stray
    # word landing only in evidence prose must not trigger it.
    qualifying = [m for m in rejects if m.score >= 2 or m.name_hit]
    if qualifying:
        triggered = sorted({kw for m in qualifying for kw in m.matched_keywords})
        lines.append(
            f"ALREADY REJECTED — {len(qualifying)} prior D-reject match"
            f"{'es' if len(qualifying) != 1 else ''} found "
            f"(triggered by keywords: {', '.join(triggered)}). Re-testing needs a NEW "
            "fact the original evidence did not have; cite it, do not silently re-run "
            "the old test."
        )
    lines.append(f"{len(matches)} prior disposition(s) found:")
    for m in matches:
        e = m.entry
        lines.append("")
        lines.append(f"- {e.mechanism}  [{e.disposition or 'unknown'}]")
        lines.append(f"  batch: {e.batch}")
        if e.fields.get("north_star"):
            lines.append(f"  north_star: {e.fields['north_star']}")
        if e.fields.get("evidence"):
            lines.append(f"  evidence: {e.fields['evidence']}")
        if e.fields.get("source"):
            lines.append(f"  source: {e.fields['source']}")
        lines.append(f"  matched on: {', '.join(m.matched_keywords)} (score={m.score})")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harness_audit_guard.py",
        description=(
            "Retest guard: grep devtasks/harness-audits/RECORD.md for a prior "
            "adjudicated disposition on a mechanism before an external-harness-audit "
            "seat re-tests it. Exits 0 once the record is read and parsed cleanly "
            "(matched or not); exits 2 on a usage/read error or an unsearchable "
            "query; with --strict, also exits 2 on any parse warning."
        ),
    )
    parser.add_argument("query", help="mechanism description or keywords to check")
    parser.add_argument(
        "--record",
        default=str(DEFAULT_RECORD),
        help="path to the disposition record (default: devtasks/harness-audits/RECORD.md)",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 2 if the record produced any parse warnings (malformed bullet, unbalanced fence)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.query.strip():
        print("harness_audit_guard: empty query", file=sys.stderr)
        return 2

    keywords = _keywords(args.query)
    if not keywords:
        print(
            f'harness_audit_guard: query="{args.query}" has no searchable keywords '
            f"(all stopwords or shorter than {_MIN_KEYWORD_LEN} characters — quote a "
            'short term to force it through, e.g. "AI") — refusing rather than '
            "reporting a false clean result",
            file=sys.stderr,
        )
        return 2

    record_path = Path(args.record)
    try:
        text = record_path.read_text()
    except (OSError, ValueError) as exc:
        # ValueError covers UnicodeDecodeError: an undecodable record is a
        # read failure, not "the record is empty/has no matches".
        print(f"harness_audit_guard: cannot read {record_path}: {exc}", file=sys.stderr)
        return 2

    warnings: list[str] = []
    entries = parse_record(text, warnings)
    for w in warnings:
        print(f"harness_audit_guard: WARNING: {w}", file=sys.stderr)
    census_line = parse_census(entries, warnings)

    matches = search(entries, args.query)

    if args.json:
        payload = {
            "query": args.query,
            "record": str(record_path),
            "census": census_line,
            "warnings": warnings,
            "matches": [
                {
                    "mechanism": m.entry.mechanism,
                    "batch": m.entry.batch,
                    "disposition": m.entry.disposition,
                    "north_star": m.entry.fields.get("north_star"),
                    "evidence": m.entry.fields.get("evidence"),
                    "source": m.entry.fields.get("source"),
                    "score": m.score,
                    "name_hit": m.name_hit,
                    "matched_keywords": m.matched_keywords,
                }
                for m in matches
            ],
        }
        print(json.dumps(payload, indent=1))
    else:
        print(render_report(args.query, matches, record_path, census_line=census_line))

    if args.strict and warnings:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
