"""memcert arm context builders (DESIGN.md §5, §12).

An "arm" is a pure context-construction policy over a fixture world already
written to disk by ``gen.py``: given a world directory, an item, and a token
budget, it returns the block of text a system-under-test would be shown.
Every arm here is a **pure function of the fixture directory + item + budget**
-- no network, no wall-clock, no randomness beyond the ``rng`` seam callers
may pass for future arms. The identical answer protocol
(``core.ANSWER_PROTOCOL``) is appended by the runner, never here.

Fixture contract (world_dir layout this module reads):
    ``world_dir/sessions/*.jsonl`` (falls back to ``world_dir/*.jsonl`` if
    there is no ``sessions`` subdirectory) -- one file per session, sorted by
    filename stem (the session id) to get chronological order. Each line is a
    JSON object shaped like a claude-bridge stream-json transcript entry::

        {"type": "user"|"assistant"|"canary", "timestamp": "<iso8601>",
         "message": {"role": "user"|"assistant",
                     "content": [{"type": "text", "text": "..."}]}}

    A flat ``{"type": "canary", "text": "MEMCERT-CANARY ..."}`` line (no
    ``message`` key) is also accepted -- either shape is skipped by every
    renderer here (DESIGN §4/§12: canaries must never leak into an arm's
    context_block). Plain ``{"type": ..., "text": "..."}`` entries (no nested
    ``message``) are accepted too, for lighter hand-written fixtures.

Budget: ``budget_tokens`` is approximate (chars / 4, DESIGN §12). Every arm
truncates to ``budget_tokens * 4`` chars and records
``meta = {"chars", "approx_tokens", "truncated": bool, "sources": [...]}``.
"""

from __future__ import annotations

import json
import math
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

try:  # imported as a package member: `scripts.memcert.arms`
    from . import core
except ImportError:  # pragma: no cover - exercised when run as a bare script
    # Mirrors scripts/northstar_cert/emit_gaps.py's fallback: when this file
    # is executed directly (Makefile target, ad-hoc `python .../arms.py`),
    # sys.path[0] is this directory and there is no parent package for the
    # relative import to resolve against.
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core  # type: ignore[no-redef]

ARM_NAMES: tuple[str, ...] = (
    "none",
    "fullhistory",
    "transcript",
    "lessons",
    "lessons_placebo",
    "lessons_shuffled",
    "rag",
)

#: Arms that need the omniagentos runtime (scratch sqlite + memory.assemble).
#: Kept OUT of ARM_NAMES: the file-based contract tests iterate ARM_NAMES over
#: hand-written fixture worlds, while these two are covered by
#: tests/memcert/test_system_arm.py against full gen.py worlds.
SYSTEM_ARM_NAMES: tuple[str, ...] = ("system", "system_legacy")

_LESSON_OVERRIDE_KEYS: dict[str, str] = {
    "lessons": "lessons_real",
    "lessons_placebo": "lessons_placebo",
    "lessons_shuffled": "lessons_shuffled",
}

_BM25_K1 = 1.5
_BM25_B = 0.75
_CHUNK_WINDOW = 12
_CHUNK_STRIDE = 6

# ~30 common English words, filtered ONLY out of the BM25 *query* (DESIGN §5:
# "query = item.question tokens (lowercased alnum split, stopword list of ~30
# common words)"). Chunk/document tokenization is unfiltered.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an the of to in on at is are was were be been being this that these
    those and or but for with as by from it its what which who whom when
    where why how do does did
    """.split()
)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------------
# Fixture loading + rendering
# --------------------------------------------------------------------------


def _session_dir(world_dir: Path) -> Path:
    nested = world_dir / "sessions"
    return nested if nested.is_dir() else world_dir


def _load_raw_sessions(world_dir: Path) -> list[tuple[str, list[dict[str, Any]]]]:
    """Parsed JSONL entries per session, sorted oldest-first by filename stem."""
    out: list[tuple[str, list[dict[str, Any]]]] = []
    for path in sorted(_session_dir(world_dir).glob("*.jsonl"), key=lambda p: p.stem):
        entries: list[dict[str, Any]] = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
        out.append((path.stem, entries))
    return out


def _entry_text(entry: dict[str, Any]) -> str | None:
    """Extracted turn text with ALL internal whitespace collapsed to spaces.

    The collapse is load-bearing, not cosmetic (gemini-critic 5-round loop,
    final finding): a rendered line is this instrument's binding unit — the
    sufficiency grader trusts each line's leading ``[stamp]`` token as the
    turn's timestamp. Content carrying an embedded newline would otherwise
    mint a fresh "line" whose fake leading bracket earns unearned trust. The
    production assembler's ``_sanitize`` collapses identically.
    """
    message = entry.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return " ".join(content.split())
        if isinstance(content, list):
            parts = [
                block["text"]
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            joined = " ".join("".join(parts).split())
            if joined:
                return joined
    text = entry.get("text")
    if isinstance(text, str) and text.strip():
        return " ".join(text.split())
    return None


def _entry_role(entry: dict[str, Any]) -> str:
    message = entry.get("message")
    if isinstance(message, dict) and isinstance(message.get("role"), str) and message["role"]:
        return message["role"]
    etype = entry.get("type")
    return etype if isinstance(etype, str) and etype else "unknown"


def _render_line(entry: dict[str, Any]) -> str | None:
    """"[<timestamp>] <role>: <text>", or None for canaries/unrenderable entries.

    Timestamp and role are whitespace-collapsed like content is (codex-critic
    CR-005-R5): a rendered LINE is the binding unit downstream, so metadata
    carrying an embedded newline could otherwise mint a fake stamped line.
    The invariant is total only if every interpolated field honours it.
    """
    if not isinstance(entry, dict) or entry.get("type") == "canary":
        return None
    text = _entry_text(entry)
    if not text:
        return None
    stamp = " ".join(str(entry.get("timestamp", "")).split())
    role = " ".join(str(_entry_role(entry)).split())
    return f"[{stamp}] {role}: {text}"


def _rendered_sessions(world_dir: Path) -> list[tuple[str, list[str]]]:
    """(session_id, rendered_lines) oldest-first, canary/unrenderable lines dropped."""
    out: list[tuple[str, list[str]]] = []
    for sid, entries in _load_raw_sessions(world_dir):
        lines = [r for e in entries if (r := _render_line(e)) is not None]
        out.append((sid, lines))
    return out


# --------------------------------------------------------------------------
# Budget-aware assembly helpers
# --------------------------------------------------------------------------


def _assemble_units(units: list[str], max_chars: int) -> tuple[list[int], bool]:
    """Indices of ``units`` kept top-down (newline-joined) within ``max_chars``.

    Whole-unit granularity: a unit that would push the running total over
    budget is dropped entirely (never partially truncated). Used for
    rank-ordered content (rag chunks, lesson lines) where unit order already
    encodes priority.
    """
    kept: list[int] = []
    total = 0
    for i, unit in enumerate(units):
        add = len(unit) + (1 if kept else 0)
        if total + add > max_chars:
            return kept, True
        kept.append(i)
        total += add
    return kept, False


def _tail_fit(pairs: list[tuple[str, str]], max_chars: int) -> tuple[list[tuple[str, str]], bool]:
    """Keep the most-recent tail of ``(session_id, line)`` pairs fitting ``max_chars``.

    Drops whole leading (oldest) entries first (DESIGN §5 fullhistory:
    "truncate OLDEST-first"). If even the single most-recent entry overflows
    the budget on its own, that one line is itself tail-truncated (its own
    head is dropped, keeping the most recent characters) — and the truncation
    is MARKED with a leading "…". The marker is load-bearing, not cosmetic
    (codex-critic CR-005-R4): a rendered line's leading position is the
    sufficiency grader's stamp channel, and an unmarked head-drop could
    re-root the line at body text that happens to begin with a date-shaped
    bracket, minting a spoofed stamp. A truncated line visibly lost its
    anchor; it can never present one.
    """
    if max_chars <= 0:
        return [], bool(pairs)
    kept_rev: list[tuple[str, str]] = []
    total = 0
    for sid, line in reversed(pairs):
        add = len(line) + (1 if kept_rev else 0)
        if total + add > max_chars:
            if not kept_rev:
                kept_rev.append((sid, "…" + line[-(max_chars - 1) :] if max_chars > 1 else "…"))
            kept_rev.reverse()
            return kept_rev, True
        kept_rev.append((sid, line))
        total += add
    kept_rev.reverse()
    return kept_rev, False


def _chars_len(lines: list[str]) -> int:
    return sum(len(ln) for ln in lines) + max(0, len(lines) - 1)


# --------------------------------------------------------------------------
# Arms
# --------------------------------------------------------------------------


def _arm_none() -> core.ArmContext:
    return core.ArmContext(
        arm="none",
        context_block="",
        meta={"chars": 0, "approx_tokens": 0.0, "truncated": False, "sources": []},
    )


def _arm_fullhistory(world_dir: Path, max_chars: int) -> core.ArmContext:
    sessions = _rendered_sessions(world_dir)
    pairs = [(sid, line) for sid, lines in sessions for line in lines]
    if not pairs:
        return core.ArmContext(
            arm="fullhistory",
            context_block="",
            meta={"chars": 0, "approx_tokens": 0.0, "truncated": False, "sources": []},
        )
    if _chars_len([line for _, line in pairs]) <= max_chars:
        kept, truncated = pairs, False
    else:
        kept, truncated = _tail_fit(pairs, max_chars)
    text = "\n".join(line for _, line in kept)
    sources = list(dict.fromkeys(sid for sid, _ in kept))
    return core.ArmContext(
        arm="fullhistory",
        context_block=text,
        meta={
            "chars": len(text),
            "approx_tokens": len(text) / 4,
            "truncated": truncated,
            "sources": sources,
        },
    )


def _arm_transcript(world_dir: Path, max_chars: int) -> core.ArmContext:
    """The K most recent sessions that fit the budget (recency priority,
    rendered chronologically)."""
    sessions = _rendered_sessions(world_dir)
    selected: list[tuple[str, list[str]]] = []
    total = 0
    truncated = False
    for sid, lines in reversed(sessions):
        block_len = _chars_len(lines)
        sep = 1 if selected else 0
        if total + block_len + sep <= max_chars:
            selected.append((sid, lines))
            total += block_len + sep
            continue
        remaining = max_chars - total - sep
        if remaining > 0 and lines:
            kept_pairs, _ = _tail_fit([(sid, ln) for ln in lines], remaining)
            if kept_pairs:
                selected.append((sid, [ln for _, ln in kept_pairs]))
        truncated = True
        break
    selected.reverse()
    text = "\n".join(line for _, lines in selected for line in lines)
    sources = [sid for sid, _ in selected]
    return core.ArmContext(
        arm="transcript",
        context_block=text,
        meta={
            "chars": len(text),
            "approx_tokens": len(text) / 4,
            "truncated": truncated,
            "sources": sources,
            "k_sessions": len(selected),
        },
    )


def _arm_lessons(arm: str, world_dir: Path, item: core.Item, max_chars: int) -> core.ArmContext:
    """Dated one-line lessons for MEM-G items; transcript fallback otherwise.

    Real/placebo/shuffled lesson lists only exist for MEM-G items (they are
    derived from the generator's answer state, which non-G items cannot use
    without leaking answers). Non-G items therefore fall back to
    ``transcript`` behaviour, tagged ``meta["fallback"] = "transcript"`` so a
    grader can tell the two situations apart.
    """
    if item.axis == "G":
        key = _LESSON_OVERRIDE_KEYS[arm]
        lines = [str(x) for x in item.arm_overrides.get(key, [])]
        kept_idx, truncated = _assemble_units(lines, max_chars)
        text = "\n".join(lines[i] for i in kept_idx)
        return core.ArmContext(
            arm=arm,
            context_block=text,
            meta={
                "chars": len(text),
                "approx_tokens": len(text) / 4,
                "truncated": truncated,
                "sources": [key],
                "override_key": key,
            },
        )
    fallback = _arm_transcript(world_dir, max_chars)
    fallback.arm = arm
    fallback.meta = {**fallback.meta, "fallback": "transcript"}
    return fallback


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _chunk_session(sid: str, lines: list[str]) -> list[tuple[str, str]]:
    """Sliding windows of ``_CHUNK_WINDOW`` lines, stride ``_CHUNK_STRIDE``, per session."""
    n = len(lines)
    if n == 0:
        return []
    chunks: list[tuple[str, str]] = []
    start = 0
    idx = 0
    while True:
        window = lines[start : start + _CHUNK_WINDOW]
        chunks.append((f"{sid}:{idx}", "\n".join(window)))
        if start + _CHUNK_WINDOW >= n:
            break
        start += _CHUNK_STRIDE
        idx += 1
    return chunks


def _bm25_scores(
    chunks: list[tuple[str, str]], query_terms: list[str]
) -> list[tuple[str, str, float]]:
    """BM25 (k1=1.5, b=0.75, +0.5/+0.5-smoothed idf) score per chunk for ``query_terms``."""
    if not chunks or not query_terms:
        return [(cid, text, 0.0) for cid, text in chunks]
    docs = [_tokenize(text) for _, text in chunks]
    doc_lens = [len(d) for d in docs]
    n_docs = len(docs)
    avgdl = (sum(doc_lens) / n_docs) if n_docs else 0.0
    df = {term: sum(1 for d in docs if term in d) for term in set(query_terms)}
    scored: list[tuple[str, str, float]] = []
    for (cid, text), doc, dl in zip(chunks, docs, doc_lens, strict=True):
        counts = Counter(doc)
        score = 0.0
        for term in query_terms:
            freq = counts.get(term, 0)
            if freq == 0:
                continue
            n_q = df.get(term, 0)
            idf = math.log((n_docs - n_q + 0.5) / (n_q + 0.5) + 1)
            norm = 1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 0.0)
            denom = freq + _BM25_K1 * norm
            score += idf * (freq * (_BM25_K1 + 1)) / denom
        scored.append((cid, text, score))
    return scored


def _arm_rag(world_dir: Path, item: core.Item, max_chars: int) -> core.ArmContext:
    """Naive BM25-over-chunks retrieval (DESIGN §5's must-beat baseline)."""
    sessions = _rendered_sessions(world_dir)
    chunks = [c for sid, lines in sessions for c in _chunk_session(sid, lines)]
    query_terms = list(dict.fromkeys(t for t in _tokenize(item.question) if t not in STOPWORDS))
    scored = _bm25_scores(chunks, query_terms)
    # Deterministic tie-break: score desc, chunk_id asc.
    scored.sort(key=lambda row: (-row[2], row[0]))
    kept_idx, truncated = _assemble_units([text for _, text, _ in scored], max_chars)
    text = "\n".join(scored[i][1] for i in kept_idx)
    sources = [scored[i][0] for i in kept_idx]
    return core.ArmContext(
        arm="rag",
        context_block=text,
        meta={
            "chars": len(text),
            "approx_tokens": len(text) / 4,
            "truncated": truncated,
            "sources": sources,
            "num_chunks": len(chunks),
        },
    )


def build_context(
    arm: str,
    world_dir: Path,
    item: core.Item,
    budget_tokens: int,
    rng: random.Random,
) -> core.ArmContext:
    """Build the arm's context for ``item`` from the fixture world at ``world_dir``.

    Pure function of the fixture dir + item + budget: no network, no
    wall-clock. ``rng`` is accepted for signature uniformity (DESIGN §12:
    "every random draw via core.rng_for") -- the file-based v1 arms are
    deterministic by construction; only the ``system`` arm receives it.
    """
    max_chars = max(0, round(budget_tokens * 4))

    if arm == "none":
        return _arm_none()
    if arm == "fullhistory":
        return _arm_fullhistory(world_dir, max_chars)
    if arm == "transcript":
        return _arm_transcript(world_dir, max_chars)
    if arm in _LESSON_OVERRIDE_KEYS:
        return _arm_lessons(arm, world_dir, item, max_chars)
    if arm == "rag":
        return _arm_rag(world_dir, item, max_chars)
    if arm in ("system", "system_legacy"):
        # Lazy import: system_arm needs the omniagentos runtime (scratch sqlite
        # + memory.assemble); the file-based arms above must stay import-light.
        try:
            from . import system_arm
        except ImportError:
            import system_arm  # type: ignore[no-redef]
        return system_arm.build_system_context(
            world_dir, item, budget_tokens, rng, hybrid=(arm == "system")
        )
    raise ValueError(f"unknown arm: {arm!r}")
