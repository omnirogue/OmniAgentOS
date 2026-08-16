"""Reflexion (arXiv:2303.11366) reflection builder for the routing cascade.

WHY: FrugalGPT's cascade (``omniagentos.routing.cascade.run_cascade``)
escalates blind by default -- the next, more expensive tier gets no
information about WHY the cheap tier's attempt failed verification.
Reflexion closes that gap: when a verified failure happens, this module
produces ONE paragraph explaining the failure and what the next attempt must
do differently, and the cascade threads it into the next tier's ``work()``
call as its ``reflection`` argument.

Adapter-agnostic on purpose, matching ``cascade.py``: ``build_reflection``
takes an optional object with a ``.run(AgentInput) -> AgentResult`` method
(the caller resolves it, e.g. via
``omniagentos.adapters.registry.resolve_adapter`` -- this module NEVER
resolves an adapter itself) and falls back to a fully deterministic TEMPLATE
reflection on ANY failure (no adapter given, the adapter raising, or an
empty reply), so a broken or unavailable LLM call never blocks the cascade
from escalating.

SEPARATE STORE, NOT ``skills.py``/``constraints.py``: this package's existing
capture surfaces (``omniagentos.selfimprove.skills.capture_skill``,
``omniagentos.selfimprove.constraints.append_constraint``) enforce a HARD
RULE -- they refuse to write unless the supplied ``VerificationGate.status``
is ``GateStatus.PASSED`` (an unverified workflow would poison every future
run that reuses it). A Reflexion paragraph is the OPPOSITE: it exists
*because* verification FAILED. ``persist_reflection`` therefore writes to
its own JSONL store under a caller-supplied ``store_dir`` and never calls
into, or shares an on-disk root with, ``skills.py``/``constraints.py`` --
mixing the two would let an unverified failure narrative leak into the
verified-only capture surfaces those modules exist to protect.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from omniagentos.contracts import AgentInput, AgentResult, ResultStatus, new_id

_LLM_EVIDENCE_CHARS = 4000
_TRUNCATION_MARKER = "\n...[truncated]...\n"
_ERROR_KEYWORDS = ("error", "assert", "traceback", "exception", "failed", "failure")


class ReflectionAdapter(Protocol):
    """Structural shape this module needs from an adapter -- exactly the
    ``AgentAdapter.run`` slice of ``omniagentos.contracts.AgentAdapter``.
    Callers may pass any object satisfying this (including
    ``omniagentos.mock_adapter.MockAdapter`` in tests); this module never
    resolves one itself."""

    def run(self, input: AgentInput) -> AgentResult: ...


@dataclass(frozen=True)
class Reflection:
    """One built reflection paragraph plus where it came from."""

    paragraph: str
    source: Literal["llm", "template"]


def _truncate_evidence(evidence: str, max_chars: int) -> str:
    """Truncate ``evidence`` to (approximately) ``max_chars``, keeping BOTH
    the head and the tail -- failures often have the informative assertion
    at the top and the actual stack trace/final error at the bottom, and a
    naive head-only or tail-only truncation would drop one of them."""
    if len(evidence) <= max_chars:
        return evidence
    marker_len = len(_TRUNCATION_MARKER)
    remaining = max(0, max_chars - marker_len)
    if remaining <= 0:
        return evidence[:max_chars]
    head_len = remaining // 2
    tail_len = remaining - head_len
    tail = evidence[-tail_len:] if tail_len else ""
    return evidence[:head_len] + _TRUNCATION_MARKER + tail


def _extract_error_lines(evidence: str, limit: int = 5) -> list[str]:
    """Deterministically pull the first ``limit`` lines that look like an
    error/assertion/traceback line out of ``evidence``, in original order."""
    lines: list[str] = []
    for raw_line in evidence.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(keyword in lowered for keyword in _ERROR_KEYWORDS):
            lines.append(stripped)
        if len(lines) >= limit:
            break
    return lines


def _template_reflection(task_summary: str, evidence: str, max_chars: int) -> str:
    """Fully deterministic fallback: extract the first error/assert/traceback
    lines from ``evidence`` (or, absent any, the first non-blank line) and
    frame them with a "next attempt must address this" instruction, capped
    at ``max_chars``."""
    error_lines = _extract_error_lines(evidence)
    if error_lines:
        cause = " | ".join(error_lines)
    else:
        first_lines = [line.strip() for line in evidence.splitlines() if line.strip()]
        cause = first_lines[0] if first_lines else "no evidence was captured for this attempt"

    paragraph = (
        f"Attempt at '{task_summary.strip()}' failed: {cause}. "
        "The next attempt must address this specific failure directly -- "
        "reproduce it, fix the root cause it points to, and re-verify before "
        "declaring success, rather than repeating the same approach unchanged."
    )
    if len(paragraph) > max_chars:
        cutoff = max(0, max_chars - 1)
        paragraph = paragraph[:cutoff].rstrip() + "…"
    return paragraph


def _llm_reflection(
    task_summary: str, evidence: str, adapter: ReflectionAdapter, max_chars: int
) -> str:
    """One cheap LLM call asking for a single-paragraph failure reflection.
    Returns ``""`` (never raises) on a non-OK result -- the caller treats
    empty as "fall back to the template"."""
    truncated_evidence = _truncate_evidence(evidence, _LLM_EVIDENCE_CHARS)
    prompt = (
        "In ONE paragraph explain why this attempt failed and what the next "
        "attempt must do differently.\n\n"
        f"Task: {task_summary}\n\nEvidence:\n{truncated_evidence}"
    )
    agent_input = AgentInput(run_id=new_id("run"), task_id=new_id("tsk"), prompt=prompt)
    result = adapter.run(agent_input)
    if result.status != ResultStatus.OK:
        return ""
    text = result.output_text.strip()
    return text[:max_chars] if text else ""


def build_reflection(
    task_summary: str,
    evidence: str,
    *,
    adapter: ReflectionAdapter | None = None,
    max_chars: int = 1200,
) -> Reflection:
    """Build a one-paragraph Reflexion-style failure reflection.

    With ``adapter`` given, tries one LLM call first; on ANY exception, or an
    empty/whitespace-only reply, falls back to the deterministic template
    (never raises either way). Without ``adapter``, goes straight to the
    template.
    """
    if adapter is not None:
        try:
            paragraph = _llm_reflection(task_summary, evidence, adapter, max_chars)
        except Exception:  # noqa: BLE001 -- any adapter failure degrades to template
            paragraph = ""
        if paragraph:
            return Reflection(paragraph=paragraph, source="llm")

    return Reflection(
        paragraph=_template_reflection(task_summary, evidence, max_chars),
        source="template",
    )


def persist_reflection(reflection: Reflection, *, task_class: str, store_dir: str) -> str:
    """Append ``{ts, task_class, source, paragraph}`` as one JSONL line under
    ``store_dir`` (created if missing) and return the path written to.

    Deliberately its own file (``reflections.jsonl``) in a caller-chosen
    directory -- see the module docstring for why this must never be
    ``skills.py``/``constraints.py``'s storage roots.
    """
    os.makedirs(store_dir, exist_ok=True)
    path = os.path.join(store_dir, "reflections.jsonl")
    row = {
        "ts": time.time(),
        "task_class": task_class,
        "source": reflection.source,
        "paragraph": reflection.paragraph,
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return path


__all__ = [
    "Reflection",
    "ReflectionAdapter",
    "build_reflection",
    "persist_reflection",
]
