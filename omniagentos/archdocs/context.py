"""Task-focused context extraction from the living architecture docs (§8).

    from omniagentos.archdocs.context import load_arch_context
    print(load_arch_context(["judge", "quorum"], max_tokens=400))

Ranks `ARCHI.md` + `docs/architecture/*.md` sections by term overlap with
`focus_terms` (same "prepend a compact, relevant block to the prompt" idea as
`omniagentos.repomap.context.repo_map_for_task`, applied to prose instead of code
symbols) and returns a trimmed block sized to `max_tokens`. Used by the analyzer,
department reviews, and the CTO passes so root-cause/proposal prompts get accurate
subsystem context instead of re-deriving architecture from scratch or hallucinating it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_CHARS_PER_TOKEN = 4  # same rough, model-agnostic heuristic as omniagentos.repomap
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.*)$")
_HEADING_WEIGHT = 3  # a term match in the heading counts more than one in the body


def _default_repo_root() -> Path:
    """Walk up from this file to find the repo root (contains ARCHI.md or .git)."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "ARCHI.md").exists() or (parent / ".git").exists():
            return parent
    return here.parents[2]


@dataclass
class _Section:
    doc: str  # relative path, e.g. "ARCHI.md" or "docs/architecture/reliability.md"
    heading: str
    level: int
    body: str

    @property
    def rendered(self) -> str:
        marker = "#" * self.level
        text = self.body.strip()
        return (
            f"{marker} [{self.doc}] {self.heading}\n{text}"
            if text
            else f"{marker} [{self.doc}] {self.heading}"
        )


def _doc_paths(repo_root: Path) -> list[Path]:
    paths = []
    archi = repo_root / "ARCHI.md"
    if archi.exists():
        paths.append(archi)
    arch_dir = repo_root / "docs" / "architecture"
    if arch_dir.is_dir():
        paths.extend(sorted(arch_dir.glob("*.md")))
    return paths


def _split_sections(repo_root: Path, path: Path) -> list[_Section]:
    """Split a doc into ## / ### sections (whatever precedes the first heading is
    folded into an implicit level-2 'Overview' section so nothing is lost)."""
    text = path.read_text(encoding="utf-8", errors="replace")
    rel = str(path.relative_to(repo_root))
    lines = text.splitlines()

    sections: list[_Section] = []
    current_heading = "Overview"
    current_level = 2
    current_lines: list[str] = []

    def _flush() -> None:
        if current_lines or current_heading != "Overview":
            sections.append(
                _Section(
                    doc=rel,
                    heading=current_heading,
                    level=current_level,
                    body="\n".join(current_lines),
                )
            )

    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            _flush()
            current_level = len(match.group(1))
            current_heading = match.group(2).strip()
            current_lines = []
        else:
            current_lines.append(line)
    _flush()
    return [s for s in sections if s.body.strip() or s.heading != "Overview"]


def _score(section: _Section, terms_lower: list[str]) -> int:
    heading_lower = section.heading.lower()
    body_lower = section.body.lower()
    score = 0
    for term in terms_lower:
        if not term:
            continue
        score += heading_lower.count(term) * _HEADING_WEIGHT
        score += body_lower.count(term)
    return score


def load_arch_context(
    focus_terms: list[str],
    max_tokens: int = 600,
    repo_root: str | None = None,
) -> str:
    """A ranked, budget-fit block of architecture-doc sections relevant to `focus_terms`.

    Empty string when there's nothing to show (no docs found, or focus_terms given but
    nothing matches — never fabricates context). With no `focus_terms`, falls back to
    each doc's leading ("Overview"/first-heading) section, in doc order, so callers get
    a general orientation instead of nothing.

    `repo_root` defaults to walking up from this file to find the repo (contains
    `ARCHI.md`/`.git`); tests pass an explicit `tmp_path`-based root.
    """
    root = Path(repo_root) if repo_root is not None else _default_repo_root()
    paths = _doc_paths(root)
    if not paths:
        return ""

    all_sections: list[_Section] = []
    for path in paths:
        all_sections.extend(_split_sections(root, path))
    if not all_sections:
        return ""

    terms_lower = [t.lower().strip() for t in (focus_terms or []) if t and t.strip()]

    if not terms_lower:
        ranked = []
        seen_docs: set[str] = set()
        for section in all_sections:
            if section.doc not in seen_docs:
                ranked.append(section)
                seen_docs.add(section.doc)
    else:
        scored = [(_score(s, terms_lower), s) for s in all_sections]
        scored = [(score, s) for score, s in scored if score > 0]
        if not scored:
            return ""
        scored.sort(key=lambda pair: pair[0], reverse=True)
        ranked = [s for _, s in scored]

    max_chars = max(1, max_tokens) * _CHARS_PER_TOKEN
    included: list[str] = []
    used = 0
    for section in ranked:
        rendered = section.rendered
        cost = len(rendered) + 2  # separating blank line
        if used + cost > max_chars and included:
            break
        included.append(rendered)
        used += cost

    if not included:
        return ""

    title = "# Architecture context — ranked by relevance (source: ARCHI.md + docs/architecture/)"
    return title + "\n\n" + "\n\n".join(included)
