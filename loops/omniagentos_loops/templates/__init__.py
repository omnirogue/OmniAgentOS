"""The loop template catalogue.

A template is a *named graph builder*; a registered workflow is a routine row
naming one. Adding loop #6..#500 is therefore instantiation, not engineering —
which was the whole point of Phase 1.
"""

from __future__ import annotations

from omniagentos_loops.runtime import LoopTemplate
from omniagentos_loops.templates import (
    dispatch_await_summarize,
    draft_approve_send,
    generate_evaluate_improve,
    monitor_diagnose_repair_verify,
    poll_classify_act_verify,
)

TEMPLATES: dict[str, LoopTemplate] = {
    template.name: template
    for template in (
        poll_classify_act_verify.TEMPLATE,
        monitor_diagnose_repair_verify.TEMPLATE,
        draft_approve_send.TEMPLATE,
        generate_evaluate_improve.TEMPLATE,
        dispatch_await_summarize.TEMPLATE,
    )
}

#: Name-only view, safe to import from a context without LangGraph.
TEMPLATE_NAMES: frozenset[str] = frozenset(TEMPLATES)


def get_template(name: str) -> LoopTemplate:
    template = TEMPLATES.get(name)
    if template is None:
        raise KeyError(f"unknown loop template {name!r}; known: {sorted(TEMPLATES)}")
    return template


__all__ = ["TEMPLATES", "TEMPLATE_NAMES", "get_template"]
