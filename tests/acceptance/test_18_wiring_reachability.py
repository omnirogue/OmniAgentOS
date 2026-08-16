"""AT-18 — Wiring reachability gate for critical production mechanisms.

This suite is collected by the default ``uv run pytest -q`` path
(``pyproject.toml`` ``testpaths = ["tests"]``), which is what ``make test``
runs AND what the release gate's ``backend`` phase runs
(``omniagentos/harnesses/release_gate.py``, PhaseSpec name=``"backend"``).

The explicit operator entry is::

    make wiring-gate

That is how this gate avoids being the sixth instance of the defect it
exists to catch: a mechanism built, tested, and never invoked from the
path that actually runs.

See ``tests.acceptance.suites.wiring_reachability`` for the engine, registry,
and the extra_edges escape hatch for engine-blind dynamic dispatch.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from tests.acceptance.suites.wiring_reachability import (
    PACKAGE_ROOT,
    REPO_ROOT,
    WIRING_REGISTRY,
    RegistryError,
    WiringEntry,
    WiringStatus,
    build_graph,
    counterfeit_find_path_by_name,
    counterfeit_grep_name_hits,
    evaluate_entry,
    find_path,
    format_path,
    invoke_default_off_probe,
    validate_registry,
)

MISSION_DARK_KEYS = (
    "context_resolver",
    "gate_evidence_writer",
    "contract_evaluator_project_grants",
    "provider_cost_ledger_writer",
    "effective_route_writer",
    "escalation_policy_consumer",
    "tool_broker_call",
    "verification_report_writer",
    "receipt_projection_reader",
)

# (mechanism, entry_point, defining FILE).
#
# WHY THE THIRD ELEMENT IS A FILE AND NOT A `file:line` CITATION
# ---------------------------------------------------------------
# It used to be `file:line`, and the line was a literal integer duplicated here
# and in ``WIRING_REGISTRY``. Every unrelated edit ABOVE a definition broke this
# suite: gate_evidence.py 710 -> 734, then 866 -> 890, then 762/918; broker.py
# 574 -> 679 -> 1440. Three separate drift repairs, none of which changed a
# single reviewed production pair — the ratchet was measuring whitespace.
#
# What the ratchet is actually for is that an arbitrary existing-but-unrelated
# symbol pair must not buy a green gate. That is a claim about SYMBOL IDENTITY,
# so it is now checked by symbol-name lookup: the mechanism's qualified name is
# resolved in the parsed call graph, and the definition it resolves to must be
# a `def`/`class` whose own name is the mechanism's last dotted component, in
# the file named here. That is strictly stronger than an integer — an integer
# cannot tell you the line still holds the symbol it was written for — and it
# does not drift when something above it grows.
MISSION_DARK_EXPECTATIONS = {
    "context_resolver": (
        "omniagentos.context.completeness.evaluate",
        "omniagentos.intake.service.dispatch_spec",
        "omniagentos/context/completeness.py",
    ),
    "gate_evidence_writer": (
        "omniagentos.scheduler.gate_evidence.GateEvidenceStore.record",
        "omniagentos.swarm.scheduler.default_verifier",
        "omniagentos/scheduler/gate_evidence.py",
    ),
    "contract_evaluator_project_grants": (
        "omniagentos.projects.policy.evaluate_action_for_project",
        "omniagentos.runner.core.Runner._execute_step",
        "omniagentos/projects/policy.py",
    ),
    "provider_cost_ledger_writer": (
        "omniagentos.swarm.dal.SwarmDal.record_attempt_usage",
        "omniagentos.swarm.provider_exec.ProviderSessionRunner._finish_process",
        "omniagentos/swarm/dal.py",
    ),
    "effective_route_writer": (
        "omniagentos.swarm.dal.SwarmDal.merge_run_params",
        "omniagentos.swarm.router.SwarmRouter.route",
        "omniagentos/swarm/dal.py",
    ),
    "escalation_policy_consumer": (
        "omniagentos.routing.learn.recommend_start_tier",
        "omniagentos.swarm.scheduler.SwarmScheduler._escalate",
        "omniagentos/routing/learn.py",
    ),
    "tool_broker_call": (
        "omniagentos.connectors.broker.call",
        "omniagentos.runner.core.Runner._execute_step",
        "omniagentos/connectors/broker.py",
    ),
    "verification_report_writer": (
        "omniagentos.graph_runtime.store.GraphStore.insert_artifact",
        "omniagentos.swarm.scheduler.SwarmScheduler._quality_gate",
        "omniagentos/graph_runtime/store.py",
    ),
    "receipt_projection_reader": (
        "omniagentos.scheduler.gate_evidence.GateEvidenceStore.load_receipt",
        "omniagentos.api.routes.swarm.get_swarm_run",
        "omniagentos/scheduler/gate_evidence.py",
    ),
}


def _definition_name_at(path: Path, lineno: int) -> str | None:
    """Name of the ``def``/``class`` that starts at ``lineno`` in ``path``.

    The symbol-name lookup that replaced the pinned integers. ``None`` means the
    line does not begin a definition at all, which is the failure the old
    integer assertion was really trying to catch.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return None
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.lineno == lineno
        ):
            return node.name
    return None

# ---------------------------------------------------------------------------
# Shared graph (one parse per process)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def graph():
    return build_graph(PACKAGE_ROOT)


def _xfail_mark(entry: WiringEntry) -> pytest.MarkDecorator:
    return pytest.mark.xfail(
        strict=True,
        reason=(
            f"GAP: no production call path from {entry.entry_point} to "
            f"{entry.mechanism} (citation {entry.citation}). "
            "If this XPASS: this is now wired, change status to REACHABLE "
            "in WIRING_REGISTRY — do not delete the test."
        ),
    )


def _params() -> list[pytest.ParameterSet]:
    out: list[pytest.ParameterSet] = []
    for entry in WIRING_REGISTRY:
        # UNREACHABLE: strict xfail so parallel-lane wiring becomes XPASS.
        # TEST_ONLY_CALLER: no xfail — a new production path must hard-fail
        # until the entry is deliberately reclassified.
        marks = [_xfail_mark(entry)] if entry.status is WiringStatus.UNREACHABLE else []
        out.append(pytest.param(entry, id=entry.key, marks=marks))
    return out


# ---------------------------------------------------------------------------
# Per-entry reachability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", _params())
def test_mechanism_reachable_from_entry_point(graph, entry: WiringEntry) -> None:
    """Assert production call/ref path matches the curated registry status."""
    verdict = evaluate_entry(graph, entry)

    if entry.status is WiringStatus.TEST_ONLY_CALLER:
        if verdict.reachable:
            pytest.fail(
                f"TEST_ONLY_CALLER entry gained a production path — reclassify "
                f"to REACHABLE (or REACHABLE_DEFAULT_OFF) in WIRING_REGISTRY; "
                f"do not silence this.\n"
                f"  mechanism:   {entry.mechanism}\n"
                f"  entry_point: {entry.entry_point}\n"
                f"  path:        {format_path(verdict.path)}\n"
                f"  citation:    {entry.citation}\n"
                f"  note:        {entry.note}"
            )
        return

    if not verdict.reachable:
        pytest.fail(
            f"no production call/ref path found\n"
            f"  mechanism:   {entry.mechanism}\n"
            f"  entry_point: {entry.entry_point}\n"
            f"  citation:    {entry.citation}\n"
            f"  declared:    {entry.status.value}\n"
            f"  note:        {entry.note}\n"
            f"  reason:      BFS over call+ref edges (+ extra_edges) found no path"
        )
    assert verdict.path is not None
    # Sanity: path starts at entry and ends at mechanism
    assert verdict.path[0].qualname == entry.entry_point
    assert verdict.path[-1].qualname == entry.mechanism


@pytest.mark.parametrize(
    "entry",
    [
        pytest.param(e, id=e.key)
        for e in WIRING_REGISTRY
        if e.status is WiringStatus.REACHABLE_DEFAULT_OFF
    ],
)
def test_default_off_probe(entry: WiringEntry) -> None:
    """Flipping a default-off flag must be loud; probe must still return off."""
    assert entry.default_off_probe is not None, (
        f"REACHABLE_DEFAULT_OFF entry {entry.key!r} missing default_off_probe"
    )
    actual = invoke_default_off_probe(entry.default_off_probe)
    assert actual == entry.default_off_probe.expected, (
        f"default_off_probe for {entry.key!r}: "
        f"{entry.default_off_probe.callable_fqn}({{}}) returned {actual!r}, "
        f"expected {entry.default_off_probe.expected!r}. "
        f"If you intentionally flipped the default, update the registry status "
        f"and probe — do not silence this assertion."
    )


# ---------------------------------------------------------------------------
# Registry integrity — anti-false-positive keystone
# ---------------------------------------------------------------------------


def test_registry_symbols_resolve_to_graph_nodes(graph) -> None:
    """Typos/moved symbols must FAIL LOUDLY, never become quiet 'unreachable'."""
    validate_registry(graph, WIRING_REGISTRY)


def test_registry_has_exactly_five_seed_entries() -> None:
    """Original five seeds stay first; memlife lanes (A, B, C) append after."""
    keys = [e.key for e in WIRING_REGISTRY]
    assert keys[:5] == [
        "cascade_ladder",
        "crash_recovery",
        "touched_modules_import_gate",
        "role_pack",
        "fleet_preflight",
    ], f"curated seed prefix must stay intact; got {keys}"
    # Lanes A, B, C append (keep-both merge). Do not reorder existing keys.
    for required in (
        "memlife_dream_cycle",
        "memlife_dream_routine_seed",
        "improve_dispatcher_tick",
        "lab_jobs_run_once",
        "memlife_candidate_writer",
        "memlife_render_on_graduation",
        "memlife_recall_leg",
        "memlife_recall_bridge_superseded",
    ):
        assert required in keys, f"missing memlife wiring entry {required!r}; got {keys}"


def test_registry_statuses_are_distinct_and_seeded() -> None:
    """All four status values exist as types; seeds exercise the intended ones."""
    statuses = {e.status for e in WIRING_REGISTRY}
    assert WiringStatus.UNREACHABLE in statuses
    assert WiringStatus.REACHABLE in statuses
    assert WiringStatus.REACHABLE_DEFAULT_OFF in statuses
    assert WiringStatus.TEST_ONLY_CALLER in statuses
    by_key = {e.key: e.status for e in WIRING_REGISTRY}
    assert by_key["cascade_ladder"] is WiringStatus.UNREACHABLE
    assert by_key["crash_recovery"] is WiringStatus.REACHABLE_DEFAULT_OFF
    assert by_key["touched_modules_import_gate"] is WiringStatus.REACHABLE
    assert by_key["role_pack"] is WiringStatus.REACHABLE_DEFAULT_OFF
    assert by_key["fleet_preflight"] is WiringStatus.TEST_ONLY_CALLER


def test_wp08_exact_suffix_count_and_status() -> None:
    """Deletion, reordering, duplication, or premature reclassification fails."""
    keys = [e.key for e in WIRING_REGISTRY]
    assert tuple(keys[-len(MISSION_DARK_KEYS) :]) == MISSION_DARK_KEYS, (
        f"WP0.8 mission dark keys must remain the exact registry suffix; got {keys}"
    )
    assert len(keys) == 22, (
        f"expected 5 seed + 8 memlife + 9 WP0.8 entries; got {len(keys)} entries"
    )
    assert len(keys) == len(set(keys)), f"registry keys must be unique; got {keys}"

    by_key = {e.key: e for e in WIRING_REGISTRY}
    for key in MISSION_DARK_KEYS:
        entry = by_key[key]
        assert entry.status is WiringStatus.UNREACHABLE, (
            f"{key} must remain UNREACHABLE until a real production path exists"
        )
        assert entry.extra_edges == (), f"{key} must not invent extra_edges"
        assert entry.default_off_probe is None, f"{key} must not hide behind a default-off probe"
        assert entry.note.startswith("GAP:"), entry.note


def test_wp08_exact_production_pairs_are_pinned(graph) -> None:
    """An arbitrary existing-but-unrelated symbol pair must not buy a green gate."""
    assert tuple(MISSION_DARK_EXPECTATIONS) == MISSION_DARK_KEYS
    by_key = {entry.key: entry for entry in WIRING_REGISTRY}
    for key, (mechanism, entry_point, citation_file) in MISSION_DARK_EXPECTATIONS.items():
        entry = by_key[key]
        registry_file = entry.citation.rpartition(":")[0] or entry.citation
        actual = (entry.mechanism, entry.entry_point, registry_file)
        assert actual == (mechanism, entry_point, citation_file), (
            f"{key} must ratchet its reviewed production pair and defining file; "
            f"expected {(mechanism, entry_point, citation_file)}, got {actual}"
        )
        # SYMBOL-NAME LOOKUP, not a line pin: the mechanism must resolve in the
        # parsed graph to a definition in the file this ratchet names.
        assert mechanism in graph.nodes, f"{key} mechanism {mechanism} is not a real symbol"
        assert graph.nodes[mechanism].file == citation_file, (
            f"{key} mechanism moved file: {graph.nodes[mechanism].file} != {citation_file}"
        )


def test_wp08_symbols_exist_but_paths_are_dark(graph) -> None:
    """Every curated pair resolves to real code and measures unreachable."""
    by_key = {entry.key: entry for entry in WIRING_REGISTRY}
    entries = tuple(by_key[key] for key in MISSION_DARK_KEYS)
    validate_registry(graph, entries)

    for entry in entries:
        verdict = evaluate_entry(graph, entry)
        assert verdict.reachable is False, (
            f"{entry.key} unexpectedly gained a production path:\n  {format_path(verdict.path)}"
        )
        assert verdict.path is None

        citation_file, separator, citation_line = entry.citation.rpartition(":")
        assert separator and citation_line.isdigit(), entry.citation
        node = graph.nodes[entry.mechanism]
        assert node.file == citation_file, (
            f"{entry.key} citation file drifted: {entry.citation}; "
            f"definition is {node.file}:{node.lineno}"
        )
        # SYMBOL-NAME LOOKUP replaces `node.lineno == int(citation_line)`.
        # The integer said nothing about whether the line still held the symbol
        # it was written for, and it broke on every unrelated edit above the
        # definition (gate_evidence.py 710 -> 734 -> 866 -> 890 -> 762/918;
        # broker.py 574 -> 679 -> 1440). What matters is that the definition the
        # graph resolved is genuinely the named symbol.
        expected_name = entry.mechanism.rsplit(".", 1)[-1]
        defined_name = _definition_name_at(REPO_ROOT / node.file, node.lineno)
        assert defined_name == expected_name, (
            f"{entry.key} does not resolve to a definition of {expected_name!r}: "
            f"{node.file}:{node.lineno} defines {defined_name!r}"
        )


def _class_methods(path: Path, class_name: str) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return {node.name: node for node in class_node.body if isinstance(node, ast.FunctionDef)}


def test_provider_cost_row_distinguishes_both_existing_usage_writes() -> None:
    """The gap is provider-call cost at completion, not either extant usage call."""
    entry = next(item for item in WIRING_REGISTRY if item.key == "provider_cost_ledger_writer")
    assert entry.entry_point == (
        "omniagentos.swarm.provider_exec.ProviderSessionRunner._finish_process"
    )
    assert entry.mechanism == "omniagentos.swarm.dal.SwarmDal.record_attempt_usage"

    provider_exec = REPO_ROOT / "omniagentos/swarm/provider_exec.py"
    methods = _class_methods(provider_exec, "ProviderSessionRunner")
    read_process = methods["_read_process"]
    finish_process = methods["_finish_process"]
    record_usage = methods["_record_usage"]
    record_effort = methods["_record_dispatched_effort"]

    read_calls = [
        node.func.attr
        for node in ast.walk(read_process)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "_finish_process" in read_calls, (
        "_finish_process must remain on the live provider-reader path"
    )

    finish_calls = [
        node.func.attr
        for node in ast.walk(finish_process)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "_record_usage" in finish_calls, (
        "_finish_process must remain the real provider-result completion edge"
    )

    attempt_calls = [
        node
        for node in ast.walk(record_effort)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "record_attempt_usage"
    ]
    assert len(attempt_calls) == 1, (
        "the existing explicit record_attempt_usage caller must be acknowledged exactly once"
    )
    assert {keyword.arg for keyword in attempt_calls[0].keywords} == {"effort"}, (
        "the existing attempt write is dispatched effort only, not provider-reported cost"
    )

    getattr_targets = [
        node.args[1].value
        for node in ast.walk(record_usage)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and isinstance(node.args[1].value, str)
    ]
    assert getattr_targets == ["record_session_usage"], (
        "_record_usage's dynamic seam targets the session writer, not record_attempt_usage"
    )


def test_verification_report_row_targets_a_durable_sqlite_seam(tmp_path: Path) -> None:
    """The ratcheted mechanism persists a report payload across a store reopen."""
    from omniagentos.graph_runtime.contracts import DIAMOND_TEMPLATE
    from omniagentos.graph_runtime.store import GraphStore

    db_path = tmp_path / "verification-seam.sqlite3"
    store = GraphStore(db_path)
    run = store.create_run(title="verification persistence proof", template=DIAMOND_TEMPLATE)
    artifact_id = store.insert_artifact(
        {
            "graph_run_id": run["id"],
            "node_key": "verify",
            "port": "verdict",
            "artifact_type": "verification_report",
            "content_hash": "sha256:verification-report-proof",
            "content_uri": "memory://verification-report-proof",
            "schema_ok": True,
            "verified": True,
            "payload": {
                "artifact_hash": "candidate-sha",
                "verdict": "pass",
                "findings": [],
                "evidence": ["focused gate"],
            },
        }
    )
    store.close()

    reopened = GraphStore(db_path)
    persisted = reopened.get_run(str(run["id"]))
    reopened.close()
    assert persisted is not None
    artifact = next(item for item in persisted["artifacts"] if item["id"] == artifact_id)
    assert artifact["artifact_type"] == "verification_report"
    assert artifact["verified"] == 1

    entry = next(item for item in WIRING_REGISTRY if item.key == "verification_report_writer")
    assert entry.mechanism == "omniagentos.graph_runtime.store.GraphStore.insert_artifact"
    assert entry.mechanism != "omniagentos.verify.report.VerificationReport.to_dict"


def test_wp08_each_param_is_a_strict_gap_xfail() -> None:
    """A newly wired mission path must XPASS loudly until reclassified."""
    params = {parameter.id: parameter for parameter in _params()}
    for key in MISSION_DARK_KEYS:
        xfail_marks = [mark for mark in params[key].marks if mark.name == "xfail"]
        assert len(xfail_marks) == 1, f"{key} must have exactly one xfail mark; got {xfail_marks}"
        mark = xfail_marks[0]
        assert mark.name == "xfail"
        assert mark.kwargs.get("strict") is True
        reason = str(mark.kwargs.get("reason", ""))
        assert reason.startswith("GAP:"), reason
        assert "change status to REACHABLE" in reason


def test_wp08_mechanism_typo_fails_registry_validation(graph) -> None:
    """A hostile symbol typo must fail, never masquerade as an open gap."""
    original = next(entry for entry in WIRING_REGISTRY if entry.key == "context_resolver")
    corrupted = replace(original, mechanism=f"{original.mechanism}_typo")
    with pytest.raises(RegistryError, match="typo or moved symbol"):
        validate_registry(graph, (corrupted,))


# ---------------------------------------------------------------------------
# Engine controls — cannot silently degenerate
# ---------------------------------------------------------------------------


def test_positive_control_known_path_present(graph) -> None:
    """A known-true DI path the engine must see (ref edge through default_verifier)."""
    entry = "omniagentos.swarm.scheduler.SwarmScheduler.start_run"
    target = "omniagentos.swarm.scheduler.assert_touched_modules_importable"
    path = find_path(graph, entry, target, ())
    assert path is not None, (
        "positive control failed: engine did not find start_run -> "
        "assert_touched_modules_importable (DI seam via default_verifier / "
        f"self._verifier). Graph nodes={graph.node_count} edges={graph.edge_count}"
    )
    assert path[0].qualname == entry
    assert path[-1].qualname == target
    # default_verifier must appear on the path (the DI value hop)
    quals = [h.qualname for h in path]
    assert "omniagentos.swarm.scheduler.default_verifier" in quals, (
        f"positive control path missing default_verifier hop: {format_path(path)}"
    )


def test_negative_control_fabricated_pair_absent(graph) -> None:
    """'Everything is reachable' must fail: lifespan does not call run_cascade."""
    entry = "omniagentos.api.main.lifespan"
    target = "omniagentos.routing.cascade.run_cascade"
    path = find_path(graph, entry, target, ())
    assert path is None, (
        f"negative control failed: fabricated pair unexpectedly reachable:\n  {format_path(path)}"
    )


def test_decisive_run_cascade_unreachable_with_evidence(graph) -> None:
    """DECISIVE: routing.cascade.run_cascade must report UNREACHABLE with evidence."""
    entry = next(e for e in WIRING_REGISTRY if e.key == "cascade_ladder")
    assert entry.mechanism == "omniagentos.routing.cascade.run_cascade"
    verdict = evaluate_entry(graph, entry)
    assert verdict.reachable is False, (
        f"run_cascade unexpectedly reachable from {entry.entry_point}:\n"
        f"  {format_path(verdict.path)}"
    )
    assert entry.status is WiringStatus.UNREACHABLE
    # Evidence: the node exists (not a typo) but no inbound production edges from entry
    assert entry.mechanism in graph.nodes
    assert entry.entry_point in graph.nodes
    inbound = [e for e in graph.edges if e.target == entry.mechanism]
    # Inbound edges may exist from same-module helpers; none may form a path from entry.
    assert find_path(graph, entry.entry_point, entry.mechanism, ()) is None
    # Citation points at a real definition line in the package.
    cit_file, _, cit_line = entry.citation.partition(":")
    assert (REPO_ROOT / cit_file).is_file(), entry.citation
    assert cit_line.isdigit(), entry.citation
    # Keep inbound list referenced so future readers see we inspected it.
    assert isinstance(inbound, list)


def test_graph_non_degeneracy(graph) -> None:
    """An engine that parses nothing must not pass (everything 'unreachable')."""
    assert graph.files_parsed >= 100, f"too few files parsed: {graph.files_parsed}"
    assert graph.node_count >= 500, f"too few nodes: {graph.node_count}"
    assert graph.edge_count >= 500, f"too few edges: {graph.edge_count}"


def test_name_collision_run_cascade_nodes_are_distinct(graph) -> None:
    """Basename matching must not merge judges.run_cascade with routing.cascade."""
    a = "omniagentos.improve.judges.run_cascade"
    b = "omniagentos.routing.cascade.run_cascade"
    assert a in graph.nodes, f"missing node {a}"
    assert b in graph.nodes, f"missing node {b}"
    assert a != b
    assert graph.nodes[a].file != graph.nodes[b].file
    # Edges to one are never attributed to the other: no collapsed basename target.
    for e in graph.edges:
        assert e.target != "run_cascade", "edge target must be fully qualified"
        assert e.source != "run_cascade"
    judges_callers = {e.source for e in graph.edges if e.target == a}
    routing_callers = {e.source for e in graph.edges if e.target == b}
    # routing.cascade.run_cascade must not inherit judges callers (or vice versa)
    assert judges_callers.isdisjoint(routing_callers) or not (judges_callers & routing_callers), (
        f"shared callers between distinct run_cascade functions: {judges_callers & routing_callers}"
    )


def test_graph_excludes_tests_package_entirely(graph) -> None:
    """tests/ is outside omniagentos/ — no node file may live under tests/."""
    tests_root = (REPO_ROOT / "tests").resolve()
    offenders: list[str] = []
    for node in graph.nodes.values():
        # node.file is repo-relative
        file_path = (REPO_ROOT / node.file).resolve()
        try:
            file_path.relative_to(tests_root)
        except ValueError:
            continue
        else:
            offenders.append(f"{node.qualname} ({node.file})")
    assert not offenders, "call graph must not include tests/ nodes; found:\n  " + "\n  ".join(
        offenders[:20]
    )


def test_unreachable_entries_are_strict_xfail_with_gap_prefix() -> None:
    """Match the repo ratchet idiom: GAP: prefix, strict=True."""
    for entry in WIRING_REGISTRY:
        if entry.status is not WiringStatus.UNREACHABLE:
            continue
        # The parametrized mark is applied in _params(); re-check reason shape.
        mark = _xfail_mark(entry)
        reason = mark.kwargs.get("reason", "")
        assert reason.startswith("GAP:"), reason
        assert mark.kwargs.get("strict") is True
        assert "change status to REACHABLE" in reason


def test_test_only_caller_entries_are_not_xfail() -> None:
    """TEST_ONLY_CALLER must not use the UNREACHABLE xfail ratchet."""
    for entry in WIRING_REGISTRY:
        if entry.status is not WiringStatus.TEST_ONLY_CALLER:
            continue
        # Params for this entry must carry no xfail mark.
        for p in _params():
            if p.id == entry.key:
                mark_names = {m.name for m in p.marks}
                assert "xfail" not in mark_names, (
                    f"TEST_ONLY_CALLER entry {entry.key!r} must not be xfail"
                )


def test_cli_module_importable() -> None:
    """Importable as a module like at17_progress."""
    from tests.acceptance.suites import wiring_reachability as wr

    assert callable(wr.main)
    assert callable(wr.build_graph)
    assert Path(wr.PACKAGE_ROOT).name == "omniagentos"


# ---------------------------------------------------------------------------
# Counterfeit trap — grep-for-name is not reachability
# ---------------------------------------------------------------------------


def test_counterfeit_grep_name_is_not_reachability(graph) -> None:
    """COUNTERFEIT: textual name presence must not be treated as a production caller.

    Subject is ``cascade_ladder`` / ``run_cascade``: still genuinely unwired,
    with many docstring/``__all__`` mentions and a same-named different
    function at ``omniagentos.improve.judges.run_cascade``. A grep-for-name
    checker claims it is reachable; the AST gate must still report no path.
    """
    entry = next(e for e in WIRING_REGISTRY if e.key == "cascade_ladder")
    assert entry.status is WiringStatus.UNREACHABLE, (
        f"counterfeit subject {entry.key!r} is no longer UNREACHABLE "
        f"(status={entry.status.value}). RE-POINT this test at another "
        f"genuinely-unwired mechanism — do NOT relax the assertion to buy green."
    )
    assert entry.mechanism == "omniagentos.routing.cascade.run_cascade"
    assert entry.entry_point == "omniagentos.orchestrator.core.Orchestrator._execute_task"
    basename = entry.mechanism.rsplit(".", 1)[-1]
    assert basename == "run_cascade"

    grep_hits = counterfeit_grep_name_hits(PACKAGE_ROOT, basename)
    assert len(grep_hits) >= 2, (
        f"counterfeit setup broken: expected textual hits for {basename!r} "
        f"in omniagentos/ (docstrings/__all__/name collisions), got {len(grep_hits)}"
    )
    non_def_hits = [
        h
        for h in grep_hits
        if not h[2].lstrip().startswith(f"def {basename}")
        and not h[2].lstrip().startswith(f"async def {basename}")
    ]
    assert non_def_hits, (
        "counterfeit setup broken: need docstring/comment/__all__/collision "
        f"textual hits, not only the definition; hits={grep_hits[:5]}"
    )
    judges_hits = [h for h in grep_hits if h[0] == "omniagentos/improve/judges.py"]
    assert judges_hits, (
        "counterfeit setup broken: expected name-collision hits in "
        "omniagentos/improve/judges.py (different function, same basename)"
    )

    counterfeit_path = counterfeit_find_path_by_name(
        PACKAGE_ROOT, entry.entry_point, entry.mechanism
    )
    assert counterfeit_path is not None, (
        "counterfeit_find_path_by_name must claim REACHABLE for a textually "
        f"mentioned but unwired mechanism ({basename!r})"
    )
    # Fabricated call site must not be a real call edge into the mechanism.
    claimed = counterfeit_path[-1]
    real_call_at_claimed_site = [
        e
        for e in graph.edges
        if e.kind == "call"
        and e.target == entry.mechanism
        and e.file == claimed.file
        and e.lineno == claimed.lineno
    ]
    assert not real_call_at_claimed_site, (
        f"counterfeit path cited a real call edge at {claimed.file}:{claimed.lineno}; "
        f"fabricated site must not be a true call into {entry.mechanism}"
    )

    real_path = find_path(graph, entry.entry_point, entry.mechanism, ())
    assert real_path is None, f"AST path unexpectedly found:\n  {format_path(real_path)}"
    # The distinction that is the entire value of this gate:
    assert counterfeit_path is not None and real_path is None, (
        "counterfeit says reachable, AST says unreachable — that gap is the "
        "whole point of the gate (text scan ≠ production call path)"
    )


def test_counterfeit_docstring_only_symbol_not_reachable(graph, tmp_path: Path) -> None:
    """A symbol mentioned only in a docstring must never gain a call edge.

    Builds a tiny package with a docstring reference and no Call; the engine
    must report no path. A grep-for-name implementation (and
    :func:`counterfeit_find_path_by_name`) would pass wrongly.

    Rationale: the real-repo counterfeit subject can go stale when a lane wires
    it (exactly what happened to crash_recovery / resume_stale_swarms). This
    synthetic fixture cannot drift, so the load-bearing proof survives.
    """
    pkg = tmp_path / "omniagentos"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "alpha.py").write_text(
        '''\
"""alpha mentions ghost_mechanism in this docstring only."""

def entry() -> None:
    """entry does not call ghost_mechanism."""
    return None


def ghost_mechanism() -> None:
    """Defined, never called. Docstring: ghost_mechanism self-mention."""
    return None
''',
        encoding="utf-8",
    )
    g = build_graph(pkg)
    entry_point = "omniagentos.alpha.entry"
    mechanism = "omniagentos.alpha.ghost_mechanism"
    path = find_path(g, entry_point, mechanism, ())
    assert path is None, f"docstring-only mention created a path: {format_path(path)}"
    hits = counterfeit_grep_name_hits(pkg, "ghost_mechanism")
    assert hits, "counterfeit grep must still find the docstring name"
    counterfeit_path = counterfeit_find_path_by_name(pkg, entry_point, mechanism)
    assert counterfeit_path is not None, (
        "counterfeit_find_path_by_name must claim REACHABLE for docstring-only "
        "ghost_mechanism (synthetic fixture that cannot drift)"
    )
    assert path is None and counterfeit_path is not None
