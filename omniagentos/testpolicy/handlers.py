"""Broad exception-handler classification (M-20).

Classify, do not sweep. Most broad handlers are legitimate defensive code;
this inventory marks actionable silent fallbacks on sensitive paths.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omniagentos.testpolicy.policy_load import load_policy_dict, repo_root


@dataclass(frozen=True)
class HandlerClassification:
    file: str
    line: int
    caught: str
    fallback: str  # swallowed | continue | default_return | handled
    logs: bool
    sensitive_path: bool
    actionable: bool
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "line": self.line,
            "caught": self.caught,
            "fallback": self.fallback,
            "logs": self.logs,
            "sensitive_path": self.sensitive_path,
            "actionable": self.actionable,
            "rationale": self.rationale,
        }


def classify_broad_handlers(
    source: str,
    *,
    relative_path: str,
    sensitive_prefixes: Iterable[str] | None = None,
    actionable_fallbacks: Iterable[str] | None = None,
) -> list[HandlerClassification]:
    """Classify broad handlers in one source string."""
    policy = load_policy_dict().get("classification") or {}
    prefixes = tuple(
        sensitive_prefixes
        if sensitive_prefixes is not None
        else policy.get("sensitive_path_prefixes") or ()
    )
    actionable_kinds = set(
        actionable_fallbacks
        if actionable_fallbacks is not None
        else policy.get("actionable_fallbacks") or ("swallowed", "default_return", "continue")
    )
    sensitive = any(relative_path.replace("\\", "/").startswith(p) for p in prefixes)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    out: list[HandlerClassification] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        caught = _handler_name(node)
        if caught != "bare" and "Exception" not in caught and "BaseException" not in caught:
            continue
        fallback = _fallback_kind(node.body)
        logs = any(_is_log_call(item) for item in node.body)
        actionable = sensitive and fallback in actionable_kinds
        if actionable:
            rationale = (
                f"sensitive path {relative_path} uses {fallback} broad handler; "
                "API/persistence/control boundaries need typed degraded results"
            )
        elif not sensitive:
            rationale = "non-sensitive path; broad handler treated as defensive isolation"
        else:
            rationale = "sensitive path but handler body is substantive (handled)"
        out.append(
            HandlerClassification(
                file=relative_path,
                line=getattr(node, "lineno", 0) or 0,
                caught=caught,
                fallback=fallback,
                logs=logs,
                sensitive_path=sensitive,
                actionable=actionable,
                rationale=rationale,
            )
        )
    return out


def classify_tree_handlers(
    root: Path | None = None,
    *,
    scan_roots: Iterable[str] = ("omniagentos",),
) -> list[HandlerClassification]:
    base = root or repo_root()
    results: list[HandlerClassification] = []
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
            results.extend(classify_broad_handlers(text, relative_path=rel))
    return results


def _handler_name(node: ast.ExceptHandler) -> str:
    if node.type is None:
        return "bare"
    if isinstance(node.type, ast.Name):
        return node.type.id
    if isinstance(node.type, ast.Tuple):
        return "|".join(
            item.id if isinstance(item, ast.Name) else ast.unparse(item) for item in node.type.elts
        )
    return ast.unparse(node.type)


def _is_log_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    function = node.value.func
    if not isinstance(function, ast.Attribute):
        return False
    return function.attr in {"debug", "info", "warning", "error", "exception", "critical"}


def _fallback_kind(body: list[ast.stmt]) -> str:
    meaningful = [node for node in body if not _is_log_call(node)]
    if not meaningful or all(isinstance(node, ast.Pass) for node in meaningful):
        return "swallowed"
    if all(isinstance(node, (ast.Pass, ast.Continue)) for node in meaningful):
        return "continue"
    if all(isinstance(node, (ast.Pass, ast.Return)) for node in meaningful):
        returns = [node for node in meaningful if isinstance(node, ast.Return)]
        if all(
            node.value is None
            or (isinstance(node.value, ast.Constant) and node.value.value in {None, False, ""})
            or isinstance(node.value, (ast.List, ast.Dict, ast.Set, ast.Tuple))
            for node in returns
        ):
            return "default_return"
    return "handled"
