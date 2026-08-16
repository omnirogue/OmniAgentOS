"""selfloop — one durable, unattended, self-improving loop tick per process.

You hand it a :class:`~selfloop.context.LoopContext` (tools plus ports), name a
template, and call ``run_once()``. That is the whole surface. It is a library and
not a framework: nothing here subclasses your code, scans your directories, or
runs at import.

The thesis, in one sentence: *a learning loop is only worth running unattended if
its promotion gate IS its effect gate.* A lesson the loop learned is promoted
through the same approval machinery an outbound email is sent through — a
T0/T1-scoped lesson auto-promotes on evidence, a T2+-scoped lesson parks for a
human — and in both cases the promotion is receipted, so a crash mid-promotion
can neither double-write nor silently drop. The risk tiers, the receipts, the
append-only ledger and the counterfeit corpus are not scaffolding around the
learner; they are the learner's own promotion path.

**Importing this package does nothing.** No ``sys.path`` mutation, no repository
root guessed from ``__file__``, no directory created, no environment variable
written, no network. The predecessor mutated ``sys.path`` at import and derived a
repository root from its own location, which was the single hardest blocker to
ever pip-installing it: the moment the package moved, that path resolved
somewhere arbitrary and checkpoints and lease files were written into it.

The heavier names below are resolved lazily on first attribute access (PEP 562),
so ``import selfloop`` costs four small pure modules and pulls in neither
``sqlite3`` nor ``subprocess`` unless you actually use them.
"""

from __future__ import annotations

from typing import Any

from selfloop.context import LoopContext
from selfloop.contracts import (
    ActionClass,
    ApprovalState,
    BlockedLoopError,
    EffectDenied,
    EffectNotApproved,
    EffectStateUnknown,
    EffectUnavailable,
    EvidenceGrade,
    GateReceipt,
    GateSpec,
    GateUnavailable,
    GateVerdict,
    LearningSignal,
    Lesson,
    LessonStatus,
    LoopError,
    LoopState,
    LoopStatus,
    LoopTool,
    PolicyDecision,
    RecordKind,
    RiskTier,
    RunReport,
    ToolRegistry,
    TransientLoopError,
    Verification,
)

__version__ = "0.1.0"

#: Names that live in modules with heavier imports (an executor, a template
#: registry, the learning pass). Mapped rather than imported so that the cost of
#: ``import selfloop`` stays flat and so that a caller who only wants the
#: vocabulary never loads the runtime.
_LAZY: dict[str, tuple[str, str]] = {
    "run_once": ("selfloop.runtime", "run_once"),
    "get_template": ("selfloop.templates", "get_template"),
    "register_template": ("selfloop.templates", "register_template"),
    "recall": ("selfloop.learn", "recall"),
}


def __getattr__(name: str) -> Any:
    """Resolve a lazily-exported public name on first access (PEP 562)."""
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    import importlib

    return getattr(importlib.import_module(module_name), attribute)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


__all__ = [
    "ActionClass",
    "ApprovalState",
    "BlockedLoopError",
    "EffectDenied",
    "EffectNotApproved",
    "EffectStateUnknown",
    "EffectUnavailable",
    "EvidenceGrade",
    "GateReceipt",
    "GateSpec",
    "GateUnavailable",
    "GateVerdict",
    "LearningSignal",
    "Lesson",
    "LessonStatus",
    "LoopContext",
    "LoopError",
    "LoopState",
    "LoopStatus",
    "LoopTool",
    "PolicyDecision",
    "RecordKind",
    "RiskTier",
    "RunReport",
    "ToolRegistry",
    "TransientLoopError",
    "Verification",
    "__version__",
    "get_template",
    "recall",
    "register_template",
    "run_once",
]
