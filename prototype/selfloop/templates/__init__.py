"""The template catalogue. A template is a NAMED GRAPH BUILDER and nothing more.

There is no plugin system here, no entry-point scan and no directory that gets
walked at import. A template is a value: a name, the tools its instance must
grant, and a function that turns a :class:`~selfloop.context.LoopContext` into a
compiled graph. Adding one is *write the file, add one line to the tuple at the
bottom of this module*; adding one from your own package is
``register_template(LoopTemplate(...))`` at any point before you call it.

Keeping the catalogue a plain dict rather than a discovery mechanism is what
makes "which templates can this process run?" answerable by reading one line
instead of by reproducing an import order.

**Why ``build`` returns a compiled graph rather than a description.** A template
is the only thing that knows its own bound. ``propose_evaluate_promote`` derives
its per-tick node ceiling from ``max_rounds``, so that a cycle which fails to
converge stops against a number computed from its own declared limit. Handing the
compile step to the caller would put that number somewhere the template cannot
reach, and the caller would have to guess it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from selfloop.context import LoopContext
from selfloop.engine import CompiledGraph


@dataclass(frozen=True)
class LoopTemplate:
    """One named graph builder — the reusable primitive an instance names.

    ``required_tools`` is a promise the template makes about its instance
    contract, and :meth:`missing_tools` is what turns that promise into a check.
    A declared-and-never-enforced requirement is worse than none: it reads like a
    guarantee and behaves like a comment, and the failure it hides — a template
    reaching for a tool nobody granted — surfaces halfway through a tick, after
    the earlier nodes have already touched the world.
    """

    name: str
    #: A coarse grouping for operators reading a catalogue ("observe_act",
    #: "refine"). It carries no behaviour; two templates in one family share a
    #: shape, not an implementation.
    family: str
    required_tools: tuple[str, ...]
    build: Callable[[LoopContext], CompiledGraph]
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("a LoopTemplate needs a non-empty name; it is the catalogue key")
        if not callable(self.build):
            raise ValueError(f"template {self.name!r}: build must be callable")
        tools = tuple(str(tool) for tool in self.required_tools)
        if any(not tool for tool in tools):
            raise ValueError(
                f"template {self.name!r}: required_tools contains an empty name — an "
                "unnamed requirement can never be checked against a registry"
            )
        object.__setattr__(self, "required_tools", tools)

    def missing_tools(self, ctx: LoopContext) -> tuple[str, ...]:
        """Required tools this context has not granted, in declaration order.

        Empty means the instance contract is met. A caller should refuse to run a
        tick when this is non-empty rather than discovering it node by node: the
        tools are checked before anything executes, so nothing has been done to
        the world by the time the refusal is reported.
        """
        granted = ctx.tools.names()
        return tuple(tool for tool in self.required_tools if tool not in granted)


#: Every template this process can run, by name. Written only through
#: :func:`register_template`.
TEMPLATES: dict[str, LoopTemplate] = {}


def register_template(template: LoopTemplate) -> LoopTemplate:
    """Add *template* to the catalogue and return it. Refuses to replace a name.

    Silently replacing a registered name would let a later import change what an
    existing instance's ``template`` field means — the same instance id, the same
    checkpoint thread, and a different graph resuming into node names it does not
    have. That resolves to a silent no-op rather than a crash, which is why this
    refuses instead of merging.
    """
    if not isinstance(template, LoopTemplate):
        raise TypeError(
            f"register_template expects a LoopTemplate, got {type(template).__name__}"
        )
    existing = TEMPLATES.get(template.name)
    if existing is not None and existing is not template:
        raise ValueError(
            f"template {template.name!r} is already registered; refusing to replace it. "
            "Register your variant under its own name — a live instance resuming into "
            "a different graph under the same name is a silent no-op, not an error."
        )
    TEMPLATES[template.name] = template
    return template


def get_template(name: str) -> LoopTemplate:
    """The registered template called *name*, or ``KeyError`` naming the known ones.

    The message lists the catalogue because the overwhelmingly common cause is a
    typo or a template that was never registered, and an operator should not have
    to read this module's source to find that out.
    """
    template = TEMPLATES.get(name)
    if template is None:
        raise KeyError(f"unknown loop template {name!r}; registered: {sorted(TEMPLATES)}")
    return template


# The shipped templates are imported HERE, below LoopTemplate, and not at the top
# of this module. Each of them does ``from selfloop.templates import LoopTemplate``
# to declare its own TEMPLATE value, so this import edge is a cycle that resolves
# only because the class is already bound by the time it is traversed. Moving
# these two lines to the top of the file — which is where a formatter will want to
# put them — turns that into an ImportError on ``import selfloop.templates``.
from selfloop.templates import (  # noqa: E402 - see the comment above
    observe_decide_act_verify,
    propose_evaluate_promote,
)

for _shipped in (
    observe_decide_act_verify.TEMPLATE,
    propose_evaluate_promote.TEMPLATE,
):
    register_template(_shipped)


__all__ = [
    "TEMPLATES",
    "LoopTemplate",
    "get_template",
    "register_template",
]
