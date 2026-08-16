"""Build the ONE prompt every candidate sample in a batch reuses byte-identically.

Byte-identical reuse matters twice over: it is what makes a provider's prompt
cache actually hit (:func:`omniagentos.promptshape.stable_prefix` is how callers
verify that), and it is what makes best-of-N sampling a fair comparison of the
model's own variance rather than an artifact of different framing per sample.

Segments follow promptshape's lost-in-the-middle ordering: system instructions,
repo map, and numbered file contents are ``stable`` (identical for every sample);
the task text is ``task`` (last, where attention is strongest).
"""

from __future__ import annotations

from omniagentos.agentless.contracts import LocalizationResult, SymbolRef
from omniagentos.promptshape.segments import Segment

_SYSTEM_INSTRUCTIONS = (
    "You are patching a repository. Output a single unified diff in a ```diff "
    "fence and nothing else. Make minimal edits that fix the described task. Do "
    "not include prose, explanations, or multiple alternatives — one diff, one "
    "fence. Hunk line numbers are approximate; match by context if they drift."
)

_WINDOW_MARGIN = 60
_WINDOW_THRESHOLD_LINES = 400
_ELISION = "⋮"


def _merge_windows(windows: list[tuple[int, int]], total_lines: int) -> list[tuple[int, int]]:
    """Merge overlapping/adjacent (start, end) 1-based inclusive line ranges."""
    clamped = [
        (max(1, start), min(total_lines, end)) for start, end in windows if start <= total_lines
    ]
    clamped.sort()
    merged: list[tuple[int, int]] = []
    for start, end in clamped:
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _windowed_content(rel_path: str, content: str, symbols: list[SymbolRef]) -> str:
    """Full content for short files; elided, line-numbered windows for long ones."""
    lines = content.splitlines()
    total = len(lines)
    if total <= _WINDOW_THRESHOLD_LINES:
        numbered = [f"{i + 1:>5}  {line}" for i, line in enumerate(lines)]
        return "\n".join(numbered)

    file_symbols = [s for s in symbols if s.rel_path == rel_path]
    if not file_symbols:
        # No ranked symbol landed in this file (shouldn't normally happen for a
        # focus file) — fall back to a window around the top of the file.
        windows = [(1, min(total, _WINDOW_MARGIN * 2))]
    else:
        windows = [(s.line - _WINDOW_MARGIN, s.line + _WINDOW_MARGIN) for s in file_symbols]
    merged = _merge_windows(windows, total)

    out: list[str] = []
    prev_end = 0
    for start, end in merged:
        if start > prev_end + 1:
            out.append(f"   {_ELISION}")
        for i in range(start, end + 1):
            out.append(f"{i:>5}  {lines[i - 1]}")
        prev_end = end
    if prev_end < total:
        out.append(f"   {_ELISION}")
    return "\n".join(out)


def build_patch_prompt(
    loc: LocalizationResult, file_contents: dict[str, str], task: str
) -> list[Segment]:
    """Assemble the stable(system+map+files)/task segment list for one batch.

    ``file_contents`` maps ``loc.focus_files`` rel_paths to their full text; files
    longer than 400 lines are windowed (±60 lines per top-ranked symbol in that
    file, overlapping windows merged, '⋮' marking elisions) so long files still
    fit while keeping true line numbers for the model to anchor hunks on."""
    segments: list[Segment] = [
        Segment(kind="stable", label="system", text=_SYSTEM_INSTRUCTIONS),
        Segment(kind="stable", label="repo_map", text=f"Repo map:\n{loc.repo_map}"),
    ]
    for rel_path in loc.focus_files:
        content = file_contents.get(rel_path)
        if content is None:
            continue
        windowed = _windowed_content(rel_path, content, loc.top_symbols)
        segments.append(
            Segment(
                kind="stable",
                label=f"file:{rel_path}",
                text=f"File: {rel_path}\n```\n{windowed}\n```",
            )
        )
    segments.append(Segment(kind="task", label="task", text=f"Task:\n{task}"))
    return segments
