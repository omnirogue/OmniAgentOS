"""Dead-code vs intentional-protocol classification (L-02).

Static noise (Protocol methods, ABC abstracts, explicit NotImplementedError
product stubs) must not be gated the same way as unreachable dead paths.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.testpolicy.policy_load import load_policy_dict, repo_root


@dataclass(frozen=True)
class DeadCodeClassification:
    file: str
    line: int
    function: str
    kind: str  # pass | ellipsis | NotImplementedError | empty
    category: str  # protocol | intentional_stub | actionable_dead
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "function": self.function,
            "kind": self.kind,
            "category": self.category,
            "rationale": self.rationale,
        }


def classify_stub(
    source: str,
    *,
    relative_path: str,
    intentional_modules: Iterable[str] | None = None,
) -> list[DeadCodeClassification]:
    policy = load_policy_dict().get("classification") or {}
    allow = policy.get("protocol_allowlist") or {}
    raise_types = set(allow.get("raise_types") or ("NotImplementedError",))
    decorators = set(allow.get("decorators") or ("abstractmethod", "overload"))
    base_classes = set(allow.get("base_classes") or ("Protocol", "ABC", "ABCMeta"))
    intentional = tuple(
        intentional_modules
        if intentional_modules is not None
        else policy.get("intentional_stub_modules") or ()
    )
    path_posix = relative_path.replace("\\", "/")
    intentional_hit = any(
        path_posix == m.rstrip("/")
        or path_posix.startswith(m if m.endswith("/") else m + "/")
        or path_posix.startswith(m)
        for m in intentional
    )

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    class_stack: list[ast.ClassDef] = []
    out: list[DeadCodeClassification] = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            class_stack.append(node)
            self.generic_visit(node)
            class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._handle(node)
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._handle(node)
            self.generic_visit(node)

        def _handle(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            kind = _stub_kind(node, raise_types=raise_types)
            if kind is None:
                return
            is_protocol = _is_protocol_context(
                node, class_stack, base_classes=base_classes, decorators=decorators
            )
            if is_protocol:
                category = "protocol"
                rationale = "intentional Protocol/ABC/abstract surface, not actionable dead code"
            elif intentional_hit or kind == "NotImplementedError":
                category = "intentional_stub"
                rationale = "declared unfinished product stub or NotImplementedError sentinel"
            else:
                category = "actionable_dead"
                rationale = "pass/ellipsis body with no protocol markers — candidate dead path"
            out.append(
                DeadCodeClassification(
                    file=relative_path,
                    line=getattr(node, "lineno", 0) or 0,
                    function=node.name,
                    kind=kind,
                    category=category,
                    rationale=rationale,
                )
            )

    Visitor().visit(tree)
    return out


def classify_tree_deadcode(
    root: Path | None = None,
    *,
    scan_roots: Iterable[str] = ("omniagentos",),
) -> list[DeadCodeClassification]:
    base = root or repo_root()
    results: list[DeadCodeClassification] = []
    for scan in scan_roots:
        scan_path = base / scan
        if not scan_path.is_dir():
            continue
        for path in sorted(scan_path.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(base).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            results.extend(classify_stub(text, relative_path=rel))
    return results


def _stub_kind(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    raise_types: set[str],
) -> str | None:
    body = list(node.body)
    # Skip docstring-only prefix
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body:
        return "empty"
    if len(body) == 1 and isinstance(body[0], ast.Pass):
        return "pass"
    if (
        len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and body[0].value.value is Ellipsis
    ):
        return "ellipsis"
    if len(body) == 1 and isinstance(body[0], ast.Raise):
        called = body[0].exc
        if isinstance(called, ast.Call) and isinstance(called.func, ast.Name):
            if called.func.id in raise_types:
                return "NotImplementedError"
        if isinstance(called, ast.Name) and called.id in raise_types:
            return "NotImplementedError"
    return None


def _is_protocol_context(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    class_stack: list[ast.ClassDef],
    *,
    base_classes: set[str],
    decorators: set[str],
) -> bool:
    for dec in node.decorator_list:
        name = _decorator_name(dec)
        if name in decorators:
            return True
    if not class_stack:
        return False
    owner = class_stack[-1]
    for base in owner.bases:
        base_name = _name_of(base)
        if base_name in base_classes:
            return True
    # typing.Protocol often appears as subscript or attribute
    for base in owner.bases:
        text = ast.unparse(base) if hasattr(ast, "unparse") else ""
        if "Protocol" in text or "ABC" in text:
            return True
    return False


def _decorator_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
