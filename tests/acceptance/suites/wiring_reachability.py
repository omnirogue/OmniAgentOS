"""Wiring reachability gate — production call-path assertions for critical mechanisms.

WHY THIS EXISTS
---------------
selfopt-001 surfaced multiple independent instances of one defect class: a
mechanism built completely, tested thoroughly, and never connected to the path
that actually runs. Tests stayed green because they call the mechanism
*directly* — exactly what production does not do. A green suite is evidence the
code works, not evidence anything invokes it. The seed registry covers five
cases (cascade, crash recovery, import-shadow gate, role pack, fleet preflight).

See ``devtasks/SIMULATION-RESULTS-selfopt-001-ADDENDUM.md``, section
"The pattern worth more than any individual fix".

WHAT THIS CHECKS
----------------
For each curated registry entry, assert a real call/ref path exists from a named
production entry point to the mechanism. Statuses (do not conflate):

* ``REACHABLE`` — path exists; mechanism runs on a default install.
* ``REACHABLE_DEFAULT_OFF`` — path exists, but a flag defaults off.
* ``UNREACHABLE`` — no production path; ratcheted ``pytest.mark.xfail(strict=True)``
  so that wiring it later is an XPASS (flip status to REACHABLE — do not delete).
* ``TEST_ONLY_CALLER`` — no production path; callers live only in tests/ (and
  possibly a self-contained CLI). Not an xfail: if a production path appears,
  the suite **fails hard** until the entry is deliberately reclassified.

DESIGN (non-negotiable)
-----------------------
1. **Explicit registry, not magic.** A small declarative table a human can
   review. No broad whole-repo inference.
2. **AST reachability, not grep.** Analysis covers ``omniagentos/`` ONLY.
   ``tests/`` is outside the package and is excluded by construction — grepping
   tests for "callers" is exactly how these defects stayed invisible. A
   textual name match on docstrings is **not** a caller
   (``run_cascade`` "has callers" under grep; the graph says no).
3. **Repo ratchet idiom.** Known-dead wiring that we expect to fix soon uses
   ``strict`` xfail with a ``GAP:`` reason so XPASS announces a newly-wired
   path. Never compute the metric via text scan (``grep -c xfail`` miscounts
   and reads a missing file as a perfect score — see ``at17_progress.py``).
4. **False positives kill this tool.** Unresolvable dynamic dispatch is NEVER
   reported as "unreachable" on the engine's own authority. The registry status
   is the contract; the engine measures. A declared-REACHABLE entry the engine
   cannot see is either a genuine unwiring or engine blindness — fix blindness
   with a reviewable ``extra_edges`` entry (from_fqn, to_fqn, citation), do not
   delete the gate.

ESCAPE HATCH (extra_edges)
--------------------------
When production wires a callable through a seam the AST cannot see (string-keyed
registry, injected Callable across a module boundary, importlib, etc.), declare
an ``extra_edges`` hop with a file:line citation. That keeps the gate honest
without inventing magic inference.

Usage::

    uv run python -m tests.acceptance.suites.wiring_reachability
    uv run python -m tests.acceptance.suites.wiring_reachability --json
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
from collections import deque
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
PACKAGE_ROOT = REPO_ROOT / "omniagentos"

# tests/ is outside omniagentos/ and is excluded by construction: the graph is
# built only from PACKAGE_ROOT. Callers that exist solely in tests/ must never
# create a production edge.


class WiringGraphError(RuntimeError):
    """Hard failure while building the call graph (e.g. unparseable source)."""


class WiringStatus(StrEnum):
    """Four distinct outcomes — do not conflate DEFAULT_OFF with UNREACHABLE,
    and do not conflate TEST_ONLY_CALLER with UNREACHABLE (different ratchet)."""

    REACHABLE = "REACHABLE"
    REACHABLE_DEFAULT_OFF = "REACHABLE_DEFAULT_OFF"
    UNREACHABLE = "UNREACHABLE"
    TEST_ONLY_CALLER = "TEST_ONLY_CALLER"


@dataclass(frozen=True)
class GraphNode:
    qualname: str
    file: str
    lineno: int
    kind: str  # "module" | "function" | "method"


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    kind: str  # "call" | "ref"
    file: str
    lineno: int


@dataclass(frozen=True)
class PathHop:
    """One node on a winning path, with the edge that entered it (if any)."""

    qualname: str
    edge_kind: str | None
    file: str | None
    lineno: int | None


@dataclass
class CallGraph:
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)
    unresolved_calls: int = 0
    files_parsed: int = 0

    def add_node(self, node: GraphNode) -> None:
        # First definition wins (duplicate def names are rare; keep earliest).
        self.nodes.setdefault(node.qualname, node)

    def add_edge(self, edge: GraphEdge) -> None:
        if edge.source not in self.nodes or edge.target not in self.nodes:
            return
        if edge.source == edge.target:
            return
        self.edges.append(edge)

    @property
    def node_count(self) -> int:
        return len(self.nodes)

    @property
    def edge_count(self) -> int:
        return len(self.edges)


@dataclass(frozen=True)
class ExtraEdge:
    from_fqn: str
    to_fqn: str
    citation: str


@dataclass(frozen=True)
class DefaultOffProbe:
    """Callable that must return ``expected`` under a default/empty env."""

    callable_fqn: str
    expected: object


@dataclass(frozen=True)
class WiringEntry:
    key: str
    mechanism: str
    entry_point: str
    status: WiringStatus
    citation: str
    note: str
    extra_edges: tuple[ExtraEdge, ...] = ()
    default_off_probe: DefaultOffProbe | None = None


# ---------------------------------------------------------------------------
# Registry — curated, small, reviewable. Do not add entries beyond the seeds
# without an explicit decision; curated beats broad.
# ---------------------------------------------------------------------------

WIRING_REGISTRY: tuple[WiringEntry, ...] = (
    WiringEntry(
        key="cascade_ladder",
        mechanism="omniagentos.routing.cascade.run_cascade",
        entry_point="omniagentos.orchestrator.core.Orchestrator._execute_task",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/routing/cascade.py:239",
        note=(
            "FrugalGPT cascade ladder (configs/cascade.yaml via load_ladder). "
            "Orchestrator._execute_task consults the cascade flag and escalates "
            "tiers manually, but never calls run_cascade/load_ladder. "
            "Name-collision trap: omniagentos.improve.judges.run_cascade is a "
            "different function — basename matching must not credit it."
        ),
    ),
    WiringEntry(
        key="crash_recovery",
        mechanism="omniagentos.swarm.scheduler.resume_stale_swarms",
        entry_point="omniagentos.api.main.lifespan",
        status=WiringStatus.REACHABLE_DEFAULT_OFF,
        citation="omniagentos/api/main.py:386",
        note=(
            "Stale-swarm crash recovery is wired into api.main.lifespan at "
            "omniagentos/api/main.py:386 under OMNIAGENTOS_SWARM_RESUME_ON_STARTUP "
            "(default on); resume_stale_swarms is defined at "
            "omniagentos/swarm/scheduler.py:5887. Phase 1 (provider orphan "
            "reconcile) always runs. The heartbeat-lease takeover that actually "
            "adopts stale runs additionally requires swarm_execute_enabled() "
            "(OMNIAGENTOS_SWARM_EXECUTE), which defaults OFF, so recovery is real "
            "but gated."
        ),
        default_off_probe=DefaultOffProbe(
            callable_fqn="omniagentos.swarm.scheduler.swarm_execute_enabled",
            expected=False,
        ),
    ),
    WiringEntry(
        key="touched_modules_import_gate",
        mechanism="omniagentos.swarm.scheduler.assert_touched_modules_importable",
        entry_point="omniagentos.swarm.scheduler.SwarmScheduler.start_run",
        status=WiringStatus.REACHABLE,
        citation="omniagentos/swarm/scheduler.py:874",
        note=(
            "Positive control for the gate itself. Path runs through a DI seam: "
            "default_verifier calls assert_touched_modules_importable; "
            "default_verifier reaches production only as a VALUE "
            "(self._verifier = verifier or default_verifier) and is invoked via "
            "self._verifier(...). Justifies the ref edge kind. Was dead until "
            "earlier today — this gate would have caught that."
        ),
    ),
    WiringEntry(
        key="role_pack",
        mechanism="omniagentos.promptshape.rolepack.role_pack",
        entry_point="omniagentos.swarm.spawn.UnifiedSpawner._apply_role_pack",
        status=WiringStatus.REACHABLE_DEFAULT_OFF,
        citation="omniagentos/swarm/spawn.py:1008",
        note=(
            "Role-pack injection is on the spawn prompt path, but "
            "role_pack_mode() returns early when mode is off, and "
            "DEFAULT_ROLE_PACK_MODE is 'off'. Flipping the default must be a "
            "loud, deliberate change (default_off_probe)."
        ),
        default_off_probe=DefaultOffProbe(
            callable_fqn="omniagentos.swarm.spawn.role_pack_mode",
            expected="off",
        ),
    ),
    WiringEntry(
        key="fleet_preflight",
        mechanism="omniagentos.routing.fleet_preflight.preflight",
        entry_point="omniagentos.api.main.lifespan",
        status=WiringStatus.TEST_ONLY_CALLER,
        citation="omniagentos/routing/fleet_preflight.py:341",
        note=(
            "Fleet preflight docstring says 'Wired nowhere on purpose'. The "
            "only production-package caller is its own CLI main(); runtime "
            "entry points (API lifespan, orchestrator, scheduler) never call "
            "it. Tests call it directly — green suites prove the function, "
            "not that anything on the live path invokes it. Not an xfail: "
            "wiring this without reclassifying must fail hard."
        ),
    ),
    # --- Lane A: built-in routine seam (memlife dream cycle) ---------------
    WiringEntry(
        key="memlife_dream_cycle",
        mechanism="omniagentos.memlife.dream.run_dream_cycle",
        entry_point="omniagentos.scheduler.routines_tick.main",
        status=WiringStatus.REACHABLE,
        citation="omniagentos/scheduler/builtin_jobs.py",
        note=(
            "Lane A: routines_tick._fire dispatches task_template.input.module "
            "through BUILTIN_JOBS to run_memlife_dream_cycle → run_dream_cycle. "
            "extra_edges covers the dict-value hop the AST cannot resolve."
        ),
        extra_edges=(
            ExtraEdge(
                from_fqn="omniagentos.scheduler.builtin_jobs.builtin_for",
                to_fqn="omniagentos.scheduler.builtin_jobs.run_memlife_dream_cycle",
                citation="omniagentos/scheduler/builtin_jobs.py:147",
            ),
        ),
    ),
    WiringEntry(
        key="memlife_dream_routine_seed",
        mechanism="omniagentos.memlife.dream.ensure_dream_cycle_routine",
        entry_point="omniagentos.api.main.lifespan",
        status=WiringStatus.REACHABLE,
        citation="omniagentos/api/main.py",
        note=(
            "Seeded on API startup under OMNIAGENTOS_SEED_ROUTINES_ON_STARTUP "
            "(defaults to '1'). REACHABLE not DEFAULT_OFF: the flag defaults on; "
            "no helper predicate DefaultOffProbe targets. Flipping the default "
            "off is a deliberate ops change."
        ),
    ),
    # Residual R1: deliberately unregistered in BUILTIN_JOBS (Lane A).
    WiringEntry(
        key="improve_dispatcher_tick",
        mechanism="omniagentos.improve.dispatcher.tick",
        entry_point="omniagentos.scheduler.routines_tick.main",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/improve/dispatcher.py",
        note=(
            "F1 / R1: ensure_improve_dispatcher_routine seeds a row, but the "
            "module is not in BUILTIN_JOBS — firing still enqueues a mock run. "
            "Arming it spends money; leave UNREACHABLE until deliberately wired."
        ),
    ),
    WiringEntry(
        key="lab_jobs_run_once",
        mechanism="omniagentos.lab.jobs.run_once",
        entry_point="omniagentos.scheduler.routines_tick.main",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/lab/jobs.py",
        note=(
            "F1 / R1: ensure_lab_jobs_routine seeds a row, but the module is "
            "not in BUILTIN_JOBS. Arming it spends money; leave UNREACHABLE."
        ),
    ),
    # --- Lane B: capture → store write path (append-only; keep-both merge) --
    WiringEntry(
        key="memlife_candidate_writer",
        mechanism="omniagentos.memlife.db.stage_candidate",
        entry_point="omniagentos.scheduler.routines_tick.main",
        status=WiringStatus.REACHABLE,
        citation="omniagentos/memlife/db.py",
        note=(
            "Lane B: dream cycle dual-writes memlife_candidates via "
            "stage_candidate. Path is main→tick→_fire→builtin→"
            "run_memlife_dream_cycle→run_dream_cycle→stage_candidate. "
            "Dict-value hop may need ExtraEdge if BFS misses BUILTIN_JOBS."
        ),
        extra_edges=(
            ExtraEdge(
                from_fqn="omniagentos.scheduler.builtin_jobs.builtin_for",
                to_fqn="omniagentos.scheduler.builtin_jobs.run_memlife_dream_cycle",
                citation="omniagentos/scheduler/builtin_jobs.py:147",
            ),
        ),
    ),
    # --- Lane C: render → recall (append-only; keep-both merge) -------------
    WiringEntry(
        key="memlife_render_on_graduation",
        mechanism="omniagentos.memlife.render.render_lessons",
        entry_point="omniagentos.api.routes.memlife.graduate_candidate",
        status=WiringStatus.REACHABLE,
        citation="omniagentos/api/routes/memlife.py",
        note=(
            "Lane C: graduate_candidate writes the filesystem lesson and "
            "calls render_lessons so LESSONS.md is the recall source of truth."
        ),
    ),
    WiringEntry(
        key="memlife_recall_leg",
        mechanism="omniagentos.memlife.render.search_rendered_lessons",
        entry_point="omniagentos.retrieval.recall.recall",
        status=WiringStatus.REACHABLE,
        citation="omniagentos/retrieval/recall.py",
        note=(
            "Lane C: recall() fuses a memlife BackendSpec whose search is "
            "_search_memlife → search_rendered_lessons. DI-seam registration "
            "is a ref edge (search=_search_memlife)."
        ),
    ),
    WiringEntry(
        key="memlife_recall_bridge_superseded",
        mechanism="omniagentos.memory.recall_bridge.default_knowledge_recaller",
        entry_point="omniagentos.memory.runner_hook.safe_memory_block",
        status=WiringStatus.TEST_ONLY_CALLER,
        citation="omniagentos/memory/recall_bridge.py",
        note=(
            "Superseded by retrieval.recall._search_memlife for the memlife "
            "leg. Do not wire into safe_memory_block (would duplicate Synapse "
            "facts). TEST_ONLY_CALLER hard-fails if someone re-wires it."
        ),
    ),
    WiringEntry(
        key="context_resolver",
        mechanism="omniagentos.context.completeness.evaluate",
        entry_point="omniagentos.intake.service.dispatch_spec",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/context/completeness.py:91",
        note=(
            "GAP: dispatch_spec does not resolve and completeness-check a "
            "ContextPackage before dispatch. The domain evaluator exists, but "
            "the production intake entry point has no call/ref path to it."
        ),
    ),
    WiringEntry(
        key="gate_evidence_writer",
        mechanism="omniagentos.scheduler.gate_evidence.GateEvidenceStore.record",
        entry_point="omniagentos.swarm.scheduler.default_verifier",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/scheduler/gate_evidence.py:762",
        note=(
            "GAP: the production swarm verifier runs mechanical gates but does "
            "not durably record their signed GateEvidence through the existing "
            "atomic evidence writer."
        ),
    ),
    WiringEntry(
        key="contract_evaluator_project_grants",
        mechanism="omniagentos.projects.policy.evaluate_action_for_project",
        entry_point="omniagentos.runner.core.Runner._execute_step",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/projects/policy.py:103",
        note=(
            "GAP: the real runner step path does not call the existing "
            "project-overlaid action evaluator before executing project work."
        ),
    ),
    WiringEntry(
        key="provider_cost_ledger_writer",
        mechanism="omniagentos.swarm.dal.SwarmDal.record_attempt_usage",
        entry_point="omniagentos.swarm.provider_exec.ProviderSessionRunner._finish_process",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/swarm/dal.py:1437",
        note=(
            "GAP: process completion has no provider-call-ledger write. It sends "
            "reported cost/tokens/wall time to SessionsDal.record_session_usage "
            "through _record_usage's getattr seam. Separately, "
            "_record_dispatched_effort explicitly calls record_attempt_usage "
            "for effort only; that existing call is not this missing cost edge. "
            "The P1 provider_call_usage writer must replace or deliberately "
            "re-point this interim attempt-writer ratchet."
        ),
    ),
    WiringEntry(
        key="effective_route_writer",
        mechanism="omniagentos.swarm.dal.SwarmDal.merge_run_params",
        entry_point="omniagentos.swarm.router.SwarmRouter.route",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/swarm/dal.py:754",
        note=(
            "GAP: SwarmRouter.route returns a decision without persisting the "
            "effective route. merge_run_params is the extant durable per-run "
            "projection seam; the planned routing_history writer must replace "
            "or deliberately re-point this ratchet when it becomes concrete."
        ),
    ),
    WiringEntry(
        key="escalation_policy_consumer",
        mechanism="omniagentos.routing.learn.recommend_start_tier",
        entry_point="omniagentos.swarm.scheduler.SwarmScheduler._escalate",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/routing/learn.py:133",
        note=(
            "GAP: the swarm scheduler uses its own tier bump and does not "
            "consume the existing escalation learning policy. This row is "
            "independent from cascade_ladder to avoid duplicate semantic rows."
        ),
    ),
    WiringEntry(
        key="tool_broker_call",
        mechanism="omniagentos.connectors.broker.call",
        entry_point="omniagentos.runner.core.Runner._execute_step",
        status=WiringStatus.UNREACHABLE,
        # Re-pinned (574 -> 679 -> 1511). The citation names a DEFINITION, so it
        # moves whenever anything earlier in broker.py grows; the gap it
        # documents is unchanged. 679 was ALREADY stale on main b9351299 —
        # `def call(` was at 1440 there — because the Phase-1 broker cluster
        # (U-R5/R3/R4/A1/R10) grew the file above it without re-pinning. Phase 2
        # widened it twice more, so this is re-pinned ONCE at the end of the
        # integration rather than per lane: U-S2's catalog consult in
        # `_resolve_secret` and the U-E lanes' `_is_inprocess` echo dispatch both
        # sit above `call`. Verified against the merged tree, not inferred.
        citation="omniagentos/connectors/broker.py:1511",
        note=(
            "GAP: runner step execution does not route a package-bound tool "
            "call through the server-side connector broker."
        ),
    ),
    WiringEntry(
        key="verification_report_writer",
        mechanism="omniagentos.graph_runtime.store.GraphStore.insert_artifact",
        entry_point="omniagentos.swarm.scheduler.SwarmScheduler._quality_gate",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/graph_runtime/store.py:331",
        note=(
            "GAP: the scheduler quality gate does not durably persist its "
            "judgment. GraphStore.insert_artifact is an extant SQLite writer "
            "for graph artifacts, including artifact_type=verification_report; "
            "unlike VerificationReport.to_dict it performs durable I/O. The P5 "
            "canonical verification_reports writer must replace or deliberately "
            "re-point this interim persistence-seam ratchet."
        ),
    ),
    WiringEntry(
        key="receipt_projection_reader",
        mechanism="omniagentos.scheduler.gate_evidence.GateEvidenceStore.load_receipt",
        entry_point="omniagentos.api.routes.swarm.get_swarm_run",
        status=WiringStatus.UNREACHABLE,
        citation="omniagentos/scheduler/gate_evidence.py:918",
        note=(
            "GAP: the swarm outcome read path does not authenticate and project "
            "a stored gate receipt through the existing receipt reader."
        ),
    ),
)


# ---------------------------------------------------------------------------
# Path / module helpers
# ---------------------------------------------------------------------------


def _repo_rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def file_to_module(file: Path, package_root: Path) -> str:
    """Map ``omniagentos/swarm/scheduler.py`` -> ``omniagentos.swarm.scheduler``."""
    root = package_root.parent  # repo-ish parent of the package dir
    rel = file.resolve().relative_to(root.resolve())
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        raise WiringGraphError(f"cannot derive module name for {file}")
    return ".".join(parts)


def _package_of(module_name: str, is_package: bool) -> str:
    if is_package:
        return module_name
    if "." not in module_name:
        return module_name
    return module_name.rsplit(".", 1)[0]


def _resolve_relative_module(
    *,
    current_module: str,
    is_package: bool,
    level: int,
    module: str | None,
) -> str:
    """Resolve a relative ImportFrom to an absolute module name."""
    if level <= 0:
        return module or ""
    pkg = _package_of(current_module, is_package)
    parts = pkg.split(".") if pkg else []
    up = level - 1
    if up > len(parts):
        raise WiringGraphError(f"relative import level {level} escapes package of {current_module}")
    base = parts[: len(parts) - up]
    if module:
        return ".".join([*base, *module.split(".")]) if base else module
    return ".".join(base)


# ---------------------------------------------------------------------------
# Import tables
# ---------------------------------------------------------------------------


def _build_import_table(
    tree: ast.AST,
    *,
    current_module: str,
    is_package: bool,
) -> dict[str, str]:
    """local name -> fully-qualified target (module or symbol).

    Imports themselves create NO graph edges — only the table for resolution.
    """
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # import a.b.c as d  -> d = a.b.c
                # import a.b.c       -> a = a  (top-level package name)
                if alias.asname:
                    table[alias.asname] = alias.name
                else:
                    top = alias.name.split(".", 1)[0]
                    table[top] = top
        elif isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            abs_mod = _resolve_relative_module(
                current_module=current_module,
                is_package=is_package,
                level=node.level,
                module=node.module,
            )
            for alias in node.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                # from a.b import c as d -> d = a.b.c
                if abs_mod:
                    table[local] = f"{abs_mod}.{alias.name}"
                else:
                    table[local] = alias.name
    return table


def _import_table_for_scope(
    scope_node: ast.AST,
    *,
    module_table: dict[str, str],
    current_module: str,
    is_package: bool,
) -> dict[str, str]:
    """Module imports plus Import/ImportFrom statements inside this scope."""
    local = dict(module_table)
    for stmt in getattr(scope_node, "body", []) or []:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            chunk = _build_import_table(
                ast.Module(body=[stmt], type_ignores=[]),
                current_module=current_module,
                is_package=is_package,
            )
            local.update(chunk)
        # also nested one level (e.g. import inside if/try at top of fn)
        if isinstance(stmt, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.While)):
            for sub in ast.walk(stmt):
                if isinstance(sub, (ast.Import, ast.ImportFrom)) and sub is not stmt:
                    chunk = _build_import_table(
                        ast.Module(body=[sub], type_ignores=[]),
                        current_module=current_module,
                        is_package=is_package,
                    )
                    local.update(chunk)
    return local


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def _iter_package_py_files(package_root: Path) -> Iterator[Path]:
    if not package_root.is_dir():
        raise WiringGraphError(f"package root missing: {package_root}")
    yield from sorted(package_root.rglob("*.py"))


def _collect_definitions(
    graph: CallGraph,
    tree: ast.Module,
    *,
    module_name: str,
    file_rel: str,
) -> dict[str, str]:
    """Register module + function/method nodes. Return short-name -> fqn for module-level."""
    graph.add_node(GraphNode(qualname=module_name, file=file_rel, lineno=1, kind="module"))
    local_defs: dict[str, str] = {}

    def add_func(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        qualname: str,
        kind: str,
        short: str | None = None,
    ) -> None:
        graph.add_node(
            GraphNode(
                qualname=qualname,
                file=file_rel,
                lineno=node.lineno,
                kind=kind,
            )
        )
        if short is not None:
            local_defs.setdefault(short, qualname)

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            q = f"{module_name}.{stmt.name}"
            add_func(stmt, qualname=q, kind="function", short=stmt.name)
            _collect_nested(graph, stmt, parent_qualname=q, file_rel=file_rel)
        elif isinstance(stmt, ast.ClassDef):
            class_q = f"{module_name}.{stmt.name}"
            # Class body executes at import time under the module node; we still
            # record methods as first-class nodes.
            for item in stmt.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mq = f"{class_q}.{item.name}"
                    add_func(item, qualname=mq, kind="method")
                    _collect_nested(graph, item, parent_qualname=mq, file_rel=file_rel)
    return local_defs


def _collect_nested(
    graph: CallGraph,
    parent: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    parent_qualname: str,
    file_rel: str,
) -> None:
    for stmt in parent.body:
        for node in ast.walk(stmt):
            if node is stmt:
                continue
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Only direct nested defs under this parent body (not methods of
                # nested classes handled separately). Walk is broad; qualify with
                # <locals> to avoid colliding with siblings.
                # Limit: only register if parent is an ancestor in the tree.
                pass
    # Direct nested functions only (one level walk of body statements).
    stack: list[ast.AST] = list(parent.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            q = f"{parent_qualname}.<locals>.{node.name}"
            graph.add_node(
                GraphNode(qualname=q, file=file_rel, lineno=node.lineno, kind="function")
            )
            # recurse into nested
            stack.extend(node.body)
        elif isinstance(node, ast.ClassDef):
            continue  # skip nested classes for simplicity
        else:
            for child in ast.iter_child_nodes(node):
                if isinstance(
                    child,
                    (
                        ast.FunctionDef,
                        ast.AsyncFunctionDef,
                        ast.ClassDef,
                        ast.If,
                        ast.Try,
                        ast.With,
                        ast.AsyncWith,
                        ast.For,
                        ast.AsyncFor,
                        ast.While,
                    ),
                ):
                    stack.append(child)


def _attribute_chain(node: ast.AST) -> list[str] | None:
    """Return name parts for Name or Attribute chain, else None."""
    parts: list[str] = []
    cur: ast.AST = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
        parts.reverse()
        return parts
    return None


def _function_nodes(graph: CallGraph) -> set[str]:
    return {q for q, n in graph.nodes.items() if n.kind in {"function", "method"}}


class _EdgeCollector(ast.NodeVisitor):
    """Collect call/ref edges for one function/method (or module top-level)."""

    def __init__(
        self,
        *,
        graph: CallGraph,
        source_qualname: str,
        file_rel: str,
        import_table: dict[str, str],
        local_defs: dict[str, str],
        function_nodes: set[str],
        class_qualname: str | None,
        class_methods: Mapping[str, str],
        class_attr_funcs: Mapping[str, set[str]],
    ) -> None:
        self.graph = graph
        self.source = source_qualname
        self.file_rel = file_rel
        self.import_table = import_table
        self.local_defs = local_defs
        self.function_nodes = function_nodes
        self.class_qualname = class_qualname
        self.class_methods = class_methods
        self.class_attr_funcs = class_attr_funcs
        self._skip_ids: set[int] = set()
        self._in_annotation = False

    def resolve_all(self, node: ast.AST) -> set[str]:
        """Resolve Name/Attribute to known function/method nodes (may be multi)."""
        parts = _attribute_chain(node)
        if not parts:
            return set()

        # self.method / self._injected_callable
        if parts[0] == "self" and self.class_qualname is not None:
            if len(parts) != 2:
                return set()
            attr = parts[1]
            found: set[str] = set()
            if attr in self.class_methods:
                found.add(self.class_methods[attr])
            found |= set(self.class_attr_funcs.get(attr, ()))
            return {f for f in found if f in self.graph.nodes}

        head, *rest = parts
        bases: list[str] = []
        if head in self.import_table:
            bases.append(self.import_table[head])
        if head in self.local_defs:
            bases.append(self.local_defs[head])
        if not rest and self.class_qualname and head in self.class_methods:
            bases.append(self.class_methods[head])
        # Same-module ClassName.method when ClassName is not an imported name.
        if rest:
            module_name = None
            if self.local_defs:
                sample = next(iter(self.local_defs.values()))
                module_name = sample.rsplit(".", 1)[0]
            elif self.class_qualname:
                module_name = self.class_qualname.rsplit(".", 1)[0]
            if module_name is not None:
                bases.append(f"{module_name}.{head}")

        out: set[str] = set()
        for base in bases:
            candidate = base if not rest else f"{base}.{'.'.join(rest)}"
            if candidate in self.graph.nodes:
                out.add(candidate)
        return out

    def _emit(self, targets: set[str], *, kind: str, lineno: int) -> None:
        for t in targets:
            if t in self.function_nodes or (
                t in self.graph.nodes
                and self.graph.nodes[t].kind in {"function", "method", "module"}
            ):
                # Only edges to function/method nodes for call/ref semantics.
                if self.graph.nodes[t].kind not in {"function", "method"}:
                    continue
                self.graph.add_edge(
                    GraphEdge(
                        source=self.source,
                        target=t,
                        kind=kind,
                        file=self.file_rel,
                        lineno=lineno,
                    )
                )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Nested function: do not attribute its body edges to the outer source.
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        if node.value is not None:
            self.visit(node.value)
        # intentionally skip node.annotation

    def visit_arg(self, node: ast.arg) -> None:
        return  # skip annotations on args

    def visit_Call(self, node: ast.Call) -> None:
        targets = self.resolve_all(node.func)
        if targets:
            self._emit(targets, kind="call", lineno=node.lineno)
        else:
            # Unresolved dynamic / external call — blindness metric, not error.
            self.graph.unresolved_calls += 1
        # Mark func AST node so visit_Name/Attribute do not also emit a ref.
        self._skip_ids.add(id(node.func))
        # Visit func tree for nested loads? Usually not needed; visit args.
        # Still walk func children that are not the root resolution target
        # for nested calls inside subscripts etc. — rare.
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw)

    def visit_Name(self, node: ast.Name) -> None:
        if self._in_annotation:
            return
        if not isinstance(node.ctx, ast.Load):
            return
        if id(node) in self._skip_ids:
            return
        targets = self.resolve_all(node)
        # ref only for function/method values
        targets = {t for t in targets if t in self.function_nodes}
        if targets:
            self._emit(targets, kind="ref", lineno=node.lineno)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self._in_annotation:
            return
        if not isinstance(node.ctx, ast.Load):
            # Still visit value for nested loads on Store? Skip.
            if isinstance(node.ctx, ast.Store):
                self.visit(node.value)
            return
        if id(node) in self._skip_ids:
            # Still visit children under value for nested refs? For
            # self._coordinate the whole Attribute is the func-or-ref target.
            return
        targets = self.resolve_all(node)
        targets = {t for t in targets if t in self.function_nodes}
        if targets:
            self._emit(targets, kind="ref", lineno=getattr(node, "lineno", 0) or 0)
        else:
            # Continue into value so nested names still resolve when this attr
            # itself is not a known function (e.g. self._dal.x — value self._dal).
            self.visit(node.value)


def _collect_class_attr_funcs(
    class_node: ast.ClassDef,
    *,
    module_name: str,
    module_table: dict[str, str],
    local_defs: dict[str, str],
    function_nodes: set[str],
    is_package: bool,
) -> dict[str, set[str]]:
    """Map instance attribute name -> set of function fqns assigned to self.attr."""
    class_q = f"{module_name}.{class_node.name}"
    methods = {
        item.name: f"{class_q}.{item.name}"
        for item in class_node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    bindings: dict[str, set[str]] = {}

    def resolve_name(name: str, table: dict[str, str]) -> str | None:
        if name in table and table[name] in function_nodes:
            return table[name]
        if name in local_defs and local_defs[name] in function_nodes:
            return local_defs[name]
        if name in methods and methods[name] in function_nodes:
            return methods[name]
        return None

    def funcs_in(expr: ast.AST, table: dict[str, str]) -> set[str]:
        found: set[str] = set()
        for n in ast.walk(expr):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                r = resolve_name(n.id, table)
                if r and r in function_nodes:
                    found.add(r)
            elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                parts = _attribute_chain(n)
                if not parts:
                    continue
                if parts[0] in table:
                    cand = table[parts[0]]
                    if len(parts) > 1:
                        cand = f"{cand}.{'.'.join(parts[1:])}"
                    if cand in function_nodes:
                        found.add(cand)
                elif parts[0] in local_defs and not parts[1:]:
                    if local_defs[parts[0]] in function_nodes:
                        found.add(local_defs[parts[0]])
        return found

    for item in class_node.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        table = _import_table_for_scope(
            item,
            module_table=module_table,
            current_module=module_name,
            is_package=is_package,
        )
        for stmt in ast.walk(item):
            target_attr: str | None = None
            value: ast.AST | None = None
            if isinstance(stmt, ast.Assign):
                if len(stmt.targets) == 1:
                    t0 = stmt.targets[0]
                    if (
                        isinstance(t0, ast.Attribute)
                        and isinstance(t0.value, ast.Name)
                        and t0.value.id == "self"
                    ):
                        target_attr = t0.attr
                        value = stmt.value
            elif isinstance(stmt, ast.AnnAssign):
                t0 = stmt.target
                if (
                    isinstance(t0, ast.Attribute)
                    and isinstance(t0.value, ast.Name)
                    and t0.value.id == "self"
                    and stmt.value is not None
                ):
                    target_attr = t0.attr
                    value = stmt.value
            if target_attr and value is not None:
                fs = funcs_in(value, table)
                if fs:
                    bindings.setdefault(target_attr, set()).update(fs)
    return bindings


def _analyze_function_body(
    graph: CallGraph,
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    qualname: str,
    file_rel: str,
    module_name: str,
    is_package: bool,
    module_table: dict[str, str],
    local_defs: dict[str, str],
    function_nodes: set[str],
    class_qualname: str | None,
    class_methods: Mapping[str, str],
    class_attr_funcs: Mapping[str, set[str]],
) -> None:
    table = _import_table_for_scope(
        node,
        module_table=module_table,
        current_module=module_name,
        is_package=is_package,
    )
    collector = _EdgeCollector(
        graph=graph,
        source_qualname=qualname,
        file_rel=file_rel,
        import_table=table,
        local_defs=local_defs,
        function_nodes=function_nodes,
        class_qualname=class_qualname,
        class_methods=class_methods,
        class_attr_funcs=class_attr_funcs,
    )
    # Decorators execute in the enclosing scope — handled by caller.
    for stmt in node.body:
        collector.visit(stmt)


def _analyze_module_toplevel(
    graph: CallGraph,
    tree: ast.Module,
    *,
    module_name: str,
    file_rel: str,
    is_package: bool,
    module_table: dict[str, str],
    local_defs: dict[str, str],
    function_nodes: set[str],
) -> None:
    collector = _EdgeCollector(
        graph=graph,
        source_qualname=module_name,
        file_rel=file_rel,
        import_table=module_table,
        local_defs=local_defs,
        function_nodes=function_nodes,
        class_qualname=None,
        class_methods={},
        class_attr_funcs={},
    )
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            # Visit decorators only at definition site (enclosing = module).
            for dec in stmt.decorator_list:
                collector.visit(dec)
            if isinstance(stmt, ast.ClassDef):
                for item in stmt.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        for dec in item.decorator_list:
                            collector.visit(dec)
            continue
        collector.visit(stmt)


def build_graph(package_root: Path | None = None) -> CallGraph:
    """AST-parse every ``*.py`` under ``omniagentos/`` and build the call/ref graph.

    A file that fails to parse is a hard :class:`WiringGraphError` — never
    silently skipped (a skipped file deletes edges and manufactures false
    unreachables).
    """
    root = Path(package_root) if package_root is not None else PACKAGE_ROOT
    graph = CallGraph()

    files = list(_iter_package_py_files(root))
    parsed: list[tuple[Path, str, bool, ast.Module, dict[str, str]]] = []

    # Pass 1: parse + register definitions
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise WiringGraphError(f"cannot read {path}: {exc}") from exc
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise WiringGraphError(f"failed to parse {path}:{exc.lineno}: {exc.msg}") from exc
        module_name = file_to_module(path, root)
        is_package = path.name == "__init__.py"
        file_rel = _repo_rel(path)
        local_defs = _collect_definitions(graph, tree, module_name=module_name, file_rel=file_rel)
        parsed.append((path, module_name, is_package, tree, local_defs))
        graph.files_parsed += 1

    function_nodes = _function_nodes(graph)

    # Pass 2: edges
    for path, module_name, is_package, tree, local_defs in parsed:
        file_rel = _repo_rel(path)
        module_table = _build_import_table(tree, current_module=module_name, is_package=is_package)
        # Restrict import walk to module-level imports only for the module table
        # (function-local imports are merged per-function). Rebuild from body:
        module_table = {}
        for stmt in tree.body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                chunk = _build_import_table(
                    ast.Module(body=[stmt], type_ignores=[]),
                    current_module=module_name,
                    is_package=is_package,
                )
                module_table.update(chunk)

        _analyze_module_toplevel(
            graph,
            tree,
            module_name=module_name,
            file_rel=file_rel,
            is_package=is_package,
            module_table=module_table,
            local_defs=local_defs,
            function_nodes=function_nodes,
        )

        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{module_name}.{stmt.name}"
                _analyze_function_body(
                    graph,
                    stmt,
                    qualname=q,
                    file_rel=file_rel,
                    module_name=module_name,
                    is_package=is_package,
                    module_table=module_table,
                    local_defs=local_defs,
                    function_nodes=function_nodes,
                    class_qualname=None,
                    class_methods={},
                    class_attr_funcs={},
                )
            elif isinstance(stmt, ast.ClassDef):
                class_q = f"{module_name}.{stmt.name}"
                class_methods = {
                    item.name: f"{class_q}.{item.name}"
                    for item in stmt.body
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                }
                class_attr_funcs = _collect_class_attr_funcs(
                    stmt,
                    module_name=module_name,
                    module_table=module_table,
                    local_defs=local_defs,
                    function_nodes=function_nodes,
                    is_package=is_package,
                )
                for item in stmt.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        mq = f"{class_q}.{item.name}"
                        _analyze_function_body(
                            graph,
                            item,
                            qualname=mq,
                            file_rel=file_rel,
                            module_name=module_name,
                            is_package=is_package,
                            module_table=module_table,
                            local_defs=local_defs,
                            function_nodes=function_nodes,
                            class_qualname=class_q,
                            class_methods=class_methods,
                            class_attr_funcs=class_attr_funcs,
                        )

    return graph


# ---------------------------------------------------------------------------
# Path finding
# ---------------------------------------------------------------------------


def find_path(
    graph: CallGraph,
    entry: str,
    target: str,
    extra_edges: tuple[ExtraEdge, ...] | tuple[tuple[str, str, str], ...] = (),
) -> tuple[PathHop, ...] | None:
    """BFS from ``entry`` to ``target`` over call+ref edges plus ``extra_edges``.

    Returns the shortest concrete winning path with file:line of every hop.
    Guarded against cycles. ``extra_edges`` items may be :class:`ExtraEdge` or
    ``(from_fqn, to_fqn, citation)`` triples.
    """
    if entry not in graph.nodes or target not in graph.nodes:
        return None

    # adjacency: source -> list of (target, kind, file, lineno)
    adj: dict[str, list[tuple[str, str, str, int]]] = {}
    for e in graph.edges:
        adj.setdefault(e.source, []).append((e.target, e.kind, e.file, e.lineno))
    for raw in extra_edges:
        if isinstance(raw, ExtraEdge):
            frm, to, cit = raw.from_fqn, raw.to_fqn, raw.citation
        else:
            frm, to, cit = raw[0], raw[1], raw[2]
        # citation is documentation; edge site uses citation string as file
        # and lineno 0 when not parseable — prefer "file:line" split.
        file_s, lineno = cit, 0
        if ":" in cit:
            left, right = cit.rsplit(":", 1)
            if right.isdigit():
                file_s, lineno = left, int(right)
        adj.setdefault(frm, []).append((to, "extra", file_s, lineno))

    # BFS
    queue: deque[str] = deque([entry])
    # pred[node] = (prev_node, edge_kind, edge_file, edge_lineno)
    pred: dict[str, tuple[str, str, str, int] | None] = {entry: None}

    while queue:
        cur = queue.popleft()
        if cur == target:
            break
        for nxt, kind, ef, el in adj.get(cur, ()):
            if nxt in pred:
                continue
            if nxt not in graph.nodes and nxt != target:
                # extra edge may reference nodes; require both ends in graph
                # for integrity — skip dangling
                if nxt not in graph.nodes:
                    continue
            pred[nxt] = (cur, kind, ef, el)
            queue.append(nxt)

    if target not in pred:
        return None

    # Reconstruct
    hops_rev: list[PathHop] = []
    node: str | None = target
    while node is not None:
        info = pred[node]
        if info is None:
            n = graph.nodes[node]
            hops_rev.append(
                PathHop(
                    qualname=node,
                    edge_kind=None,
                    file=n.file,
                    lineno=n.lineno,
                )
            )
            break
        prev, kind, ef, el = info
        hops_rev.append(PathHop(qualname=node, edge_kind=kind, file=ef, lineno=el))
        node = prev
    hops_rev.reverse()
    return tuple(hops_rev)


# ---------------------------------------------------------------------------
# Registry evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryVerdict:
    key: str
    status_declared: str
    reachable: bool
    path: tuple[PathHop, ...] | None
    mechanism: str
    entry_point: str
    citation: str
    note: str
    error: str | None = None


class RegistryError(RuntimeError):
    """Registry names a symbol the graph does not contain."""


def _require_node(graph: CallGraph, fqn: str, *, role: str, entry_key: str) -> None:
    if fqn not in graph.nodes:
        raise RegistryError(
            f"registry integrity: entry {entry_key!r} {role} {fqn!r} is not a "
            f"node in the omniagentos/ call graph (typo or moved symbol — this "
            f"must fail loudly, never degrade to 'unreachable')"
        )


def validate_registry(
    graph: CallGraph, registry: tuple[WiringEntry, ...] = WIRING_REGISTRY
) -> None:
    """Every mechanism, entry_point, extra_edges fqn, and probe must be real nodes."""
    for entry in registry:
        _require_node(graph, entry.mechanism, role="mechanism", entry_key=entry.key)
        _require_node(graph, entry.entry_point, role="entry_point", entry_key=entry.key)
        for ee in entry.extra_edges:
            _require_node(graph, ee.from_fqn, role="extra_edges.from", entry_key=entry.key)
            _require_node(graph, ee.to_fqn, role="extra_edges.to", entry_key=entry.key)
        if entry.default_off_probe is not None:
            _require_node(
                graph,
                entry.default_off_probe.callable_fqn,
                role="default_off_probe",
                entry_key=entry.key,
            )


def evaluate_entry(graph: CallGraph, entry: WiringEntry) -> EntryVerdict:
    path = find_path(
        graph,
        entry.entry_point,
        entry.mechanism,
        entry.extra_edges,
    )
    return EntryVerdict(
        key=entry.key,
        status_declared=entry.status.value,
        reachable=path is not None,
        path=path,
        mechanism=entry.mechanism,
        entry_point=entry.entry_point,
        citation=entry.citation,
        note=entry.note,
    )


def invoke_default_off_probe(probe: DefaultOffProbe) -> object:
    """Import ``probe.callable_fqn`` and call it with an empty env mapping."""
    mod_name, _, attr = probe.callable_fqn.rpartition(".")
    if not mod_name or not attr:
        raise RegistryError(f"invalid default_off_probe fqn: {probe.callable_fqn!r}")
    mod = importlib.import_module(mod_name)
    fn = getattr(mod, attr)
    return fn({})


def format_path(path: tuple[PathHop, ...] | None) -> str:
    if not path:
        return "(no path)"
    parts: list[str] = []
    for i, hop in enumerate(path):
        loc = f"{hop.file}:{hop.lineno}" if hop.file is not None else "?"
        if i == 0:
            parts.append(f"{hop.qualname} [{loc}]")
        else:
            parts.append(f"--{hop.edge_kind}--> {hop.qualname} @{loc}")
    return " ".join(parts)


def counterfeit_grep_name_hits(
    package_root: Path,
    symbol_basename: str,
) -> list[tuple[str, int, str]]:
    """TEXTUAL name-scan counterfeit — the false-positive trap this gate exists to beat.

    Greps ``package_root`` for the bare symbol name. Counts docstring mentions,
    comments, ``__all__`` strings, and the definition itself as "callers". A
    checker that uses this (or equivalent) will claim ``run_cascade`` is wired
    because its own docstring, module re-export, and unrelated basename collision
    mention it.

    The real gate must NOT use this. Tests assert that for known-dead symbols
    this returns hits while :func:`find_path` returns None.
    """
    hits: list[tuple[str, int, str]] = []
    root = Path(package_root)
    for path in sorted(root.rglob("*.py")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if symbol_basename in line:
                hits.append((_repo_rel(path), i, line.strip()[:120]))
    return hits


def counterfeit_find_path_by_name(
    package_root: Path,
    entry_point: str,
    mechanism: str,
) -> tuple[PathHop, ...] | None:
    """COUNTERFEIT text-scan reachability — deliberately wrong; never used by the real gate.

    Declares a mechanism "reachable" from ``entry_point`` whenever the bare
    function name appears textually anywhere under ``package_root`` outside its
    own ``def`` / ``async def`` line. Fabricates a two-hop path that cites a
    docstring, comment, ``__all__`` string, or same-named different function as
    if it were a call site.

    Exists solely so tests can prove the real AST engine disagrees with a
    faithful grep-for-name implementation. Do not call this from production
    evaluation paths.
    """
    basename = mechanism.rsplit(".", 1)[-1]
    hits = counterfeit_grep_name_hits(package_root, basename)
    surviving: list[tuple[str, int, str]] = []
    for file_s, lineno, line in hits:
        stripped = line.lstrip()
        if stripped.startswith(f"def {basename}") or stripped.startswith(f"async def {basename}"):
            continue
        surviving.append((file_s, lineno, line))
    if not surviving:
        return None
    hit_file, hit_lineno, _ = surviving[0]
    return (
        PathHop(qualname=entry_point, edge_kind=None, file=None, lineno=None),
        PathHop(
            qualname=mechanism,
            edge_kind="call",
            file=hit_file,
            lineno=hit_lineno,
        ),
    )


def build_report(
    graph: CallGraph | None = None,
    registry: tuple[WiringEntry, ...] = WIRING_REGISTRY,
) -> dict[str, Any]:
    g = graph if graph is not None else build_graph()
    validate_registry(g, registry)
    verdicts = [evaluate_entry(g, e) for e in registry]
    return {
        "files_parsed": g.files_parsed,
        "node_count": g.node_count,
        "edge_count": g.edge_count,
        "unresolved_calls": g.unresolved_calls,
        "entries": [
            {
                "key": v.key,
                "status_declared": v.status_declared,
                "reachable": v.reachable,
                "mechanism": v.mechanism,
                "entry_point": v.entry_point,
                "citation": v.citation,
                "path": (
                    [
                        {
                            "qualname": h.qualname,
                            "edge_kind": h.edge_kind,
                            "file": h.file,
                            "lineno": h.lineno,
                        }
                        for h in v.path
                    ]
                    if v.path
                    else None
                ),
                "path_formatted": format_path(v.path),
                "note": v.note,
            }
            for v in verdicts
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wiring reachability gate — production call paths for critical mechanisms"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the full report as JSON",
    )
    parser.add_argument(
        "--package-root",
        type=Path,
        default=PACKAGE_ROOT,
        help="Package root to analyze (default: omniagentos/)",
    )
    args = parser.parse_args(argv)

    try:
        graph = build_graph(args.package_root)
        report = build_report(graph)
    except (WiringGraphError, RegistryError) as exc:
        print(f"WIRING REACHABILITY ERROR: {exc}", file=sys.stderr)
        return 2

    if args.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"wiring graph: {report['files_parsed']} files, "
            f"{report['node_count']} nodes, {report['edge_count']} edges, "
            f"{report['unresolved_calls']} unresolved calls"
        )
        for entry in report["entries"]:
            flag = "REACHABLE" if entry["reachable"] else "UNREACHABLE"
            print(f"  [{entry['key']}] declared={entry['status_declared']} measured={flag}")
            print(f"    mechanism:   {entry['mechanism']}")
            print(f"    entry_point: {entry['entry_point']}")
            print(f"    citation:    {entry['citation']}")
            if entry["reachable"]:
                print(f"    path: {entry['path_formatted']}")
            else:
                print("    path: (none — no production call/ref path found)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
