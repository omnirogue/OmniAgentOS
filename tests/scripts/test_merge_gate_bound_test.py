"""The closure binding: a fix is not closed until the test it names is GREEN.

A finding was "fixed" whenever a lane said so. Nothing in ``merge-gate.sh``
knew which failing test a candidate claimed to close, so three different states
were byte-identical to every consumer of the receipt:

  * the fix landed and the named test now passes;
  * the fix landed and the named test still fails (nobody ran it);
  * the fix landed and the named test was EDITED, SKIPPED or DESELECTED out of
    existence, which is how a finding starves instead of closing.

``--bound-test <node-id>`` binds the candidate to the node it claims to close,
and this module pins the ways that binding can be defeated:

  1. **the parser** — the flag is REPEATABLE, because a train carries N members
     and therefore up to N bindings. A store-not-append parser keeps only the
     last one and grades N-1 members as if they had no binding at all, which is
     a false GREEN of exactly the class the flag exists to remove.
  2. **the receipt** — a run that was never told what it was closing must be
     distinguishable from one that closed something. ``bound_test`` is an ARRAY
     and ``bound_test_result`` is null-by-default, never green-by-default.
  3. **anti-weakening** — a candidate that edits its own bound test file is a
     CANDIDATE DEFECT (exit 1), not a gate refusal (exit 2), and its slug is
     therefore kept out of ``GATE_INSTRUMENT_SLUGS``.
  4. **NOT_EVALUABLE is not GREEN** — pytest exits 0 both when every named node
     passed and when every named node was SKIPPED. The marker override
     (``-m "counterfeit_gate or not counterfeit_gate"``) exists because
     ``pyproject.toml`` puts a global ``-m 'not (...)'`` in ``addopts`` and
     pytest applies it to explicit node ids too — without it a bound node
     carrying an excluded marker deselects silently and grades as "did not
     fail".
  5. **the conftest route** — the bound test file can be left untouched while a
     ``conftest.py`` anywhere above it skips the node or fakes its report. The
     conftest chain is part of the test.
  6. **the comparison space** — ``./tests/x.py``, a case variant or a bare
     directory matches nothing in ``CHANGED_PATHS``, so the untouched check
     prints ``ok`` having compared nothing at all. A binding that cannot be
     compared is worse than no binding, and refuses as a caller error (exit 2).

And one thing that is deliberately NOT here: a step receipt. A closure proof is
re-run every time, never cached.

THE FIXTURE'S FAKE INTERPRETER IS PART OF THE TEST. The merge-gate fixtures'
stub answers ``-m pytest`` with a ``printf`` and swallows every flag, so a
bound-test assertion driven through the unmodified stub is vacuously green: the
stub would print ``1 passed`` no matter what the gate did or did not pass it.
This module teaches the stub the node-id invocation (and logs the argv it was
handed) so the assertions have something real to observe. For the same reason
the fixture's ``tests/counterfeits/harness.py`` is a LOADER for the real
module rather than the inert stand-in the sibling fixtures use — the gate loads
its "did this actually execute?" rule from ``$REPO/tests/counterfeits/harness.py``
BY PATH, and a test that exercised a stand-in copy of that rule would be
measuring the wrong function.

By path, not by module name, and that distinction was found here: with
``tests/__init__.py`` present, ``import tests.counterfeits.harness`` resolves
through this venv's editable-install ``.pth`` rather than through
``PYTHONPATH``, so from an unrelated cwd it returned the LIVE checkout's copy.
In pinned mode that would have judged a pinned candidate with a rule read out of
a mutable tree.
"""

from __future__ import annotations

import json
import os
import subprocess
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

BOUND_FILE = "tests/fixture/test_bound.py"
BOUND_NODE = f"{BOUND_FILE}::test_the_finding_is_closed"
SECOND_BOUND_FILE = "tests/fixture/test_second.py"
SECOND_BOUND_NODE = f"{SECOND_BOUND_FILE}::test_the_second_finding_is_closed"
MARKER_OVERRIDE = "counterfeit_gate or not counterfeit_gate"

# A conftest ON the bound test's import chain (tests/fixture/test_bound.py imports
# through tests/conftest.py) that declares an ORDINARY module as a plugin via
# pytest_plugins. This is the real shape shipping in the tree at tests/conftest.py:90
# (omniagentos.testpolicy.pytest_plugin). The declared module is NOT named
# conftest.py and is NOT on the walked conftest chain, so before F6 a candidate
# could edit it to flip its bound node green and pass the untouched check.
CHAIN_CONFTEST = "tests/conftest.py"
PLUGIN_MODULE = "tests/fixture/report_plugin.py"
PLUGIN_DOTTED = "tests.fixture.report_plugin"
CHAIN_CONFTEST_SOURCE = f'pytest_plugins = ["{PLUGIN_DOTTED}"]\n'
_BENIGN_PLUGIN_SOURCE = (
    '"""Declared via pytest_plugins on the bound test chain (benign at base)."""\n'
    "\n\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    return None\n"
)
# The weaponization: the SAME hook a conftest could register, rewriting the bound
# node's report failed->passed without touching the test file or any conftest.
_WEAPONIZED_PLUGIN_SOURCE = (
    '"""Declared via pytest_plugins on the bound test chain (weaponized)."""\n'
    "\n\n"
    "def pytest_runtest_makereport(item, call):\n"
    "    outcome = yield\n"
    "    report = outcome.get_result()\n"
    "    if report.failed:\n"
    "        report.outcome = 'passed'  # flip the bound node green\n"
)
# A module that merely LOOKS like a plugin but is NOT named by any pytest_plugins
# on the chain — the over-fire control. Touching it must not refuse.
UNDECLARED_MODULE = "tests/fixture/other_helper.py"

# The pytest branch of the shared fixture stub, verbatim. Asserted rather than
# assumed: if it moves, this module's rewrite silently stops applying and every
# assertion below goes vacuous.
_STUB_PYTEST_BRANCH = """if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  printf '1 passed in 0.01s\\n'
"""

# Node ids are the one argument shape the ladder never uses, so `*::*` is a
# sufficient discriminator between "the gate is running a suite directory" and
# "the gate is running the binding".
_STUB_BOUND_BRANCH = """if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  _mg_bound=0
  for _mg_arg in "$@"; do
    case "$_mg_arg" in *::*) _mg_bound=1 ;; esac
  done
  if [ "$_mg_bound" = "1" ]; then
    {
      printf 'ARGV'
      for _mg_arg in "$@"; do printf '\\t%s' "$_mg_arg"; done
      printf '\\n'
    } >> "${MERGE_GATE_TEST_PYTEST_LOG:-/dev/null}"
    printf '%s\\n' "${MERGE_GATE_TEST_BOUND_OUT-1 passed in 0.01s}"
    exit "${MERGE_GATE_TEST_BOUND_RC:-0}"
  fi
  printf '1 passed in 0.01s\\n'
"""

# The gate imports its NOT_EVALUABLE discrimination from the trusted checkout's
# tests/counterfeits/harness.py. The fixture repo carries a loader for the REAL
# module so these tests grade the production rule, not a look-alike.
_HARNESS_LOADER = '''"""Loader for the real counterfeit harness (fixture only).

merge-gate.sh imports ``_executed_no_tests`` / ``_is_collection_failure`` from
``tests.counterfeits.harness`` in the checkout it RUNS IN. A fixture repo has no
such module, and a hand-written stand-in would mean these tests grade a copy of
the rule instead of the rule. Load the real one by path.
"""

import importlib.util as _importlib_util
import sys as _sys

_REAL_ROOT = {real_root!r}
if _REAL_ROOT not in _sys.path:
    _sys.path.insert(0, _REAL_ROOT)

_spec = _importlib_util.spec_from_file_location(
    "_real_counterfeit_harness", _REAL_ROOT + "/tests/counterfeits/harness.py"
)
_module = _importlib_util.module_from_spec(_spec)
_sys.modules[_spec.name] = _module  # @dataclass reads sys.modules[cls.__module__]
_spec.loader.exec_module(_module)

_executed_no_tests = _module._executed_no_tests
_is_collection_failure = _module._is_collection_failure
'''

_BOUND_TEST_SOURCE = """def test_the_finding_is_closed():
    assert True
"""


def _install_bound_aware_python(repo: Path) -> None:
    """Teach the fixture stub the node-id invocation, and make it log its argv.

    Without this the stub answers every ``-m pytest`` with ``1 passed`` and
    ignores its arguments, so "the gate ran the bound node with the marker
    override" and "the gate ran nothing at all" produce identical output.
    """
    _install_fake_python(repo)
    python = repo / ".venv" / "bin" / "python"
    source = python.read_text(encoding="utf-8")
    assert source.count(_STUB_PYTEST_BRANCH) == 1, (
        "the shared fixture stub's pytest branch moved; this module's rewrite "
        "would silently stop applying and every bound-test assertion below "
        "would go vacuously green"
    )
    python.write_text(
        source.replace(_STUB_PYTEST_BRANCH, _STUB_BOUND_BRANCH), encoding="utf-8"
    )
    python.chmod(0o755)


def _sign_for(fixture: M8Repo, name: str, sha: str, base_sha: str) -> FixtureBranch:
    """Register a branch created after the fixture was built.

    The gate refuses anything without a signed candidate receipt, so a case that
    invents its own candidate has to mint one too.
    """
    case = FixtureBranch(
        name=name, candidate_sha=sha, merge_base_sha=base_sha, refusal=None, reason=None
    )
    store = GateEvidenceStore(fixture.evidence_root)
    signed = store.sign(_receipt(case, fixture.path))
    receipt_path = fixture.evidence_root / "records" / "merge-gate" / f"{sha}.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt_path.write_text(
        json.dumps(signed.to_payload(), sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return case


def _branch_adding(fixture: M8Repo, slug: str, relative: str, content: str) -> FixtureBranch:
    base_sha = fixture.branches["control"].merge_base_sha
    name = f"fixture/{slug}"
    sha = _commit_on_branch(fixture.path, name, relative, content)
    return _sign_for(fixture, name, sha, base_sha)


def _commit_on_branch(repo: Path, branch: str, relative: str, content: str) -> str:
    _git(repo, "checkout", "-q", "-b", branch, "main")
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", relative)
    _git(repo, "commit", "-q", "-m", f"fixture: {branch}")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    return sha


@pytest.fixture
def bound_repo(tmp_path: Path) -> M8Repo:
    """A minimal repo carrying a bound test file and both candidate shapes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Bound Test Gate")
    _git(repo, "config", "user.email", "bound-test-gate@example.com")
    (repo / ".gitignore").write_text(".venv\nnode_modules\nvar\n", encoding="utf-8")
    (repo / "ARCHI.md").write_text("main architecture\n", encoding="utf-8")
    (repo / "WORKBOOK.md").write_text("shared workbook\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    reachability = repo / "scripts" / "reachability-gate.py"
    reachability.parent.mkdir(parents=True, exist_ok=True)
    reachability.write_bytes((REPO_ROOT / "scripts" / "reachability-gate.py").read_bytes())
    reachability.chmod(0o755)
    (repo / "scripts" / "merge-gate.sh").write_bytes(MERGE_GATE.read_bytes())
    (repo / "scripts" / "merge-gate.sh").chmod(0o755)
    harness = repo / "tests" / "counterfeits" / "harness.py"
    harness.parent.mkdir(parents=True, exist_ok=True)
    harness.write_text(_HARNESS_LOADER.format(real_root=str(REPO_ROOT)), encoding="utf-8")
    for path, name in ((BOUND_FILE, "test_the_finding_is_closed"),
                       (SECOND_BOUND_FILE, "test_the_second_finding_is_closed")):
        bound = repo / path
        bound.parent.mkdir(parents=True, exist_ok=True)
        bound.write_text(f"def {name}():\n    assert True\n", encoding="utf-8")
    # A conftest on the bound test's chain declares an ordinary module as a plugin,
    # and that module ships at base — this is the F6 surface. The module is a plain
    # .py that pytest loads via pytest_plugins, so it is NOT on the conftest walk.
    (repo / CHAIN_CONFTEST).write_text(CHAIN_CONFTEST_SOURCE, encoding="utf-8")
    plugin = repo / PLUGIN_MODULE
    plugin.parent.mkdir(parents=True, exist_ok=True)
    plugin.write_text(_BENIGN_PLUGIN_SOURCE, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    specs = {
        # The honest fix: it changes production code and leaves the bound test
        # alone.
        "control": (
            "fixture/clean-control",
            _commit_on_branch(repo, "fixture/clean-control", "candidate.txt", "clean\n"),
        ),
        # The weakening: the "fix" edits the very test it is bound to.
        "edits_bound_test": (
            "fixture/edits-bound-test",
            _commit_on_branch(
                repo,
                "fixture/edits-bound-test",
                BOUND_FILE,
                _BOUND_TEST_SOURCE.replace("assert True", "assert True  # relaxed"),
            ),
        ),
    }
    _install_bound_aware_python(repo)

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


def _gate_env(fixture: M8Repo, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        **os.environ,
        "REPO": str(fixture.path),
        "MERGE_GATE_EVIDENCE_ROOT": str(fixture.evidence_root),
        # STATE THE MODE / THE INTERPRETER / THE DEPTH, never inherit them — the
        # three rules the sibling merge-gate fixtures learned the expensive way.
        "MERGE_GATE_PINNED": "0",
        "MERGE_GATE_PY": fake_python_for(fixture.path),
        "MERGE_GATE_DEPTH": "0",
        # Step-receipt REUSE skips the suite worker entirely, so a reused
        # bound-test step would run no pytest, write no argv log and emit no
        # summary — every emission assertion here would go vacuous. Pin the
        # executing path.
        "MERGE_GATE_STEP_RECEIPTS": "0",
    }
    for knob in ("MERGE_GATE_LADDER_WORKERS", "MERGE_GATE_CF_POOL_WORKERS",
                 "MERGE_GATE_JUNIT_DIR", "MERGE_GATE_TEST_BOUND_RC",
                 "MERGE_GATE_TEST_BOUND_OUT", "MERGE_GATE_TEST_PYTEST_LOG"):
        env.pop(knob, None)
    env.pop("OMNIAGENTOS_GATE_WORKSPACE", None)
    env.update(extra or {})
    return env


def _run_gate(
    fixture: M8Repo,
    case: FixtureBranch,
    *,
    bound_tests: tuple[str, ...] = (),
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    emit = fixture.path.parent / f"run-receipt-{case.candidate_sha[:12]}-{os.getpid()}.json"
    argv = ["bash", str(MERGE_GATE), "--emit-receipt", str(emit)]
    for node in bound_tests:
        argv += ["--bound-test", node]
    argv.append(case.name)
    result = run_contained(argv, cwd=fixture.path, env=_gate_env(fixture, env_extra))
    receipt = json.loads(emit.read_text(encoding="utf-8")) if emit.exists() else {}
    return result, receipt


def _pytest_argv(log: Path) -> list[list[str]]:
    """Every bound-node pytest invocation the fixture stub was handed."""
    if not log.exists():
        return []
    return [
        line.split("\t")[1:]
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.startswith("ARGV")
    ]


def _step(receipt: dict, name: str) -> dict | None:
    for entry in receipt.get("steps", []):
        if entry.get("name") == name:
            return entry
    return None


# ---------------------------------------------------------------------------
# A. the parser — a binding that is dropped on the floor is a false GREEN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("argv_tail", [["--bound-test"], ["--bound-test", "--candidate"]])
def test_missing_bound_test_value_exits_2(tmp_path: Path, argv_tail: list[str]) -> None:
    """An empty binding must REFUSE, never be accepted as "no binding".

    Same ``''|-*`` shape the neighbouring value flags use: a caller whose
    variable expanded to nothing would otherwise silently buy an unbound run.
    """
    result = subprocess.run(
        ["bash", str(MERGE_GATE), *argv_tail],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    output = _output(result)
    assert result.returncode == 2, output
    assert "missing-value" in output, output
    assert "--bound-test needs a node id" in output, output


def test_repeated_bound_test_flags_all_survive_into_the_receipt(
    bound_repo: M8Repo, tmp_path: Path
) -> None:
    """THE STORE-NOT-APPEND TRAP, in both carriers.

    A train carries N members and up to N bindings. A parser that overwrites
    keeps the last one only, and the other N-1 members are then graded as if
    nothing was bound to them — the run still says PASS and the receipt still
    looks like a closure. Both node ids have to reach the receipt AND both have
    to be RUN.
    """
    log = tmp_path / "pytest-argv.log"
    result, receipt = _run_gate(
        bound_repo,
        bound_repo.branches["control"],
        bound_tests=(BOUND_NODE, SECOND_BOUND_NODE),
        env_extra={"MERGE_GATE_TEST_PYTEST_LOG": str(log)},
    )
    output = _output(result)
    print(f"TWO-BINDINGS rc={result.returncode}\n{output}")
    assert receipt.get("bound_test") == [BOUND_NODE, SECOND_BOUND_NODE], (
        "a repeated --bound-test flag did not accumulate; the receipt records "
        f"{receipt.get('bound_test')!r}"
    )
    invocations = _pytest_argv(log)
    ran = {argv[-1] for argv in invocations}
    assert ran == {BOUND_NODE, SECOND_BOUND_NODE}, (
        f"only {ran} was actually re-run on the merged tree; the other binding "
        f"was graded without ever being executed. invocations={invocations}"
    )


def test_two_bindings_are_two_gradeable_steps(bound_repo: M8Repo, tmp_path: Path) -> None:
    """One step per binding, or one member's green hides another's absence.

    The step id carries the index because a step receipt is keyed on (step id,
    command); two bindings sharing an id would let one member's receipt satisfy
    the other member's step.
    """
    log = tmp_path / "pytest-argv.log"
    _, receipt = _run_gate(
        bound_repo,
        bound_repo.branches["control"],
        bound_tests=(BOUND_NODE, SECOND_BOUND_NODE),
        env_extra={"MERGE_GATE_TEST_PYTEST_LOG": str(log)},
    )
    steps = [s["name"] for s in receipt.get("steps", []) if s["name"].startswith("bound-test-")]
    assert steps == ["bound-test-1", "bound-test-2"], (
        f"expected one indexed step per binding, got {steps}: "
        f"{json.dumps(receipt.get('steps', []), indent=2)}"
    )


# ---------------------------------------------------------------------------
# B. the receipt — "never told" must not be spelled like "closed"
# ---------------------------------------------------------------------------


def test_no_bound_test_flag_records_null_in_receipt_not_green(
    bound_repo: M8Repo,
) -> None:
    """A run that received no binding says so, and stays absent-safe.

    ``null`` means "this run was never told what it was closing" — the same
    distinction ``counterfeit_pool_workers`` already carries. Recording an empty
    list, or worse a green result, would make an unbound run indistinguishable
    from a closure. And because the flag is opt-in, an unbound run must be the
    gate's existing behaviour exactly: no bound-test step, no bound-test line.
    """
    result, receipt = _run_gate(bound_repo, bound_repo.branches["control"])
    output = _output(result)
    print(f"NO-BINDING rc={result.returncode}\n{output}")
    assert result.returncode == 0, output
    assert "MERGE GATE: PASS" in output, output
    assert receipt.get("bound_test") is None, receipt.get("bound_test")
    assert receipt.get("bound_test_result") is None, (
        "an unbound run recorded a bound_test_result; absent evidence must "
        f"never render as a verdict: {receipt.get('bound_test_result')!r}"
    )
    assert not [s for s in receipt.get("steps", []) if s["name"].startswith("bound-test")], (
        f"the opt-in flag was absent but a bound-test step ran anyway: {receipt.get('steps')}"
    )
    assert "bound-test" not in output, output


def test_a_green_bound_test_is_recorded_green_with_the_node_named(
    bound_repo: M8Repo,
) -> None:
    """THE OVER-FIRE CONTROL. A check that refuses everything proves nothing."""
    result, receipt = _run_gate(
        bound_repo, bound_repo.branches["control"], bound_tests=(BOUND_NODE,)
    )
    output = _output(result)
    print(f"GREEN-BINDING rc={result.returncode}\n{output}")
    assert result.returncode == 0, output
    assert "MERGE GATE: PASS" in output, output
    assert receipt.get("bound_test_result") == "green", receipt
    entry = _step(receipt, "bound-test-1")
    assert entry is not None and entry["status"] == "ok", entry
    assert BOUND_NODE in entry["detail"], entry


# ---------------------------------------------------------------------------
# C. anti-weakening — editing the test you are bound to is not a fix
# ---------------------------------------------------------------------------


def test_candidate_touching_the_bound_test_file_fails_with_exit_1_not_2(
    bound_repo: M8Repo,
) -> None:
    """A CANDIDATE DEFECT, not a gate refusal, and the difference is the point.

    Exit 2 means "the gate could not judge this"; exit 1 means "the gate judged
    it and the answer is no". Reading the candidate's own diff is judging it, so
    this lands in FAILURES — and the run receipt has to say ``weakened`` even
    though the bound test then passes on the merged tree, because it passes
    there only because the candidate relaxed it.
    """
    result, receipt = _run_gate(
        bound_repo, bound_repo.branches["edits_bound_test"], bound_tests=(BOUND_NODE,)
    )
    output = _output(result)
    print(f"EDITS-BOUND-TEST rc={result.returncode}\n{output}")
    assert result.returncode == 1, (
        f"expected a candidate-defect exit 1, got {result.returncode}:\n{output}"
    )
    assert "bound-test-untouched" in output, output
    assert "candidate edits its own bound test file" in output, output
    assert "MERGE GATE: PASS" not in output, output
    assert receipt.get("bound_test_result") == "weakened", receipt
    assert receipt.get("instrument_error") is None, (
        "a candidate defect was labelled an instrument error, which excuses it: "
        f"{receipt}"
    )


def test_bound_test_slug_is_not_in_gate_instrument_slugs() -> None:
    """The admission rule, asserted mechanically on the script.

    ``GATE_INSTRUMENT_SLUGS`` may only carry conditions measured BEFORE, or
    INDEPENDENTLY OF, reading candidate content. "The bound test file was
    edited" is read straight out of the candidate's diff, so listing it there
    would label a real defect an instrument error — and a wrong instrument label
    EXCUSES the defect, which is worse than no label at all.
    """
    src = MERGE_GATE.read_text(encoding="utf-8")
    slug_lines = [
        line for line in src.splitlines() if line.startswith("GATE_INSTRUMENT_SLUGS=")
    ]
    assert slug_lines, "GATE_INSTRUMENT_SLUGS moved; this assertion is now vacuous"
    for line in slug_lines:
        assert "bound-test" not in line, (
            f"a candidate-content check was admitted to the instrument list: {line}"
        )


def test_the_untouched_check_reads_the_tree_diff_not_the_history_sweep() -> None:
    """PLACEMENT IS THE BEHAVIOUR, and it is invisible in the output.

    Above the ``CHANGED_PATHS="$SWEPT_PATHS"`` rebind, ``CHANGED_PATHS`` is the
    net TREE diff; below it, it is everything the merge makes permanent. "Does
    the merge change the bound test file?" is a TREE question — a lane that
    edits the test and restores it changes nothing anyone reads, exactly the
    argument the oracle-path and root-workbook guards already make. Sliding this
    check below the rebind would newly refuse honest self-correcting branches.
    """
    lines = MERGE_GATE.read_text(encoding="utf-8").splitlines()
    check = next(i for i, line in enumerate(lines) if 'fail "bound-test-untouched"' in line)
    rebind = next(i for i, line in enumerate(lines) if line == 'CHANGED_PATHS="$SWEPT_PATHS"')
    assert check < rebind, (
        f"the anti-weakening check (line {check + 1}) reads the history sweep "
        f"rather than the tree diff (rebind at line {rebind + 1})"
    )


# ---------------------------------------------------------------------------
# D. NOT_EVALUABLE is not GREEN
# ---------------------------------------------------------------------------


def test_bound_test_step_uses_the_tautological_marker_override() -> None:
    """Pinned in the SCRIPT TEXT, like the quotePath idiom is pinned elsewhere.

    ``pyproject.toml`` puts a global ``-m 'not (...)'`` in ``addopts``, ``-m`` is
    store-not-append, and pytest applies the marker filter to explicit node ids
    too. Drop the override and a bound node carrying an excluded marker
    deselects SILENTLY and grades as "did not fail". The tautology is the whole
    guard, and it is one deletion away from vanishing without a trace.
    """
    src = MERGE_GATE.read_text(encoding="utf-8")
    assert f'BOUND_MARKER="{MARKER_OVERRIDE}"' in src, (
        "the tautological marker override is gone from the bound-test step; a "
        "bound node carrying an excluded marker now deselects silently"
    )
    assert (
        'suite_worker "$STEP_DIR/$step.out" "$STEP_DIR/$step.status" -m "$BOUND_MARKER" "$node"'
        in src
    ), "the executed bound-test invocation no longer carries the marker override"


def test_the_marker_override_actually_reaches_pytest(
    bound_repo: M8Repo, tmp_path: Path
) -> None:
    """The behavioural half of the assertion above."""
    log = tmp_path / "pytest-argv.log"
    _run_gate(
        bound_repo,
        bound_repo.branches["control"],
        bound_tests=(BOUND_NODE,),
        env_extra={"MERGE_GATE_TEST_PYTEST_LOG": str(log)},
    )
    invocations = _pytest_argv(log)
    assert invocations, f"the bound node was never handed to pytest at all: {log}"
    for argv in invocations:
        # The LAST `-m`, not the first: `-m pytest` selects the module and
        # `-m <expr>` selects the markers, and it is the second one that decides
        # whether the bound node is deselected into a silent green.
        assert argv.count("-m") >= 2, argv
        last_m = len(argv) - 1 - argv[::-1].index("-m")
        assert argv[last_m + 1] == MARKER_OVERRIDE, argv
        assert argv[-1] == BOUND_NODE, argv


@pytest.mark.parametrize(
    ("label", "rc", "out"),
    [
        ("skipped", "0", "1 skipped in 0.01s"),
        ("deselected", "0", "1 deselected in 0.01s"),
        ("no-tests-ran", "5", "no tests ran in 0.01s"),
        (
            "never-collected",
            "4",
            "ERROR: not found: tests/fixture/test_bound.py::test_the_finding_is_closed",
        ),
        ("no-summary-at-all", "0", ""),
    ],
)
def test_skipped_bound_test_is_a_failure_not_a_pass(
    bound_repo: M8Repo, label: str, rc: str, out: str
) -> None:
    """pytest exits 0 for "all passed" AND for "all skipped".

    So the exit code alone cannot tell a guard that HELD from a guard that was
    never ASKED, and every row here is a way a binding can be satisfied without
    anything being asserted. The last row is the nastiest: rc 0 with no summary
    line at all, where the only evidence of execution is its own absence.
    """
    result, receipt = _run_gate(
        bound_repo,
        bound_repo.branches["control"],
        bound_tests=(BOUND_NODE,),
        env_extra={"MERGE_GATE_TEST_BOUND_RC": rc, "MERGE_GATE_TEST_BOUND_OUT": out},
    )
    output = _output(result)
    print(f"NOT-EVALUABLE[{label}] rc={result.returncode}\n{output}")
    assert result.returncode != 0, (
        f"a bound test that never executed ({label}) was scored as a closure:\n{output}"
    )
    assert "MERGE GATE: PASS" not in output, output
    assert "NOT_EVALUABLE is not GREEN" in output, output
    # The gate imports its discrimination from the trusted checkout's
    # tests/counterfeits/harness.py. If that import ever fails the run degrades
    # to `unclassifiable` — which is also not-green, so the refusal above would
    # still fire and this whole table would stop measuring the real rule.
    assert "unclassifiable" not in output, (
        f"the settled NOT_EVALUABLE rule was never loaded; this row is grading "
        f"the degraded path instead:\n{output}"
    )
    assert receipt.get("bound_test_result") == "weakened", receipt
    entry = _step(receipt, "bound-test-1")
    assert entry is not None and entry["status"] == "failed", entry


def test_a_red_bound_test_is_red_not_weakened(bound_repo: M8Repo) -> None:
    """The two not-green states must stay distinguishable.

    "The fix does not close its finding" and "the binding was defeated" send an
    operator to different places, and collapsing them into one verdict is how a
    weakening gets triaged as an ordinary red.
    """
    result, receipt = _run_gate(
        bound_repo,
        bound_repo.branches["control"],
        bound_tests=(BOUND_NODE,),
        env_extra={
            "MERGE_GATE_TEST_BOUND_RC": "1",
            "MERGE_GATE_TEST_BOUND_OUT": "1 failed in 0.01s",
        },
    )
    output = _output(result)
    print(f"RED-BINDING rc={result.returncode}\n{output}")
    assert result.returncode != 0, output
    assert receipt.get("bound_test_result") == "red", receipt
    assert "does not close the finding it is bound to" in output, output


def test_a_run_that_never_reached_the_rerun_records_null_not_a_verdict(
    bound_repo: M8Repo,
) -> None:
    """A refusal ABOVE the re-run must leave ``bound_test_result`` null.

    ``null`` is "the re-run was never reached", not "the bound test was fine" —
    the same rule ``counterfeit_pool_workers`` states in its own comment. Here
    the gate refuses on a dirty workspace, which is decided long before any
    suite runs, and the binding still has to ride in the receipt so the caller
    can see WHICH closure claim went ungraded.
    """
    (bound_repo.path / "operator_left_this_here.txt").write_text("dirt\n", encoding="utf-8")
    try:
        result, receipt = _run_gate(
            bound_repo,
            bound_repo.branches["control"],
            bound_tests=(BOUND_NODE,),
            env_extra={"MERGE_GATE_PINNED": "1",
                       "OMNIAGENTOS_GATE_WORKSPACE": str(bound_repo.path)},
        )
    finally:
        (bound_repo.path / "operator_left_this_here.txt").unlink()
    output = _output(result)
    print(f"NEVER-REACHED rc={result.returncode}\n{output}")
    assert result.returncode != 0, output
    assert receipt.get("bound_test") == [BOUND_NODE], receipt
    assert receipt.get("bound_test_result") is None, (
        "a run that never reached the re-run recorded a verdict for it: "
        f"{receipt.get('bound_test_result')!r}"
    )


# ---------------------------------------------------------------------------
# E. the conftest route — the test file is not the only way to edit the test
# ---------------------------------------------------------------------------

_CONFTEST_SKIPPER = """import pytest


def pytest_collection_modifyitems(config, items):
    for item in items:
        item.add_marker(pytest.mark.skip(reason="not today"))
"""


@pytest.mark.parametrize(
    "conftest",
    ["conftest.py", "tests/conftest.py", "tests/fixture/conftest.py"],
)
def test_a_conftest_on_the_bound_tests_import_path_is_touching_the_test(
    bound_repo: M8Repo, conftest: str
) -> None:
    """THE SPOOF THE EXECUTED-PROOF ALONE DOES NOT CATCH.

    A candidate can leave the bound test file untouched and still defeat it from
    a ``conftest.py`` ANYWHERE above it — ``pytest_collection_modifyitems`` can
    skip the node, and a terminal-reporter hook can print a passing summary the
    gate then reads as evidence of execution. pytest loads every conftest from
    the rootdir down, so every level is a way in, and the root one is the
    cheapest. The conftest chain is part of the test; editing it is editing the
    test.
    """
    case = _branch_adding(
        bound_repo, f"conftest-{conftest.count('/')}", conftest, _CONFTEST_SKIPPER
    )
    result, receipt = _run_gate(bound_repo, case, bound_tests=(BOUND_NODE,))
    output = _output(result)
    print(f"CONFTEST[{conftest}] rc={result.returncode}\n{output}")
    assert result.returncode == 1, (
        f"adding {conftest} beside the bound test bought a pass:\n{output}"
    )
    assert "bound-test-untouched" in output, output
    assert conftest in output, f"the refusal does not name which conftest tripped it:\n{output}"
    assert receipt.get("bound_test_result") == "weakened", receipt


def test_an_unrelated_sibling_conftest_does_not_trip_the_check(
    bound_repo: M8Repo,
) -> None:
    """THE OVER-FIRE CONTROL, and it is the whole reason this walks the path
    rather than grepping for ``conftest.py``.

    ``tests/other/conftest.py`` is not on the bound test's import path — pytest
    never loads it for ``tests/fixture/test_bound.py`` — so refusing it would
    block honest work in an unrelated directory and teach everyone to stop
    passing bindings.
    """
    case = _branch_adding(
        bound_repo, "unrelated-conftest", "tests/other/conftest.py", _CONFTEST_SKIPPER
    )
    result, receipt = _run_gate(bound_repo, case, bound_tests=(BOUND_NODE,))
    output = _output(result)
    print(f"UNRELATED-CONFTEST rc={result.returncode}\n{output}")
    assert result.returncode == 0, (
        f"a conftest in a directory the bound test never imports was refused:\n{output}"
    )
    assert "MERGE GATE: PASS" in output, output
    assert receipt.get("bound_test_result") == "green", receipt


# ---------------------------------------------------------------------------
# E2. the pytest_plugins route — a conftest names a plugin, and the plugin is
#     an ordinary module that is NOT on the conftest walk (F6)
# ---------------------------------------------------------------------------


def test_a_declared_pytest_plugin_on_the_bound_path_is_part_of_the_test(
    bound_repo: M8Repo,
) -> None:
    """THE HOLE THE CONFTEST WALK ALONE DOES NOT CLOSE.

    ``tests/conftest.py`` declares ``pytest_plugins = ["tests.fixture.report_plugin"]``
    at the merge base, so pytest loads that ORDINARY module as a plugin whenever it
    collects the bound test. The module is not named ``conftest.py`` and is not on
    the walked conftest chain, yet it can register the very same
    ``pytest_runtest_makereport`` hook a conftest can — one that rewrites the bound
    node's report failed->passed. A candidate that edits ONLY that plugin module —
    leaving the test file and every conftest byte-identical — was, before this fix,
    a GREEN closure with the bug unfixed. It is editing the test by another route,
    so it must land in FAILURES (exit 1, candidate defect) and the refusal must name
    the plugin file.
    """
    case = _branch_adding(
        bound_repo, "weaponized-plugin", PLUGIN_MODULE, _WEAPONIZED_PLUGIN_SOURCE
    )
    result, receipt = _run_gate(bound_repo, case, bound_tests=(BOUND_NODE,))
    output = _output(result)
    print(f"DECLARED-PLUGIN rc={result.returncode}\n{output}")
    assert result.returncode == 1, (
        f"editing a pytest_plugins-declared module on the bound test's chain bought "
        f"a pass:\n{output}"
    )
    assert "bound-test-untouched" in output, output
    assert PLUGIN_MODULE in output, (
        f"the refusal does not name which declared plugin tripped it:\n{output}"
    )
    assert "MERGE GATE: PASS" not in output, output
    assert receipt.get("bound_test_result") == "weakened", receipt
    assert receipt.get("instrument_error") is None, (
        f"a candidate defect was labelled an instrument error, which excuses it: {receipt}"
    )


def test_a_plugin_not_declared_for_the_bound_test_is_not_part_of_it(
    bound_repo: M8Repo,
) -> None:
    """THE OVER-FIRE CONTROL, mirroring the unrelated-sibling-conftest guard.

    ``tests/fixture/other_helper.py`` looks like a plugin but is named by NO
    ``pytest_plugins`` on the bound test's chain, so pytest never loads it as a
    plugin for this test. Refusing it would block honest work on ordinary modules
    that merely share a directory with the test and teach everyone to stop passing
    bindings. Only the DECLARED modules are part of the test surface.
    """
    case = _branch_adding(
        bound_repo, "undeclared-module", UNDECLARED_MODULE, "HELPER = 1\n"
    )
    result, receipt = _run_gate(bound_repo, case, bound_tests=(BOUND_NODE,))
    output = _output(result)
    print(f"UNDECLARED-MODULE rc={result.returncode}\n{output}")
    assert result.returncode == 0, (
        f"a module not declared via pytest_plugins for this test was refused:\n{output}"
    )
    assert "MERGE GATE: PASS" in output, output
    assert receipt.get("bound_test_result") == "green", receipt


# ---------------------------------------------------------------------------
# F. a binding that cannot be compared is worse than no binding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "binding"),
    [
        ("dot-slash-prefixed", f"./{BOUND_NODE}"),
        ("absolute", f"/{BOUND_NODE}"),
        ("case-variant", "tests/Fixture/test_bound.py::test_the_finding_is_closed"),
        ("directory-form", "tests/fixture"),
        ("file-form-no-test", BOUND_FILE),
        ("never-existed", "tests/fixture/test_typo.py::test_nope"),
    ],
)
def test_a_non_canonical_binding_refuses_at_exit_2(
    bound_repo: M8Repo, label: str, binding: str
) -> None:
    """VACUOUS PASSES ARE THE FAILURE MODE HERE, not false refusals.

    ``CHANGED_PATHS`` holds canonical repo-relative paths, so ``grep -Fx``
    against ``./tests/x.py``, a case variant or a bare directory matches NOTHING
    and the untouched check prints ``ok`` having compared nothing at all. That is
    strictly worse than passing no binding, because it looks like closure. A
    caller error is exit 2 ("the gate cannot judge this input"), never exit 1
    ("the gate judged the candidate and the answer is no") — and the listing it
    is checked against comes from the MERGE BASE, so a candidate cannot turn its
    own binding into a usage error by deleting the file.
    """
    result, _ = _run_gate(bound_repo, bound_repo.branches["control"], bound_tests=(binding,))
    output = _output(result)
    print(f"NON-CANONICAL[{label}] rc={result.returncode}\n{output}")
    assert result.returncode == 2, (
        f"a binding the gate cannot compare ({label}) was not refused as a "
        f"caller error:\n{output}"
    )
    assert "bad-bound-test" in output, output
    assert "MERGE GATE: PASS" not in output, output


@pytest.mark.parametrize("value", ["   ", "\t", " \n "])
def test_a_whitespace_only_binding_is_the_same_as_no_value(
    tmp_path: Path, value: str
) -> None:
    """A wrapper that quoted an empty field must not buy an unbound run.

    Untrimmed, ``" "`` is a perfectly good non-empty string: it survives the
    ``''|-*`` refusal, rides into the receipt as a node id, and no pytest can
    ever run it.
    """
    result = subprocess.run(
        ["bash", str(MERGE_GATE), "--bound-test", value],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    output = _output(result)
    assert result.returncode == 2, output
    assert "--bound-test needs a node id" in output, output


def test_a_binding_is_trimmed_before_it_reaches_the_receipt(bound_repo: M8Repo) -> None:
    """The other half of the trim: a surviving value must be the canonical one.

    A node id with leading whitespace would fail the canonical-path check for a
    reason that has nothing to do with the caller's intent.
    """
    result, receipt = _run_gate(
        bound_repo, bound_repo.branches["control"], bound_tests=(f"  {BOUND_NODE}  ",)
    )
    assert receipt.get("bound_test") == [BOUND_NODE], receipt
    assert result.returncode == 0, _output(result)


# ---------------------------------------------------------------------------
# G. a closure proof is never cached, and never silently un-writable
# ---------------------------------------------------------------------------


def test_the_bound_step_is_never_receipt_cached(bound_repo: M8Repo) -> None:
    """ARMED step receipts must leave the bound step alone, and say nothing.

    The step ids a receipt may carry are enumerated in
    ``gate_evidence.MERGE_GATE_STEP_NAMES``; ``bound-test-*`` is not among them,
    so a ``record_step_receipt`` call here would REFUSE with "unknown merge-gate
    step" and print a failure line on every GREEN production run — an error
    message on the success path is how a step gets quietly disarmed later.

    Not caching is also the right answer independently: the other steps buy back
    ~12 minutes, a single named node costs seconds, and a cached closure proof is
    exactly the artifact this feature exists to make impossible.
    """
    result, receipt = _run_gate(
        bound_repo,
        bound_repo.branches["control"],
        bound_tests=(BOUND_NODE,),
        env_extra={"MERGE_GATE_STEP_RECEIPTS": "1"},
    )
    output = _output(result)
    print(f"STEP-RECEIPTS-ARMED rc={result.returncode}\n{output}")
    assert result.returncode == 0, output
    assert "MERGE GATE: PASS" in output, output
    assert receipt.get("bound_test_result") == "green", receipt
    assert "record failed for bound-test" not in output, (
        f"the bound step tried to mint a receipt the store refuses:\n{output}"
    )
    assert "unknown merge-gate step" not in output, output
    entry = _step(receipt, "bound-test-1")
    assert entry is not None and entry["status"] == "ok", (
        f"the binding was re-proven, so its step must be ok (never 'reused'): {entry}"
    )
    assert not [
        p
        for p in (bound_repo.evidence_root / "records" / "merge-gate").glob("*.json")
        if "bound-test" in p.read_text(encoding="utf-8")
        and p.read_text(encoding="utf-8").count('"step"') > 0
        and '"step": "bound-test' in p.read_text(encoding="utf-8")
    ], "a bound-test step receipt reached the durable evidence store"


def test_an_unloadable_rule_gets_its_own_slug_and_is_not_green(
    bound_repo: M8Repo,
) -> None:
    """FAIL CLOSED, AND SAY WHICH THING BROKE.

    The gate imports its "did this actually execute?" rule from the trusted
    checkout. If that import fails there is deliberately no fallback copy — a
    fallback is how two definitions drift — so the run must refuse. It gets its
    OWN slug because every other ``bound-test`` failure is a statement about the
    CANDIDATE and this one is a statement about the gate, and an operator should
    not have to parse prose to tell those apart.
    """
    (bound_repo.path / "tests" / "counterfeits" / "harness.py").write_text(
        'raise ImportError("the rule is unavailable in this checkout")\n', encoding="utf-8"
    )
    result, receipt = _run_gate(
        bound_repo, bound_repo.branches["control"], bound_tests=(BOUND_NODE,)
    )
    output = _output(result)
    print(f"UNCLASSIFIABLE rc={result.returncode}\n{output}")
    assert result.returncode != 0, (
        f"the gate could not tell whether the bound test ran, and passed it:\n{output}"
    )
    assert "bound-test-unclassifiable" in output, output
    assert "MERGE GATE: PASS" not in output, output
    assert receipt.get("bound_test_result") == "weakened", receipt
