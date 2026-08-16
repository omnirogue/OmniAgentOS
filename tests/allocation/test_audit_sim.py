"""Decomposition audit + fan-out simulator fixtures."""

from __future__ import annotations

from omniagentos.allocation import (
    audit_decomposition,
    fixture_suite,
    simulate_fanout,
)
from omniagentos.swarm.planner import _compute_disjoint_dag_width

# Capacity headroom so topology is decided by DAG shape, not slot starvation.
_CAP = {
    "global_free_slots": 10,
    "repository_writer_slots": 5,
    "verifier_absorption": 2,
}


def test_audit_flags_unknown_dep() -> None:
    audit = audit_decomposition(
        [
            {"id": "a", "title": "A", "depends_on": ["missing"], "acceptance": "ok"},
        ]
    )
    assert audit.ok is False
    assert any(f.code == "unknown_dependency" for f in audit.findings)


def test_audit_independent_roots() -> None:
    audit = audit_decomposition(
        [
            {"id": "a", "title": "A", "acceptance": "ok"},
            {"id": "b", "title": "B", "acceptance": "ok"},
        ]
    )
    assert audit.ok is True
    assert set(audit.independent_units) == {"a", "b"}


def test_simulate_trivial_one_worker() -> None:
    r = simulate_fanout([{"id": "t1", "title": "one", "acceptance": "done"}])
    assert r.worker_count <= 1
    assert r.idle_spawn is False


def test_fixture_suite_runs() -> None:
    for case in fixture_suite():
        r = simulate_fanout(case["tasks"], case.get("capacity"))
        if "expect_max_workers" in case:
            assert r.worker_count <= case["expect_max_workers"], case["name"]
        if "expect_min_workers" in case:
            assert r.worker_count >= case["expect_min_workers"], (
                f"{case['name']}: worker_count={r.worker_count} topology={r.topology}"
            )
        if case.get("expect_parallel"):
            assert r.topology != "sequential", (
                f"{case['name']}: expected parallel topology, got {r.topology}"
            )
            assert r.worker_count >= 2, case["name"]
        if "expect_topology" in case:
            assert r.topology == case["expect_topology"], case["name"]


def test_single_root_wide_body_is_parallel() -> None:
    """Decisive: one contract + five independent siblings → parallel, workers >= 2.

    Root-layer count is 1; antichain width is 5. Using roots alone certifies
    collapsed sequential behaviour as correct.
    """
    tasks = [
        {"id": "contract", "title": "contract", "acceptance": "ok"},
        {"id": "s1", "title": "s1", "depends_on": ["contract"], "acceptance": "ok"},
        {"id": "s2", "title": "s2", "depends_on": ["contract"], "acceptance": "ok"},
        {"id": "s3", "title": "s3", "depends_on": ["contract"], "acceptance": "ok"},
        {"id": "s4", "title": "s4", "depends_on": ["contract"], "acceptance": "ok"},
        {"id": "s5", "title": "s5", "depends_on": ["contract"], "acceptance": "ok"},
    ]
    audit = audit_decomposition(tasks)
    assert len(audit.independent_units) == 1  # root-layer trap
    assert _compute_disjoint_dag_width(tasks) == 5

    r = simulate_fanout(tasks, _CAP)
    assert r.topology != "sequential", r
    assert r.worker_count >= 2, r


def test_strict_chain_stays_sequential() -> None:
    """Counterfeit catch: hardcoding worker_count = max(worker_count, 2) fails here."""
    tasks = [
        {"id": "a", "title": "a", "acceptance": "ok"},
        {"id": "b", "title": "b", "depends_on": ["a"], "acceptance": "ok"},
        {"id": "c", "title": "c", "depends_on": ["b"], "acceptance": "ok"},
        {"id": "d", "title": "d", "depends_on": ["c"], "acceptance": "ok"},
        {"id": "e", "title": "e", "depends_on": ["d"], "acceptance": "ok"},
    ]
    assert _compute_disjoint_dag_width(tasks) == 1
    r = simulate_fanout(tasks, _CAP)
    assert r.topology == "sequential", r
    assert r.worker_count == 1, r


def test_fully_independent_siblings_parallel() -> None:
    """Third shape: no shared root — pure independent units (width == root count)."""
    tasks = [
        {"id": "a", "title": "a", "acceptance": "ok", "owned_paths": ["a/"]},
        {"id": "b", "title": "b", "acceptance": "ok", "owned_paths": ["b/"]},
        {"id": "c", "title": "c", "acceptance": "ok", "owned_paths": ["c/"]},
    ]
    assert _compute_disjoint_dag_width(tasks) == 3
    r = simulate_fanout(tasks, _CAP)
    assert r.topology != "sequential", r
    assert r.worker_count >= 2, r


# --- dependency cycles (#175) ------------------------------------------------
#
# `dep == tid` in audit_decomposition is a one-node cycle check. Before #175 a
# single intermediate node defeated it: `a -> a` was an error, `a -> b -> a`
# audited clean. These pin every arity, and the embedded case pins the shape
# that a cheap "no dependency-free root" test would miss (the healthy root
# keeps the root set non-empty while the cycle sits downstream of it).


def _task(task_id: str, deps: list[str]) -> dict[str, object]:
    """A fully populated task, so only dependency findings can fire."""
    return {
        "id": task_id,
        "title": f"task {task_id}",
        "acceptance": "done",
        "depends_on": deps,
    }


def test_audit_flags_two_node_cycle() -> None:
    audit = audit_decomposition([_task("a", ["b"]), _task("b", ["a"])])
    assert audit.ok is False
    assert any(f.code == "dependency_cycle" for f in audit.findings)
    assert audit.false_dependencies, "the offending back-edge must be reported"


def test_audit_flags_longer_cycles() -> None:
    """Arity is not the property under test: 3- and 4-node cycles too."""
    three = audit_decomposition([_task("a", ["b"]), _task("b", ["c"]), _task("c", ["a"])])
    four = audit_decomposition(
        [_task("a", ["b"]), _task("b", ["c"]), _task("c", ["d"]), _task("d", ["a"])]
    )
    assert three.ok is False
    assert four.ok is False
    assert any(f.code == "dependency_cycle" for f in three.findings)
    assert any(f.code == "dependency_cycle" for f in four.findings)


def test_audit_flags_cycle_downstream_of_a_healthy_root() -> None:
    """The root set is non-empty here, so root-counting alone cannot catch it."""
    audit = audit_decomposition([_task("root", []), _task("x", ["root", "y"]), _task("y", ["x"])])
    assert audit.independent_units == ("root",), "a real root still exists"
    assert audit.ok is False
    assert any(f.code == "dependency_cycle" for f in audit.findings)


def test_audit_cycle_message_names_the_tasks() -> None:
    """An operator must be able to see which tasks form the cycle."""
    audit = audit_decomposition([_task("alpha", ["beta"]), _task("beta", ["alpha"])])
    cycle_findings = [f for f in audit.findings if f.code == "dependency_cycle"]
    assert cycle_findings
    message = cycle_findings[0].message
    assert "alpha" in message and "beta" in message, message


def test_audit_still_passes_acyclic_graphs() -> None:
    """The control: cycle detection must not reject legitimate DAGs."""
    diamond = audit_decomposition(
        [
            _task("root", []),
            _task("left", ["root"]),
            _task("right", ["root"]),
            _task("join", ["left", "right"]),
        ]
    )
    assert diamond.ok is True
    assert not [f for f in diamond.findings if f.severity == "error"]
    # And every shipped fixture stays clean.
    for case in fixture_suite():
        assert audit_decomposition(case["tasks"]).ok is True, case["name"]


def test_audit_self_dependency_still_reported_as_such() -> None:
    """A 1-node cycle keeps its own, more specific code."""
    audit = audit_decomposition([_task("a", ["a"])])
    assert audit.ok is False
    assert any(f.code == "self_dependency" for f in audit.findings)
