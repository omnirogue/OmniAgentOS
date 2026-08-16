"""Bridge the repo-map into an agent's prompt.

``repo_map_for_task(working_dir, task_text)`` is the one call an orchestrator makes to
add codebase structure to an executor's context: it parses the task for the files and
symbols it mentions (so the map is task-focused), renders a budgeted map, and titles it
for the prompt. One line at the injection site; everything else stays here.
"""

from __future__ import annotations

import re

from omniagentos.repomap.service import build_repo_map

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_PATHISH = re.compile(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|mjs|cjs|sql|md)")
# Free-text task words that are NOT code identifiers — never treat as focus terms.
_TASK_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "into",
        "from",
        "make",
        "create",
        "add",
        "fix",
        "update",
        "change",
        "remove",
        "delete",
        "write",
        "build",
        "run",
        "test",
        "tests",
        "file",
        "files",
        "function",
        "class",
        "method",
        "code",
        "please",
        "should",
        "would",
        "could",
        "need",
        "want",
        "when",
        "where",
        "which",
        "what",
        "then",
        "also",
        "must",
        "does",
        "using",
        "use",
        "new",
        "old",
        "your",
        "you",
        "task",
        "goal",
        "step",
        "steps",
        "implement",
        "ensure",
        "check",
        "verify",
        "return",
    }
)


def focus_from_task(task: str) -> tuple[list[str], list[str]]:
    """Parse free-form task text into ``(focus_files, focus_terms)``.

    File-looking tokens (``intake/service.py``) become focus files; identifiers that
    look like code — snake_case, CamelCase, or simply long — become focus terms. Plain
    English words are dropped so they don't dilute the ranking."""
    files = sorted({match for match in _PATHISH.findall(task)})
    terms: list[str] = []
    seen: set[str] = set()
    for token in _IDENT.findall(task):
        lower = token.lower()
        if lower in _TASK_STOPWORDS or token in seen:
            continue
        looks_like_code = "_" in token or any(ch.isupper() for ch in token[1:]) or len(token) >= 9
        if looks_like_code:
            terms.append(token)
            seen.add(token)
    return files, terms[:24]


def repo_map_for_task(
    working_dir: str,
    task_text: str = "",
    max_tokens: int = 800,
    title: str = "# Repository map — most relevant code, ranked (structure only, bodies elided)",
) -> str:
    """A titled, task-focused repo-map block ready to prepend to an executor's prompt.
    Empty string when there's nothing to map (missing dir / no source)."""
    focus_files, focus_terms = focus_from_task(task_text)
    body = build_repo_map(
        working_dir, focus_files=focus_files, focus_terms=focus_terms, max_tokens=max_tokens
    )
    return f"{title}\n{body}" if body else ""
