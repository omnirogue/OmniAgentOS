"""``contracts`` width, the reap-race SERIAL split, and the JUnit failure trail.

Three properties land here.

**Width.** The step that runs ``tests/contracts`` was serial by construction:
484s of a ~570s gate, ~70% of the wall clock. Measured on this Mac,
``-n 8 --dist loadfile`` runs the identical surface in 91-93s with zero failures
over five consecutive runs. The flags come from ``MERGE_GATE_SUITE_WORKERS``,
defaulting to ``MERGE_GATE_LADDER_WORKERS`` because every real caller already
exports that one (``pipeline/bridge/gate_loop.py``,
``pipeline/bridge/integration.py``).

**The reap-race SERIAL split (2026-08-12).** ``tests/scripts`` (its merge-gate
suites spawn the real ``merge-gate.sh``) and ``tests/scheduler`` drive the real
gate's ``os.killpg`` process-group reap. Under ``-n`` a sibling xdist worker can
exit before the pgroup leader is signalled, ``killpg`` returns EPERM, and the
run raises ``GateExecutionInfraError`` — false-refusing the WHOLE train on a
failure that does not reproduce standalone (~25% of runs). So ``tests/scripts``
was split OUT of the (now ``contracts``-only) parallel leg into its own SERIAL
step, and ``tests/scheduler`` was lifted OUT of the xdist ladder into its own
SERIAL step. Coverage is PRESERVED — same tests, still counted — but they never
run under ``-n``, so the cross-worker race cannot occur.

What has to be TRUE, not merely intended:

* the width the process uses is the width the step-receipt command string
  claims — otherwise a serial receipt could certify a parallel run;
* ``--dist loadfile``, never ``worksteal`` on the parallel ``contracts`` leg:
  ``tests/conftest.py`` pins one session ledger/vault root per worker, and only
  loadfile gives a stable file->worker map;
* the SERIAL legs (``scheduler``, ``scripts``) are NEVER handed ``-n``, however
  wide either width knob is set — a ``-n`` on either is the reap race back;
* neither reap-driving suite was DROPPED: ``tests/scheduler`` left the ladder
  argv and ``tests/scripts`` left the contracts argv, but each still runs once
  as its own serial step;
* a NESTED gate stays serial no matter what any caller asks for;
* unset knobs reproduce each leg's serial argument list byte for byte.

**JUnit.** ``pytest -q`` names failures on stdout and the gate echoes five of
them, which is enough for a reproducible failure and useless for a flake: one
unnamed failure ate five candidates because nothing durable recorded WHICH node
it was. ``suite_worker`` could already emit JUnit; nothing set
``MERGE_GATE_JUNIT_DIR``, so it never did. It now defaults into ``$SCRATCH``,
and a step that FAILS has its XML copied next to the receipts with the path in
the run receipt. Green runs must leave nothing — the retention rule the ladder
capture already uses.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from tests.scripts.test_merge_gate_absent_suite_guards import (
    _build_repo,
    _gate_env,
    _run_or_contain,
    _step,
)
from tests.scripts.test_merge_gate_m8_refusals import (
    MERGE_GATE,
    REAL_PYTHON,
    REPO_ROOT,
    M8Repo,
    _output,
)

# A fake interpreter that RECORDS every pytest argv it is handed, and can be
# told to fail exactly the contracts step. Both channels are separate
# on purpose, the same way the M8 harness splits MERGE_GATE_TEST_CF_RC from
# MERGE_GATE_TEST_CF_REPORT: a gate must never be able to pass on a clean
# summary line from a run that exited non-zero, and a test must never be able to
# prove "the flags were passed" from a stub that ignored them.
_ARGV_STUB = """#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "omniagentos.scheduler.gate_evidence" ]; then
  PYTHONPATH={source_root} exec {real_python} "$@"
fi
if [ "$1" = "-m" ] && [ "$2" = "tests.counterfeits.harness" ]; then
  printf 'COUNTERFEIT CORPUS REPORT\\n'
  printf 'CAUGHT    cf-fixture\\n'
  printf -- '------------------------------------------------------------\\n'
  printf 'total=1  caught=1  survived=0  skipped_platform=0  other=0\\n'
  exit 0
fi
if [ "$1" = "-c" ]; then
  printf '%s/omniagentos/__init__.py' "$PWD"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "ruff" ]; then
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  shift 2
  printf '%s\\n' "$*" >> {argv_log}
  _junit=""
  _is_contracts=0
  for _arg in "$@"; do
    case "$_arg" in
      --junitxml=*) _junit="${{_arg#--junitxml=}}" ;;
      tests/contracts/) _is_contracts=1 ;;
    esac
  done
  if [ "$_is_contracts" = "1" ] && [ -n "${{MG_FAKE_FAIL_CONTRACTS:-}}" ]; then
    if [ -n "$_junit" ]; then
      printf '%s' '<?xml version="1.0" encoding="utf-8"?><testsuite name="pytest" tests="653" failures="1" errors="0"><testcase classname="tests.contracts.test_flaky_node" name="test_the_one_that_flaked"><failure message="boom">AssertionError</failure></testcase></testsuite>' > "$_junit"
    fi
    printf 'FAILED tests/contracts/test_flaky_node.py::test_the_one_that_flaked\\n'
    printf '1 failed, 652 passed in 90.00s\\n'
    exit 1
  fi
  if [ -n "$_junit" ]; then
    printf '%s' '<?xml version="1.0" encoding="utf-8"?><testsuite name="pytest" tests="1" failures="0" errors="0"><testcase classname="fake" name="test_fixture"/></testsuite>' > "$_junit"
  fi
  printf '1 passed in 0.01s\\n'
  exit 0
fi
exec {real_python} "$@"
"""


def _install_argv_stub(repo: Path, argv_log: Path) -> None:
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(
        _ARGV_STUB.format(
            source_root=shlex.quote(str(REPO_ROOT)),
            real_python=shlex.quote(str(REAL_PYTHON)),
            argv_log=shlex.quote(str(argv_log)),
        ),
        encoding="utf-8",
    )
    python.chmod(0o755)


@pytest.fixture
def width_repo(tmp_path: Path) -> tuple[M8Repo, Path]:
    """An M8 fixture repo WITH the guarded suite dirs, plus an argv log."""
    fixture = _build_repo(tmp_path)
    argv_log = tmp_path / "pytest-argv.log"
    _install_argv_stub(fixture.path, argv_log)
    return fixture, argv_log


#: The ceiling every node in this file runs under, STATED rather than measured.
#:
#: ``clamp_workers`` bounds every requested width by the host's performance-core
#: count, so an asserted width of 3 or 4 is only reachable on a wide box: on
#: GitHub's 2-core ubuntu runners the gate correctly logged
#: ``cf-pool-workers clamped 4 -> 2 (depth 0, 2 perf cores)`` and the two width
#: nodes below went red on EVERY PR while passing on this Mac. A test whose
#: verdict depends on the width of the machine it happens to run on is not
#: testing the width knob.
#:
#: ``MERGE_GATE_MAX_WORKERS`` is the gate's own explicit-ceiling input
#: (merge-gate.sh:585) and it is already how
#: ``test_a_width_beyond_the_host_is_clamped_and_the_clamp_is_visible`` makes
#: its clamp deterministic — same instrument, opposite direction. Stating it
#: here changes NOTHING about the production clamp: depth still collapses the
#: ceiling to 1 (``test_a_nested_gate_runs_this_step_serial_however_it_is_asked``
#: passes ``MERGE_GATE_DEPTH`` through ``extra`` and still gets a serial step),
#: and a case that wants to observe a clamp can override this key the same way.
#: It is the third sibling of the STATE-THE-INTERPRETER / STATE-THE-MODE /
#: STATE-THE-DEPTH rules in ``_gate_env``: a host property an assertion depends
#: on is an INPUT, and inputs are stated.
_STATED_CEILING = "8"


def _run(
    fixture: M8Repo,
    *,
    extra: dict[str, str] | None = None,
) -> tuple[object, dict]:
    case = fixture.branches["control"]
    emit = fixture.path.parent / f"width-receipt-{case.candidate_sha[:12]}.json"
    env_extra = {"MERGE_GATE_MAX_WORKERS": _STATED_CEILING, **(extra or {})}
    result = _run_or_contain(
        ["bash", str(MERGE_GATE), "--emit-receipt", str(emit), case.name],
        cwd=fixture.path,
        env=_gate_env(fixture, pinned=True, extra=env_extra),
    )
    receipt = json.loads(emit.read_text(encoding="utf-8")) if emit.exists() else {}
    return result, receipt


def _leg_argv(argv_log: Path, needle: str, *, label: str) -> str:
    """The one recorded pytest argv whose target list contains ``needle``.

    ``--junitxml=`` is dropped from the returned string: it carries an absolute
    tmp path (which can itself contain the word "worksteal" when pytest names the
    tmpdir after the test), and — more importantly — it is added inside
    ``suite_worker`` rather than by ``run_suite``, so it is deliberately NOT part
    of the command string the step receipt binds. Comparing it here would assert
    the opposite of ``test_junit_is_not_part_of_the_receipt_bound_command``.

    Since 2026-08-12 the reap-driving suites each get their OWN invocation, so a
    per-leg needle finds exactly one line: ``tests/scripts/`` and
    ``tests/scheduler/`` are their own serial steps, distinct from the parallel
    ``tests/contracts/`` leg and from each other.
    """
    assert argv_log.is_file(), "the stub interpreter was never invoked for pytest"
    lines = [
        line
        for line in argv_log.read_text(encoding="utf-8").splitlines()
        if f" {needle}" in f" {line}"
    ]
    assert len(lines) == 1, (
        f"expected exactly one {label} pytest invocation, got {len(lines)}:\n"
        + "\n".join(lines)
    )
    return " ".join(
        word for word in lines[0].split() if not word.startswith("--junitxml=")
    )


def _contracts_argv(argv_log: Path) -> str:
    """The PARALLEL contracts leg (tests/contracts/) — the one that carries width."""
    return _leg_argv(argv_log, "tests/contracts/", label="contracts")


def _scripts_argv(argv_log: Path) -> str:
    """The SERIAL scripts step (tests/scripts/) — must never carry ``-n``."""
    return _leg_argv(argv_log, "tests/scripts/", label="scripts")


def _scheduler_argv(argv_log: Path) -> str:
    """The SERIAL scheduler step (tests/scheduler/) — must never carry ``-n``."""
    return _leg_argv(argv_log, "tests/scheduler/", label="scheduler")


def _ladder_argv(argv_log: Path) -> str:
    return _leg_argv(argv_log, "tests/api/", label="ladder")


# ---------------------------------------------------------------------------
# A. the width reaches the process, and only the process it was meant for
# ---------------------------------------------------------------------------


def test_unset_knobs_reproduce_todays_serial_argument_list(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """Absence renders as today's behaviour, never as a widened default.

    Each leg's argument list is what run_suite binds into its step-receipt
    command string; with the knobs unset every leg renders exactly serial —
    ``contracts`` no longer carries its old ``tests/scripts/`` companion (that
    is now its own serial step), so this is also the record that the split gave
    each leg a distinct, stable serial key.
    """
    fixture, argv_log = width_repo
    _run(fixture)

    assert _contracts_argv(argv_log) == "-q tests/contracts/", _contracts_argv(argv_log)
    assert _scripts_argv(argv_log) == "-q tests/scripts/", _scripts_argv(argv_log)
    assert _scheduler_argv(argv_log) == "-q tests/scheduler/", _scheduler_argv(argv_log)


def test_the_suite_width_reaches_the_contracts_leg_but_not_scripts(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """The width reaches the PARALLEL contracts leg — and MUST NOT reach the
    serial scripts step, whose whole reason to exist is to avoid ``-n``."""
    fixture, argv_log = width_repo
    _, receipt = _run(fixture, extra={"MERGE_GATE_SUITE_WORKERS": "2"})

    argv = _contracts_argv(argv_log)
    assert "-n 2 --dist loadfile" in argv, argv
    # BEFORE the targets, exactly as the ladder spells it — the command string
    # is a receipt key, so its word order is part of the contract.
    assert argv == "-q -n 2 --dist loadfile tests/contracts/", argv
    # EFFECTIVE, i.e. post-clamp, and therefore the width the receipt claims.
    assert receipt.get("suite_workers") == 2, receipt.get("suite_workers")
    # The serial scripts step is handed the SAME width knob and must ignore it:
    # a `-n` here is the killpg cross-worker reap race the split removed.
    scripts = _scripts_argv(argv_log)
    assert "-n" not in scripts.split(), scripts
    assert scripts == "-q tests/scripts/", scripts


def test_the_width_defaults_to_the_ladders_because_that_is_what_callers_set(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """Every real caller exports MERGE_GATE_LADDER_WORKERS=8 and nothing else.

    A brand-new variable would have shipped this step still serial until three
    other files were changed to agree, which is the shape of a fix that is
    present in the tree and absent in production.
    """
    fixture, argv_log = width_repo
    _, receipt = _run(fixture, extra={"MERGE_GATE_LADDER_WORKERS": "3"})

    assert "-n 3 --dist loadfile" in _contracts_argv(argv_log)
    assert receipt.get("suite_workers") == 3


def test_an_explicit_suite_width_overrides_the_ladders(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """The two are separable — bisecting one must not serialise the other."""
    fixture, argv_log = width_repo
    _run(
        fixture,
        extra={"MERGE_GATE_LADDER_WORKERS": "4", "MERGE_GATE_SUITE_WORKERS": "2"},
    )

    assert "-n 2 --dist loadfile" in _contracts_argv(argv_log)
    assert "-n 4 --dist loadfile" in _ladder_argv(argv_log)


def test_worksteal_is_never_what_this_step_is_handed(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """``--dist loadfile``, never ``worksteal``.

    tests/conftest.py pins ONE session ledger/vault root per worker process, so
    worksteal makes the test->worker partition nondeterministic run to run and a
    green becomes unreproducible. It is also what keeps every test of
    test_refresh_contracts.py — the only file in either directory that rewrites
    the checked-out contracts/ artifacts — on one worker.
    """
    fixture, argv_log = width_repo
    _run(fixture, extra={"MERGE_GATE_SUITE_WORKERS": "8"})

    argv = _contracts_argv(argv_log)
    assert "worksteal" not in argv, argv
    assert "--dist loadfile" in argv, argv


def test_a_width_of_one_runs_genuinely_serial_rather_than_forking_one_worker(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """``-n 1`` forks a worker to buy no parallelism; it is not spelled at all."""
    fixture, argv_log = width_repo
    _run(fixture, extra={"MERGE_GATE_SUITE_WORKERS": "1"})

    argv = _contracts_argv(argv_log)
    assert "-n" not in argv.split(), argv
    assert "--dist" not in argv, argv


def test_a_nested_gate_runs_this_step_serial_however_it_is_asked(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """Depth collapses the ceiling to 1, and the ceiling is what is applied.

    Load-bearing for this very suite: the merge-gate fixture tests live in
    tests/scripts (its own serial step since 2026-08-12), so every fixture gate
    they start is a nested gate, and its OWN parallel contracts leg must collapse
    to serial. If depth did not clamp, a gate run would fan out multiplicatively.
    """
    fixture, argv_log = width_repo
    _, receipt = _run(
        fixture,
        extra={"MERGE_GATE_SUITE_WORKERS": "8", "MERGE_GATE_DEPTH": "1"},
    )

    argv = _contracts_argv(argv_log)
    assert "-n" not in argv.split(), argv
    assert receipt.get("suite_workers") == 1, receipt.get("suite_workers")


def test_the_workers_blank_the_suite_width_for_their_children(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """A child gate must DERIVE its width, never inherit the parent's.

    The depth marker is the belt and this blanking is the braces — the comment
    at the ceiling block says all three width knobs, so all three must be there.
    """
    fixture, _ = width_repo
    source = MERGE_GATE.read_text(encoding="utf-8")
    blanked = [
        line
        for line in source.splitlines()
        if "MERGE_GATE_LADDER_WORKERS=" in line and "MERGE_GATE_CF_POOL_WORKERS=" in line
    ]
    assert len(blanked) == 2, f"expected both workers to blank the widths: {blanked}"
    for line in blanked:
        assert "MERGE_GATE_SUITE_WORKERS=" in line, (
            "a worker blanks the ladder and counterfeit widths but not the "
            f"contracts one, so a nested gate inherits it: {line.strip()}"
        )


@pytest.mark.parametrize("bad", ["0", "-1", "two", "2.5", " "])
def test_a_nonsense_width_refuses_instead_of_guessing(
    width_repo: tuple[M8Repo, Path], bad: str
) -> None:
    """And it refuses BEFORE the ladder is backgrounded.

    `refuse` exits the script; a refusal after the ladder's `&` would orphan a
    pytest process holding the scratch worktree the EXIT trap is about to
    remove. That is why the validation sits beside the other two width blocks.
    """
    fixture, _ = width_repo
    result, _receipt = _run(fixture, extra={"MERGE_GATE_SUITE_WORKERS": bad})

    assert result.returncode != 0, _output(result)
    assert "bad-suite-workers" in _output(result), _output(result)


# ---------------------------------------------------------------------------
# B. a failing test names itself in the durable evidence
# ---------------------------------------------------------------------------


def test_a_failing_suite_leaves_a_junit_file_that_names_the_node(
    width_repo: tuple[M8Repo, Path],
) -> None:
    fixture, _ = width_repo
    result, receipt = _run(fixture, extra={"MG_FAKE_FAIL_CONTRACTS": "1"})

    assert result.returncode != 0, _output(result)
    entry = _step(receipt, "contracts")
    assert entry is not None and entry["status"] == "failed", entry

    kept = receipt.get("junit_dir")
    assert kept, (
        "the contracts step failed and no JUnit was kept — the whole "
        "point is that the next run should not have to rediscover the node id"
    )
    xml = Path(kept) / "contracts.xml"
    assert xml.is_file(), sorted(p.name for p in Path(kept).iterdir())
    body = xml.read_text(encoding="utf-8")
    assert "test_the_one_that_flaked" in body, body[:400]


def test_a_green_run_keeps_no_junit_at_all(width_repo: tuple[M8Repo, Path]) -> None:
    """Same retention rule as the ladder capture: failures only.

    A durable XML per suite per gate run would be a new unbounded artifact class
    bought for output nobody reads.
    """
    fixture, _ = width_repo
    result, receipt = _run(fixture)

    assert result.returncode == 0, _output(result)
    assert receipt.get("junit_dir") is None, receipt.get("junit_dir")
    records = fixture.evidence_root / "records" / "merge-gate"
    kept = [p for p in records.glob("*.junit-*")] if records.is_dir() else []
    assert not kept, f"a green run left JUnit behind: {kept}"


def test_junit_is_not_part_of_the_receipt_bound_command() -> None:
    """``--junitxml`` is armed inside ``suite_worker``, never by ``run_suite``.

    ``run_suite`` builds ``cmd`` from its own arguments and that string is the
    step-receipt key. If the JUnit flag went in there, its path — which contains
    a PID and a scratch directory — would change every run, so no receipt could
    ever be reused and every gate would re-run every suite. Evidence must not
    move a key that execution shape owns.
    """
    source = MERGE_GATE.read_text(encoding="utf-8")
    cmd_line = [
        line for line in source.splitlines() if 'local cmd="python -m pytest -q' in line
    ]
    assert len(cmd_line) == 1, cmd_line
    assert "junit" not in cmd_line[0].lower(), cmd_line[0]
    assert "--junitxml=" in source, "the JUnit flag vanished from the script"


def test_junit_is_evidence_only_and_an_unwritable_dir_never_fails_a_suite(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """An instrument error must never be reported as a candidate defect.

    Pytest exits non-zero at sessionfinish if it cannot write ``--junitxml``,
    and report_suite would score that as the candidate's suite failing. So the
    flag is armed only after the directory is proven writable; point it
    somewhere impossible and the gate must still pass.
    """
    fixture, argv_log = width_repo
    result, receipt = _run(
        fixture,
        extra={"MERGE_GATE_JUNIT_DIR": "/dev/null/not-a-directory"},
    )

    assert result.returncode == 0, _output(result)
    assert "--junitxml" not in _contracts_argv(argv_log)
    assert receipt.get("junit_dir") is None


# ---------------------------------------------------------------------------
# C. the reap-driving suites left the parallel legs — coverage preserved, but
#    SERIAL, so the os.killpg cross-worker race cannot occur (2026-08-12)
# ---------------------------------------------------------------------------
#
# tests/scheduler and tests/scripts drive the real gate's process-group reap
# (os.killpg). Run under `-n`, a sibling xdist worker can exit before the pgroup
# leader is signalled -> EPERM -> GateExecutionInfraError, which false-refused
# the whole train (~25% of runs) on a failure that never reproduced standalone.
# The fix moves each to its OWN serial step; these tests pin that the move (a)
# actually removed them from the parallel legs and (b) did not drop them.


def test_scheduler_left_the_xdist_ladder_for_a_serial_step(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """tests/scheduler must be GONE from the ladder argv and run as its own
    serial step — present and counted, but never under ``-n`` — even when the
    caller asks for a wide ladder."""
    fixture, argv_log = width_repo
    _run(fixture, extra={"MERGE_GATE_LADDER_WORKERS": "8"})

    ladder = _ladder_argv(argv_log)
    assert "tests/scheduler/" not in ladder, (
        f"tests/scheduler is still inside the xdist ladder — the reap race is "
        f"back: {ladder}"
    )
    scheduler = _scheduler_argv(argv_log)
    assert "-n" not in scheduler.split(), scheduler
    assert scheduler == "-q tests/scheduler/", scheduler


def test_scripts_left_the_contracts_leg_for_a_serial_step(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """tests/scripts must be GONE from the parallel contracts leg and run as its
    own serial step, even at a wide suite width — its merge-gate suites spawn the
    real gate, which is exactly what drives the killpg reap."""
    fixture, argv_log = width_repo
    _run(fixture, extra={"MERGE_GATE_SUITE_WORKERS": "8"})

    contracts = _contracts_argv(argv_log)
    assert "tests/scripts/" not in contracts, (
        f"tests/scripts is still inside the parallel contracts leg — the reap "
        f"race is back: {contracts}"
    )
    scripts = _scripts_argv(argv_log)
    assert "-n" not in scripts.split(), scripts
    assert scripts == "-q tests/scripts/", scripts


def test_no_reap_split_suite_is_ever_handed_xdist(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """The whole point of the split: NEITHER serial leg gets ``-n``, no matter
    how wide either knob is set. A ``-n`` on either is the cross-worker reap race
    this change exists to remove."""
    fixture, argv_log = width_repo
    _run(
        fixture,
        extra={"MERGE_GATE_LADDER_WORKERS": "8", "MERGE_GATE_SUITE_WORKERS": "8"},
    )
    for argv in (_scheduler_argv(argv_log), _scripts_argv(argv_log)):
        assert "-n" not in argv.split(), argv
        assert "--dist" not in argv, argv


def test_the_split_dropped_no_suite_from_the_gate(
    width_repo: tuple[M8Repo, Path],
) -> None:
    """COVERAGE LEDGER. Every directory the gate covered before the split still
    runs after it: the ladder keeps its six dirs, and scheduler / contracts /
    scripts each run as their own step. The reap-race fix moved suites between
    carriers; it removed none. This is the guard that a future "just drop the
    flaky suite" shortcut cannot masquerade as this fix."""
    fixture, argv_log = width_repo
    _run(
        fixture,
        extra={"MERGE_GATE_LADDER_WORKERS": "8", "MERGE_GATE_SUITE_WORKERS": "8"},
    )

    ladder = _ladder_argv(argv_log)
    for target in (
        "tests/api/",
        "tests/swarm/",
        "tests/sessions/",
        "tests/db/",
        "tests/reflection/",
        "tests/selfimprove/",
    ):
        assert target in ladder, f"{target} was dropped from the ladder: {ladder}"
    # tests/scheduler is NOT in the ladder any more — it is its own serial step.
    assert "tests/scheduler/" not in ladder, ladder
    # The three suites pulled out of the parallel legs each still run exactly
    # once (the per-leg helpers assert len == 1), and in the expected shape.
    assert _scheduler_argv(argv_log) == "-q tests/scheduler/"
    assert _contracts_argv(argv_log).endswith(" tests/contracts/")
    assert _scripts_argv(argv_log) == "-q tests/scripts/"
