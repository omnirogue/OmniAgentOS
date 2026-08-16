"""The mechanical review suite: eight "exactly one place" rules, checked by AST.

Every guarantee in this package is worth exactly as much as the number of places
that can violate it. A rule with one enforcement site is a rule a reviewer can
read end to end and a counterfeit mutation can be aimed at; the same rule with
three sites is a convention, and a convention is what the system this package was
extracted from had when a reviewer executed an irreversible effect through three
separate routes to the same raw callable.

So these tests do not exercise behaviour. They parse ``selfloop/`` and count
call sites. Each one pins a sentence that appears in a module docstring, and the
pairing is deliberate: a docstring that says "the ONLY place" and a test that
counts to one are the same claim written twice, and the day somebody adds a
second place the prose and the code disagree loudly instead of quietly.

What is checked
---------------

============================================  ======================================
invariant                                     the failure it forecloses
============================================  ======================================
one ``tool.call(...)`` site                   a second door into the tool plane
one ``_IN_SEAM.set(...)`` site                a ticket minted where nothing checks it
``_invoke_in_seam`` is module-private         the seam's front door standing open
no module calls a callable it registers       the one hole the closure-seal cannot
                                              close: the module that KEPT its own
                                              name for the implementation
one ``raise ParkRequested``                   a second park protocol nobody wrote
one ``approvals.create(...)``                 a row minted without a binding
receipts written from one module              an effect completed without a receipt
one ``lesson_block(...)`` call                an unauditable feedback edge
============================================  ======================================

**What this suite does NOT promise**, stated because an overclaimed boundary is
worse than an honest one. It scans ``selfloop/`` only, and the entire point of
the package is that the recipient writes the tools. Nothing here — and nothing in
``_sealed`` — stops a caller's own module from keeping its own reference to an
implementation and calling it directly; ``__closure__[0].cell_contents``,
``gc.get_referrers`` and ``ctypes`` all still reach it, and no Python wrapper
closes those. The seal is a strong convention with a machine checking it inside
this package. For untrusted tool code, run effects in a separate process.

The last two tests move from source text to compiled shape: every shipped
template is built and its graph is inspected, and no edge may reach an effect
node except from that effect's own gate. That is the structural half of the
gate-precedes-every-effect guarantee — the execution seam re-derives the verdict
anyway, which is the half that survives a template losing its gate to a refactor.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from selfloop.adapters.memory import build_memory_context
from selfloop.contracts import LoopTool, RiskTier
from selfloop.engine import END, CompiledGraph
from selfloop.templates import TEMPLATES, LoopTemplate

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "selfloop"

#: The seam's private invoker, its ticket, and the live-nonce set. Named as
#: constants so that renaming one in the source produces a legible failure here
#: rather than a suite that silently stops checking anything.
SEAM_MODULE = "tools.py"
SEAM_INVOKER = "_invoke_in_seam"
SEAM_TICKET = "_IN_SEAM"
SEAM_LIVE_SET = "_OPEN_SEAMS"


# ---------------------------------------------------------------------------
# Parsing the package once
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """One syntactic occurrence: which module, which line, inside which function."""

    module: str
    lineno: int
    function: str

    def __str__(self) -> str:
        return f"selfloop/{self.module}:{self.lineno} in {self.function or '<module>'}()"


@dataclass(frozen=True)
class Module:
    """One parsed source file of the package."""

    module: str
    tree: ast.Module
    #: ``(first_line, last_line, dotted_name)`` for every function in the file,
    #: innermost last. Used to name the function a call site sits in, because
    #: "one call site in tools.py" is a much weaker statement than "one call site,
    #: in ``_invoke_in_seam``".
    scopes: tuple[tuple[int, int, str], ...]

    def enclosing(self, lineno: int) -> str:
        best, tightest = "", None
        for start, end, name in self.scopes:
            if start <= lineno <= end:
                span = end - start
                if tightest is None or span < tightest:
                    best, tightest = name, span
        return best

    def site(self, node: ast.AST) -> Site:
        lineno = int(getattr(node, "lineno", 0))
        return Site(module=self.module, lineno=lineno, function=self.enclosing(lineno))


def _function_scopes(tree: ast.Module) -> tuple[tuple[int, int, str], ...]:
    found: list[tuple[int, int, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = f"{prefix}{child.name}"
                found.append((child.lineno, int(child.end_lineno or child.lineno), name))
                walk(child, f"{name}.")
            elif isinstance(child, ast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return tuple(found)


def _load_package() -> tuple[Module, ...]:
    modules: list[Module] = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules.append(
            Module(
                module=str(path.relative_to(PACKAGE_ROOT)),
                tree=tree,
                scopes=_function_scopes(tree),
            )
        )
    return tuple(modules)


MODULES = _load_package()


def _find(predicate: Callable[[ast.AST], bool]) -> list[Site]:
    """Every node in the package satisfying *predicate*, as located sites."""
    return [
        module.site(node)
        for module in MODULES
        for node in ast.walk(module.tree)
        if predicate(node)
    ]


def _attribute_calls(attr: str, on: str | None = None) -> Callable[[ast.AST], bool]:
    """Match ``<...>.<attr>(...)``, optionally requiring ``<...>`` to end in *on*.

    Matching the ATTRIBUTE CHAIN rather than a receiver variable name is what
    makes this robust: ``ctx.approvals.create`` and ``self._ctx.approvals.create``
    are the same rule, and a test that keyed on ``ctx`` would stop noticing the
    second the day somebody introduced a wrapper.
    """

    def predicate(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            return False
        if node.func.attr != attr:
            return False
        if on is None:
            return True
        receiver = node.func.value
        return isinstance(receiver, ast.Attribute) and receiver.attr == on

    return predicate


def _name_calls(name: str) -> Callable[[ast.AST], bool]:
    def predicate(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name

    return predicate


def _only(sites: list[Site], *, module: str, function: str, what: str) -> Site:
    """Assert *sites* is exactly one site, in *module*, inside *function*."""
    rendered = "\n  ".join(str(site) for site in sites) or "<none>"
    assert len(sites) == 1, f"{what} must exist in exactly ONE place; found:\n  {rendered}"
    site = sites[0]
    assert site.module == module, f"{what} moved out of {module}: {site}"
    assert site.function == function, f"{what} moved out of {function}(): {site}"
    return site


# ---------------------------------------------------------------------------
# 0. The suite must be scanning something
# ---------------------------------------------------------------------------


def test_the_scan_actually_covers_the_package() -> None:
    """A mechanical suite that parses nothing passes every rule in it.

    This is the guard on the guards. A broken glob, a moved package directory or
    a renamed source tree would turn every "exactly one" assertion below into
    "exactly zero, which is not one" — or, worse, into a vacuous pass if any of
    them were ever written as "at most one".
    """
    names = {module.module for module in MODULES}
    assert len(names) >= 20, f"only found {sorted(names)}"
    for required in (
        "tools.py",
        "approvals.py",
        "receipts.py",
        "kit.py",
        "policy.py",
        "engine.py",
        "learn.py",
        "runtime.py",
        "adapters/memory.py",
        "templates/__init__.py",
    ):
        assert required in names, f"{required} was not scanned"


def test_every_module_ends_with_an_explicit_all() -> None:
    """``__all__`` is the module's statement about its own surface.

    It matters here more than it does in most packages: ``selfloop.tools``
    deliberately does NOT export its invoker, and a module with no ``__all__`` has
    made no statement at all about what a caller may reach.
    """
    missing = [
        module.module
        for module in MODULES
        if not any(
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            for node in module.tree.body
        )
    ]
    assert missing == []


# ---------------------------------------------------------------------------
# 1. Effects reach the world through exactly one seam
# ---------------------------------------------------------------------------


def test_effects_reach_the_world_through_exactly_one_seam() -> None:
    """There is ONE expression in this package that invokes a tool's callable.

    ``selfloop.tools`` says so in its first line and this counts it. A second
    ``tool.call(...)`` anywhere would be an effect performed with no policy
    verdict, no approval and no receipt — which is precisely the shape of the
    bypass the seam exists to close, and it would not look like one in review: it
    looks like a helper.
    """
    site = _only(
        _find(_attribute_calls("call")),
        module=SEAM_MODULE,
        function=SEAM_INVOKER,
        what="a tool callable invocation (`.call(...)`)",
    )
    assert site.function == SEAM_INVOKER


def test_the_seam_is_opened_in_exactly_one_place() -> None:
    """The ticket is minted once, and the nonce is registered alongside it.

    Both halves are load-bearing and both live in the same function on purpose.
    The NAME on the ticket stops one tool's open seam from running a different
    tool's callable; the NONCE exists because a ContextVar is COPIED, not shared,
    into an asyncio task — so a task spawned inside the seam inherits a snapshot
    that still says "open for me", and a name-only check let leftover async work
    execute an effect long after the seam had returned.
    """
    _only(
        _find(_receiver_calls(SEAM_TICKET, "set")),
        module=SEAM_MODULE,
        function=SEAM_INVOKER,
        what=f"opening the seam ({SEAM_TICKET}.set)",
    )
    _only(
        _find(_receiver_calls(SEAM_LIVE_SET, "add")),
        module=SEAM_MODULE,
        function=SEAM_INVOKER,
        what=f"registering a live seam nonce ({SEAM_LIVE_SET}.add)",
    )


def test_the_seam_is_revoked_in_exactly_one_place() -> None:
    """Opening without revoking is a ticket that outlives its frame forever."""
    _only(
        _find(_receiver_calls(SEAM_TICKET, "reset")),
        module=SEAM_MODULE,
        function=SEAM_INVOKER,
        what=f"resetting the seam ticket ({SEAM_TICKET}.reset)",
    )
    _only(
        _find(_receiver_calls(SEAM_LIVE_SET, "discard")),
        module=SEAM_MODULE,
        function=SEAM_INVOKER,
        what=f"revoking a seam nonce ({SEAM_LIVE_SET}.discard)",
    )


def test_the_seams_invoker_is_private_and_never_reached_from_outside_its_module() -> None:
    """As a public ``invoke(tool, args)`` this function WAS the bypass.

    Anything holding a :class:`~selfloop.contracts.LoopTool` could have run an
    irreversible effect through it with no verdict, no approval and no receipt —
    the seam's own front door standing open next to the locked one.
    """
    callers = {site.module for site in _find(_name_calls(SEAM_INVOKER))}
    assert callers == {SEAM_MODULE}, f"{SEAM_INVOKER} is called from {sorted(callers)}"

    importers = [
        module.module
        for module in MODULES
        for node in ast.walk(module.tree)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == SEAM_INVOKER for alias in node.names)
    ]
    assert importers == [], f"{SEAM_INVOKER} is imported by {importers}"

    seam = next(module for module in MODULES if module.module == SEAM_MODULE)
    exported = _declared_all(seam)
    assert SEAM_INVOKER not in exported
    assert "invoke" not in exported, (
        "exporting an invoker under any name restores the bypass this module exists "
        "to close"
    )


def test_the_sealer_is_installed_in_exactly_one_place() -> None:
    """One writer for the hook that wraps every registered tool.

    ``contracts`` defines the hook and defaults it to the identity function so it
    can stay dependency-free; ``tools`` supplies the behaviour at import. A second
    caller of :func:`~selfloop.contracts.install_sealer` inside the package would
    mean the seal a tool gets depends on module import order.
    """
    _only(
        _find(_name_calls("install_sealer")),
        module=SEAM_MODULE,
        function="",
        what="installing the tool sealer",
    )


# ---------------------------------------------------------------------------
# 2. The one hole the closure-seal cannot close
# ---------------------------------------------------------------------------


def test_no_module_invokes_a_callable_it_registers_as_a_tool() -> None:
    """Wrapping cannot take away a reference somebody already has.

    ``_sealed`` replaces ``tool.call`` with a closure, and every route to the
    implementation THROUGH THE TOOL RECORD is then gone. What it cannot touch is
    the module that defined the implementation in the first place and still has
    the plain name bound at module scope: ``default_propose`` is right there, and
    calling it directly reaches the world with no verdict, no receipt and no
    ledger entry, while type-checking perfectly and reading like a refactor.

    That hole is not closable by a wrapper, so it is closed mechanically here:
    a name handed to ``LoopTool(call=...)`` may never appear as the function of a
    call expression in the same module.
    """
    offences: list[str] = []
    checked = 0
    for module in MODULES:
        registered: dict[str, ast.AST] = {}
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id != "LoopTool":
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "call":
                        dotted = _dotted(keyword.value)
                        if dotted:
                            registered[dotted] = node
        if not registered:
            continue
        checked += 1
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.Call):
                continue
            called = _dotted(node.func)
            if called and called in registered:
                offences.append(f"{module.site(node)} calls {called}, which it registers as a tool")

    assert checked >= 2, "no module was found registering a tool; the check has gone vacuous"
    assert offences == [], "\n".join(offences)


# ---------------------------------------------------------------------------
# 3. One park protocol, one approval minting site, one receipt writer
# ---------------------------------------------------------------------------


def test_park_requested_is_raised_from_exactly_one_place() -> None:
    """A park is a protocol between the effect gate and the executor.

    A second raise site is a second protocol nobody wrote down: the executor
    catches this and settles the durable checkpoint as parked-at-this-node, so a
    raise from anywhere that is not an effect gate parks a thread on a node whose
    approval nothing will ever resolve.
    """
    sites = _find(
        lambda node: isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and _dotted(node.exc.func) in ("ParkRequested", "engine.ParkRequested")
    )
    rendered = "\n  ".join(str(site) for site in sites) or "<none>"
    assert len(sites) == 1, f"ParkRequested is raised in more than one place:\n  {rendered}"
    assert sites[0].module == "kit.py", str(sites[0])


def test_approval_rows_are_created_in_exactly_one_place() -> None:
    """One minting site, so every row carries a binding and a deadline.

    A row created anywhere else would be a row with no
    :func:`~selfloop.tools.effect_binding` on it, and ``read_outcome`` refuses a
    row it cannot bind — so the effect would park forever against an approval a
    human had already granted.
    """
    _only(
        _find(_attribute_calls("create", on="approvals")),
        module="approvals.py",
        function="ensure_approval",
        what="creating an approval row (`.approvals.create(...)`)",
    )


def test_receipts_are_written_in_exactly_one_place() -> None:
    """Claim, complete and release live in one module and nowhere else.

    The exactly-once property is a three-call protocol — claim, act, complete —
    and a second writer is a second protocol. In particular ``release`` MUST NOT
    be reachable from a caller: the ``result_json IS NULL`` guard that makes the
    crash window safe lives in the STORE, and a caller who could call release
    from elsewhere is a caller who could talk the store out of it.
    """
    writers = {
        site.module
        for verb in ("claim", "complete", "release")
        for site in _find(_attribute_calls(verb, on="receipts"))
    }
    assert writers == {"receipts.py"}, f"receipts are written from {sorted(writers)}"


def test_lessons_are_injected_from_exactly_one_place() -> None:
    """THE feedback edge. Sever it and the package is an audit trail with ambitions.

    Named exactly as ``selfloop.kit.inject_lessons`` says it is named, because a
    docstring that points at a test is only worth something if the test is there
    under that name.
    """
    _only(
        _find(_name_calls("lesson_block")),
        module="kit.py",
        function="inject_lessons",
        what="injecting promoted lessons (`lesson_block(...)`)",
    )


# ---------------------------------------------------------------------------
# 4. The compiled shape: no edge reaches an effect except through its gate
# ---------------------------------------------------------------------------


def _outgoing(graph: CompiledGraph) -> dict[str, set[str]]:
    """Every destination each node can route to, read off the COMPILED graph.

    Read from the compiled graph rather than from the builder calls on purpose:
    the builder is what a template author writes, and the compiled graph is what
    actually runs. The assertion has to be about the second one.
    """
    targets: dict[str, set[str]] = {}
    for name in graph.nodes:
        if name in graph.edges:
            targets[name] = {graph.edges[name]}
        else:
            targets[name] = set(graph.conditionals[name][1].values())
    return targets


def _built(template: LoopTemplate) -> CompiledGraph:
    """Compile *template* against a context granting exactly its declared tools."""
    ctx = build_memory_context(instance_id="ast-probe", template=template.name)
    for tool in template.required_tools:
        # T1 uniformly: building a graph never consults a tier (only ``retries > 1``
        # does, and no shipped template raises it), so a faithful tier ladder here
        # would be decoration that a reader has to check against the templates.
        ctx.tools.register(LoopTool(name=tool, tier=RiskTier.T1, call=lambda **kw: {"ok": True}))
    return template.build(ctx)


#: Every template in this process's catalogue, so a template added later is
#: covered by the two shape tests below without anybody remembering to add it.
SHIPPED_TEMPLATES = [
    pytest.param(template, id=name) for name, template in sorted(TEMPLATES.items())
]


@pytest.mark.parametrize("template", SHIPPED_TEMPLATES)
def test_no_edge_reaches_an_effect_node_except_from_its_gate(template: LoopTemplate) -> None:
    """``add_effect`` adds the gate and the effect together, and this proves it held.

    An effect node is identified the way the builder creates it: ``<name>`` whose
    companion ``<name>_gate`` is also in the graph. The gate must be the ONLY
    source that routes to it — not one of several, and not merely the usual one.

    This is the structural half of the guarantee. The execution seam re-derives
    the policy verdict and re-reads the approval row regardless, which is the half
    that survives a template losing its gate to a refactor; this half is what
    stops the loop from reaching the effect at all.
    """
    graph = _built(template)
    targets = _outgoing(graph)
    effects = [name for name in graph.nodes if f"{name}_gate" in graph.nodes]

    assert effects, (
        f"template {template.name!r} compiled with no gated effect node, so this test "
        "asserted nothing about it"
    )

    for effect in effects:
        gate = f"{effect}_gate"
        sources = sorted(node for node, reachable in targets.items() if effect in reachable)
        assert sources == [gate], (
            f"template {template.name!r}: node {effect!r} is reachable from {sources}, "
            f"but the only edge into an effect must come from {gate!r}"
        )
        assert graph.entry != effect, "an effect node must never be a template's entry point"


@pytest.mark.parametrize("template", SHIPPED_TEMPLATES)
def test_every_effect_gate_can_only_route_to_its_effect_or_stand_down(
    template: LoopTemplate,
) -> None:
    """A gate has two exits: run the effect, or stop. There is no third.

    Without this, the previous test is satisfiable by a gate that routes onward
    into the happy path — the branch a router falls through to when it does not
    recognise its own key is always the happy path, which is why the executor
    refuses an unmapped branch rather than defaulting.
    """
    graph = _built(template)
    targets = _outgoing(graph)
    for effect in [name for name in graph.nodes if f"{name}_gate" in graph.nodes]:
        gate = f"{effect}_gate"
        assert gate in graph.conditionals, f"{gate!r} must route conditionally"
        reachable = targets[gate]
        assert effect in reachable
        assert END not in reachable, (
            f"{gate!r} routes straight to END; a denied effect must land on the shared "
            "terminal that records why the tick stood down"
        )
        assert len(reachable) == 2, f"{gate!r} routes to {sorted(reachable)}"


# ---------------------------------------------------------------------------
# Small AST helpers
# ---------------------------------------------------------------------------


def _receiver_calls(receiver: str, attr: str) -> Callable[[ast.AST], bool]:
    """Match ``<receiver>.<attr>(...)`` where *receiver* is a bare name."""

    def predicate(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == attr
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == receiver
        )

    return predicate


def _dotted(node: ast.AST) -> str:
    """``a.b.c`` for a Name/Attribute chain, or ``""`` for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _declared_all(module: Module) -> frozenset[str]:
    """The names in a module's literal ``__all__``."""
    for node in module.tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            if isinstance(node.value, ast.List | ast.Tuple):
                return frozenset(
                    element.value
                    for element in node.value.elts
                    if isinstance(element, ast.Constant) and isinstance(element.value, str)
                )
    return frozenset()
