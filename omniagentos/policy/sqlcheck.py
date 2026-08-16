"""Additive-SQL detection for the approvals hard-stop lane (A1.5).

``is_additive_sql`` answers one narrow question: does a piece of SQL consist
ENTIRELY of additive statements (CREATE TABLE / CREATE [UNIQUE] INDEX /
ALTER TABLE ... ADD ... / INSERT / transaction+pragma noise)?  Additive
migrations auto-run under the operator's policy; destructive SQL (DROP / TRUNCATE /
DELETE FROM / UPDATE ... SET) stays a hard stop.  The check FAILS CLOSED:
anything empty, unparseable, mixed, or containing a destructive token outside
comments returns False.

Used by :func:`omniagentos.orchestrator.approvals.classify_hard_stop` so a
purely-additive migration whose text merely mentions a destructive keyword
inside a comment is not parked, while real destructive SQL always is.
"""

from __future__ import annotations

import re

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

# Cheap applicability probe: does the text contain SQL statement heads at all?
_SQL_HEAD_RE = re.compile(
    r"\b(?:create|alter|drop|delete|truncate|insert|update|select|pragma|begin|commit)\b",
    re.IGNORECASE,
)

# A statement is additive only when its HEAD matches one of these shapes.
_ADDITIVE_STMT_RE = re.compile(
    r"^(?:"
    r"create\s+table\b"
    r"|create\s+(?:unique\s+)?index\b"
    r"|create\s+virtual\s+table\b"
    r"|alter\s+table\s+\S+\s+add\b"
    r"|insert\s+into\b"
    r"|pragma\b"
    r"|begin\b"
    r"|commit\b"
    r"|end\b"
    r")",
    re.IGNORECASE,
)

# Any of these ANYWHERE (comments already stripped) disqualifies the whole text.
_DESTRUCTIVE_TOKEN_RE = re.compile(
    r"\b(?:drop|truncate)\b|\bdelete\s+from\b|\bupdate\s+\S+\s+set\b",
    re.IGNORECASE,
)


def strip_sql_comments(text: str) -> str:
    """Remove ``-- line`` and ``/* block */`` comments (block first, so a line
    comment inside a block does not orphan the block terminator)."""
    return _LINE_COMMENT_RE.sub(" ", _BLOCK_COMMENT_RE.sub(" ", text))


def looks_like_sql(text: str) -> bool:
    """True when the comment-stripped text contains SQL statement heads."""
    return bool(_SQL_HEAD_RE.search(strip_sql_comments(text)))


def is_additive_sql(text: str) -> bool:
    """True only when EVERY statement (comments stripped) is additive.

    Fail closed: empty input, a destructive token, or any statement whose head
    is not an additive shape all return False.
    """
    stripped = strip_sql_comments(text)
    if _DESTRUCTIVE_TOKEN_RE.search(stripped):
        return False
    statements = [s.strip() for s in stripped.split(";") if s.strip()]
    if not statements:
        return False
    return all(_ADDITIVE_STMT_RE.match(stmt) for stmt in statements)


__all__ = ["is_additive_sql", "looks_like_sql", "strip_sql_comments"]
