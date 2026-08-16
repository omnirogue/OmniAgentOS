"""Who writes a lesson's *guidance* — the text that gets injected back into the loop.

This module exists because the question had no answer, and every unstated answer
was bad.

A promoted lesson's ``guidance`` is the only part of it that reaches the next
run's prompt. The design that this package was extracted from never said where
that text comes from, and the three implicit options are each a distinct defect:

* **guidance = the claim.** The claim is a description of a failure ("http call
  timed out after 30s"), so injecting it tells the next run "you failed with tag
  X" and asks it to infer the remedy. That is not a lesson; it is a complaint.
* **guidance written by a model.** This is the one that looks most attractive and
  is the most dangerous, because it reintroduces exactly the thing the whole
  package refuses: free-form actor prose becoming the promoted artefact. The
  predecessor promoted knowledge by matching an agent's own output text against
  a fact id, so an agent that mentioned ``fact_id=42`` in its report promoted
  fact 42. A model that writes the guidance for a cluster mined from its own
  failures is the same loop with a longer sentence in it.
* **guidance written by a human, always.** Honest, and it makes the machine's
  half of the loop pointless: nothing can ever auto-promote, so the loop learns
  at the speed of a person's inbox, which in the source system was zero.

**The rule this module implements: guidance is a TEMPLATE FILL, and it is pure.**
:func:`guidance_for` reads one thing the machine did not write — the caller's
``remedy_table``, which maps a structured failure tag to a sentence a human
typed once, in advance, for that class of failure. The machine decides *which*
remedy applies by mining evidence; the human decided *what the remedy says*
before any evidence existed. Neither half can be forged by the actor.

**Free-form model text is banned as the sole promotion payload in v1.** A model
may legitimately write a lesson's ``claim`` — that is a description of evidence
and it is never injected as an instruction — but the payload that reaches the
next prompt must be template-derived or must carry a human's decision through the
approval gate. A cluster whose failure tag has no remedy in the table yields
guidance marked with :data:`NEEDS_HUMAN_MARKER`, :func:`is_template_derived`
returns ``False`` for it, and :func:`selfloop.learn.stage` forces that lesson to
the approval floor tier so a person writes the text and approves it.

Nothing here touches a clock, a store, a context or a model. Two callers with
the same cluster and the same table get the same string, forever, which is what
lets a lesson's content fingerprint mean anything at all.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

#: Prefix on any guidance this module could not derive from the remedy table.
#:
#: A sentinel rather than a heuristic: :func:`is_template_derived` tests this
#: exact prefix and nothing else, so "was this text written by the template or
#: not?" has one answer that a reader, a test and a counterfeit mutation can all
#: aim at. It is also the right thing for an operator to see on the approvals
#: page — a lesson whose guidance opens with ``[needs-human]`` is asking for a
#: sentence, not for a rubber stamp.
#:
#: The marker is a convention and is spoofable by anyone who can edit the stored
#: text, and that is deliberately not load-bearing: the tier decision it drives
#: is taken ONCE, at staging, and stored on the lesson row, so editing the text
#: afterwards cannot lower a lesson's tier — it only breaks the content
#: fingerprint, which makes the row unpromotable and unrecallable.
NEEDS_HUMAN_MARKER = "[needs-human]"

#: The one sentence shape a promoted lesson may carry. Deliberately rigid: a
#: reader of an injected block can tell at a glance that every line in it was
#: filled from a table rather than composed, and a template with one hole is a
#: template whose output cannot smuggle an instruction.
GUIDANCE_TEMPLATE = "when {failure_tag} then {remedy}"

#: Hard cap on a remedy sentence. The injected block competes for the same
#: context the task needs, so an unbounded remedy is how a "memory" feature
#: becomes a quality regression. Truncation is marked, never silent.
MAX_REMEDY_CHARS = 400

#: Appended when a remedy is truncated, so a reader can tell a short sentence
#: from a cut one.
TRUNCATION_SUFFIX = " …"

_WHITESPACE = re.compile(r"\s+")


class ClusterView(Protocol):
    """The three fields :func:`guidance_for` reads from a cluster.

    A structural type and not an import, because the concrete cluster lives in
    :mod:`selfloop.learn` and ``learn`` imports **this** module. Declaring the
    shape here keeps that edge one-directional on paper as well as in practice:
    there is no import of ``learn`` in this file, not even under
    ``TYPE_CHECKING``, so no future refactor can turn the dependency into a
    cycle by accident.

    It also states the honest minimum. Guidance is a function of the failure tag
    and nothing else; ``scope`` and ``claim`` are here so the module can say
    something useful when there is no remedy, and neither of them may influence
    the remedy that is chosen.
    """

    scope: str
    failure_tag: str
    claim: str


def _one_line(text: str) -> str:
    """Collapse *text* to a single trimmed line.

    A remedy is rendered into a prompt block whose structure is one lesson per
    line, so a table entry containing a newline would break the block's shape and
    let a caller's own text look like a second lesson. Collapsing here rather
    than at render time means the fingerprinted content and the rendered content
    are the same bytes.
    """
    return _WHITESPACE.sub(" ", str(text)).strip()


def guidance_for(cluster: ClusterView, remedy_table: Mapping[str, str]) -> str:
    """The guidance text for *cluster*: ``when <failure_tag> then <remedy>``.

    Pure and model-free. The only input that is not derived from the cluster is
    *remedy_table* — ``failure_tag -> remedy sentence`` — which the caller
    supplies on :class:`~selfloop.context.LoopContext` and which a human writes
    in advance, before any evidence exists. That ordering is the whole safety
    argument: the machine chooses which pre-authored sentence applies, and never
    authors one.

    Returns a string beginning with :data:`NEEDS_HUMAN_MARKER` when the tag has
    no usable entry in the table. That is not a failure and not a placeholder —
    it is the honest statement that nobody has written down what to do about this
    class of failure, and it carries the consequence with it:
    :func:`selfloop.learn.stage` reads :func:`is_template_derived` and forces
    such a lesson to the approval floor tier, so it can only ever be promoted by
    a person who has replaced the text.

    A blank or whitespace-only remedy counts as absent. An operator who left the
    value empty has not written a remedy, and treating an empty string as one
    would inject the sentence ``when timeout then`` into the next prompt.
    """
    tag = _one_line(cluster.failure_tag)
    if not tag:
        # A cluster cannot legally reach here without a tag — LearningSignal's
        # constructor refuses one, and clustering partitions by it — so an empty
        # tag means a caller built a cluster by hand. Refusing to render is the
        # fail-closed answer; a lesson keyed on nothing would match everything.
        return (
            f"{NEEDS_HUMAN_MARKER} this candidate carries no structured failure tag, so no "
            "remedy can be selected for it; a human must decide what it is about"
        )

    remedy = _one_line(remedy_table.get(tag, ""))
    if not remedy:
        claim = _one_line(cluster.claim)
        seen = f' observed as "{claim}"' if claim else ""
        return (
            f"{NEEDS_HUMAN_MARKER} no remedy is recorded for failure tag {tag!r} in this "
            f"loop's remedy table{seen}; a human must write the guidance before this lesson "
            "can be injected"
        )

    if len(remedy) > MAX_REMEDY_CHARS:
        remedy = remedy[:MAX_REMEDY_CHARS].rstrip() + TRUNCATION_SUFFIX

    return GUIDANCE_TEMPLATE.format(failure_tag=tag, remedy=remedy)


def is_template_derived(guidance: str) -> bool:
    """Was *guidance* filled from the remedy table, rather than owed to a human?

    ``False`` for empty text and for anything carrying
    :data:`NEEDS_HUMAN_MARKER`. Both readings are fail-closed on purpose: a
    lesson with no guidance has nothing to inject, and a lesson whose guidance is
    a request for a sentence must not auto-promote into a prompt.

    Callers use this to decide a TIER, never to decide whether to store
    something. A lesson that needs a human is still staged, still accumulates
    evidence, and still appears on the approvals page — it simply cannot be
    promoted without one.
    """
    return bool(guidance.strip()) and not guidance.lstrip().startswith(NEEDS_HUMAN_MARKER)


__all__ = [
    "GUIDANCE_TEMPLATE",
    "MAX_REMEDY_CHARS",
    "NEEDS_HUMAN_MARKER",
    "TRUNCATION_SUFFIX",
    "ClusterView",
    "guidance_for",
    "is_template_derived",
]
