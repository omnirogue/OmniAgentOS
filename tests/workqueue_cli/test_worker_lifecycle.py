"""The worker's claim → clone → run → verify → report loop (SPEC §7, steps 1-8).

Every test drives the REAL loop against a real throwaway git repo and the REAL
``WorkQueueStore`` on a tmp DB, because the parts worth testing are exactly the
parts that touch git and the store contract.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from omniagentos.workqueue.store import WorkQueueStore
from omniagentos.workqueue.worker import Worker, path_in_scope
from tests.workqueue_cli.demorepo import make_repo, unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = yaml.safe_load((REPO_ROOT / "configs" / "workqueue.yaml").read_text())


@pytest.fixture
def bench(tmp_path: Path) -> Iterator[dict[str, Any]]:
    repo, sha = make_repo(tmp_path / "src")
    store = WorkQueueStore(str(tmp_path / "wq.sqlite3"))
    worker = Worker(store, "test-machine", config=CONFIG, home=tmp_path / "wq")
    try:
        yield {"repo": repo, "sha": sha, "store": store, "worker": worker, "tmp": tmp_path}
    finally:
        store.close()


def _claim(bench: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """Enqueue one unit and claim it through the frozen store surface."""
    unit_id, _ = bench["store"].enqueue(unit(bench["sha"], bench["repo"], **kw))
    bench["unit_id"] = unit_id
    claim = bench["store"].claim("test-machine", bench["worker"].worker_id, [])
    assert claim is not None
    return claim


def _stored(bench: dict[str, Any]) -> dict[str, Any]:
    stored = bench["store"].get_unit(bench["unit_id"])
    assert stored is not None
    return stored


def _attempt_rows(bench: dict[str, Any]) -> int:
    """Every attempt row in the DB — not just one unit's, so 'nothing was claimed'
    is checkable without naming a unit."""
    with sqlite3.connect(bench["store"].db_path) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM wq_attempts").fetchone()[0])


def test_script_unit_passes_and_pushes_its_branch(bench: dict[str, Any]) -> None:
    result = bench["worker"].execute(_claim(bench))
    assert result.outcome == "pass"
    assert result.exit_code == 0
    assert result.result_branch == "wq/test"
    assert result.result_sha == bench["sha"]
    # Pushed into the mirror, which is the only push remote a local demo repo has.
    mirror = bench["tmp"] / "wq" / "repos" / f"{bench['repo'].name}.git"
    refs = (mirror / "refs" / "heads" / "wq" / "test").exists()
    packed = (
        "wq/test" in (mirror / "packed-refs").read_text()
        if (mirror / "packed-refs").exists()
        else False
    )
    assert refs or packed, "a pass must publish its branch"
    assert Path(result.log_path).exists()
    stored = _stored(bench)
    assert stored["state"] == "done"


def test_exit_1_is_a_candidate_defect_and_consumes_the_attempt(bench: dict[str, Any]) -> None:
    claim = _claim(bench, acceptance_cmd="python3 -c 'raise SystemExit(1)'")
    result = bench["worker"].execute(claim)
    assert result.outcome == "candidate-defect"
    assert result.exit_code == 1
    assert result.retryable == 0
    assert _stored(bench)["attempt"] == 1  # stands


def test_quota_signature_overrides_the_exit_code(bench: dict[str, Any]) -> None:
    """SPEC §7 Phase-3 demo #4 — the Initech repair-cycle-68→71 failure.

    The command exits 1, which on its own reads as a defect. The §4.4b classifier
    sees the quota signature and reclassifies to `environment`: it never enters
    the defect count, and the store gives the attempt back.
    """
    claim = _claim(
        bench,
        acceptance_cmd="python3 -c \"print('Error: rate limit exceeded'); raise SystemExit(1)\"",
    )
    result = bench["worker"].execute(claim)
    assert result.outcome == "environment"
    assert result.exit_code == 1, "the PROCESS code is still recorded truthfully"
    assert result.retryable == 1
    stored = _stored(bench)
    assert stored["attempt"] == 0, "an instrument fault must never consume a candidate's budget"
    assert stored["instrument_retries"] == 1


def test_auth_signature_is_terminal_at_the_first_occurrence(bench: dict[str, Any]) -> None:
    claim = _claim(
        bench,
        acceptance_cmd="python3 -c \"print('HTTP 401 Unauthorized'); raise SystemExit(1)\"",
    )
    result = bench["worker"].execute(claim)
    assert (result.outcome, result.retryable) == ("environment", 0)
    stored = _stored(bench)
    assert stored["state"] == "parked"
    assert stored["terminal_reason"] == "terminal-instrument"


def test_writing_outside_owned_paths_fails_regardless_of_exit_code(bench: dict[str, Any]) -> None:
    claim = _claim(
        bench,
        acceptance_cmd="python3 -c \"open('escaped.txt','w').write('x')\"",
        owned_paths=["demo/**"],
    )
    result = bench["worker"].execute(claim)
    assert result.outcome == "candidate-defect", "exit 0 does not excuse an out-of-scope write"
    assert "escaped.txt" in result.remedy


def test_writing_inside_owned_paths_is_fine(bench: dict[str, Any]) -> None:
    claim = _claim(
        bench,
        acceptance_cmd="python3 -c \"open('demo/output.txt','w').write('x')\"",
        owned_paths=["demo/**"],
    )
    assert bench["worker"].execute(claim).outcome == "pass"


def test_an_already_refused_input_is_refused_before_anything_is_spent(
    bench: dict[str, Any],
) -> None:
    """§4.2 — the refusal is consulted BEFORE the clone, so it costs ~0.2s."""
    worker = bench["worker"]
    first = worker.execute(_claim(bench, acceptance_cmd="python3 -c 'raise SystemExit(1)'"))
    assert first.outcome == "candidate-defect"
    key = first.input_key
    assert bench["store"].refusal_check(key, "raw")["count"] == 1

    # A sentinel a re-clone would destroy: the refusal must happen before the clone.
    sentinel = bench["tmp"] / "wq" / "work" / _stored(bench)["id"] / "sentinel"
    sentinel.write_text("still here\n")

    second = worker.execute(_claim(bench, acceptance_cmd="python3 -c 'raise SystemExit(1)'"))
    assert second.outcome == "unchanged-retry"
    assert second.input_key == key
    assert "re-running unchanged is not a strategy" in second.remedy
    assert sentinel.exists(), "the workdir was re-cloned for an input the ledger had refused"
    assert bench["store"].refusal_check(key, "raw")["count"] == 2
    assert _stored(bench)["state"] == "parked"
    # SOFT park: no terminal_reason, because nothing ran and nothing is broken.
    assert _stored(bench)["terminal_reason"] is None


def test_storm_cap_parks_the_unit(bench: dict[str, Any]) -> None:
    worker = bench["worker"]
    claim = _claim(bench, acceptance_cmd="python3 -c 'raise SystemExit(1)'")
    key = worker.execute(claim).input_key
    for _ in range(4):
        bench["store"].refusal_record(key, "raw", "unchanged-retry", 0, "x")
    result = worker.execute(_claim(bench, acceptance_cmd="python3 -c 'raise SystemExit(1)'"))
    assert result.outcome == "storm-parked"
    assert "PARKED" in result.remedy
    assert _stored(bench)["terminal_reason"] == "storm-parked"


def test_an_agent_unit_is_fingerprinted_AFTER_its_turn(bench: dict[str, Any]) -> None:
    """§4.1 hashes the tree the GATE grades — which, for an agent unit, only
    exists once the agent has run.

    Fingerprinting the pre-agent tree instead would give every attempt of one unit
    the same key, so attempt 2 would be refused as unchanged-retry before the
    agent could produce anything different, and §4.6's lineage swap (a DIFFERENT
    profile on the second defect) would never get to run. That failure is silent —
    the queue would simply stop retrying agent work — so it is pinned here.
    """
    config = dict(CONFIG)
    config["profiles"] = dict(CONFIG["profiles"])
    config["profiles"]["fake-agent"] = {
        "lineage": "test",
        "cmd": [
            "python3",
            "-c",
            "import time; open('demo/agent.txt','w').write(str(time.time_ns()))",
        ],
    }
    worker = Worker(bench["store"], "test-machine", config=config, home=bench["tmp"] / "wq")
    bench["worker"] = worker  # so _claim() uses this worker's id

    first = worker.execute(
        _claim(bench, profile="fake-agent", acceptance_cmd="python3 -c 'raise SystemExit(1)'")
    )
    assert first.outcome == "candidate-defect"
    second = worker.execute(
        _claim(bench, profile="fake-agent", acceptance_cmd="python3 -c 'raise SystemExit(1)'")
    )
    assert second.outcome == "candidate-defect", "the retry was refused before the agent ran"
    assert second.input_key != first.input_key, "the agent's output must change the key"


def _require_gate_disk_headroom(tmp: Path) -> None:
    # Premise pin: unit-acceptance.yaml declares `disk-free NG {workdir}` and the
    # gate (correctly) refuses to grade below it — on small CI disks that refusal
    # is a property of the host, not of the code under test.
    import re
    import shutil

    text = (Path(__file__).parents[2] / "configs" / "gates.d" / "unit-acceptance.yaml").read_text()
    m = re.search(r"disk-free (\d+)G", text)
    need = int(m.group(1)) if m else 5
    free = shutil.disk_usage(tmp).free / 1024**3
    if free < need:
        pytest.skip(
            f"unit-acceptance gate declares disk-free {need}G and this host has "
            f"{free:.1f}G free at {tmp} — the gate would (correctly) refuse to grade"
        )


def test_gated_unit_runs_through_scripts_accurate_gate(bench: dict[str, Any]) -> None:
    """The real acceptance path: worker → configs/gates.d/unit-acceptance.yaml.

    The worker passes ``--ledger off`` because IT owns the ledger write for a unit
    attempt: only the worker knows the class after the §4.4b override, and a row
    recorded before the override would name the wrong cause.
    """
    _require_gate_disk_headroom(bench["tmp"])
    result = bench["worker"].execute(_claim(bench, gate="unit-acceptance"))
    assert result.outcome == "pass", result.remedy
    assert result.exit_code == 0
    assert result.gate_exit_code == 0, "the wrapped command's own code is recorded too"
    receipts = list(
        (bench["tmp"] / "wq" / "gate-evidence" / "unit-acceptance").glob("*/receipt.json")
    )
    assert receipts, "evidence always: every run writes a receipt"
    receipt = json.loads(receipts[0].read_text())
    assert receipt["class"] == "pass"
    assert receipt["input_key"] == result.input_key, "worker and gate must agree on the key"
    assert receipt["input_key_source"] == "caller"


def test_gated_unit_failure_is_a_candidate_defect(bench: dict[str, Any]) -> None:
    _require_gate_disk_headroom(bench["tmp"])
    result = bench["worker"].execute(
        _claim(bench, gate="unit-acceptance", acceptance_cmd="python3 -c 'raise SystemExit(1)'")
    )
    assert result.outcome == "candidate-defect"
    assert result.exit_code == 1
    assert result.gate_exit_code == 1


def test_unknown_profile_is_an_instrument_error_not_a_defect(bench: dict[str, Any]) -> None:
    result = bench["worker"].execute(_claim(bench, profile="no-such-profile"))
    assert result.outcome == "instrument-error"
    assert result.retryable == 0
    assert "configs/workqueue.yaml" in result.remedy


def test_timeout_kills_the_process_group_and_stands_as_an_attempt(
    bench: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit's own timeout_s bounds the WORK (the lease only bounds silence).

    SPEC §3.1 lists six reversing outcomes and timeout is not one of them, so the
    attempt stands: exceeding the declared budget is evidence about the candidate.
    """
    import omniagentos.workqueue.worker as worker_module

    monkeypatch.setattr(worker_module, "KILL_GRACE_S", 0)
    claim = _claim(bench, acceptance_cmd="python3 -c 'import time; time.sleep(120)'")
    claim["unit"]["timeout_s"] = 1
    result = bench["worker"].execute(claim)
    assert result.outcome == "timeout"
    assert _stored(bench)["attempt"] == 1, "timeout consumes the attempt"


def test_capacity_gate_blocks_claiming_when_the_box_is_loaded(
    bench: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import omniagentos.workqueue.worker as worker_module

    bench["store"].enroll_machine(
        {
            "machine_id": "test-machine",
            "hostname": "h",
            "os": "linux",
            "labels": [],
            "max_concurrent": 2,
            "ncpu": 8,
            "perf_cores": 8,
            "ceiling_fraction": 0.75,
        }
    )
    monkeypatch.setattr(worker_module, "load1", lambda: 99.0)
    worker = Worker(bench["store"], "test-machine", config=CONFIG, home=bench["tmp"] / "wq")
    ok, current = worker.capacity_ok()
    assert (ok, current) == (False, 99.0)
    monkeypatch.setattr(worker_module, "load1", lambda: 1.0)
    assert worker.capacity_ok()[0] is True


def test_drain_stops_claiming_without_breaking_a_lease(bench: dict[str, Any]) -> None:
    bench["store"].enroll_machine(
        {
            "machine_id": "test-machine",
            "hostname": "h",
            "os": "linux",
            "labels": [],
            "max_concurrent": 1,
            "ncpu": 8,
            "perf_cores": 8,
        }
    )
    bench["store"].set_drain("test-machine", True)
    # A unit is waiting, so "claimed nothing" is a statement about the drain flag
    # and not about an empty queue.
    bench["store"].enqueue(unit(bench["sha"], bench["repo"]))
    worker = bench["worker"]
    worker.beat(force=True)
    assert worker.drain is True
    assert worker.run_forever(once=True) == 0
    assert _attempt_rows(bench) == 0, "a draining worker must claim nothing"
    bench["store"].set_drain("test-machine", False)
    worker.beat(force=True)
    assert worker.drain is False, "--undo must be observable, so a drained worker keeps beating"


def test_self_enrolls_when_the_machine_row_is_missing(bench: dict[str, Any]) -> None:
    info = bench["worker"].machine(force=True)
    assert info.ncpu >= 1
    row = bench["store"].list_machines()[0]
    assert row["machine_id"] == "test-machine"
    assert row["ncpu"] == info.ncpu
    assert row["os"] in ("darwin", "linux")


@pytest.mark.parametrize(
    ("path", "owned", "expected"),
    [
        ("demo/a.txt", ["demo/**"], True),
        ("demo/deep/nested/a.txt", ["demo/**"], True),
        ("escaped.txt", ["demo/**"], False),
        ("demo2/a.txt", ["demo/**"], False),
        ("omniagentos/workqueue/worker.py", ["omniagentos/workqueue/*.py"], True),
        # A bare '*' must NOT cross a separator, or owned_paths stops meaning anything.
        ("omniagentos/workqueue/sub/x.py", ["omniagentos/workqueue/*.py"], False),
        ("configs/gates.d/unit-acceptance.yaml", ["configs/gates.d/"], True),
        ("scripts/accurate-gate.py", ["scripts/accurate-gate.py"], True),
    ],
)
def test_owned_paths_glob_semantics(path: str, owned: list[str], expected: bool) -> None:
    assert path_in_scope(path, owned) is expected


def test_worker_id_is_globally_unique(bench: dict[str, Any]) -> None:
    """'<machine_id>:<pid>:<boot-nonce>' — a pid is meaningless across machines."""
    other = Worker(bench["store"], "test-machine", config=CONFIG, home=bench["tmp"] / "wq")
    assert other.worker_id != bench["worker"].worker_id
    assert other.worker_id.startswith(f"test-machine:{os.getpid()}:")


# ---- what a submitted unit may reach on someone else's Mac ---------------------
#
# Both of these are the same question: a unit is TEXT that arrives from another
# box, and the worker turns it into a path and a process. Neither may become a
# way to read the host.


@pytest.mark.parametrize(
    "brief_path",
    [
        "../../../../etc/passwd",
        "demo/../../escape.md",
        "/etc/passwd",
        "/Users/youruser/.config/omni/connections.env",
    ],
)
def test_a_brief_path_that_escapes_the_worktree_is_refused(
    bench: dict[str, Any], brief_path: str
) -> None:
    """An escaping brief_path is an INSTRUMENT error, terminal at the first try.

    Terminal because re-running cannot help: the unit as submitted is malformed,
    and a retry would just ask the same box for the same file again.
    """
    config = dict(CONFIG)
    config["profiles"] = dict(CONFIG["profiles"])
    config["profiles"]["fake-agent"] = {"lineage": "test", "cmd": ["python3", "-c", "pass"]}
    worker = Worker(bench["store"], "test-machine", config=config, home=bench["tmp"] / "wq")
    bench["worker"] = worker

    claim = _claim(bench, profile="fake-agent")
    claim["unit"]["brief_path"] = brief_path
    result = worker.execute(claim)

    assert result.outcome == "instrument-error"
    assert result.retryable == 0
    assert "brief_path escapes worktree" in result.remedy
    assert _stored(bench)["terminal_reason"] == "terminal-instrument"


def test_an_in_tree_brief_path_still_works(bench: dict[str, Any]) -> None:
    config = dict(CONFIG)
    config["profiles"] = dict(CONFIG["profiles"])
    # The agent asserts it can actually READ the brief it was handed.
    config["profiles"]["fake-agent"] = {
        "lineage": "test",
        "cmd": [
            "python3",
            "-c",
            "import sys, pathlib; print(pathlib.Path(sys.argv[1]).name)",
            "{brief}",
        ],
    }
    worker = Worker(bench["store"], "test-machine", config=config, home=bench["tmp"] / "wq")
    bench["worker"] = worker

    claim = _claim(bench, profile="fake-agent")
    claim["unit"]["brief_path"] = "demo/NOTES.md"
    result = worker.execute(claim)
    assert result.outcome == "pass", result.remedy


def test_the_child_environment_carries_no_pool_credentials(
    bench: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """acceptance_cmd is submitter-controlled text executed on someone else's box.

    With WQ_TOKEN in its environment, "run my tests" is also "print the pool's
    bearer token", which is claim/cancel/enqueue on every machine in the pool.
    WQ_HOME stays (it names a directory the child legitimately writes into);
    everything else non-WQ is untouched, or the command would not run at all.
    """
    monkeypatch.setenv("WQ_TOKEN", "s3cret-pool-token")
    monkeypatch.setenv("WQ_SERVER", "http://primary:8487")
    monkeypatch.setenv("WQ_DB", "/Users/youruser/var/workqueue.sqlite3")
    monkeypatch.delenv("WQ_HOME", raising=False)
    monkeypatch.setenv("SOME_UNRELATED_VAR", "kept")

    dump = tmp_path / "child-env.json"
    result = bench["worker"].execute(
        _claim(
            bench,
            acceptance_cmd=(
                'python3 -c "import json,os,pathlib; '
                f'pathlib.Path({str(dump)!r}).write_text(json.dumps(dict(os.environ)))"'
            ),
        )
    )
    assert result.outcome == "pass", result.remedy

    child_env = json.loads(dump.read_text())
    assert "WQ_TOKEN" not in child_env, "the pool bearer token must never reach a unit's process"
    assert "WQ_SERVER" not in child_env and "WQ_DB" not in child_env
    assert child_env["WQ_HOME"] == str(bench["worker"].home)
    assert child_env["SOME_UNRELATED_VAR"] == "kept"
    assert child_env.get("PATH"), "PATH must survive, or nothing is runnable"
