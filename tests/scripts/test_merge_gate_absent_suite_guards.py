"""A step that never ran must never be indistinguishable from a step that passed.

``scripts/merge-gate.sh`` guarded five of its suites with bare existence tests
(``[ -d "$SCRATCH/tests/doctrine" ] && run_suite ...``) and its counterfeit
corpus with an ``if``/``then`` carrying no ``else``. All six no-opped with ZERO
output, so a candidate that DELETES the tests turned the steps off and the gate
called it safe — proven end to end by
``test_deleting_the_suites_no_longer_buys_a_pass`` below, which passes the gate
a candidate whose only change is ``git rm -r tests/{contracts,scripts,objective,
doctrine,memlife}``.

The absence was unfalsifiable in all three carriers at once:

  * the EXIT CODE — ``FAILURES`` is never appended to, so the verdict is PASS;
  * the RECEIPT — the step is simply missing from ``steps[]``, and there is no
    expected-step manifest to compare against, so nothing can notice;
  * the HUMAN OUTPUT — neither ``pass`` nor ``fail`` is called, so not one line
    is printed.

This module pins the whole family, including the three siblings that a fix
aimed only at the five ``[ -d ... ] &&`` lines would miss:

  * ``counterfeit-gate`` — a different shape (a variable consumed at two later
    sites), so it needs a skip record at BOTH of them;
  * ``openapi-drift`` — worse than absent: it recorded ``ok`` with an empty
    detail when it did not apply, so "schema never examined" read exactly like
    "schema examined and clean";
  * ``on_exit`` — an abnormally terminated run minted ``exit_code: 0``, because
    ``$?`` is 0 when bash takes SIGTERM between commands.

And the identity of the judge itself: ``$0`` is chosen by the CALLER while
``$GATE_WS`` is pinned and verified, so nothing stopped a caller with a
hardcoded script path from grading a correctly-pinned modern workspace with a
stale gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import signal
import subprocess
import time
from pathlib import Path

import pytest

from omniagentos.scheduler.gate_evidence import GateEvidenceStore
from tests.scripts.test_merge_gate_m8_refusals import (
    MERGE_GATE,
    REPO_ROOT,
    FixtureBranch,
    M8Repo,
    _git,
    _install_fake_python,
    _output,
    _receipt,
    fake_python_for,
    run_contained,
)

# The suites guarded by a bare `[ -d ... ] &&` today, in the order the gate runs
# them. Since 2026-08-12 each directory is its OWN step: tests/scheduler was
# lifted out of the xdist ladder into a SERIAL "scheduler" step, and the old
# "contracts-scripts" step was split into a PARALLEL "contracts" leg and a
# SERIAL "scripts" step — both because tests/scheduler and tests/scripts drive
# the real gate's os.killpg reap, which false-refuses trains under xdist. So
# there is no longer a two-directory step; deleting ANY one directory is caught
# as that step's own skipped-required refusal.
GUARDED_SUITES: dict[str, tuple[str, ...]] = {
    "scheduler": ("tests/scheduler",),
    "contracts": ("tests/contracts",),
    "scripts": ("tests/scripts",),
    "pipeline-tests": ("pipeline/tests",),
    "dominance-corpus": ("tests/objective",),
    "doctrine": ("tests/doctrine",),
    "memlife": ("tests/memlife",),
}
DELETED_SUITE_DIRS = tuple(d for dirs in GUARDED_SUITES.values() for d in dirs)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_FAST_PYTEST = """if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  printf '1 passed in 0.01s\\n'
"""
_SLOW_PYTEST = """if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  sleep "${MERGE_GATE_TEST_SUITE_SLEEP:-0}"
  printf '1 passed in 0.01s\\n'
"""


def _make_pytest_slow(repo: Path) -> None:
    """Give the fixture's fake interpreter a suite long enough to interrupt."""
    python = repo / ".venv" / "bin" / "python"
    source = python.read_text(encoding="utf-8")
    assert source.count(_FAST_PYTEST) == 1, "the M8 fake python's pytest branch moved"
    python.write_text(source.replace(_FAST_PYTEST, _SLOW_PYTEST), encoding="utf-8")
    python.chmod(0o755)


def _branch_deleting(repo: Path, branch: str, *paths: str) -> str:
    """A candidate whose ONLY change is removing directories from the tree."""
    _git(repo, "checkout", "-b", branch, "main")
    _git(repo, "rm", "-r", "-q", *paths)
    _git(repo, "commit", "-m", f"fixture: {branch}")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    return sha


def _branch_adding(repo: Path, branch: str, relative: str, content: str) -> str:
    _git(repo, "checkout", "-b", branch, "main")
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", relative)
    _git(repo, "commit", "-m", f"fixture: {branch}")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    return sha


_UNSET = object()


def _build_repo(tmp_path: Path, *, gate_script=_UNSET, suites: bool = True) -> M8Repo:
    """A minimal repo that DOES carry every guarded suite on main.

    That is the whole difference from the existing merge-gate fixtures, and it
    is what makes the deletion measurable: "main never had this suite" and "the
    candidate removed the suite main has" are different claims, and only the
    second one is a defect.

    ``gate_script`` defaults to THIS script's own bytes because a real gate
    workspace is a checkout of this repository and therefore always carries
    scripts/merge-gate.sh; ``None`` models the workspace that does not, which
    the gate now refuses.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Absent Suite Gate Test")
    _git(repo, "config", "user.email", "absent-suite-gate@example.com")
    (repo / ".gitignore").write_text(".venv\nnode_modules\nvar\n", encoding="utf-8")
    (repo / "ARCHI.md").write_text("main architecture\n", encoding="utf-8")
    (repo / "WORKBOOK.md").write_text("shared workbook\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    reachability = repo / "scripts" / "reachability-gate.py"
    reachability.parent.mkdir(parents=True, exist_ok=True)
    reachability.write_bytes((REPO_ROOT / "scripts" / "reachability-gate.py").read_bytes())
    reachability.chmod(0o755)
    if gate_script is _UNSET:
        gate_script = MERGE_GATE.read_bytes()
    if gate_script is not None:
        (repo / "scripts" / "merge-gate.sh").write_bytes(gate_script)
        (repo / "scripts" / "merge-gate.sh").chmod(0o755)
    # `suites=False` models a workspace whose main NEVER CARRIED these suites —
    # the minimal repos the other merge-gate fixture modules build. That is the
    # other half of "pinned AND present": absence there is not a deletion.
    if suites:
        corpus = repo / "tests" / "counterfeits" / "harness.py"
        corpus.parent.mkdir(parents=True, exist_ok=True)
        corpus.write_text("# fixture stand-in; the fake python answers -m\n", encoding="utf-8")
        for directory in DELETED_SUITE_DIRS:
            marker = repo / directory / "test_fixture.py"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("def test_fixture():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    specs = {
        "control": (
            "fixture/clean-control",
            _branch_adding(repo, "fixture/clean-control", "candidate.txt", "clean\n"),
        ),
    }
    if suites:
        specs["deletes_suites"] = (
            "fixture/deletes-suites",
            _branch_deleting(repo, "fixture/deletes-suites", *DELETED_SUITE_DIRS),
        )
        specs["deletes_counterfeits"] = (
            "fixture/deletes-counterfeits",
            _branch_deleting(repo, "fixture/deletes-counterfeits", "tests/counterfeits"),
        )
    _install_fake_python(repo)

    branches = {
        key: FixtureBranch(
            name=name,
            candidate_sha=sha,
            merge_base_sha=base_sha,
            refusal=None,
            reason=None,
        )
        for key, (name, sha) in specs.items()
    }

    evidence_root = tmp_path / "gate-evidence"
    store = GateEvidenceStore(evidence_root)
    for case in branches.values():
        signed = store.sign(_receipt(case, repo))
        receipt_path = evidence_root / "records" / "merge-gate" / f"{case.candidate_sha}.json"
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(
            json.dumps(signed.to_payload(), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return M8Repo(path=repo, evidence_root=evidence_root, branches=branches)


@pytest.fixture
def suites_repo(tmp_path: Path) -> M8Repo:
    return _build_repo(tmp_path)


def _gate_env(fixture: M8Repo, *, pinned: bool, extra: dict[str, str]) -> dict[str, str]:
    env = {
        **os.environ,
        "REPO": str(fixture.path),
        # STATE THE INTERPRETER, never inherit it — see fake_python_for().
        "MERGE_GATE_PY": fake_python_for(fixture.path),
        "MERGE_GATE_EVIDENCE_ROOT": str(fixture.evidence_root),
        # STATE THE MODE, never inherit it — same reasoning as the M8 harness.
        "MERGE_GATE_PINNED": "1" if pinned else "0",
        # STATE THE DEPTH, never inherit it (2026-08-08) — the third sibling of
        # the two rules above, and it lands on the width assertions in this very
        # file. merge-gate.sh's suite worker exports
        # MERGE_GATE_DEPTH="$GATE_CHILD_DEPTH" (=1) into the pytest that runs
        # the "scripts" step (these merge-gate fixtures live in tests/scripts,
        # split off contracts-scripts on 2026-08-12), and at depth >= 1 forces
        # GATE_CONCURRENCY_CEILING=1 (merge-gate.sh:409). So every fixture gate
        # started from inside the merge gate silently ran at depth 1 with a
        # ceiling of 1, and the two width nodes below — which assert
        # gate_depth == 0 and that a fitting width passes through unclamped —
        # went red INSIDE the gate while passing everywhere else. Exactly the
        # shape MERGE_GATE_PINNED had in 2026-08-05, reproduced on clean main
        # with `MERGE_GATE_DEPTH=1 pytest <this file>`: 2 failed, 28 passed.
        #
        # These fixtures exercise a TOP-LEVEL gate, so depth is an input they
        # must state. The one node that wants the nested shape
        # (test_a_nested_gate_gets_no_parallelism_however_it_is_asked) passes
        # MERGE_GATE_DEPTH="1" through `extra`, and `env.update(extra)` below
        # still wins — so stating a default here cannot mask it. Containment is
        # unaffected: a fixture gate's own children still go to depth 1.
        "MERGE_GATE_DEPTH": "0",
        "MERGE_GATE_STEP_RECEIPTS": "0",
        # STATE THE DEPTH AND THE WIDTHS, never inherit them — same class as the
        # interpreter and the mode above. A real gate exports MERGE_GATE_DEPTH and
        # its width knobs into every child, so when this suite runs INSIDE a gate
        # (the "scripts" step does), a fixture gate inherits the parent's depth=1
        # and every "an OUTER gate ..." assertion reads the wrong subject:
        # test_an_outer_gate_keeps_its_width_when_it_fits_the_host got
        # `assert 1 == 0` on gate_depth. Scrubbing DEPTH gate-side is NOT the fix
        # — it is the nesting backstop and must propagate to real children.
        # The key itself is stated ONCE, above: two commits landed the same
        # `"MERGE_GATE_DEPTH": "0"` entry with its own rationale, and the rebase
        # kept both, so this dict repeated a literal key (ruff F601). Both values
        # were "0", so nothing behaved differently — but a repeated key silently
        # discards one binding, and next time the two need not agree.
    }
    # These are knobs a case sets deliberately via `extra`; an inherited value is
    # never the subject under test.
    for _knob in ("MERGE_GATE_LADDER_WORKERS", "MERGE_GATE_CF_POOL_WORKERS",
                  "MERGE_GATE_SUITE_WORKERS",
                  "MERGE_GATE_FD_FLOOR", "MERGE_GATE_MAX_WORKERS"):
        env.pop(_knob, None)
    if pinned:
        env["OMNIAGENTOS_GATE_WORKSPACE"] = str(fixture.path)
    else:
        env.pop("OMNIAGENTOS_GATE_WORKSPACE", None)
    env.update(extra)
    return env


def _fd_limiter(soft: int, hard: int | None = None):
    """A preexec_fn that hands the gate a specific RLIMIT_NOFILE.

    Lowering the HARD limit too is what makes the gate's own raise fail, which
    is the only way to exercise the floor without depending on the host's
    configuration.
    """

    def _apply() -> None:  # pragma: no cover - runs in the forked child
        import resource

        # hard=None means "leave the ceiling alone", which is the real-world
        # ssh shape: a low SOFT limit the gate is allowed to raise back up.
        current_hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
        resource.setrlimit(
            resource.RLIMIT_NOFILE, (soft, current_hard if hard is None else hard)
        )

    return _apply



def _run_or_contain(command, *, cwd, env, preexec_fn=None):
    """``run_contained``, except when a test needs its own ``preexec_fn``.

    ``run_contained`` already puts the child in its own session; the fd-limit
    tests need a preexec hook instead, and stacking both is not worth the
    complexity for a run that cannot recurse (its interpreter is the stub).
    """
    if preexec_fn is None:
        return run_contained(command, cwd=cwd, env=env)
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        preexec_fn=preexec_fn,
    )


def _run_gate(
    fixture: M8Repo,
    case: FixtureBranch,
    *,
    pinned: bool = True,
    gate_script: Path = MERGE_GATE,
    env_extra: dict[str, str] | None = None,
    preexec_fn=None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    """Run the gate and return (process, run receipt). The receipt is the point.

    Every assertion here is about what the RECEIPT can be made to say, because
    that is the carrier every downstream consumer keys on.
    """
    emit = fixture.path.parent / f"run-receipt-{case.candidate_sha[:12]}-{os.getpid()}.json"
    result = _run_or_contain(
        ["bash", str(gate_script), "--emit-receipt", str(emit), case.name],
        cwd=fixture.path,
        env=_gate_env(fixture, pinned=pinned, extra=env_extra or {}),
        preexec_fn=preexec_fn,
    )
    receipt = json.loads(emit.read_text(encoding="utf-8")) if emit.exists() else {}
    return result, receipt


def _step(receipt: dict, name: str) -> dict | None:
    for entry in receipt.get("steps", []):
        if entry.get("name") == name:
            return entry
    return None


# ---------------------------------------------------------------------------
# A. a deleted suite is a REFUSAL, and it is on the record either way
# ---------------------------------------------------------------------------


def test_deleting_the_suites_no_longer_buys_a_pass(suites_repo: M8Repo) -> None:
    """THE DEFECT, executed end to end (SCENARIO C).

    The candidate's only change is ``git rm -r`` over five suite directories.
    Before the fix this printed ``MERGE GATE: PASS`` with process exit 0 and a
    receipt whose ``exit_code`` was 0 and whose ``steps[]`` simply did not
    mention any of them.
    """
    case = suites_repo.branches["deletes_suites"]
    result, receipt = _run_gate(suites_repo, case)
    output = _output(result)
    print(f"DELETES-SUITES rc={result.returncode}\n{output}")

    assert result.returncode != 0, (
        "a candidate that DELETES tests/contracts, tests/scripts, "
        f"tests/objective, tests/doctrine and tests/memlife was scored safe "
        f"to merge:\n{output}"
    )
    assert "MERGE GATE: PASS" not in output, output
    assert receipt.get("exit_code") not in (0, None), receipt.get("exit_code")

    for step_name in GUARDED_SUITES:
        entry = _step(receipt, step_name)
        assert entry is not None, (
            f"{step_name} is ABSENT from the receipt's steps[] — a step that "
            f"never ran left no trace at all:\n{json.dumps(receipt, indent=2)}"
        )
        assert entry["status"] == "skipped-required", entry
    for step_name, dirs in GUARDED_SUITES.items():
        assert any(d in _step(receipt, step_name)["detail"] for d in dirs), _step(
            receipt, step_name
        )


def test_each_absent_suite_is_named_in_the_human_output(suites_repo: M8Repo) -> None:
    """The third carrier. Exit code and receipt are for machines; an operator
    reads the printed report, and it printed NOTHING for a skipped suite."""
    case = suites_repo.branches["deletes_suites"]
    result, _ = _run_gate(suites_repo, case)
    output = _output(result)
    for step_name in GUARDED_SUITES:
        assert step_name in output, f"{step_name} never appears in the report:\n{output}"


@pytest.mark.parametrize(
    ("deleted_dir", "step"),
    [
        ("tests/scheduler", "scheduler"),
        ("tests/contracts", "contracts"),
        ("tests/scripts", "scripts"),
    ],
)
def test_deleting_a_single_reap_split_suite_dir_refuses(
    tmp_path: Path, deleted_dir: str, step: str
) -> None:
    """The 2026-08-12 reap-race split must not open a favourable-absence hole.

    Until 2026-08-12 tests/contracts + tests/scripts were ONE ``contracts-scripts``
    step chained with ``&&`` (deleting either turned it off), and tests/scheduler
    rode inside the xdist ladder. They were pulled apart so the real gate's
    ``os.killpg`` process-group reap stops false-refusing trains under xdist —
    each is now its OWN step. The split moved a suite from one carrier to
    another, and a suite that quietly stops being gated is exactly this file's
    subject, so deleting any ONE of the three has to refuse as THAT step's
    skipped-required, naming the directory it dropped.
    """
    fixture = _build_repo(tmp_path)
    branch = "fixture/deletes-" + deleted_dir.replace("/", "-") + "-only"
    sha = _branch_deleting(fixture.path, branch, deleted_dir)
    case = FixtureBranch(
        name=branch,
        candidate_sha=sha,
        merge_base_sha=fixture.branches["control"].merge_base_sha,
        refusal=None,
        reason=None,
    )
    store = GateEvidenceStore(fixture.evidence_root)
    signed = store.sign(_receipt(case, fixture.path))
    receipt_path = fixture.evidence_root / "records" / "merge-gate" / f"{sha}.json"
    receipt_path.write_text(
        json.dumps(signed.to_payload(), sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    result, receipt = _run_gate(fixture, case)
    output = _output(result)
    print(f"DELETES-{deleted_dir}-ONLY rc={result.returncode}\n{output}")
    assert result.returncode != 0, output
    assert "MERGE GATE: PASS" not in output, output
    entry = _step(receipt, step)
    assert entry is not None and entry["status"] == "skipped-required", entry
    assert deleted_dir in entry["detail"], entry


def test_unpinned_records_the_skip_instead_of_staying_silent(suites_repo: M8Repo) -> None:
    """Un-armed, the gate still must not be SILENT about a suite it did not run.

    The un-pinned path deliberately keeps its verdict (these fixtures, the
    counterfeit corpus and ~10 sibling test modules all drive it), but "did not
    run" has to reach both the printed report and ``steps[]`` or the absence
    stays unfalsifiable exactly where it started.
    """
    case = suites_repo.branches["deletes_suites"]
    result, receipt = _run_gate(suites_repo, case, pinned=False)
    output = _output(result)
    print(f"UNPINNED-DELETES rc={result.returncode}\n{output}")
    for step_name in GUARDED_SUITES:
        entry = _step(receipt, step_name)
        assert entry is not None, (
            f"{step_name} left no receipt entry on the un-pinned path:\n"
            f"{json.dumps(receipt, indent=2)}"
        )
        assert entry["status"] == "skipped", entry
        assert step_name in output, output


def test_a_suite_main_never_had_is_skipped_but_not_required(tmp_path: Path) -> None:
    """THE *PRESENT* HALF of "pinned AND present", pinned on its own.

    Required-ness is not "pinned mode"; it is "the candidate REMOVED a suite the
    pinned workspace has". A workspace whose main never carried these
    directories — which is every other merge-gate fixture module, all of which
    drive real pinned runs — must still PASS. Dropping this half turns ~10 test
    files red for a property they do not model, and it would also make the guard
    measure mere absence rather than deletion.
    """
    fixture = _build_repo(tmp_path, suites=False)
    case = fixture.branches["control"]
    result, receipt = _run_gate(fixture, case)
    output = _output(result)
    print(f"NO-SUITES-ON-MAIN rc={result.returncode}\n{output}")
    assert result.returncode == 0, output
    assert "MERGE GATE: PASS" in output, output
    for step_name in GUARDED_SUITES:
        entry = _step(receipt, step_name)
        assert entry is not None, json.dumps(receipt, indent=2)
        assert entry["status"] == "skipped", (
            "a suite main never carried was marked REQUIRED; the guard is "
            f"measuring absence instead of deletion: {entry}"
        )
    counterfeit = _step(receipt, "counterfeit-gate")
    assert counterfeit is not None and counterfeit["status"] == "skipped", counterfeit


def test_a_present_suite_still_runs_and_is_not_recorded_as_skipped(
    suites_repo: M8Repo,
) -> None:
    """The guard must not fire on a healthy candidate — a check that refuses
    everything proves nothing."""
    case = suites_repo.branches["control"]
    result, receipt = _run_gate(suites_repo, case)
    output = _output(result)
    print(f"CONTROL rc={result.returncode}\n{output}")
    assert result.returncode == 0, output
    assert "MERGE GATE: PASS" in output, output
    for step_name in GUARDED_SUITES:
        entry = _step(receipt, step_name)
        assert entry is not None, json.dumps(receipt, indent=2)
        assert entry["status"] == "ok", entry


# ---------------------------------------------------------------------------
# B. the siblings a fix aimed at the five `[ -d ... ] &&` lines would miss
# ---------------------------------------------------------------------------


def test_deleting_the_counterfeit_corpus_no_longer_buys_a_pass(
    suites_repo: M8Repo,
) -> None:
    """THE TRAP. ``counterfeit-gate`` is a DIFFERENT SHAPE — an ``if``/``then``
    with no ``else`` setting ``CF_PRESENT``, consumed at two later sites — so a
    fix that edits only the ``[ -d ... ] &&`` lines leaves the entire
    counterfeit corpus silently skippable."""
    case = suites_repo.branches["deletes_counterfeits"]
    result, receipt = _run_gate(suites_repo, case)
    output = _output(result)
    print(f"DELETES-COUNTERFEITS rc={result.returncode}\n{output}")
    assert result.returncode != 0, (
        f"a candidate that DELETES tests/counterfeits was scored safe:\n{output}"
    )
    assert "MERGE GATE: PASS" not in output, output
    entry = _step(receipt, "counterfeit-gate")
    assert entry is not None, (
        "counterfeit-gate is ABSENT from steps[] — the corpus was never run and "
        f"the receipt cannot say so:\n{json.dumps(receipt, indent=2)}"
    )
    assert entry["status"] == "skipped-required", entry
    assert "counterfeit-gate" in output, output


def test_openapi_drift_does_not_report_ok_when_it_did_not_apply(
    suites_repo: M8Repo,
) -> None:
    """``openapi-drift`` recorded ``step_end "ok"`` with an EMPTY detail when
    ``API_TOUCHED`` was empty, so a candidate whose schema was never examined
    was byte-identical in the receipt to one examined and found clean."""
    case = suites_repo.branches["control"]
    _, receipt = _run_gate(suites_repo, case)
    entry = _step(receipt, "openapi-drift")
    assert entry is not None, json.dumps(receipt, indent=2)
    assert entry["status"] == "n/a", (
        "the candidate touches no omniagentos/api/*.py, so the schema was never "
        f"examined — recording that as 'ok' is a favourable absence: {entry}"
    )
    assert entry["detail"], f"an n/a with no reason is still unfalsifiable: {entry}"


# ---------------------------------------------------------------------------
# C. abnormal termination must never mint a passing receipt
# ---------------------------------------------------------------------------


def test_a_killed_run_never_mints_exit_code_zero(suites_repo: M8Repo) -> None:
    """``on_exit`` minted ``mint_run_receipt "$rc"`` with ``$rc=$?``.

    Measured (``bash 3.2``/``5.x``, this box): a script that takes SIGTERM
    between commands runs its EXIT trap with ``$?`` == 0. So a gate killed
    mid-suite recorded ``exit_code: 0`` with every completed step ``ok``, and
    any consumer keying on ``exit_code`` read PASS. Six such receipts are in the
    corpus and two LANDED candidates have no other complete clean run.
    """
    case = suites_repo.branches["control"]
    emit = suites_repo.path.parent / "killed-run-receipt.json"
    _make_pytest_slow(suites_repo.path)
    env = _gate_env(
        suites_repo,
        pinned=True,
        # Make the ladder long enough to be interrupted inside it.
        extra={"MERGE_GATE_TEST_SUITE_SLEEP": "12"},
    )
    proc = subprocess.Popen(
        ["bash", str(MERGE_GATE), "--emit-receipt", str(emit), case.name],
        cwd=suites_repo.path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Wait until the run is unambiguously inside a suite, then terminate it.
    deadline = time.time() + 60
    scratch = suites_repo.path / "var" / "swarm" / f"gate-{proc.pid}"
    while time.time() < deadline and not (scratch / "var" / "gate-steps").exists():
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    try:
        output = proc.communicate(timeout=90)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        output = proc.communicate()[0]
    print(f"KILLED rc={proc.returncode}\n{output}")

    assert emit.exists(), (
        "the killed run left no receipt at all — every early exit is supposed "
        f"to leave evidence:\n{output}"
    )
    receipt = json.loads(emit.read_text(encoding="utf-8"))
    assert receipt["exit_code"] != 0, (
        "a gate killed mid-run minted a receipt whose exit_code is 0; every "
        f"consumer keying on exit_code reads that as PASS:\n{receipt}"
    )
    assert "MERGE GATE: PASS" not in output, output
    assert receipt["refusal_reason"], receipt


# ---------------------------------------------------------------------------
# D. the gate has to say WHICH gate it is
# ---------------------------------------------------------------------------


def test_a_stale_gate_script_refuses_and_names_its_remedy(tmp_path: Path) -> None:
    """``$0`` is the CALLER's choice; ``$GATE_WS`` is pinned and verified.

    Nothing tied them together, so a caller with a hardcoded script path graded
    a correctly-pinned modern workspace with a 19-commit-stale gate. There are
    70 copies of this script on the authoring machine and 65 of them lack the
    ``contracts-scripts`` step entirely.
    """
    fixture = _build_repo(tmp_path, gate_script=b"#!/usr/bin/env bash\n# a different gate\n")
    case = fixture.branches["control"]
    result, receipt = _run_gate(fixture, case)
    output = _output(result)
    print(f"STALE-JUDGE rc={result.returncode}\n{output}")
    assert result.returncode != 0, output
    assert "stale-gate-script" in output, output
    assert "MERGE GATE: PASS" not in output, output
    # A refusal must name its own remedy or it just costs another cycle.
    assert "scripts/merge-gate.sh" in output, output
    assert receipt.get("gate_script_pin_match") is False, receipt
    assert receipt.get("gate_script_sha256") == _sha256_bytes(MERGE_GATE.read_bytes()), receipt
    assert receipt.get("gate_script_path", "").endswith("merge-gate.sh"), receipt


def test_a_matching_gate_script_records_the_match_and_passes(suites_repo: M8Repo) -> None:
    """The positive control: the pinned workspace carries THIS script's bytes."""
    case = suites_repo.branches["control"]
    result, receipt = _run_gate(suites_repo, case)
    output = _output(result)
    print(f"MATCHING-JUDGE rc={result.returncode}\n{output}")
    assert result.returncode == 0, output
    assert "MERGE GATE: PASS" in output, output
    assert receipt.get("gate_script_pin_match") is True, receipt


def test_an_unverifiable_identity_refuses_and_is_null_never_true(tmp_path: Path) -> None:
    """FAIL CLOSED (review 2026-08-07, overturning the original fail-open).

    When the pinned workspace has no scripts/merge-gate.sh the comparison cannot
    be made. Recording ``null`` makes that VISIBLE; it does not make it SAFE,
    and a run that continues is still a PASS earned by an unmeasured identity —
    which is the exact defect class this whole change exists to close.

    Both halves are asserted: the run refuses, AND the receipt still records
    ``null`` rather than a favourable ``true``.
    """
    fixture = _build_repo(tmp_path, gate_script=None)
    case = fixture.branches["control"]
    result, receipt = _run_gate(fixture, case)
    output = _output(result)
    print(f"UNVERIFIABLE-JUDGE rc={result.returncode}\n{output}")
    assert result.returncode != 0, output
    assert "unverifiable-gate-script" in output, output
    assert "MERGE GATE: PASS" not in output, output
    assert receipt.get("gate_script_pin_match") is None, (
        "an unmeasurable gate identity was recorded as a favourable one: "
        f"{receipt}"
    )
    assert receipt.get("gate_script_sha256"), receipt
    assert receipt.get("instrument_error") is True, receipt
    # A refusal must name its own remedy or it just costs another cycle.
    assert "gate-workspace.sh" in output, output


def test_a_zero_byte_gate_script_in_the_workspace_refuses(tmp_path: Path) -> None:
    """EMPTY-BLOB EDGE CASE. Piping an absent or empty blob into a hasher yields
    the digest of the empty string — a perfectly confident-looking 64 hex chars
    for a file with no content. The size probe exists to stop that from being
    compared as if it were a real identity."""
    fixture = _build_repo(tmp_path, gate_script=b"")
    case = fixture.branches["control"]
    result, receipt = _run_gate(fixture, case)
    output = _output(result)
    print(f"ZERO-BYTE-JUDGE rc={result.returncode}\n{output}")
    assert result.returncode != 0, output
    assert "unverifiable-gate-script" in output, output
    assert receipt.get("gate_script_pin_match") is None, receipt


def test_a_directory_at_the_gate_script_path_refuses(tmp_path: Path) -> None:
    """CORRUPT-WORKSPACE EDGE CASE. ``rev-parse PIN_SHA:scripts/merge-gate.sh``
    happily resolves a TREE object, whose ``cat-file -s`` size is non-zero, so
    the size probe alone would let a directory through to the hasher. The digest
    then comes back empty (``cat-file blob`` refuses a tree) and the run must
    refuse rather than compare nothing."""
    fixture = _build_repo(tmp_path, gate_script=None)
    directory = fixture.path / "scripts" / "merge-gate.sh"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "placeholder.txt").write_text("not a script\n", encoding="utf-8")
    _git(fixture.path, "add", "scripts/merge-gate.sh")
    _git(fixture.path, "commit", "-m", "fixture: a directory where the judge should be")
    result, receipt = _run_gate(fixture, fixture.branches["control"])
    output = _output(result)
    print(f"TREE-AT-JUDGE-PATH rc={result.returncode}\n{output}")
    assert result.returncode != 0, output
    assert "unverifiable-gate-script" in output, output
    assert receipt.get("gate_script_pin_match") is not True, receipt


# ---------------------------------------------------------------------------
# E. the instrument's own capacity, and instrument-vs-candidate labelling
# ---------------------------------------------------------------------------


def test_the_gate_raises_its_own_fd_limit_and_records_both_values(
    suites_repo: M8Repo,
) -> None:
    """`launchctl limit maxfiles` is 256 soft on this estate, and a
    non-interactive ``ssh host cmd`` shell inherits exactly that because the
    login profile that raises it never runs. A gate started that way ran the
    whole ladder on 256 descriptors and died with ``OSError: [Errno 24]`` inside
    two suites the candidate cannot reach.

    The raise has to live in the SCRIPT, not in one machine's ~/.zshenv, or it
    does not travel to a new box, a launchd job or CI. And both numbers have to
    reach the receipt, or the next run that dies this way is diagnosable only
    from a traceback that the EXIT trap has already deleted.
    """
    case = suites_repo.branches["control"]
    result, receipt = _run_gate(suites_repo, case, preexec_fn=_fd_limiter(256))
    output = _output(result)
    print(f"FD-RAISE rc={result.returncode}\n{output}")
    assert result.returncode == 0, output
    assert receipt.get("fd_limit_soft_initial") == "256", receipt
    raised = receipt.get("fd_limit_soft")
    assert raised not in (None, "", "256"), (
        f"the gate did not raise its own descriptor limit: {receipt}"
    )
    assert raised == "unlimited" or int(raised) >= 1024, receipt


def test_an_already_generous_fd_limit_is_never_lowered(suites_repo: M8Repo) -> None:
    """The interactive shell on this box hands the gate 1048576 — ABOVE the
    65536 target. A bare ``ulimit -n $TARGET`` would silently SHRINK a healthy
    limit, which is the same class of harm pointed the other way."""
    inherited_hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    generous = (
        200_000
        if inherited_hard == resource.RLIM_INFINITY
        else min(200_000, inherited_hard)
    )
    if generous <= 65_536:
        pytest.skip("host hard limit cannot represent the already-generous precondition")
    case = suites_repo.branches["control"]
    _, receipt = _run_gate(suites_repo, case, preexec_fn=_fd_limiter(generous))
    assert receipt.get("fd_limit_soft_initial") == str(generous), receipt
    assert receipt.get("fd_limit_soft") == str(generous), (
        f"the gate lowered a limit that was already above its target: {receipt}"
    )


def test_a_capacity_the_gate_cannot_reach_refuses_as_an_instrument_error(
    suites_repo: M8Repo,
) -> None:
    """Hard-capped below the floor: the raise cannot succeed, so the gate is
    about to spend twelve minutes on a ladder verdict it cannot trust.

    Refusing in under a second is better — and it must be labelled INSTRUMENT,
    because a refusal that blames the code for the gate's own broken environment
    sends the next agent to debug the wrong thing.
    """
    case = suites_repo.branches["control"]
    result, receipt = _run_gate(suites_repo, case, preexec_fn=_fd_limiter(512, 512))
    output = _output(result)
    print(f"FD-FLOOR rc={result.returncode}\n{output}")
    assert result.returncode != 0, output
    assert "fd-limit-too-low" in output, output
    assert "MERGE GATE: PASS" not in output, output
    assert receipt.get("instrument_error") is True, receipt
    assert receipt.get("fd_limit_soft") == "512", receipt
    # A refusal must name its own remedy or it just costs another cycle.
    assert "MERGE_GATE_FD_FLOOR" in output, output


def test_a_candidate_defect_is_never_labelled_an_instrument_error(
    suites_repo: M8Repo,
) -> None:
    """THE ANTI-GUESS CONTROL. A wrong instrument label EXCUSES a real defect,
    which is worse than no label at all, so the field is asserted only where the
    gate measured its own environment — never inferred from suite output.

    A candidate that deletes its test suites is a defect; the run that catches it
    must stay unclassified.
    """
    case = suites_repo.branches["deletes_suites"]
    result, receipt = _run_gate(suites_repo, case)
    assert result.returncode != 0, _output(result)
    assert receipt.get("instrument_error") is None, (
        "a candidate defect was labelled an instrument error, which excuses it: "
        f"{receipt}"
    )


def test_an_environmental_refusal_is_labelled_an_instrument_error(
    suites_repo: M8Repo,
) -> None:
    """The mirror control, on a refusal whose text ALREADY calls itself an
    instrument condition: 64 of this gate's 90 recorded refusals were mechanics
    (dirty or unpinned workspace, stale judge), and none of them said so in the
    only carrier a machine reads."""
    case = suites_repo.branches["control"]
    (suites_repo.path / "operator_left_this_here.txt").write_text("dirt\n", encoding="utf-8")
    try:
        result, receipt = _run_gate(suites_repo, case)
    finally:
        (suites_repo.path / "operator_left_this_here.txt").unlink()
    output = _output(result)
    assert "dirty-workspace" in output, output
    assert receipt.get("instrument_error") is True, receipt


def test_a_killed_run_is_labelled_an_instrument_error(suites_repo: M8Repo) -> None:
    """A run that produced no verdict is, by construction, saying nothing about
    the candidate — the one instrument classification that needs no list."""
    case = suites_repo.branches["control"]
    emit = suites_repo.path.parent / "killed-instrument-receipt.json"
    _make_pytest_slow(suites_repo.path)
    proc = subprocess.Popen(
        ["bash", str(MERGE_GATE), "--emit-receipt", str(emit), case.name],
        cwd=suites_repo.path,
        env=_gate_env(
            suites_repo, pinned=True, extra={"MERGE_GATE_TEST_SUITE_SLEEP": "12"}
        ),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 60
    scratch = suites_repo.path / "var" / "swarm" / f"gate-{proc.pid}"
    while time.time() < deadline and not (scratch / "var" / "gate-steps").exists():
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    time.sleep(0.5)
    proc.send_signal(signal.SIGTERM)
    try:
        proc.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
    receipt = json.loads(emit.read_text(encoding="utf-8"))
    assert receipt["instrument_error"] is True, receipt


def test_a_sigkilled_run_leaves_no_receipt_rather_than_a_passing_one(
    suites_repo: M8Repo,
) -> None:
    """SIGKILL runs no trap at all, so the honest outcome is NO receipt.

    That is safe in a way silence elsewhere was not: every consumer looks
    receipts up by exact path, and an absent record cannot be read as a verdict.
    This pins the distinction so a future "always write something on the way
    out" change cannot quietly start emitting a zero-exit receipt for a run that
    was destroyed mid-suite.
    """
    case = suites_repo.branches["control"]
    emit = suites_repo.path.parent / "sigkilled-receipt.json"
    _make_pytest_slow(suites_repo.path)
    proc = subprocess.Popen(
        ["bash", str(MERGE_GATE), "--emit-receipt", str(emit), case.name],
        cwd=suites_repo.path,
        env=_gate_env(suites_repo, pinned=True, extra={"MERGE_GATE_TEST_SUITE_SLEEP": "12"}),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.time() + 60
    scratch = suites_repo.path / "var" / "swarm" / f"gate-{proc.pid}"
    while time.time() < deadline and not (scratch / "var" / "gate-steps").exists():
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    time.sleep(0.5)
    proc.send_signal(signal.SIGKILL)
    try:
        output = proc.communicate(timeout=90)[0]
    except subprocess.TimeoutExpired:
        proc.kill()
        output = proc.communicate()[0]
    print(f"SIGKILLED rc={proc.returncode}\n{output}")
    assert proc.returncode != 0, output
    assert "MERGE GATE: PASS" not in output, output
    if emit.exists():
        receipt = json.loads(emit.read_text(encoding="utf-8"))
        assert receipt["exit_code"] != 0, (
            f"a SIGKILLed run somehow minted a passing receipt: {receipt}"
        )


# ---------------------------------------------------------------------------
# G. nested concurrency: the guard must bound what runs UNDER it
# ---------------------------------------------------------------------------


def test_a_nested_gate_gets_no_parallelism_however_it_is_asked(
    suites_repo: M8Repo,
) -> None:
    """`heavy-run` holds one token on sem id `merge-gate`, so one OUTER gate
    runs at a time — and nothing under it was bounded at all. Two corpus entries
    name tests/scripts/test_merge_gate_*.py in their must_fail sets, those tests
    spawn real gates, and harness.py's _env() never scrubbed the width
    variables. Measured on the twin: load 107 on 24 cores from ONE gate run.

    A gate that knows it is nested must refuse to fan out, no matter how loudly
    the environment asks.
    """
    case = suites_repo.branches["control"]
    result, receipt = _run_gate(
        suites_repo,
        case,
        env_extra={
            "MERGE_GATE_DEPTH": "1",
            "MERGE_GATE_LADDER_WORKERS": "8",
            "MERGE_GATE_CF_POOL_WORKERS": "8",
        },
    )
    output = _output(result)
    print(f"NESTED rc={result.returncode}\n{output}")
    assert result.returncode == 0, output
    assert receipt.get("gate_depth") == 1, receipt
    assert receipt.get("concurrency_ceiling") == 1, receipt
    assert receipt.get("ladder_workers") == 1, (
        f"a nested gate kept the outer ladder width: {receipt}"
    )
    assert receipt.get("counterfeit_pool_workers") == 1, (
        f"a nested gate kept the outer entry-pool width: {receipt}"
    )
    # The request is recorded beside the effective value: a clamp is evidence,
    # not a silent correction.
    assert receipt.get("ladder_workers_requested") == 8, receipt
    assert receipt.get("counterfeit_pool_workers_requested") == 8, receipt


def test_an_outer_gate_keeps_its_width_when_it_fits_the_host(
    suites_repo: M8Repo,
) -> None:
    """THE OVER-FIRE CONTROL. A ceiling that clamps a perfectly reasonable
    request is a throughput bug, and the estate runs the ladder at 8. Depth 0 on
    a multi-core host must pass 2 through untouched."""
    case = suites_repo.branches["control"]
    _, receipt = _run_gate(suites_repo, case, env_extra={"MERGE_GATE_LADDER_WORKERS": "2"})
    assert receipt.get("gate_depth") == 0, receipt
    assert receipt.get("ladder_workers") == 2, (
        f"the ceiling clamped a request that fits the host: {receipt}"
    )
    assert (receipt.get("concurrency_ceiling") or 0) >= 2, receipt


def test_a_width_beyond_the_host_is_clamped_and_the_clamp_is_visible(
    suites_repo: M8Repo,
) -> None:
    """An explicit ceiling makes this deterministic on any host."""
    case = suites_repo.branches["control"]
    result, receipt = _run_gate(
        suites_repo,
        case,
        env_extra={"MERGE_GATE_LADDER_WORKERS": "64", "MERGE_GATE_MAX_WORKERS": "4"},
    )
    output = _output(result)
    assert receipt.get("ladder_workers") == 4, receipt
    assert receipt.get("ladder_workers_requested") == 64, receipt
    assert "clamped 64 -> 4" in output, output


#: Every width knob merge-gate.sh reads. A child gate must DERIVE its width
#: from its depth, so each of these has to be blanked on the way in — the depth
#: marker is the belt, this list is the braces.
WIDTH_KNOBS = (
    "MERGE_GATE_LADDER_WORKERS",
    "MERGE_GATE_CF_POOL_WORKERS",
    "MERGE_GATE_SUITE_WORKERS",
)


def test_the_workers_hand_children_a_derived_budget_not_the_inherited_one() -> None:
    """The scrub is the mechanical half of the fix, and it is asserted on the
    source because the leak is an ABSENCE — there is no output to observe when a
    variable is merely passed along. Same shape as the
    OMNIAGENTOS_GATE_WORKSPACE leak this file already pins (2026-08-04, F5).

    BOUND TO THE PROPERTY, NOT THE SPELLING (2026-08-10). This used to match one
    verbatim string, ``'MERGE_GATE_LADDER_WORKERS= MERGE_GATE_CF_POOL_WORKERS=
    \\\\'``, which meant adding a THIRD width knob and blanking it correctly —
    a change that strengthens exactly this property — took the assertion to
    "found 0 of 2". That is the same anchor-brittleness
    tests/scripts/test_merge_gate_worker_env_isolation.py was rewritten to
    escape on 2026-08-08: a guard that a strengthening refactor can switch off
    was never guarding the thing it named. Per knob, per worker, spacing and
    order free.
    """
    src = MERGE_GATE.read_text(encoding="utf-8")
    # The handoff lines are the ones that BLANK the ladder width; each worker
    # has exactly one, and every other knob must ride on the same line. Comments
    # and ``${MERGE_GATE_LADDER_WORKERS:-...}`` reads are neither.
    handoffs = [
        line
        for line in src.splitlines()
        if re.search(r"(?<![${])\bMERGE_GATE_LADDER_WORKERS=(?:\s|$|\\)", line)
        and not line.lstrip().startswith("#")
    ]
    assert len(handoffs) == 2, (
        "both the suite worker and the counterfeit worker must blank the width "
        f"variables for their children; found {len(handoffs)} such lines:\n"
        + "\n".join(handoffs)
    )
    for line in handoffs:
        for knob in WIDTH_KNOBS:
            assert re.search(rf"\b{re.escape(knob)}=(?:\s|$|\\)", line), (
                f"{knob} is not blanked on this handoff line, so a nested gate "
                f"inherits the parent's width for it: {line.strip()}"
            )
    assert src.count('MERGE_GATE_DEPTH="$GATE_CHILD_DEPTH" \\') == 2, src.count(
        'MERGE_GATE_DEPTH="$GATE_CHILD_DEPTH" \\'
    )


def test_the_final_load_is_recorded_beside_the_starting_one(
    suites_repo: M8Repo,
) -> None:
    """The start-of-run figure cannot show what the run did to the host. Both
    numbers, or a flood stays undiagnosable after the EXIT trap cleans up."""
    _, receipt = _run_gate(suites_repo, suites_repo.branches["control"])
    assert "load_avg_1m_final" in receipt, receipt
    final = receipt["load_avg_1m_final"]
    assert final is None or float(final) >= 0.0, receipt


# ---------------------------------------------------------------------------
# H. the interpreter is a test premise, at every layer that hands one down
# ---------------------------------------------------------------------------


def test_every_child_env_site_scrubs_the_interpreter() -> None:
    """MERGE_GATE_PY is the FIRST candidate in the gate's own resolution.

    The production command exports one (AccurateGate:
    ``MERGE_GATE_PY={workspace}/.venv/bin/python``), and every site that builds a
    child environment previously scrubbed exactly ONE variable and inherited the
    rest. Leaked into pytest it overrode the fixture stub whose whole job is to
    make a nested gate answer instantly: the nested gate resolved the real
    interpreter, ``tests.counterfeits.harness`` resolved the REAL package (the
    fixture's stand-in has no ``__init__.py``, so it is only a PEP-420 namespace
    portion and a regular package wins), and the 96-entry corpus re-entered.
    Each generation spawned more — 13+ generations, 166 orphans at PPID 1,
    load 62 on 24 cores, measured here.

    Asserted on the SOURCE because a leak is an ABSENCE: there is no output to
    observe when a variable is merely passed along. The behavioural half lives
    in test_merge_gate_worker_env_isolation's env markers.
    """
    src = MERGE_GATE.read_text(encoding="utf-8")
    scrubbed = src.count("env -u OMNIAGENTOS_GATE_WORKSPACE -u MERGE_GATE_PY")
    bare = src.count("env -u OMNIAGENTOS_GATE_WORKSPACE \\")
    assert bare == 0, (
        f"{bare} child-env site(s) still scrub only the workspace and inherit "
        "the interpreter — 'scrubbed one variable, inherited the rest' is the "
        "pattern, not the exception"
    )
    assert scrubbed == 4, (
        "expected all four child-env sites (suite worker, counterfeit worker, "
        f"openapi checker, tests-own-tree probe) to scrub it; found {scrubbed}"
    )


def test_the_counterfeit_harness_scrubs_the_interpreter_too() -> None:
    """SECOND, INDEPENDENT LAYER. The harness is reachable from
    ``make counterfeit-gate`` and from a bare operator shell that never passed
    through merge-gate.sh, so its own child env has to drop the interpreter
    rather than trusting a caller to have done it."""
    src = (REPO_ROOT / "tests" / "counterfeits" / "harness.py").read_text(encoding="utf-8")
    assert 'env.pop("MERGE_GATE_PY", None)' in src, src[:0] or (
        "tests/counterfeits/harness.py hands its pytest subprocess whatever "
        "interpreter override the ambient environment carried"
    )
    assert 'env.pop("MERGE_GATE_PINNED", None)' in src


def test_every_meta_test_states_the_interpreter_it_wants() -> None:
    """THIRD LAYER, and the one that survives a future caller re-introducing the
    export. Same doctrine the modules already apply to MERGE_GATE_PINNED —
    "STATE THE MODE, never inherit it" — applied to the interpreter, which is
    the more expensive of the two by three orders of magnitude."""
    for module in sorted((REPO_ROOT / "tests" / "scripts").glob("test_merge_gate_*.py")):
        src = module.read_text(encoding="utf-8")
        if "**os.environ" not in src or "run_contained(" not in src:
            continue
        assert "MERGE_GATE_PY" in src, (
            f"{module.name} builds a gate child env from os.environ but never "
            "states MERGE_GATE_PY, so an ambient interpreter override reaches "
            "the nested gate"
        )


# ---------------------------------------------------------------------------
# F. the instrument's own bit-rot: the counterfeit patches must still apply
# ---------------------------------------------------------------------------


def test_the_merge_gate_counterfeit_patches_still_apply(tmp_path: Path) -> None:
    """A near-miss caught by hand on 2026-08-07, now mechanical.

    ``cf-merge-gate-trusts-summary-over-exit-code.patch`` carries the closing
    ``fi`` of the counterfeit scoring block as trailing CONTEXT. Turning that
    ``fi`` into an ``else`` — the obvious way to add a CF_PRESENT=0 branch —
    makes the patch fail to apply, and the harness treats a failed apply as a
    HARD ERROR, so every gate run would refuse on its own instrument.

    ``tests/counterfeits/test_gate.py`` already checks this, but it is marked
    ``counterfeit_gate`` and deselected from the default lane, so the cost of
    discovering it was a full corpus run. This is the same assertion for the two
    patches that target merge-gate.sh, in the fast lane, beside the change most
    likely to break them.
    """
    patches = sorted((REPO_ROOT / "tests" / "counterfeits" / "patches").glob("cf-merge-gate-*.patch"))
    assert patches, "the merge-gate counterfeit patches vanished"
    for patch in patches:
        proc = subprocess.run(
            ["git", "apply", "--check", "--whitespace=nowarn", str(patch)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, (
            f"{patch.name} no longer applies to scripts/merge-gate.sh — the "
            "counterfeit harness treats that as a hard error, so every gate run "
            f"would refuse on its own instrument:\n{proc.stderr or proc.stdout}"
        )


# ---------------------------------------------------------------------------
# J. JUnit emission is evidence-only — it can never change a verdict
# ---------------------------------------------------------------------------


def test_a_writable_junit_dir_gets_per_suite_xml(suites_repo: M8Repo, tmp_path: Path) -> None:
    """When MERGE_GATE_JUNIT_DIR is usable, every guarded suite leaves an XML.

    The point of per-suite JUnit is that a red run's failing TEST NAME survives
    the scratch teardown (the 2026-08-09 twin-repair rejection recorded only
    "1 failed, 669 passed" with the name already deleted). A green control run
    must therefore write one XML per suite the gate actually ran.
    """
    junit_dir = tmp_path / "junit-out"
    case = suites_repo.branches["control"]
    result, receipt = _run_gate(
        suites_repo,
        case,
        env_extra={
            "MERGE_GATE_JUNIT_DIR": str(junit_dir),
            # Receipt reuse (the run_suite short-circuit) skips suite_worker and
            # therefore legitimately writes no XML; force real suite runs so
            # this test asserts the EMITTING path, deterministically.
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )
    output = _output(result)
    assert result.returncode == 0, output
    assert "MERGE GATE: PASS" in output, output
    written = sorted(p.name for p in junit_dir.glob("*.xml"))
    assert written, f"no JUnit XML written to {junit_dir}; gate output:\n{output}"
    for step_name in GUARDED_SUITES:
        step = _step(receipt, step_name)
        if step is not None and step.get("status") in {"ok", "failed"}:
            assert any(step_name in name for name in written), (
                f"suite {step_name} ran (status {step.get('status')}) but left no "
                f"JUnit XML; wrote only {written}"
            )


def test_an_unwritable_junit_dir_never_changes_the_verdict(
    suites_repo: M8Repo, tmp_path: Path
) -> None:
    """EVIDENCE PLUMBING MUST NOT FLIP A VERDICT (cross-lineage review B1).

    Pointing MERGE_GATE_JUNIT_DIR at a path that cannot become a directory
    (here: an existing regular file) must leave the gate verdict byte-identical
    to the unset path: pytest is never handed --junitxml it cannot honour, the
    suites run, the gate passes, and the degradation is SAID on stderr instead
    of surfacing later as a fake candidate-suite failure.
    """
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("occupied\n", encoding="utf-8")
    case = suites_repo.branches["control"]
    result, receipt = _run_gate(
        suites_repo,
        case,
        env_extra={
            "MERGE_GATE_JUNIT_DIR": str(blocker),
            # Force real suite runs (see the writable-dir test above): a
            # receipt-reused suite never consults the JUnit dir at all.
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )
    output = _output(result)
    assert result.returncode == 0, (
        f"an unusable JUnit dir changed the gate outcome:\n{output}"
    )
    assert "MERGE GATE: PASS" in output, output
    assert "WITHOUT JUnit" in output, (
        "the JUnit degradation must be announced, not silent — favourable "
        f"absence is the estate's top defect class:\n{output}"
    )
    for step_name in GUARDED_SUITES:
        step = _step(receipt, step_name)
        if step is not None:
            assert step.get("status") != "failed", (
                f"suite {step_name} was scored failed under an unusable JUnit "
                f"dir — the B1 verdict flip this test exists to refuse: {receipt}"
            )
