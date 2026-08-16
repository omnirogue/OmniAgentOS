"""Failure taxonomy for trace mining.

Reuses the in-repo reflection taxonomy (single source of truth for taxon
names already wired into the reflection lane) and extends it with taxons that
only show up in external corpora. Classification runs ONLY over tool-result
excerpts that are actually marked as errors — mentioning the word "error" in
prose never counts, which is the core defect this replaces in the old
AI-Traces keyword scan.
"""

from __future__ import annotations

import re

from omniagentos.reflection.taxonomy import TAXONOMY_PATTERNS, classify_content
from omniagentos.tracelab.events import EventKind, Trace

#: Corpus-observed taxons absent from the reflection lane's operational set.
EXTENDED_PATTERNS: dict[str, list[str]] = {
    "file_not_found": [
        r"no such file or directory",
        r"filenotfounderror",
        r"cannot find (?:file|path|module)",
        r"enoent",
    ],
    "permission_denied": [
        r"permission denied",
        r"\beacces\b",
        r"\beperm\b",
        r"operation not permitted",
    ],
    "syntax_error": [
        r"syntaxerror",
        r"unexpected token",
        r"invalid syntax",
        r"indentationerror",
    ],
    "missing_dependency": [
        r"modulenotfounderror",
        r"importerror",
        r"command not found",
        r"no module named",
        r"package .{1,60} is not installed",
    ],
    "network_error": [
        r"connection refused",
        r"connection reset",
        r"ssl.{0,20}error",
        r"name resolution",
        r"getaddrinfo",
    ],
    "resource_exhaustion": [
        r"out of memory",
        r"\boom\b",
        r"disk (?:quota|full)",
        r"no space left",
    ],
    "context_overflow": [
        r"context (?:length|window) exceeded",
        r"maximum context",
        r"prompt is too long",
        r"input length exceeds",
    ],
}

_COMPILED_EXTENDED = {
    tag: [re.compile(p, re.IGNORECASE) for p in patterns]
    for tag, patterns in EXTENDED_PATTERNS.items()
}

# A reflection taxon and an extended taxon sharing a name would silently
# double-count in classify_error_text — fail loudly at import instead.
_overlap = set(TAXONOMY_PATTERNS) & set(EXTENDED_PATTERNS)
if _overlap:
    raise RuntimeError(f"taxon name collision with reflection taxonomy: {sorted(_overlap)}")

#: Full taxon vocabulary, reflection taxons first.
ALL_TAXONS: tuple[str, ...] = tuple(TAXONOMY_PATTERNS) + tuple(EXTENDED_PATTERNS)


def classify_error_text(content: str) -> list[str]:
    """Classify one error payload into taxonomy tags (reflection + extended)."""
    tags = classify_content(content)
    for tag, regexes in _COMPILED_EXTENDED.items():
        if any(regex.search(content) for regex in regexes):
            tags.append(tag)
    return tags


def classify_trace_errors(trace: Trace) -> dict[str, int]:
    """Taxon → count over the trace's *error* tool results only."""
    counts: dict[str, int] = {}
    for event in trace.events:
        if event.kind is not EventKind.TOOL_RESULT or event.is_error is not True:
            continue
        for tag in classify_error_text(event.excerpt) or ["unclassified"]:
            counts[tag] = counts.get(tag, 0) + 1
    return counts
