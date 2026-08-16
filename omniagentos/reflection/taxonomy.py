"""Mechanical failure taxonomy mapping and regex-based tagging."""

from __future__ import annotations

import re

# Taxon regexes based on observed failure patterns in spec and seed corpus.
# We compile them with re.IGNORECASE, so inline flags like (?i) are not needed.
TAXONOMY_PATTERNS: dict[str, list[str]] = {
    "rate_limit": [
        r"rate\s*limit",
        r"too\s*many\s*requests",
        r"\b429\b",
        r"quota\s*exhausted",
        r"limit\s*exceeded",
    ],
    "tool_error": [
        r"tool\s*error",
        r"failed\s*to\s*execute\s*tool",
        r"invalid\s*tool",
        r"tool\s*execution\s*failed",
    ],
    "wrong_model_id": [
        r"wrong\s*model\s*id",
        r"ModelNotFoundError",
        r"model\s*not\s*found",
        r"invalid\s*model",
    ],
    "uncommitted_work": [
        r"uncommitted",
        r"dirty\s*tree",
        r"unstaged",
        r"did\s*not\s*commit",
        r"uncommitted\s*work",
    ],
    "test_pin_break": [
        r"test\s*pin\s*break",
        r"pin\s*break",
        r"pin\s*test",
        r"broke\s*test\s*pin",
    ],
    "scope_violation": [
        r"scope\s*violation",
        r"outside\s*approved\s*roots",
        r"outside\s*workspace",
        r"unauthorized\s*root",
    ],
    "timeout": [
        r"timeout",
        r"timed\s*out",
        r"watchdog",
        r"deadline",
    ],
    "merge_conflict": [
        r"merge\s*conflict",
        r"conflict\s*at\s*merge",
        r"add/add\s*conflict",
    ],
    "policy_denial": [
        r"policy\s*denial",
        r"denied",
        r"denial",
        r"openhands_gate",
    ],
    "hung": [
        r"\bhung\b",
        r"hanging",
        r"stalled",
        r"never\s*returned",
    ],
    "killed": [
        r"\bkilled\b",
        r"killed_by",
        r"supervisor-shutdown",
    ],
}

_COMPILED_PATTERNS = {
    tag: [re.compile(p, re.IGNORECASE) for p in patterns]
    for tag, patterns in TAXONOMY_PATTERNS.items()
}


def classify_content(content: str) -> list[str]:
    """Scan content for failure patterns and return matching taxonomy tags."""
    matched: list[str] = []
    for tag, regexes in _COMPILED_PATTERNS.items():
        for regex in regexes:
            if regex.search(content):
                matched.append(tag)
                break
    return matched
