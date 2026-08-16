"""Decomposition auditor — challenges planner DAGs before fan-out (Volume III)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AuditFinding:
    severity: str  # info | warn | block
    code: str
    message: str
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class DecompositionAudit:
    ok: bool
    findings: tuple[AuditFinding, ...]
    independent_units: tuple[str, ...]
    false_dependencies: tuple[tuple[str, str], ...]
    oversized_nodes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "findings": [
                {
                    "severity": f.severity,
                    "code": f.code,
                    "message": f.message,
                    "task_id": f.task_id,
                }
                for f in self.findings
            ],
            "independent_units": list(self.independent_units),
            "false_dependencies": [list(p) for p in self.false_dependencies],
            "oversized_nodes": list(self.oversized_nodes),
        }


def _find_dependency_cycles(
    edges: Mapping[str, list[str]],
) -> list[tuple[list[str], tuple[str, str]]]:
    """Return ``(cycle_path, back_edge)`` for every distinct cycle in ``edges``.

    Three-colour DFS, kept iterative on purpose: ``audit_decomposition`` is
    reached from ``POST /api/grok/allocate/simulate`` with the task list taken
    straight from the request body, so traversal depth is caller-controlled and
    a recursive walk would trade one wrong answer for a RecursionError.
    """
    white, grey, black = 0, 1, 2
    colour: dict[str, int] = dict.fromkeys(edges, white)
    cycles: list[tuple[list[str], tuple[str, str]]] = []
    seen_back_edges: set[tuple[str, str]] = set()

    for root in edges:
        if colour[root] != white:
            continue
        colour[root] = grey
        path: list[str] = [root]
        stack: list[tuple[str, Iterator[str]]] = [(root, iter(edges[root]))]
        while stack:
            node, pending = stack[-1]
            descended = False
            for dep in pending:
                state = colour.get(dep, black)
                if state == white:
                    colour[dep] = grey
                    path.append(dep)
                    stack.append((dep, iter(edges[dep])))
                    descended = True
                    break
                if state == grey:
                    # dep is on the current path: node -> dep closes a cycle.
                    back_edge = (node, dep)
                    if back_edge not in seen_back_edges:
                        seen_back_edges.add(back_edge)
                        cycles.append((path[path.index(dep) :] + [dep], back_edge))
            if not descended:
                stack.pop()
                colour[node] = black
                path.pop()
    return cycles


def audit_decomposition(
    tasks: Sequence[Mapping[str, Any]],
    *,
    max_owned_paths: int = 40,
    max_est_minutes: int = 180,
) -> DecompositionAudit:
    """Deterministic audit of a proposed task graph.

    Flags: missing ids, self-deps, dependency cycles, unknown deps, empty
    objectives, oversized ownership, and nodes with no acceptance / verify
    signal.
    """
    findings: list[AuditFinding] = []
    ids = [str(t.get("id") or t.get("task_id") or "") for t in tasks]
    id_set = {i for i in ids if i}
    if len(ids) != len(id_set):
        findings.append(
            AuditFinding("error", "duplicate_or_empty_ids", "Task ids must be unique and non-empty")
        )

    false_deps: list[tuple[str, str]] = []
    oversized: list[str] = []
    independent: list[str] = []
    # Adjacency over real, in-graph, non-self edges only: unknown deps and
    # self-deps are already reported below with their own, more specific codes.
    edges: dict[str, list[str]] = {}

    for task in tasks:
        tid = str(task.get("id") or task.get("task_id") or "")
        title = str(task.get("title") or task.get("objective") or "").strip()
        if not title:
            findings.append(
                AuditFinding("error", "missing_objective", "Task has no title/objective", tid)
            )
        deps = [str(d) for d in (task.get("depends_on") or task.get("deps") or [])]
        if tid:
            edges.setdefault(tid, []).extend(dep for dep in deps if dep != tid and dep in id_set)
        for dep in deps:
            if dep == tid:
                findings.append(
                    AuditFinding("error", "self_dependency", "Task depends on itself", tid)
                )
                false_deps.append((tid, dep))
            elif dep and dep not in id_set:
                findings.append(
                    AuditFinding(
                        "error",
                        "unknown_dependency",
                        f"Dependency {dep!r} is not a task in the graph",
                        tid,
                    )
                )
                false_deps.append((tid, dep))
        owned = list(task.get("owned_paths") or task.get("write_set") or [])
        if len(owned) > max_owned_paths:
            oversized.append(tid)
            findings.append(
                AuditFinding(
                    "warn",
                    "oversized_ownership",
                    f"owned_paths={len(owned)} exceeds {max_owned_paths}",
                    tid,
                )
            )
        est = task.get("est_agent_minutes") or task.get("est_minutes")
        if est is not None and float(est) > max_est_minutes:
            oversized.append(tid)
            findings.append(
                AuditFinding(
                    "warn",
                    "oversized_estimate",
                    f"estimate {est}m exceeds {max_est_minutes}m",
                    tid,
                )
            )
        acceptance = str(task.get("acceptance") or task.get("acceptance_criteria") or "").strip()
        verify = str(task.get("verify_command") or "").strip()
        if not acceptance and not verify:
            findings.append(
                AuditFinding(
                    "warn",
                    "no_verifier_signal",
                    "No acceptance text or verify_command — fan-out should wait",
                    tid,
                )
            )
        if not deps:
            independent.append(tid)

    # A cycle is the defect that makes a decomposition unexecutable: no task in
    # it ever becomes eligible. Counting roots is not enough to find one — a
    # cycle downstream of a healthy root leaves the root set non-empty — so the
    # graph is actually traversed.
    for cycle_path, back_edge in _find_dependency_cycles(edges):
        findings.append(
            AuditFinding(
                "error",
                "dependency_cycle",
                "Dependency cycle: " + " -> ".join(cycle_path),
                back_edge[0],
            )
        )
        false_deps.append(back_edge)

    # Independent units = tasks with no deps (roots of the ready set)
    independent_units = tuple(dict.fromkeys(independent))
    errors = [f for f in findings if f.severity == "error"]
    return DecompositionAudit(
        ok=not errors,
        findings=tuple(findings),
        independent_units=independent_units,
        false_dependencies=tuple(false_deps),
        oversized_nodes=tuple(dict.fromkeys(oversized)),
    )
