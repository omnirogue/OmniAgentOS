"""Mechanical security properties of the loop runtime.

These are source-level invariants, deliberately. The plan's security section
makes four claims about this package; each is only true if there is exactly ONE
place in the code that can violate it, so each test pins that place. A reviewer
then checks one call site instead of trusting a paragraph.

Every check reads the AST, never raw text: a rule that a docstring can trip is a
rule nobody keeps, and a rule a comment can satisfy is not a rule at all.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "omniagentos_loops"
SOURCES = sorted(p for p in PACKAGE.rglob("*.py"))


def _identifiers(path: Path) -> set[str]:
    """Every NAME actually referenced in code — no strings, comments or docstrings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name.rsplit(".", 1)[-1])
            if node.asname:
                names.add(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.rsplit(".", 1)[-1])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
    return names


def _modules_referencing(name: str) -> set[str]:
    return {str(path.relative_to(PACKAGE)) for path in SOURCES if name in _identifiers(path)}


def _steward_write_methods() -> set[str]:
    """Derive the real write surface so a newly added store writer is covered."""
    roots = PACKAGE.parents[1] / "omniagentos"
    stores = (
        (roots / "steward" / "store.py", "StewardStore"),
        (roots / "db" / "store.py", "SqliteStore"),
    )
    writers: set[str] = set()
    for path, class_name in stores:
        assert path.is_file(), path
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        classes = [
            node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
        ]
        assert len(classes) == 1
        writers |= {
            node.name
            for node in classes[0].body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and any(
                marker in (ast.get_source_segment(source, node) or "").upper()
                for marker in ("_WRITE(", "INSERT ", "UPDATE ", "DELETE ")
            )
        }
    assert "append_goal_reading" in writers
    assert "_write" in writers
    return writers


def _local_module_path(module: str) -> Path | None:
    prefix = "omniagentos_loops"
    if module == prefix:
        candidate = PACKAGE / "__init__.py"
    elif module.startswith(prefix + "."):
        relative = module.removeprefix(prefix + ".").replace(".", "/")
        candidate = PACKAGE / f"{relative}.py"
        if not candidate.is_file():
            candidate = PACKAGE / relative / "__init__.py"
    else:
        return None
    return candidate if candidate.is_file() else None


def _local_import_closure(entry: Path) -> set[Path]:
    """Follow every statically named first-party import reachable from entry."""
    found: set[Path] = set()
    pending = [entry]
    while pending:
        path = pending.pop()
        if path in found:
            continue
        found.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
                modules.update(f"{node.module}.{alias.name}" for alias in node.names)
        pending.extend(
            candidate
            for module in modules
            if (candidate := _local_module_path(module)) is not None and candidate not in found
        )
    return found


def test_goal_dry_run_source_can_name_only_its_single_store_write() -> None:
    """Guard-2 follows the real store authority, not the unrelated tool registry."""
    paths = _local_import_closure(PACKAGE / "instances" / "goal_controller.py")
    accessed: dict[str, set[str]] = {}
    writers = _steward_write_methods()
    for path in paths:
        assert path.is_file(), path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        named_writers = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr in writers
        }
        if named_writers:
            accessed[str(path.relative_to(PACKAGE))] = named_writers
    assert accessed == {
        "approvals.py": {"create_approval", "decide_approval", "insert_event"},
        "instances/goal_controller.py": {"append_goal_reading"},
        "observability.py": {"insert_event"},
        "receipts.py": {"idem_complete", "idem_insert", "idem_release"},
    }


def _env_names(path: Path) -> set[str]:
    """Literal environment-variable names this module reads."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            attr = getattr(func, "attr", None)
            if attr in {"get", "getenv"} and node.args:
                target = getattr(func, "value", None)
                base = getattr(target, "attr", None) or getattr(target, "id", None)
                if base in {"environ", "os"} and isinstance(node.args[0], ast.Constant):
                    found.add(str(node.args[0].value))
        elif isinstance(node, ast.Subscript):
            base = getattr(node.value, "attr", None) or getattr(node.value, "id", None)
            if base == "environ" and isinstance(node.slice, ast.Constant):
                found.add(str(node.slice.value))
    return found


def test_the_package_has_sources_to_check():
    assert len(SOURCES) >= 12


def test_effects_reach_the_world_through_exactly_one_seam():
    """``execute_effect`` is defined once and called from ONE place."""
    assert _modules_referencing("execute_effect") == {"tools.py", "templates/common.py"}


def test_interrupt_is_raised_from_exactly_one_place():
    assert _modules_referencing("interrupt") == {"templates/common.py"}


def test_approval_rows_are_created_from_exactly_one_place():
    assert _modules_referencing("create_approval") == {"approvals.py"}


def test_receipts_are_written_from_exactly_one_place():
    writers = _modules_referencing("idem_insert") | _modules_referencing("idem_complete")
    assert writers == {"receipts.py"}


def test_a_receipt_claim_is_released_from_exactly_one_place():
    """``idem_release`` DELETES a claim, so it gets the same one-call-site rule.

    The deletion is legitimate in exactly one situation — the effect's authority
    was never reached, so nothing happened and the claim describes nothing — and
    that judgement lives in ``receipts._attempt``'s ``EffectUnavailable``
    handler. Anywhere else it would be a way to make a crash-window claim
    disappear, which is the guarantee the receipt table exists for.
    """
    assert _modules_referencing("idem_release") == {"receipts.py"}


def test_policy_is_consulted_only_through_the_gate():
    assert _modules_referencing("evaluate_action") == {"policy_gate.py"}
    assert _modules_referencing("approval_satisfies_gate") == {"approvals.py"}


def test_model_calls_go_only_through_the_short_call_client():
    """Requirement 8: the $10/day cap is inherited, never bypassed."""
    assert _modules_referencing("ShortCallClient") == {"models.py"}
    assert _modules_referencing("BudgetGuard") == {"models.py"}
    for path in SOURCES:
        names = _identifiers(path)
        assert not names & {"OpenAI", "Anthropic", "init_chat_model", "ChatOpenAI"}, path


def test_no_module_opens_a_listening_socket():
    """Security requirement: workers add no new listening ports."""
    for path in SOURCES:
        names = _identifiers(path)
        assert not names & {"bind", "listen", "uvicorn", "socketserver", "HTTPServer"}, path


CREDENTIAL_SHAPES = re.compile(r"(TOKEN|SECRET|PASSWORD|API_KEY|WEBHOOK|CREDENTIAL|DSN|_KEY)")


def test_only_loops_scoped_environment_variables_are_read():
    """Defence in depth behind a real process boundary.

    The primary guarantee is NOT this test: it is that
    ``omniagentos/scheduler/loop_jobs.py`` launches the worker with a scrubbed
    environment (``adapters.common._scrubbed_env`` + an enumerated list of
    non-secret pointers), so no credential is present in this process to read.
    That is enforced by
    ``tests/scheduler/test_loop_jobs.py::test_the_worker_environment_is_scrubbed_not_inherited``.

    This test is the second lock: even if a future caller launched the worker
    with an inherited environment, nothing in this package NAMES a credential
    variable, so the leak would still not become a read.
    """
    # Every name here is one of the five non-credential POINTERS
    # ``loop_jobs._WORKER_ENV_PASSTHROUGH`` enumerates, and that list is itself
    # run through the credential-shape filter before the worker sees it. The
    # credential assertion below is what keeps this allowlist honest: a name may
    # be added only if it cannot match CREDENTIAL_SHAPES.
    #
    # ``OMNIAGENTOS_VAR_DIR`` is read by ``parent_seam.var_dir`` so that a
    # verification predicate can DERIVE the artifact path from its arguments
    # instead of taking it from the renderer's answer.
    allowed = {"OMNIAGENTOS_LOOPS_ROOT", "OMNIAGENTOS_LOOPS_VENV", "OMNIAGENTOS_VAR_DIR"}
    seen: set[str] = set()
    for path in SOURCES:
        seen |= _env_names(path)
    assert seen <= allowed, seen
    assert not any(CREDENTIAL_SHAPES.search(name) for name in seen)


def test_the_seam_is_opened_in_exactly_one_place():
    """Only ``tools.py`` may open the guard.

    Asserted on ``_IN_SEAM`` rather than on ``invoke``: LangGraph's own
    ``graph.invoke`` shares the name, and a rule that matches an unrelated
    method is a rule that will be silenced the first time it cries wolf.
    """
    assert _modules_referencing("_IN_SEAM") == {"tools.py"}


def test_no_module_calls_a_tool_callable_directly():
    """``.call(`` outside the seam is a bypass; the guard raises, but say it too."""
    offenders = []
    for path in SOURCES:
        if path.name == "tools.py":
            continue
        text = path.read_text(encoding="utf-8")
        if ".call(" in text:
            offenders.append(str(path.relative_to(PACKAGE)))
    assert not offenders, offenders


def _tool_implementation_names(tree: ast.Module) -> set[str]:
    """Names handed to ``LoopTool(call=...)`` in this module."""
    registered: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (getattr(func, "id", None) or getattr(func, "attr", None)) != "LoopTool":
            continue
        for keyword in node.keywords:
            if keyword.arg != "call":
                continue
            name = getattr(keyword.value, "id", None) or getattr(keyword.value, "attr", None)
            if name:
                registered.add(name)
    return registered


def _self_invoked_tools(source: str) -> set[str]:
    """Names this module both REGISTERS as a tool and CALLS itself."""
    tree = ast.parse(source)
    registered = _tool_implementation_names(tree)
    if not registered:
        return set()
    offenders: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
        if called in registered:
            offenders.add(called)
    return offenders


def test_the_self_invocation_detector_actually_detects():
    """A vacuous scan is worse than none, so prove the detector on a fixture.

    ``loops/omniagentos_loops/instances/`` is empty in Phase 1; the sibling
    instance lanes are what this ratchet is for.
    """
    guilty = """
from omniagentos_loops.tools import LoopTool
def send_email(**kwargs): ...
def register(ctx):
    ctx.tools.register(LoopTool(name="send", tier=1, idempotency_key=k, call=send_email))
    send_email(to="a@b.c")
"""
    innocent = """
from omniagentos_loops.tools import LoopTool
def send_email(**kwargs): ...
def register(ctx):
    ctx.tools.register(LoopTool(name="send", tier=1, idempotency_key=k, call=send_email))
"""
    assert _self_invoked_tools(guilty) == {"send_email"}
    assert _self_invoked_tools(innocent) == set()


def test_no_module_invokes_a_callable_it_registers_as_a_tool():
    """The one seam route sealing CANNOT close, closed mechanically instead.

    Wrapping a callable inside ``LoopTool`` removes every route THROUGH the tool
    object, but it cannot take away a reference the defining module already
    had: ``def send_email(...)`` followed by ``send_email(...)`` is an unpoliced
    effect no runtime guard can see, because the guard was never on that path.
    Python offers no way to fix that at runtime, so the rule is enforced at
    source level: a module may register an implementation or call it, not both.
    """
    offenders = {
        str(path.relative_to(PACKAGE)): sorted(found)
        for path in SOURCES
        if (found := _self_invoked_tools(path.read_text(encoding="utf-8")))
    }
    assert not offenders, offenders


@pytest.mark.parametrize(
    "template_module",
    sorted(
        p.name
        for p in (PACKAGE / "templates").glob("*.py")
        if p.name not in {"__init__.py", "common.py"}
    ),
)
def test_no_template_can_reach_a_tool_without_the_kit(template_module):
    """A template wires nodes; it never touches a tool or the execution seam."""
    names = _identifiers(PACKAGE / "templates" / template_module)
    assert "execute_effect" not in names, template_module
    assert "call" not in names, template_module
    assert "ToolRegistry" not in names, template_module


def test_every_template_gates_every_effect_node():
    """Structural: each effect node has a ``<name>_gate`` and no other entrance."""
    from omniagentos_loops.templates import TEMPLATES

    class _NullTools:
        tools: dict = {}

        @staticmethod
        def names() -> frozenset:
            return frozenset()

    class _NullCtx:
        instance_id = "structural"
        template = "structural"
        tools = _NullTools()

    expected = {
        "poll_classify_act_verify": {"act"},
        "monitor_diagnose_repair_verify": {"repair", "escalate"},
        "draft_approve_send": {"send"},
        "generate_evaluate_improve": {"publish"},
        "dispatch_await_summarize": {"dispatch", "summarize"},
    }
    for name, template in TEMPLATES.items():
        graph = template.build(_NullCtx())
        nodes = set(graph.nodes)
        for effect in expected[name]:
            assert effect in nodes, f"{name}: missing effect node {effect}"
            assert f"{effect}_gate" in nodes, f"{name}: effect {effect} has no policy gate"
        for source, target in graph.edges:
            if target in expected[name]:
                assert source == f"{target}_gate", f"{name}: {source} -> {target} bypasses the gate"
