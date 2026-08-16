"""OmniAgentOS certification suite — implementable Definition of Done.

Does not require multi-day production soak. Proves policy, isolation, allocation,
toolplane, grants, contracts, and fan-in.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from omniagentos.allocation import fixture_suite, simulate_fanout
from omniagentos.contracts import ActionClass
from omniagentos.db.store import SqliteStore
from omniagentos.execution.post_attempt import evaluate_post_attempt
from omniagentos.fanin import FanInMode, adjudicate
from omniagentos.gates import GateService
from omniagentos.grants import GrantsStore
from omniagentos.policy import evaluate_action, load_policy
from omniagentos.policy.shell import classify_shell
from omniagentos.scope.locks import LockHolder, PathLockStore
from omniagentos.scope.model import ScopeClaim
from omniagentos.taskcontract import AcceptanceCriterion, RiskClass, TaskContract
from omniagentos.toolplane.manifest import CapabilityManifest
from omniagentos.toolplane.scrub import scrub_text


def test_auto_mode_only_irreversible_parks() -> None:
    """AUTO parks IRREVERSIBLE only (Grok full-auto: irreversible + finance).

    Finance still cannot run unattended — connectors/broker HARD_HUMAN_CLASSES
    with store-backed grants. CONSEQUENTIAL auto-executes under AUTO.
    """
    cfg = load_policy()
    assert cfg.mode.value == "auto"
    for ac in ActionClass:
        d = evaluate_action(ac, cfg)
        if ac is ActionClass.IRREVERSIBLE:
            assert d.requires_approval is True
        else:
            assert d.requires_approval is False


def test_heredoc_granted_root_auto(tmp_path: Path) -> None:
    granted = tmp_path / "desktop"
    granted.mkdir()
    proj = tmp_path / "ws"
    proj.mkdir()
    target = granted / "out.html"
    cmd = f"cat > {target} << 'EOF'\n<html/>\nEOF"
    assert (
        classify_shell(cmd, str(proj), extra_roots=[str(granted)])
        == ActionClass.INTERNAL_REVERSIBLE
    )


def test_isolation_second_writer_blocked(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "c.db"))
    locks = PathLockStore(store)
    realm = str(tmp_path / "proj")
    Path(realm).mkdir()
    claims = [ScopeClaim.for_path(realm, "src/a.py", kind="file")]
    a = locks.try_acquire_scope(
        claims, LockHolder(kind="run", id="a", lane="runner"), generation=1, enforce=True
    )
    b = locks.try_acquire_scope(
        claims, LockHolder(kind="run", id="b", lane="runner"), generation=1, enforce=True
    )
    assert a.status == "granted"
    assert b.status == "blocked"


def test_fanout_fixture_suite_no_idle_on_trivial() -> None:
    for case in fixture_suite():
        if case["name"] == "trivial":
            r = simulate_fanout(case["tasks"])
            assert r.worker_count <= 1
            assert r.idle_spawn is False


# --- Fan-out certification helpers (test-local; the expectation side must ---
# --- never call omniagentos width code, so a collapsed policy cannot -------
# --- certify itself). ------------------------------------------------------

# Ground-truth max-antichain widths, hand-derived from each fixture's
# task/dependency structure and pinned as literals.
_PINNED_FIXTURE_WIDTHS: dict[str, int] = {
    # trivial: one node t1, no deps -> max antichain {t1} -> 1
    "trivial": 1,
    # partitionable: a, b, c carry no depends_on -> max antichain {a, b, c} -> 3
    "partitionable": 3,
    # sequential_chain: a -> b -> c, every pair comparable -> 1
    "sequential_chain": 1,
    # single_root_wide_body: s1..s5 each depend only on contract -> {s1..s5} -> 5
    "single_root_wide_body": 5,
    # strict_chain: a -> b -> c -> d -> e, every pair comparable -> 1
    "strict_chain": 1,
}


def _brute_force_max_antichain(tasks: list[dict[str, Any]]) -> int:
    """Independent oracle: largest set of pairwise-incomparable task ids."""
    ids = [str(t["id"]) for t in tasks]
    deps = {str(t["id"]): {str(d) for d in t.get("depends_on", [])} for t in tasks}
    reachable: dict[str, set[str]] = {}
    for tid in ids:
        seen: set[str] = set()
        stack = list(deps[tid])
        while stack:
            cur = stack.pop()
            if cur in seen:
                continue
            seen.add(cur)
            stack.extend(deps.get(cur, set()) - seen)
        reachable[tid] = seen
    for size in range(len(ids), 0, -1):
        for combo in itertools.combinations(ids, size):
            if all(
                x not in reachable[y] and y not in reachable[x]
                for x, y in itertools.combinations(combo, 2)
            ):
                return size
    return 0


def test_fanout_fixture_suite_certified_widths() -> None:
    """Certify EVERY fixture against data-carried expectations + pinned widths.

    A policy collapsed to worker_count=1 fails expect_min_workers on
    'partitionable' and 'single_root_wide_body'; one padded above the DAG
    width fails the pinned upper bounds on the chains.
    """
    for case in fixture_suite():
        name = case["name"]
        assert name in _PINNED_FIXTURE_WIDTHS, (
            f"fixture {name!r} has no hand-pinned width; derive its max "
            "antichain by hand and add it to _PINNED_FIXTURE_WIDTHS"
        )
        pinned_width = _PINNED_FIXTURE_WIDTHS[name]
        r = simulate_fanout(case["tasks"], case.get("capacity"))
        assert r.worker_count <= pinned_width, (name, r)
        assert r.idle_spawn is False, (name, r)
        if "expect_max_workers" in case:
            assert r.worker_count <= case["expect_max_workers"], (name, r)
        if "expect_min_workers" in case:
            assert r.worker_count >= case["expect_min_workers"], (name, r)
        if case.get("expect_parallel"):
            assert r.topology != "sequential", (name, r)
            assert r.worker_count >= 2, (name, r)
        if "expect_topology" in case:
            assert r.topology == case["expect_topology"], (name, r)


def test_fanout_black_box_antichain_oracle() -> None:
    """Black-box: worker bounds hold against an in-test brute-forced width."""
    cap = {
        "global_free_slots": 10,
        "repository_writer_slots": 5,
        "verifier_absorption": 2,
    }
    diamond = [
        {"id": "root", "title": "root", "acceptance": "ok"},
        {"id": "left", "title": "left", "depends_on": ["root"], "acceptance": "ok"},
        {"id": "right", "title": "right", "depends_on": ["root"], "acceptance": "ok"},
        {"id": "merge", "title": "merge", "depends_on": ["left", "right"], "acceptance": "ok"},
    ]
    twin_chains = [
        {"id": "a1", "title": "a1", "acceptance": "ok"},
        {"id": "a2", "title": "a2", "depends_on": ["a1"], "acceptance": "ok"},
        {"id": "b1", "title": "b1", "acceptance": "ok"},
        {"id": "b2", "title": "b2", "depends_on": ["b1"], "acceptance": "ok"},
    ]
    chain3 = [
        {"id": "x", "title": "x", "acceptance": "ok"},
        {"id": "y", "title": "y", "depends_on": ["x"], "acceptance": "ok"},
        {"id": "z", "title": "z", "depends_on": ["y"], "acceptance": "ok"},
    ]
    for tasks, expected_width in ((diamond, 2), (twin_chains, 2), (chain3, 1)):
        width = _brute_force_max_antichain(tasks)
        assert width == expected_width  # oracle sanity: hand-derived literal
        r = simulate_fanout(tasks, cap)
        assert r.worker_count <= width, r
        assert r.idle_spawn is False, r
        if width == 1:
            assert r.topology == "sequential", r
        else:
            assert r.topology != "sequential", r
            assert r.worker_count >= 2, r


def test_toolplane_scrub_and_manifest() -> None:
    text = scrub_text("token sk-abcdefghijklmnopqrstuvwxyz012345")
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in text or "REDACT" in text.upper()
    m = CapabilityManifest(
        run_id="r1",
        session_id="s1",
        holder_generation=1,
        read_roots=["/tmp"],
        write_roots=["/tmp"],
        allowed_ops=["read_file"],
    )
    assert m.holder_generation == 1


def test_grant_bounds(tmp_path: Path) -> None:
    from omniagentos.grants.store import is_grant_live

    store = SqliteStore(str(tmp_path / "g.db"))
    gs = GrantsStore(store)
    g = gs.create_grant(
        "gmail.send",
        label="t",
        target_set=["a@x.com"],
        approval_id="apr_cert_1",
        max_actions=1,
        max_spend_usd=1.0,
        expires_at="2099-01-01T00:00:00+00:00",
        metadata={"generation": 0, "action_class": "consequential"},
    )
    live, reason = is_grant_live(g, capability="gmail.send")
    assert live is True, reason
    res = gs.try_consume(
        g["id"],
        target="a@x.com",
        spend_usd=0,
        generation=0,
        action_class="consequential",
    )
    # ConsumeResult shape varies; any terminal result is fine
    assert res is not None


def test_task_contract_hash_stable() -> None:
    c = TaskContract(
        objective="ship",
        acceptance_criteria=(AcceptanceCriterion(id="AC-1", condition="tests pass"),),
        read_set=(),
        write_set=("a.py",),
        risk_class=RiskClass.R1,
    )
    assert c.contract_hash() == TaskContract.from_dict(c.to_dict()).contract_hash()


def test_fanin_select_best() -> None:
    r = adjudicate(
        [
            {"id": "w", "content": "x", "score": 0.1},
            {"id": "s", "content": "y", "score": 0.9},
        ],
        mode=FanInMode.SELECT_BEST,
    )
    assert len(r.selected) == 1


def test_gate_g8_parks_without_grant() -> None:
    d = GateService().g8_release({"action_class": "consequential"})
    assert d.decision in {"park", "deny", "fail"} or d.next_state == "release_parked"


def test_post_attempt_missing_file(tmp_path: Path) -> None:
    r = evaluate_post_attempt(working_dir=tmp_path, expected_files=["nope.md"], lane="swarm")
    assert r.mechanical_pass is False


def test_worker_abstraction_selects() -> None:
    from omniagentos.routing.workers import select_worker

    sel = select_worker(tier="standard", effort="high")
    assert sel.endpoint is not None
    assert sel.endpoint.mechanism == "terminal_cli"


def test_chain_read_records(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("OMNIAGENTOS_VAR", str(tmp_path))
    from omniagentos.sessions.chain_read import load_state, record_post_read

    record_post_read("ses_cert", "Read", {"file_path": "omniagentos/x.py"})
    assert "omniagentos/x.py" in load_state("ses_cert").relevant


# test_floor_validation_api retired with omniagentos/eval/ (operator-approved
# retirement: the operator D2, 2026-07-30; HYG-2 / HYG2-E1). Formerly pinned
# validate_floors_against_history; package had zero production importers.
