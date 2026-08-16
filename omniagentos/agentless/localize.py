"""Localization: turn a task description into a small, ranked focus set.

This is the step that makes Agentless beat agentic wandering (arXiv 2407.01489):
instead of letting a model spend turns exploring the repo, we do ONE cheap,
deterministic pass over :mod:`omniagentos.repomap` to find the files/symbols most
relevant to the task, then hand every sample the SAME focused context. Two signals
seed repomap's personalized PageRank: ``focus_files`` (paths the task mentions
verbatim, if they exist in the repo) and ``focus_terms`` (identifier-like tokens in
the task text, e.g. function/class names), mirroring what
``ranking.personalization_vector`` expects.
"""

from __future__ import annotations

import os
import re

from omniagentos.agentless.contracts import LocalizationResult, SymbolRef
from omniagentos.repomap.ranking import (
    build_graph,
    build_index,
    common_names,
    pagerank,
    personalization_vector,
    rank_definitions,
)
from omniagentos.repomap.service import render_map
from omniagentos.repomap.tags import Definition, FileTags, iter_source_files, read_and_extract

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
# Generic English words that happen to look like identifiers but carry no
# localization signal (would otherwise flood focus_terms with noise).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "that",
        "this",
        "with",
        "from",
        "should",
        "when",
        "task",
        "please",
        "make",
        "sure",
        "add",
        "fix",
        "update",
        "change",
        "function",
        "method",
        "class",
        "file",
        "code",
        "test",
        "tests",
        "bug",
        "issue",
        "error",
        "returns",
        "return",
        "value",
        "does",
        "not",
    }
)
_PATH_TOKEN_RE = re.compile(r"[\w./\\-]+\.\w+")


def _candidate_paths(task_text: str, repo_dir: str) -> list[str]:
    """Paths the task mentions verbatim that actually exist under repo_dir."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _PATH_TOKEN_RE.finditer(task_text):
        token = match.group(0).strip("`'\"()[]{},:;")
        if not token or token in seen:
            continue
        seen.add(token)
        candidate = token.lstrip("./")
        abs_candidate = os.path.join(repo_dir, candidate)
        if os.path.isfile(abs_candidate):
            found.append(candidate)
    return found


def _candidate_terms(task_text: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for match in _IDENT_RE.finditer(task_text):
        token = match.group(0)
        lowered = token.lower()
        if lowered in _STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        terms.append(token)
    return terms


def _rank(
    all_tags: list[FileTags],
    focus_files: list[str] | None,
    focus_terms: list[str] | None,
) -> list[Definition]:
    index = build_index(all_tags)
    common = common_names(all_tags)
    nodes, out_edges = build_graph(all_tags, index, common)
    teleport = personalization_vector(all_tags, focus_files, focus_terms)
    file_rank = pagerank(nodes, out_edges, personalization=teleport)
    if teleport:
        top = max(file_rank.values(), default=0.0) or 1.0
        for path, weight in teleport.items():
            if path in file_rank:
                file_rank[path] = top * (8.0 + weight)
    return rank_definitions(all_tags, index, file_rank, common)


def localize(
    repo_dir: str,
    task_text: str,
    *,
    max_files: int = 5,
    map_tokens: int = 1024,
) -> LocalizationResult:
    """Rank the repo's files/symbols against ``task_text`` and return the focus set.

    ``max_files`` bounds how many distinct files land in ``focus_files``;
    ``map_tokens`` bounds the rendered repo-map size handed to the prompt builder."""
    repo_dir = os.path.abspath(repo_dir)
    focus_files_seed = _candidate_paths(task_text, repo_dir)
    focus_terms = _candidate_terms(task_text)

    all_tags = [
        read_and_extract(path, os.path.relpath(path, repo_dir))
        for path in iter_source_files(repo_dir)
    ]
    if not all_tags:
        return LocalizationResult(
            repo_dir=repo_dir, focus_files=focus_files_seed[:max_files], top_symbols=[], repo_map=""
        )

    ranked = _rank(all_tags, focus_files_seed or None, focus_terms or None)
    repo_map = render_map(ranked, max_tokens=map_tokens)

    focus_files: list[str] = list(focus_files_seed)
    for definition in ranked:
        if len(focus_files) >= max_files:
            break
        if definition.rel_path not in focus_files:
            focus_files.append(definition.rel_path)
    focus_files = focus_files[:max_files]

    top_symbols = [
        SymbolRef(
            rel_path=d.rel_path,
            name=d.name,
            line=d.line,
            signature=d.signature,
        )
        for d in ranked[: max(max_files * 4, 10)]
    ]

    return LocalizationResult(
        repo_dir=repo_dir,
        focus_files=focus_files,
        top_symbols=top_symbols,
        repo_map=repo_map,
    )
