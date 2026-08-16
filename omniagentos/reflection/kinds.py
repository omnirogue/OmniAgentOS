"""The one routing table for reflection proposal kinds.

Every stage of the reflection loop has to answer the same two questions about a
proposal: *may this kind be applied at all*, and *which file will it actually
write*.  Until now each stage answered from its own private copy of the
literals — ``validate.validate_proposal`` had ``allowed_kinds``,
``validate.resolve_target_path`` had one fallback table, ``apply.apply_proposal``
had the yaml/document split, ``apply.write_document_change`` had a second
fallback table, and ``fable_gate._eligibility`` had a third (in a comment).

They drifted, and the drift was exploitable.  ``skill`` was added to the writer's
document branch and to ``allowed_kinds`` but never to the resolver, so
``resolve_target_path("skill", {})`` returned ``None``, the hard-stop gate was
skipped, and the writer's ``else: file_path = "AGENTS.md"`` appended the proposal
to the agents' own instruction file — unreviewed, on a surface an LLM system
executes.  Adding ``"skill"`` to one more list would have fixed that instance and
left the class untouched: the next kind added would land in exactly the same
hole.

So the table lives here, once, and everything else imports it.  Two rules make
it fail closed:

1. **A kind not listed here is refused**, by the validator *and* by the writers.
   There is no default branch that routes an unknown kind anywhere.
2. **A kind with no entry in** :data:`DOCUMENT_FALLBACKS` **has no default
   document.**  Absent a target it resolves to ``None``, and ``None`` means
   refuse — never "write the default file".

This module deliberately owns no I/O and imports nothing from ``validate`` or
``apply``, so both can depend on it without a cycle.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from omniagentos.contracts import utc_now_iso

#: Kinds applied by editing a YAML file under ``configs/``.
#: Mirrors :func:`omniagentos.reflection.apply.apply_yaml_change`, which reads
#: ``target["file"]`` ONLY — never ``target["doc"]``.
CONFIG_KINDS: frozenset[str] = frozenset(
    {
        "model_config",
        "formation",
        "router_weight",
        "effort_override",
        "category_pin",
        "rollback",
    }
)

#: Kinds applied by appending to a markdown document.
#: Mirrors :func:`omniagentos.reflection.apply.write_document_change`, which
#: reads ``target["doc"] or target["file"]``.
DOCUMENT_KINDS: frozenset[str] = frozenset({"lesson", "brief_template", "skill"})

#: The complete set a proposal's ``kind`` may take.  Anything else is refused.
ALLOWED_KINDS: frozenset[str] = CONFIG_KINDS | DOCUMENT_KINDS

#: Kinds the nightly loop may apply with NO human in the loop.
#:
#: Deliberately a strict subset of :data:`DOCUMENT_KINDS`, and deliberately NOT
#: derived from it: adding a document kind must not silently grant it unattended
#: write access.  ``skill`` is excluded — a skill is executable guidance, so an
#: unattended append to it is unreviewed instruction to the fleet.
#: (``model_config`` availability flags are also auto-appliable, but only under
#: a per-target condition, so that stays a rule in ``apply.py`` rather than a
#: membership test here.)
UNATTENDED_APPLY_KINDS: frozenset[str] = frozenset({"lesson", "brief_template"})


def _dated_lesson_path() -> str:
    return f"docs/lessons/{utc_now_iso()[:10]}-reflection.md"


#: Where a document proposal goes when it names no target.
#:
#: ``lesson`` is the only kind with a legitimate default: it creates a new,
#: dated file in the lessons directory — a place whose whole purpose is to
#: receive appended lessons.  Every other kind is absent ON PURPOSE.  A
#: ``brief_template`` or ``skill`` with no target used to be routed to
#: ``AGENTS.md``; that is not a default, it is a privilege escalation, and the
#: escalation is what this table's emptiness prevents.
#:
#: DO NOT add an entry here to "make a kind work".  A kind that cannot name its
#: own target is unroutable, and unroutable means refused.
DOCUMENT_FALLBACKS: Mapping[str, Callable[[], str]] = {
    "lesson": _dated_lesson_path,
}


def document_fallback_path(kind: str) -> str | None:
    """The default document for ``kind``, or ``None`` when it has none.

    ``None`` is an instruction to REFUSE, never an instruction to pick a
    default.
    """
    factory = DOCUMENT_FALLBACKS.get(kind)
    return factory() if factory is not None else None


def config_target_path(target: Any) -> str | None:
    """The YAML file a config proposal names — ``target["file"]`` only.

    Called by :func:`omniagentos.reflection.apply.apply_yaml_change`, which is
    why ``doc`` is deliberately NOT consulted here.
    """
    if isinstance(target, Mapping):
        raw = target.get("file")
    elif isinstance(target, str):
        raw = target
    else:
        raw = None
    return str(raw) if raw else None


def document_target_path(target: Any) -> str | None:
    """The document a doc proposal names — ``doc`` first, then ``file``.

    Called by :func:`omniagentos.reflection.apply.write_document_change`.
    """
    if isinstance(target, Mapping):
        raw = target.get("doc") or target.get("file")
    elif isinstance(target, str):
        raw = target
    else:
        raw = None
    return str(raw) if raw else None


def target_key(target: Any) -> str | None:
    """The ``key`` a proposal names, read once for every stage that needs it.

    The hard-stop set covers keys as well as paths (a budget or credential knob
    is refused wherever it lives), so the validator and the writers must read
    ``key`` the same way for the same reason they must read ``file`` the same
    way — a stage that reads it differently is checking something the writer is
    not writing.
    """
    if isinstance(target, Mapping):
        raw = target.get("key")
        return str(raw) if raw else None
    return None


def explicit_target_path(kind: str, target: Any) -> str | None:
    """The path spelled out in ``target``, read the way that kind's WRITER reads it.

    The two writers read different keys, and that difference is itself a
    hazard: ``apply_yaml_change`` uses ``target["file"]`` only, so a config
    proposal carrying ``{"doc": "docs/harmless.md", "file":
    "configs/governance.yaml"}`` would have been validated against the harmless
    doc and then written to governance.yaml.  Resolving per-kind here keeps the
    check and the write pointed at the same file.
    """
    if kind in DOCUMENT_KINDS:
        return document_target_path(target)
    return config_target_path(target)


def resolve_target_path(kind: str, target: Any) -> str | None:
    """The path a proposal of ``kind`` will actually be written to.

    THE VALIDATOR AND THE WRITERS MUST BOTH CALL THIS.  ``None`` means the
    proposal is unroutable and must be refused — by every caller, including the
    ones reachable without validation (the dashboard's ``/approve`` route calls
    ``apply_proposal`` directly).
    """
    if kind not in ALLOWED_KINDS:
        return None
    explicit = explicit_target_path(kind, target)
    if explicit:
        return explicit
    return document_fallback_path(kind)
