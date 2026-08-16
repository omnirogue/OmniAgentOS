"""Optional, env-gated shrinking of noisy prompt content — never diffs/patches.

Test output and tracebacks repeat themselves (the same failing assertion N times
in a loop, the same traceback frames) in ways that cost tokens without adding
information the model needs. ``compress`` gives callers one place to shrink that
noise; it is OFF by default (``mode="off"``) so nothing changes unless a caller or
operator opts in via ``OMNIAGENTOS_COMPRESS``.

``kind in ('diff', 'patch')`` is a hard exception: a diff/patch is the literal bytes
we intend to apply, and any lossy transformation of it (even "harmless" whitespace
folding) can silently corrupt a hunk. Those are NEVER compressed, in any mode.
"""

from __future__ import annotations

import os
import re

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_TRACEBACK_HEADER_RE = re.compile(r"^Traceback \(most recent call last\):\s*$")
_FRAME_RE = re.compile(r"^  File \"")
_NEVER_COMPRESS_KINDS = frozenset({"diff", "patch"})
_MIN_REPEAT_RUN = 4  # collapse a run of >3 identical lines


def _resolve_mode(mode: str | None) -> str:
    if mode is not None:
        return mode
    return os.environ.get("OMNIAGENTOS_COMPRESS", "off")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _collapse_repeated_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        j = i + 1
        while j < n and lines[j] == lines[i]:
            j += 1
        run_len = j - i
        if run_len >= _MIN_REPEAT_RUN:
            out.append(lines[i])
            out.append(f"... [repeated {run_len} times]")
        else:
            out.extend(lines[i:j])
        i = j
    return out


def _split_frames(block: list[str]) -> list[list[str]]:
    """Split the body of a traceback block (everything between the header and the
    final exception line) into per-frame chunks. A frame is a '  File "..."' line
    plus any immediately-following non-frame lines (source context)."""
    frames: list[list[str]] = []
    for line in block:
        if _FRAME_RE.match(line):
            frames.append([line])
        elif frames:
            frames[-1].append(line)
        else:
            # Stray line before any recognized frame; keep it attached loosely.
            frames.append([line])
    return frames


def _fold_tracebacks(lines: list[str]) -> list[str]:
    """Keep the first 2 and last 4 frames of each traceback block, plus the header
    and the final exception line, eliding the middle frames."""
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        if _TRACEBACK_HEADER_RE.match(lines[i]):
            header_idx = i
            j = i + 1
            while j < n and (lines[j].startswith(" ") or lines[j].strip() == ""):
                j += 1
            exc_idx = j if j < n else None
            body = lines[header_idx + 1 : (exc_idx if exc_idx is not None else n)]
            frames = _split_frames(body)
            out.append(lines[header_idx])
            if len(frames) <= 6:
                out.extend(body)
            else:
                skipped = len(frames) - 6
                for frame in frames[:2]:
                    out.extend(frame)
                out.append(f"    ... [{skipped} frames elided] ...")
                for frame in frames[-4:]:
                    out.extend(frame)
            if exc_idx is not None:
                out.append(lines[exc_idx])
                i = exc_idx + 1
            else:
                i = n
        else:
            out.append(lines[i])
            i += 1
    return out


def _collapse_blank_runs(lines: list[str]) -> list[str]:
    out: list[str] = []
    prev_blank = False
    for line in lines:
        blank = line.strip() == ""
        if blank and prev_blank:
            continue
        out.append(line)
        prev_blank = blank
    return out


def _basic(text: str) -> str:
    text = _strip_ansi(text)
    lines = text.split("\n")
    lines = _fold_tracebacks(lines)
    lines = _collapse_repeated_lines(lines)
    lines = _collapse_blank_runs(lines)
    return "\n".join(lines)


def _llmlingua2(text: str) -> str:
    try:
        import llmlingua  # type: ignore[import-not-found]

        compressor = llmlingua.PromptCompressor()
        result = compressor.compress_prompt(text)
        compressed = result.get("compressed_prompt") if isinstance(result, dict) else None
        if isinstance(compressed, str) and compressed:
            return compressed
        return _basic(text)
    except Exception:  # noqa: BLE001 -- any failure degrades to the deterministic path
        return _basic(text)


def compress(text: str, kind: str = "log", mode: str | None = None) -> str:
    """Shrink ``text`` (a ``kind`` like 'log'/'traceback') for prompt inclusion.

    ``mode`` resolves from ``OMNIAGENTOS_COMPRESS`` when not given explicitly:
    'off' (default, unchanged), 'basic' (deterministic), 'llmlingua2' (optional
    third-party, lazily imported, falls back to 'basic' on any failure). Diffs and
    patches are NEVER compressed regardless of mode."""
    if kind in _NEVER_COMPRESS_KINDS:
        return text
    resolved = _resolve_mode(mode)
    if resolved == "basic":
        return _basic(text)
    if resolved == "llmlingua2":
        return _llmlingua2(text)
    return text
