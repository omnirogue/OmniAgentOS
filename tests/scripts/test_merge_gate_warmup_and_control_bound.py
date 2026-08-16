"""Two ways a COLD, LOADED box was being reported as a defect in the candidate.

Both properties here are about the same 2026-08-10 evidence: every gate run gets
a brand-new scratch worktree — no ``__pycache__``, no page cache — and two
independent stages died of that alone.

**A. the pre-ladder warm-up.** The first process to touch each module pays the
read and the compile; under ``-n 8 --dist loadfile`` eight workers pay it at the
same time for the same files, and every pytest subprocess a test starts pays it
again. A test's own subprocess importing ``omniagentos.swarm.dal`` on that cold
tree died with ``FATAL(preflight): swarm ledger CLI unusable``. ``merge-gate.sh``
now compiles the heavy roots ONCE before the ladder starts. The whole value of
the step is that it is *cheap insurance*, so the thing that must be true is that
it can never cost anything: a warm-up that fails, or one that hangs and is
killed at its own bound, is recorded and shouted about and the gate carries on.
A warm-up that can refuse a candidate is worse than no warm-up at all.

**B. the counterfeit CONTROL's bound.** The corpus harness runs its ``must_fail``
union UNPATCHED first, under a 300s bound, before it scores a single entry. When
that bound is what ends the run the harness says so — "instrument bound
exhausted, not a corpus verdict" — and then returns 1, the same code it returns
for a control that came back RED. The gate read both as the corpus refusing, and
the daemon converted the refusal into rejecting an innocent 2-member train. An
instrument error must never be reported as a candidate defect: exhaustion is now
``exit 2`` with ``instrument_error`` on the receipt, so the train is parked
rather than blamed, while every genuine corpus verdict stays ``exit 1``.

Each fix carries its own negative control. Making the warm-up fatal, or putting
the control timeout back on the refusal path, each turns a named test red — so
neither guard can be deleted while looking green.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from omniagentos.scheduler.gate_evidence import GateEvidenceStore
from tests.scripts.test_merge_gate_absent_suite_guards import (
    _branch_adding,
    _build_repo,
    _gate_env,
    _run_or_contain,
    _step,
)
from tests.scripts.test_merge_gate_m8_refusals import (
    MERGE_GATE,
    REAL_PYTHON,
    REPO_ROOT,
    FixtureBranch,
    M8Repo,
    _git,
    _output,
    _receipt,
)

HARNESS = REPO_ROOT / "tests" / "counterfeits" / "harness.py"

#: The harness's control-failure exit code. It is the SAME code for "the control
#: ran out of time" and "the control came back red", which is exactly why the
#: gate has to read the text to tell them apart.
CF_CONTROL_RC = "1"

#: What the harness really prints when run_control's bound is what ended the run
#: (tests/counterfeits/harness.py, run_control + main). Reproduced here as a
#: fixture; kept honest against the real code by
#: test_the_gate_pattern_still_matches_the_harness_diagnostic below, which drives
#: a genuine TimeoutExpired through run_control instead of trusting this string.
CF_CONTROL_TIMED_OUT = (
    "COUNTERFEIT GATE CONTROL FAILED:\n"
    "control (unpatched) timed out after 300.0s — instrument bound exhausted, "
    "not a corpus verdict\n"
    "Command '['pytest']' timed out after 300.0 seconds"
)

#: A control that RAN and came back red. Same exit code, different meaning: this
#: one is a statement about the corpus and must stay a refusal.
CF_CONTROL_RED = (
    "COUNTERFEIT GATE CONTROL FAILED:\n"
    "control (unpatched) must_fail set is not green — corpus points at broken "
    "or missing tests (rc=1)"
)

#: A counterfeit that survived: the anti-counterfeit check itself failing.
CF_SURVIVED_REPORT = "total=59  caught=58  survived=1  other=0"

# A stub interpreter that answers every child the gate starts, and RECORDS the
# argv of the two that matter here. compileall and pytest are logged to ONE file
# so their ORDER is evidence: the warm-up has to happen before the ladder is
# launched, and a log that interleaved them would say so.
_STUB = """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "omniagentos.scheduler.gate_evidence" ] || [ "$1" = "-" ]; then
  # `cd` FIRST, and this is load-bearing for this module alone: the fixture
  # repo now carries an `omniagentos/` package (that is what the warm-up warms),
  # and for `-m` and `-` python puts the CWD ahead of PYTHONPATH on sys.path —
  # so the real gate_evidence resolved to the two-line fixture stub and every
  # receipt check failed on ModuleNotFoundError. Every argument these two are
  # handed is an absolute path, so the cwd is not otherwise theirs to use.
  cd {source_root} || exit 1
  PYTHONPATH={source_root} exec {real_python} "$@"
fi
if [ "$1" = "-m" ] && [ "$2" = "compileall" ]; then
  printf 'compileall %s\\n' "$*" >> {argv_log}
  case "${{MG_WARMUP_MODE:-ok}}" in
    fail) printf 'compileall: cannot list omniagentos\\n' >&2 ; exit 1 ;;
    hang) sleep {hang_seconds} ; exit 0 ;;
  esac
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "tests.counterfeits.harness" ]; then
  printf 'COUNTERFEIT CORPUS REPORT\\n'
  printf '%s\\n' "${{MERGE_GATE_TEST_CF_REPORT:-total=1  caught=1  survived=0  other=0}}"
  exit "${{MERGE_GATE_TEST_CF_RC:-0}}"
fi
if [ "$1" = "-c" ]; then
  printf '%s/omniagentos/__init__.py' "$PWD"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "ruff" ]; then
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  printf 'pytest %s\\n' "$*" >> {argv_log}
  printf '1 passed in 0.01s\\n'
  exit 0
fi
exec {real_python} "$@"
"""

HANG_SECONDS = 30


def _install_stub(repo: Path, argv_log: Path) -> None:
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(
        _STUB.format(
            source_root=shlex.quote(str(REPO_ROOT)),
            real_python=shlex.quote(str(REAL_PYTHON)),
            argv_log=shlex.quote(str(argv_log)),
            hang_seconds=HANG_SECONDS,
        ),
        encoding="utf-8",
    )
    python.chmod(0o755)


def _add_to_main(repo: Path, files: dict[str, str], message: str) -> None:
    """Commit files onto main AFTER the candidate branches were cut.

    The candidates keep branching from the original base, so the merge base the
    gate resolves — and therefore the one the signed receipt is bound to — is
    unchanged; only the tree the trial merge lands on grows.
    """
    for relative, content in files.items():
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _sign_receipt(fixture: M8Repo, case: FixtureBranch) -> None:
    store = GateEvidenceStore(fixture.evidence_root)
    signed = store.sign(_receipt(case, fixture.path))
    path = fixture.evidence_root / "records" / "merge-gate" / f"{case.candidate_sha}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(signed.to_payload(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _extra_branch(fixture: M8Repo, key: str, relative: str, content: str) -> FixtureBranch:
    """A second candidate on the same fixture repo, with its own signed receipt."""
    base = fixture.branches["control"]
    name = f"fixture/{key}"
    sha = _branch_adding(fixture.path, name, relative, content)
    case = FixtureBranch(
        name=name,
        candidate_sha=sha,
        merge_base_sha=base.merge_base_sha,
        refusal=None,
        reason=None,
    )
    _sign_receipt(fixture, case)
    fixture.branches[key] = case
    return case


# The package the warm-up exists for. Content is irrelevant — the stub answers
# `-m compileall` — but the DIRECTORIES have to be in the merged tree, because
# that is what decides whether the step runs or records an honest skip.
_WARM_TREE = {
    "omniagentos/__init__.py": "# fixture package\n",
    "omniagentos/swarm/__init__.py": "# fixture subpackage\n",
    "pipeline/bridge/gate_loop.py": "# fixture module\n",
}


@pytest.fixture
def warm_repo(tmp_path: Path) -> tuple[M8Repo, Path]:
    """A gate fixture repo whose merged tree carries the warm roots."""
    fixture = _build_repo(tmp_path)
    argv_log = tmp_path / "child-argv.log"
    _install_stub(fixture.path, argv_log)
    _add_to_main(fixture.path, _WARM_TREE, "fixture: warm roots")
    return fixture, argv_log


def _run(
    fixture: M8Repo,
    *,
    case_key: str = "control",
    gate_script: Path = MERGE_GATE,
    extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    case = fixture.branches[case_key]
    emit = fixture.path.parent / f"warm-receipt-{case.candidate_sha[:12]}-{os.getpid()}.json"
    if emit.exists():
        emit.unlink()
    result = _run_or_contain(
        ["bash", str(gate_script), "--emit-receipt", str(emit), case.name],
        cwd=fixture.path,
        # UNPINNED, so the mutated-script runs below are not refused for
        # `stale-gate-script` before they can reach the property under test —
        # the pinned identity check compares the running script's sha256 with
        # the workspace's copy, and a negative control is by definition not it.
        env=_gate_env(fixture, pinned=False, extra=extra or {}),
    )
    receipt = json.loads(emit.read_text(encoding="utf-8")) if emit.exists() else {}
    return result, receipt


def _argv_lines(argv_log: Path, prefix: str) -> list[str]:
    if not argv_log.is_file():
        return []
    return [
        line
        for line in argv_log.read_text(encoding="utf-8").splitlines()
        if line.startswith(prefix)
    ]


class MutationNotApplied(AssertionError):
    """The negative control did not bind — it observed NOTHING, so it proved nothing."""


def _mutate(tmp_path: Path, anchor: str, replacement: str, label: str) -> Path:
    source = MERGE_GATE.read_text(encoding="utf-8")
    count = source.count(anchor)
    if count != 1:
        raise MutationNotApplied(
            f"EXPERIMENT NOT SET UP [{label}]: the anchor binds {count} times in "
            f"{MERGE_GATE.name}, not once. The code moved — re-anchor this "
            "negative control rather than deleting it, or the guard it protects "
            "becomes deletable while every test stays green."
        )
    path = tmp_path / f"merge-gate-{label}.sh"
    path.write_text(source.replace(anchor, replacement), encoding="utf-8")
    check = subprocess.run(
        ["bash", "-n", str(path)], capture_output=True, text=True, check=False
    )
    if check.returncode != 0:
        raise MutationNotApplied(
            f"EXPERIMENT NOT SET UP [{label}]: the mutated script is not valid "
            f"bash, so its run would fail for the wrong reason: {check.stderr}"
        )
    print(f"MUTATION APPLIED [{label}]")
    return path


# The one place that decides whether a non-zero warm-up costs anything. Adding a
# `fail` here makes BOTH failure arms (a broken command and an exhausted bound)
# fatal, which is precisely the regression the two tests below forbid.
_WARMUP_NONFATAL_ANCHOR = (
    '      WARM_HEAD=$(head -n 3 "$STEP_DIR/warmup.out" 2>/dev/null'
    " | tr '\\n' ' ' | cut -c1-200)\n"
)
_WARMUP_MADE_FATAL = _WARMUP_NONFATAL_ANCHOR + (
    '      fail "workspace-warmup" "warm-up exited $WARM_RC"\n'
)

# The guard that classifies an exhausted CONTROL as the instrument. `false &&`
# leaves the call in place (so the helper is still defined and still parses) and
# routes the same run back down the ordinary refusal path.
_CONTROL_BOUND_CALL = (
    '  if [ "${CF_DEFERRED:-0}" = "1" ] &&\n'
    "     cf_control_bound_exhausted \\\n"
)
_CONTROL_BOUND_DISARMED = (
    '  if [ "${CF_DEFERRED:-0}" = "1" ] &&\n'
    "     false && cf_control_bound_exhausted \\\n"
)


# ---------------------------------------------------------------------------
# A. the warm-up: visible, bounded, and never a verdict
# ---------------------------------------------------------------------------


def test_the_warmup_runs_before_the_ladder_and_is_recorded_as_its_own_step(
    warm_repo: tuple[M8Repo, Path],
) -> None:
    """Its COST has to be visible, or nobody can decide whether to keep it.

    Order is the other half of the claim: the point is to compile once, serially,
    BEFORE eight xdist workers start doing it concurrently. A warm-up that ran
    after the ladder was launched would be pure cost.
    """
    fixture, argv_log = warm_repo
    result, receipt = _run(fixture)

    assert result.returncode == 0, _output(result)
    step = _step(receipt, "workspace-warmup")
    assert step is not None, (
        "the warm-up left no step in the receipt — an invisible step cannot be "
        f"budgeted, and its cost is exactly what is in question: {receipt.get('steps')}"
    )
    assert step["status"] == "ok", step
    assert step["started_at"] and step["finished_at"], step

    warm = _argv_lines(argv_log, "compileall ")
    assert len(warm) == 1, f"expected exactly one warm-up, got {warm}"
    # ABSOLUTE paths under the scratch tree, from a cwd that is NOT the scratch:
    # `-m` puts the cwd on sys.path, so warming from inside the candidate's tree
    # would let a candidate-planted compileall.py shadow the stdlib module.
    assert "/omniagentos" in warm[0] and "/pipeline/bridge" in warm[0], warm[0]
    assert re.search(r"compileall -q /\S+/omniagentos ", warm[0]), warm[0]

    ladder = _argv_lines(argv_log, "pytest ")
    assert ladder, "the ladder never ran, so the ordering claim is vacuous"
    lines = argv_log.read_text(encoding="utf-8").splitlines()
    assert lines.index(warm[0]) < lines.index(ladder[0]), (
        "the warm-up did not run before the ladder:\n" + "\n".join(lines)
    )


def test_a_warmup_that_fails_is_loud_and_still_never_refuses_the_candidate(
    warm_repo: tuple[M8Repo, Path], tmp_path: Path
) -> None:
    """PROTECTED: a broken warm-up command is recorded and the gate passes.

    REVERTED: with a `fail` added to the same arm, the identical run refuses —
    which is the whole hazard. The warm-up executes the candidate's file tree; a
    step that can refuse on its own instrument is a new refusal class bought for
    an optimisation.
    """
    fixture, _ = warm_repo
    broken = {"MG_WARMUP_MODE": "fail"}

    result, receipt = _run(fixture, extra=broken)
    print(f"PROTECTED [warmup-fail] rc={result.returncode}\n{_output(result)}")
    assert result.returncode == 0, (
        "A FAILED WARM-UP REFUSED THE CANDIDATE. It is an optimisation; it does "
        f"not get a vote:\n{_output(result)}"
    )
    assert "MERGE GATE: PASS" in _output(result)
    step = _step(receipt, "workspace-warmup")
    assert step is not None and step["status"] == "failed-nonfatal", step
    # LOUD: the reason survives in the operator's output, because $SCRATCH (and
    # warmup.out inside it) is deleted by the EXIT trap.
    assert "warm-up FAILED" in _output(result), _output(result)
    assert "NOT a candidate defect" in _output(result), _output(result)

    mutated = _mutate(
        tmp_path, _WARMUP_NONFATAL_ANCHOR, _WARMUP_MADE_FATAL, "warmup-made-fatal"
    )
    reverted, _ = _run(fixture, gate_script=mutated, extra=broken)
    print(f"REVERTED [warmup-fail] rc={reverted.returncode}\n{_output(reverted)}")
    assert reverted.returncode != 0, (
        "the negative control did not reproduce the hazard — a fatal warm-up "
        f"should have refused this run:\n{_output(reverted)}"
    )
    assert "workspace-warmup" in _output(reverted)


def test_a_warmup_that_hangs_is_killed_at_its_own_bound_and_still_passes(
    warm_repo: tuple[M8Repo, Path], tmp_path: Path
) -> None:
    """The bound is the reason this can be enabled on a loaded box at all.

    Unbounded, the warm-up would be a new way for a contended host to hang the
    gate — the same class of failure it was added to prevent.
    """
    fixture, _ = warm_repo
    hanging = {"MG_WARMUP_MODE": "hang", "MERGE_GATE_WARMUP_TIMEOUT": "2"}

    started = time.monotonic()
    result, receipt = _run(fixture, extra=hanging)
    elapsed = time.monotonic() - started
    print(f"PROTECTED [warmup-hang] rc={result.returncode} in {elapsed:.1f}s")
    assert result.returncode == 0, _output(result)
    assert "MERGE GATE: PASS" in _output(result)
    step = _step(receipt, "workspace-warmup")
    assert step is not None and step["status"] == "bound-exhausted", step
    assert elapsed < HANG_SECONDS, (
        f"the gate waited {elapsed:.1f}s for a warm-up bounded at 2s — the bound "
        "did not fire, so a hung compile still holds the box"
    )
    assert "hit its own 2s bound" in _output(result), _output(result)

    mutated = _mutate(
        tmp_path, _WARMUP_NONFATAL_ANCHOR, _WARMUP_MADE_FATAL, "warmup-made-fatal"
    )
    reverted, _ = _run(fixture, gate_script=mutated, extra=hanging)
    print(f"REVERTED [warmup-hang] rc={reverted.returncode}")
    assert reverted.returncode != 0, (
        "the negative control did not reproduce the hazard — a fatal warm-up "
        f"should have refused this run:\n{_output(reverted)}"
    )


def test_a_tree_with_no_warm_roots_records_an_honest_skip(tmp_path: Path) -> None:
    """A step that did not run must never be indistinguishable from one that did.

    The minimal fixture repos every merge-gate test module builds carry no
    ``omniagentos/`` at all; the warm-up says so rather than compiling nothing
    and reporting ok.
    """
    fixture = _build_repo(tmp_path)
    argv_log = tmp_path / "child-argv.log"
    _install_stub(fixture.path, argv_log)

    result, receipt = _run(fixture)

    assert result.returncode == 0, _output(result)
    step = _step(receipt, "workspace-warmup")
    assert step is not None and step["status"] == "skipped", step
    assert not _argv_lines(argv_log, "compileall "), "warmed a tree with no roots"


def test_one_pycache_from_the_import_probe_does_not_read_as_a_warm_tree(
    tmp_path: Path,
) -> None:
    """The heuristic is a COUNT, and this is why.

    ``merge-gate.sh`` already runs ``python -c "import omniagentos"`` in the
    scratch tree (the tests-own-tree probe) before it gets here, and that leaves
    EXACTLY ONE ``__pycache__`` — measured on a fresh worktree of this repo. A
    presence test would therefore have read every genuinely cold tree as warm
    and skipped the warm-up in every real run: the fix present in the tree and
    absent in production.
    """
    fixture = _build_repo(tmp_path)
    argv_log = tmp_path / "child-argv.log"
    _install_stub(fixture.path, argv_log)
    tree = dict(_WARM_TREE)
    tree["omniagentos/__pycache__/__init__.cpython-312.pyc"] = "probe residue\n"
    _add_to_main(fixture.path, tree, "fixture: warm roots + one probe pycache")

    result, receipt = _run(fixture)

    assert result.returncode == 0, _output(result)
    step = _step(receipt, "workspace-warmup")
    assert step is not None and step["status"] == "ok", step
    assert _argv_lines(argv_log, "compileall "), (
        "a single __pycache__ — which the gate's own import probe always leaves "
        "behind — was read as a warm tree, so the warm-up would never run"
    )


def test_an_already_warm_tree_skips_the_warmup(tmp_path: Path) -> None:
    """Above the floor there is nothing to compile, and the skip says so."""
    fixture = _build_repo(tmp_path)
    argv_log = tmp_path / "child-argv.log"
    _install_stub(fixture.path, argv_log)
    tree = dict(_WARM_TREE)
    for index in range(20):
        tree[f"omniagentos/mod{index:02d}/__pycache__/mod.cpython-312.pyc"] = "warm\n"
    _add_to_main(fixture.path, tree, "fixture: an already warm tree")

    result, receipt = _run(fixture)

    assert result.returncode == 0, _output(result)
    step = _step(receipt, "workspace-warmup")
    assert step is not None and step["status"] == "skipped-warm", step
    assert not _argv_lines(argv_log, "compileall "), "recompiled an already warm tree"


# ---------------------------------------------------------------------------
# B. the counterfeit control bound: instrument, not candidate
# ---------------------------------------------------------------------------


def test_a_control_that_exhausts_its_bound_is_an_instrument_error_not_a_refusal(
    warm_repo: tuple[M8Repo, Path], tmp_path: Path
) -> None:
    """PROTECTED: exit 2 + ``instrument_error``, so the daemon parks the train.

    REVERTED: with the classification disarmed, the identical run comes back as
    a plain ``exit 1`` refusal carrying no instrument label — which is what let
    a gate daemon reject the innocent members of a 2-member train on 2026-08-10.
    """
    fixture, _ = warm_repo
    timed_out = {
        "MERGE_GATE_TEST_CF_RC": CF_CONTROL_RC,
        "MERGE_GATE_TEST_CF_REPORT": CF_CONTROL_TIMED_OUT,
        "MERGE_GATE_STEP_RECEIPTS": "0",
    }

    result, receipt = _run(fixture, extra=timed_out)
    print(f"PROTECTED [control-bound] rc={result.returncode}\n{_output(result)}")
    assert result.returncode == 2, (
        "an exhausted CONTROL was not reported as an instrument failure. exit 1 "
        "is the candidate-defect code, and the daemon rejects a train's members "
        f"on it:\n{_output(result)}"
    )
    assert receipt.get("exit_code") == 2, receipt.get("exit_code")
    assert receipt.get("instrument_error") is True, (
        "the receipt does not say this was the instrument, so no machine "
        f"consumer can tell it from a corpus refusal: {receipt.get('instrument_error')}"
    )
    assert "counterfeit-control-timeout" in (receipt.get("refusal_reason") or ""), receipt
    assert "MERGE GATE: PASS" not in _output(result)
    # It must not be laundered into the steps[] carrier as a candidate defect.
    step = _step(receipt, "counterfeit-gate")
    assert step is not None and step["status"] == "instrument-failure", step
    # And it names its own remedy, rather than sending the next reader to debug
    # the candidate.
    assert "NOT a verdict about" in _output(result), _output(result)
    assert "REMEDY:" in _output(result), _output(result)

    mutated = _mutate(
        tmp_path,
        _CONTROL_BOUND_CALL,
        _CONTROL_BOUND_DISARMED,
        "control-timeout-back-to-refusal",
    )
    reverted, reverted_receipt = _run(fixture, gate_script=mutated, extra=timed_out)
    print(f"REVERTED [control-bound] rc={reverted.returncode}\n{_output(reverted)}")
    assert reverted.returncode == 1, (
        "the negative control did not reproduce the defect — with the "
        "classification disarmed this run should be a plain candidate refusal:"
        f"\n{_output(reverted)}"
    )
    assert reverted_receipt.get("instrument_error") is not True, reverted_receipt


def test_a_corpus_verdict_failure_is_still_a_refusal(
    warm_repo: tuple[M8Repo, Path],
) -> None:
    """The anti-counterfeit check itself is untouched.

    A counterfeit that SURVIVED is the corpus telling the truth about the
    candidate's test suite, and it must keep costing the candidate its merge —
    the instrument label is not a way out of a red corpus.
    """
    fixture, _ = warm_repo

    result, receipt = _run(
        fixture,
        extra={
            "MERGE_GATE_TEST_CF_RC": "1",
            "MERGE_GATE_TEST_CF_REPORT": CF_SURVIVED_REPORT,
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )

    assert result.returncode == 1, _output(result)
    assert receipt.get("instrument_error") is not True, receipt
    assert "MERGE GATE: PASS" not in _output(result)
    assert re.search(r"counterfeit-gate\s+FAIL", _output(result)), _output(result)


def test_a_control_that_ran_and_came_back_red_is_still_a_refusal(
    warm_repo: tuple[M8Repo, Path],
) -> None:
    """Same exit code, same header, different sentence — and different verdict.

    "The control could not finish" and "the control finished and was not green"
    are the two halves the harness collapses into rc=1. Only the first is the
    instrument; the second says the corpus points at broken or missing tests,
    which is a real finding about the tree under judgement.
    """
    fixture, _ = warm_repo

    result, receipt = _run(
        fixture,
        extra={
            "MERGE_GATE_TEST_CF_RC": CF_CONTROL_RC,
            "MERGE_GATE_TEST_CF_REPORT": CF_CONTROL_RED,
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )

    assert result.returncode == 1, _output(result)
    assert receipt.get("instrument_error") is not True, receipt
    assert "counterfeit-control-timeout" not in _output(result), _output(result)


def test_a_candidate_that_ships_its_own_harness_gets_no_instrument_label(
    warm_repo: tuple[M8Repo, Path],
) -> None:
    """PROVENANCE. The classification is read out of text the harness printed,
    and the harness runs from the MERGED tree — so it is only trustworthy while
    the candidate has not touched it.

    This is the admission rule for GATE_INSTRUMENT_SLUGS being honoured as
    closely as a text-derived condition can be: with
    ``tests/counterfeits/harness.py`` anywhere in the candidate's history, the
    same diagnostic buys nothing and the ordinary refusal stands.
    """
    fixture, _ = warm_repo
    _extra_branch(
        fixture,
        "own_harness",
        "tests/counterfeits/harness.py",
        "# candidate-supplied harness\n",
    )

    result, receipt = _run(
        fixture,
        case_key="own_harness",
        extra={
            "MERGE_GATE_TEST_CF_RC": CF_CONTROL_RC,
            "MERGE_GATE_TEST_CF_REPORT": CF_CONTROL_TIMED_OUT,
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )

    assert result.returncode == 1, (
        "a candidate that supplies the harness printed its own 'instrument bound "
        "exhausted' line and was excused for it — a wrong instrument label "
        f"EXCUSES a real defect:\n{_output(result)}"
    )
    assert receipt.get("instrument_error") is not True, receipt
    assert "counterfeit-control-timeout" not in _output(result), _output(result)


def test_the_gate_pattern_still_matches_the_harness_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE DRIFT ANCHOR, and the cheapest test in this file.

    The gate identifies an exhausted control by grepping the harness's own
    words. Reword ``run_control``'s message and the classification silently
    stops binding — the gate would go back to refusing on a timeout with every
    test still green, because the safe direction of this guard is also the
    invisible one. So the pattern is taken out of ``merge-gate.sh`` and matched
    against the text the REAL ``run_control`` produces when its bound fires.
    """
    pattern_line = [
        line
        for line in MERGE_GATE.read_text(encoding="utf-8").splitlines()
        if "grep -qE" in line and "instrument bound exhausted" in line
    ]
    assert len(pattern_line) == 1, (
        f"expected exactly one control-bound pattern in {MERGE_GATE.name}: {pattern_line}"
    )
    match = re.search(r"grep -qE '([^']+)'", pattern_line[0])
    assert match, pattern_line[0]
    pattern = match.group(1)

    from tests.counterfeits import harness

    def _boom(**kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["pytest"], timeout=300.0)

    monkeypatch.setattr(harness, "run_pytest_nodes", _boom)
    with pytest.raises(harness.CounterfeitControlError) as raised:
        # ``runs_on_this_platform`` is load-bearing on the stub: run_control now
        # drops platform-pinned entries from the control union before running
        # anything, and an entry that is pinned away would never reach the
        # timeout path this test exists to exercise (it would refuse with the
        # "nothing runnable" instrument error instead, and the pattern assert
        # below would be checking the wrong message).
        harness.run_control(
            [
                SimpleNamespace(
                    must_fail=("tests/fake.py::test_node",),
                    runs_on_this_platform=True,
                )
            ]
        )

    message = str(raised.value)
    assert re.search(pattern, message, re.M), (
        "the gate's control-bound pattern no longer matches what run_control "
        f"actually prints.\npattern: {pattern}\nmessage: {message}"
    )
    # The header half of the same condition, printed by main()'s handler.
    assert "COUNTERFEIT GATE CONTROL FAILED" in HARNESS.read_text(encoding="utf-8")
