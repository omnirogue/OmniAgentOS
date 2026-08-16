"""Human-edit preservation (contracts/vault-frontmatter.md "Human-edit rule").

Regenerating a system note must overwrite only the system-generated sections; a
`## Notes (human)` section, if present in the note already on disk, is carried
forward VERBATIM into the freshly rendered content.
"""

from __future__ import annotations

NOTES_HUMAN_HEADING = "## Notes (human)"


def _heading_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    """Find `heading` as an exact H2 line in `lines`. Returns (start, end) line
    indices (end exclusive) spanning the heading through the line before the
    next H1/H2 heading (or end of file). Returns None if not found."""
    start: int | None = None
    for i, line in enumerate(lines):
        if line.rstrip("\r\n") == heading:
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        stripped = lines[j].rstrip("\r\n")
        if stripped.startswith("## ") or stripped.startswith("# "):
            end = j
            break
    return start, end


def extract_section(content: str, heading: str = NOTES_HUMAN_HEADING) -> str | None:
    """Return the raw text (heading line included, verbatim) of `heading`'s
    section in `content`, or None if that heading is not present."""
    lines = content.splitlines(keepends=True)
    bounds = _heading_bounds(lines, heading)
    if bounds is None:
        return None
    start, end = bounds
    return "".join(lines[start:end])


def merge_human_section(new_content: str, old_content: str | None) -> str:
    """Return `new_content` with its `## Notes (human)` section replaced by the
    one found (verbatim) in `old_content`, if any. If `old_content` has no such
    section, or is None (no prior note), `new_content` is returned unchanged. If
    `new_content` itself has no such section (template omitted it), the
    preserved section is appended at the end so the human text is never lost.
    """
    if old_content is None:
        return new_content
    old_section = extract_section(old_content)
    if old_section is None:
        return new_content

    new_lines = new_content.splitlines(keepends=True)
    bounds = _heading_bounds(new_lines, NOTES_HUMAN_HEADING)
    if bounds is None:
        prefix = new_content if new_content.endswith("\n") else new_content + "\n"
        if not prefix.endswith("\n\n"):
            prefix += "\n"
        return prefix + old_section

    start, end = bounds
    rebuilt = new_lines[:start] + [old_section] + new_lines[end:]
    return "".join(rebuilt)
