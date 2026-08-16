"""LLM-link-suggestion scaffold.

Proposes `[[wiki-links]]` for *unlinked* mentions of known vault entities so a
human can approve them (suggestions are never auto-applied). Two backends behind
one ``LinkSuggester`` interface:

* ``HeuristicLinkSuggester`` — the working, dependency-free MVP: it finds
  surface mentions of a node's title/id that are not already inside a `[[...]]`.
* ``LLMLinkSuggester`` — the stub: it builds the prompt and delegates the actual
  extraction to an injected ``llm_fn`` callable (the interface seam). With no
  callable wired it raises, so the LLM path is explicit, not silently empty.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from omniagentos.vaultgraph.contracts import LinkSuggestion, Node
from omniagentos.vaultgraph.graph import VaultGraph

# Spans already inside a `[[...]]` link — mentions here are already linked.
_EXISTING_LINK_RE = re.compile(r"\[\[[^\[\]]+?\]\]")


@runtime_checkable
class LinkSuggester(Protocol):
    """Proposes links for unlinked entity mentions in a note."""

    def suggest(
        self, source_id: str, note_text: str, candidates: list[Node]
    ) -> list[LinkSuggestion]: ...


def _linked_spans(note_text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for m in _EXISTING_LINK_RE.finditer(note_text)]


def _inside_link(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def _unlinked_occurrence(
    note_text: str, mention: str, spans: list[tuple[int, int]]
) -> tuple[int, int] | None:
    """First occurrence of ``mention`` in ``note_text`` that lies outside every
    existing `[[link]]`. Returns None when the phrase never occurs unlinked —
    so a hallucinated mention (never present) is rejected (F10). Matching is
    case-insensitive; the returned span indexes the *actual* source text."""
    if not mention:
        return None
    needle = mention.lower()
    hay = note_text.lower()
    start = hay.find(needle)
    while start != -1:
        end = start + len(mention)
        if not _inside_link(start, spans):
            return start, end
        start = hay.find(needle, start + 1)
    return None


class HeuristicLinkSuggester:
    """Surface-form matcher: proposes a link when a candidate's title (or id)
    appears verbatim in the note text outside any existing `[[link]]`."""

    def __init__(self, *, min_length: int = 4) -> None:
        self._min_length = min_length

    def suggest(
        self, source_id: str, note_text: str, candidates: list[Node]
    ) -> list[LinkSuggestion]:
        spans = _linked_spans(note_text)
        lowered = note_text.lower()
        out: list[LinkSuggestion] = []
        seen: set[str] = set()
        for node in candidates:
            if node.id == source_id or node.id in seen:
                continue
            mention = self._match(lowered, spans, node)
            if mention is None:
                continue
            seen.add(node.id)
            surface = note_text[mention[0] : mention[1]]
            target = node.id
            markdown = (
                f"[[{target}]]" if surface.lower() == target.lower() else f"[[{target}|{surface}]]"
            )
            out.append(
                LinkSuggestion(
                    source_note=source_id,
                    target_note=target,
                    mention=surface,
                    confidence=0.6,
                    reason="unlinked surface mention of a known note (heuristic)",
                    suggested_markdown=markdown,
                )
            )
        return out

    def _match(
        self, lowered: str, spans: list[tuple[int, int]], node: Node
    ) -> tuple[int, int] | None:
        for needle in self._needles(node):
            for m in re.finditer(rf"\b{re.escape(needle)}\b", lowered):
                if not _inside_link(m.start(), spans):
                    return m.start(), m.end()
        return None

    def _needles(self, node: Node) -> list[str]:
        needles = {node.id.lower()}
        if node.title and len(node.title) >= self._min_length:
            needles.add(node.title.lower())
        return [n for n in needles if len(n) >= self._min_length]


class LLMLinkSuggester:
    """Interface seam for an LLM-backed suggester.

    Pass ``llm_fn=<callable(prompt: str) -> str>`` to wire a real model; the
    callable's job is to return newline-separated ``target\ttab\tmention`` rows,
    which this class parses into :class:`LinkSuggestion` objects. Left unwired,
    ``suggest`` raises ``NotImplementedError`` — the stub is explicit.
    """

    def __init__(self, llm_fn: Callable[[str], str] | None = None) -> None:
        self._llm_fn = llm_fn

    def build_prompt(self, note_text: str, candidates: list[Node]) -> str:
        catalog = "\n".join(f"- {n.id}: {n.title}" for n in candidates)
        return (
            "You are linking a note into an Obsidian knowledge graph.\n"
            "Given the NOTE and the CANDIDATE notes, list every phrase in the "
            "note that clearly refers to a candidate and is not already a "
            "[[wiki-link]]. Reply one per line as `candidate_id<TAB>mention`.\n\n"
            f"CANDIDATES:\n{catalog}\n\nNOTE:\n{note_text}\n"
        )

    def suggest(
        self, source_id: str, note_text: str, candidates: list[Node]
    ) -> list[LinkSuggestion]:
        if self._llm_fn is None:
            raise NotImplementedError(
                "LLMLinkSuggester is a stub: inject llm_fn=callable(prompt)->str to enable"
            )
        raw = self._llm_fn(self.build_prompt(note_text, candidates))
        by_id = {node.id: node for node in candidates}
        spans = _linked_spans(note_text)
        out: list[LinkSuggestion] = []
        seen: set[tuple[str, str]] = set()
        for line in raw.splitlines():
            if "\t" not in line:
                continue
            target, mention = (part.strip() for part in line.split("\t", 1))
            if target not in by_id or not mention:
                continue
            # Reject any mention the model did not ground in the source note, and
            # bind the suggestion to the note's own text, not the model's echo (F10).
            occurrence = _unlinked_occurrence(note_text, mention, spans)
            if occurrence is None:
                continue
            surface = note_text[occurrence[0] : occurrence[1]]
            key = (target, surface.lower())
            if key in seen:
                continue
            seen.add(key)
            markdown = (
                f"[[{target}]]" if surface.lower() == target.lower() else f"[[{target}|{surface}]]"
            )
            out.append(
                LinkSuggestion(
                    source_note=source_id,
                    target_note=target,
                    mention=surface,
                    confidence=0.8,
                    reason="LLM-proposed entity mention (requires human approval)",
                    suggested_markdown=markdown,
                )
            )
        return out


def propose_links(
    graph: VaultGraph,
    note_id: str,
    *,
    suggester: LinkSuggester | None = None,
    note_text: str | None = None,
    vault_dir: str | Path | None = None,
) -> list[LinkSuggestion]:
    """Propose links for ``note_id`` against every other resolved node.

    Supply ``note_text`` directly, or ``vault_dir`` to read it from the note's
    file. Returns suggestions for human approval — nothing is written.
    """
    if note_text is None:
        node = graph.get_node(note_id)
        if node is None or node.relpath is None or vault_dir is None:
            return []
        note_text = (Path(vault_dir) / node.relpath).read_text(encoding="utf-8")

    candidates = [n for n in graph.nodes() if n.resolved and n.id != note_id]
    engine = suggester or HeuristicLinkSuggester()
    return engine.suggest(note_id, note_text, candidates)
