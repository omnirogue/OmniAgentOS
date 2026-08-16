"""M8 acceptance and per-refusal revert checks for ``merge-gate.sh``."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.scheduler.gate_evidence import (
    SCHEMA,
    GateEvidence,
    GateEvidenceStore,
    binding_digest,
    workspace_digest_for,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_GATE = REPO_ROOT / "scripts" / "merge-gate.sh"
# The counterfeit harness materialises the tree WITHOUT .venv, so a corpus entry
# that names these tests would otherwise exec an interpreter that is not there.
# The running interpreter is the same venv; falling back to it keeps this file
# usable as a counterfeit must_fail target.
_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
REAL_PYTHON = _VENV_PYTHON if _VENV_PYTHON.is_file() else Path(sys.executable)


@dataclass(frozen=True)
class FixtureBranch:
    name: str
    candidate_sha: str
    merge_base_sha: str
    refusal: str | None
    reason: str | None


@dataclass(frozen=True)
class M8Repo:
    path: Path
    evidence_root: Path
    branches: dict[str, FixtureBranch]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _install_fake_python(repo: Path) -> None:
    """Use the real receipt verifier and make the unrelated gate ladder instant.

    The counterfeit harness is faked too, and INDEPENDENTLY in its two channels:
    ``MERGE_GATE_TEST_CF_RC`` is the exit status it returns, ``MERGE_GATE_TEST_CF_REPORT``
    is the summary line it prints. Splitting them is the whole point — the gate
    must not be able to pass on a clean-looking line from a harness that failed.
    """
    python = repo / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    source_root = shlex.quote(str(REPO_ROOT))
    real_python = shlex.quote(str(REAL_PYTHON))
    python.write_text(
        f"""#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "omniagentos.scheduler.gate_evidence" ]; then
  PYTHONPATH={source_root} exec {real_python} "$@"
fi
if [ "$1" = "-m" ] && [ "$2" = "tests.counterfeits.harness" ]; then
  printf 'COUNTERFEIT CORPUS REPORT\\n'
  printf 'CAUGHT    cf-fixture\\n'
  printf -- '------------------------------------------------------------\\n'
  printf '%s\\n' "${{MERGE_GATE_TEST_CF_REPORT:-total=1  caught=1  survived=0  skipped_platform=0  other=0}}"
  exit "${{MERGE_GATE_TEST_CF_RC:-0}}"
fi
if [ "$1" = "-c" ]; then
  printf '%s/omniagentos/__init__.py' "$PWD"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  printf '1 passed in 0.01s\\n'
  # Honour --junitxml the way real pytest does: the gate's suite_worker now
  # passes it when MERGE_GATE_JUNIT_DIR is set, and a stub that silently
  # swallows the flag makes every emission assertion vacuously red.
  for _arg in "$@"; do
    case "$_arg" in
      --junitxml=*)
        printf '<?xml version="1.0" encoding="utf-8"?><testsuite name="fake" tests="1" failures="0" errors="0"><testcase classname="fake" name="test_fixture"/></testsuite>' > "${{_arg#--junitxml=}}"
        ;;
    esac
  done
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "ruff" ]; then
  exit 0
fi
exec {real_python} "$@"
""",
        encoding="utf-8",
    )
    python.chmod(0o755)


def _commit_file(
    repo: Path,
    branch: str,
    relative: str,
    content: str,
    *,
    force: bool = False,
) -> str:
    _git(repo, "checkout", "-b", branch, "main")
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    add_args = ("add", "-f", relative) if force else ("add", relative)
    _git(repo, *add_args)
    _git(repo, "commit", "-m", f"fixture: {branch}")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    return sha


def _receipt(case: FixtureBranch, repo: Path) -> GateEvidence:
    command = "anthropic-review candidate"
    targets = ("candidate",)
    workspace_digest = workspace_digest_for(repo)
    finished = datetime.now(UTC)
    candidate = case.candidate_sha
    return GateEvidence(
        schema=SCHEMA,
        routine_id="merge-gate",
        run_id=candidate,
        iteration=1,
        gate_type="merge_candidate",
        command=command,
        targets=targets,
        workspace_digest=workspace_digest,
        binding_digest=binding_digest(
            routine_id="merge-gate",
            run_id=candidate,
            iteration=1,
            gate_type="merge_candidate",
            command=command,
            targets=targets,
            workspace_digest=workspace_digest,
            candidate_sha=candidate,
            merge_base_sha=case.merge_base_sha,
        ),
        # Merge receipts are pytest evidence by contract — see
        # gate_evidence.MERGE_GATE_TOOL. Kept valid so the refusal each case
        # asserts stays the OPERATIVE one.
        tool="pytest",
        tool_version="8.3.2",
        exit_code=0,
        checks_collected=1,
        checks_passed=1,
        checks_skipped=0,
        checks_failed=0,
        started_at=(finished - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        finished_at=finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        nonce=hashlib.sha256(case.name.encode()).hexdigest()[:32],
        workspace_sha=candidate,
        workspace_tree_clean=True,
        interpreter=str(REAL_PYTHON),
        interpreter_version="3.12",
        node_inventory_digest="0" * 64,
        deselected_count=0,
        candidate_sha=candidate,
        merge_base_sha=case.merge_base_sha,
    )


@pytest.fixture
def m8_repo(tmp_path: Path) -> M8Repo:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "M8 Gate Test")
    _git(repo, "config", "user.email", "m8-gate@example.com")
    (repo / ".gitignore").write_text(".venv\nnode_modules\nvar\n", encoding="utf-8")
    (repo / "ARCHI.md").write_text("main architecture\n", encoding="utf-8")
    (repo / "WORKBOOK.md").write_text("shared workbook\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    reachability = repo / "scripts" / "reachability-gate.py"
    reachability.parent.mkdir(parents=True, exist_ok=True)
    reachability.write_bytes((REPO_ROOT / "scripts" / "reachability-gate.py").read_bytes())
    reachability.chmod(0o755)
    # The gate runs its counterfeit step only when this directory exists in the
    # trial-merge worktree; the harness itself is intercepted by the fake python.
    corpus = repo / "tests" / "counterfeits" / "harness.py"
    corpus.parent.mkdir(parents=True, exist_ok=True)
    corpus.write_text("# fixture stand-in; the fake python answers -m\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    candidates = {
        "archi": (
            "fixture/archi-diff",
            _commit_file(repo, "fixture/archi-diff", "ARCHI.md", "lane architecture\n"),
            "oracle-path",
            "regenerate on main",
        ),
        "empty": ("fixture/empty", base_sha, "no-change", "NO_CHANGE"),
        "venv": (
            "fixture/tracked-venv",
            _commit_file(
                repo,
                "fixture/tracked-venv",
                ".venv/counterfeit.txt",
                "tracked environment\n",
                force=True,
            ),
            "tracked-env",
            "candidate tracks environment path",
        ),
        "workbook": (
            "fixture/root-workbook",
            _commit_file(
                repo,
                "fixture/root-workbook",
                "WORKBOOK.md",
                "overwritten lane record\n",
            ),
            "root-workbook",
            "use var/swarm/<run>/<task>/WORKBOOK.md",
        ),
        "control": (
            "fixture/clean-control",
            _commit_file(repo, "fixture/clean-control", "candidate.txt", "clean\n"),
            None,
            None,
        ),
    }
    _git(repo, "branch", "fixture/empty", base_sha)
    _install_fake_python(repo)

    branches = {
        key: FixtureBranch(
            name=name,
            candidate_sha=candidate_sha,
            merge_base_sha=base_sha,
            refusal=refusal,
            reason=reason,
        )
        for key, (name, candidate_sha, refusal, reason) in candidates.items()
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


def _run_gate(
    fixture: M8Repo,
    case: FixtureBranch,
    *,
    gate_script: Path = MERGE_GATE,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "REPO": str(fixture.path),
        "MERGE_GATE_EVIDENCE_ROOT": str(fixture.evidence_root),
        # STATE THE MODE, never inherit it (2026-08-05). The operator arms
        # determinism with a command prefix — `MERGE_GATE_PINNED=1 bash
        # scripts/merge-gate.sh ...` — which EXPORTS it, so the value rode
        # through merge-gate's own counterfeit worker into the corpus's control
        # pytest and into every fixture gate this module starts. In pinned mode
        # GATE_WS derives from the SCRIPT's location, which under the corpus is
        # the throwaway trial-merge worktree, so these gates refused
        # `gate-workspace-missing — .../var/swarm/gate-<pid>-gate` before
        # scoring a single step: ten nodes here went red INSIDE the merge gate
        # while passing everywhere else, several of them in the counterfeit
        # corpus's must_fail union. These fixtures exercise the DEFAULT, un-armed
        # gate; test_merge_gate_openapi_drift and test_gate_venv_resolution set
        # the flag explicitly when they want the pinned path, and `env_extra`
        # below still overrides this default for any test that asks.
        "MERGE_GATE_PINNED": "0",
        # STATE THE INTERPRETER, never inherit it — see fake_python_for().
        "MERGE_GATE_PY": fake_python_for(fixture.path),
        **(env_extra or {}),
    }
    return run_contained(
        ["bash", str(gate_script), case.name],
        cwd=fixture.path,
        env=env,
    )


def _output(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stdout + completed.stderr


def fake_python_for(repo: Path) -> str:
    """The stub interpreter a fixture repo installs, as an absolute path.

    STATE THE INTERPRETER, NEVER INHERIT IT (2026-08-08) — the exact sibling of
    the MERGE_GATE_PINNED rule two functions down, and it cost more. These
    helpers build the child env from ``os.environ``, and merge-gate.sh resolves
    ``MERGE_GATE_PY`` FIRST, ahead of every workspace .venv. The production gate
    command exports one (AccurateGate: ``MERGE_GATE_PY={workspace}/.venv/bin/python``)
    and merge-gate.sh's suite worker passed it straight through into pytest — so
    the stub whose entire job is to make a nested gate answer instantly was
    bypassed, the nested gate ran the REAL 96-entry counterfeit corpus, and each
    generation spawned more. Caught live at 13+ generations.

    MEASURED on one node in this file's own fixture shape: 2.14s with the
    variable scrubbed, 180.44s with it inherited — and 180s is pytest-timeout
    cutting in, not the work completing. 22 such stalls accounted for 3,960s of
    a 4,371s gate run.

    merge-gate.sh now scrubs it in both workers; this is the second, independent
    defence, so a future caller that re-introduces the export cannot resurrect
    the storm.
    """
    return str(repo / ".venv" / "bin" / "python")


def run_contained(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run`` that can actually kill what it started.

    ``subprocess.run(timeout=...)`` kills only the DIRECT child. A gate spawns
    pytest, which spawns more gates; on timeout those descendants reparent to
    PPID 1 and keep reproducing after the test has moved on — six manual reaps
    of 123 orphans on the twin before the source was found, because they
    regenerate faster than they can be killed. Own session, then signal the
    whole PROCESS GROUP, so a refused or wedged gate cannot outlive its test.
    """
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        stdout, stderr = proc.communicate()
        stderr = (stderr or "") + f"\nCONTAINED: process group killed after {timeout}s\n"
    else:
        # Green runs leak too: a backgrounded suite worker outlives a gate that
        # already printed its verdict. Reap unconditionally, ignoring "no such
        # group" which is the normal, healthy case.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    return subprocess.CompletedProcess(command, proc.returncode, stdout or "", stderr or "")


GUARD_SNIPPETS = {
    "oracle-path": """ORACLE_PATHS=$(printf '%s\\n' "$CHANGED_PATHS" | grep -Ex \\
  'ARCHI\\.md|ARCHI\\.json|docs/architecture/system-map\\.(md|mmd)'); rc=$?
classifier_rc "$rc" "oracle-path"
[ -n "$ORACLE_PATHS" ] && fail "oracle-path" \\
  "candidate touches merge-owned oracle(s): $(echo "$ORACLE_PATHS" | tr '\\n' ' '); regenerate on main" || pass "oracle-path"
""",
    "no-change": """[ -z "$CHANGED_PATHS" ] && fail "no-change" \\
  "NO_CHANGE: candidate has no tree changes against current HEAD" || pass "no-change"
""",
    "tracked-env": """TRACKED_ENV=$(printf '%s\\n' "$CHANGED_PATHS" | grep -E '(^|/)(\\.venv[^/]*|node_modules)(/|$)'); rc=$?
classifier_rc "$rc" "tracked-env"
[ -n "$TRACKED_ENV" ] && fail "tracked-env" \\
  "candidate tracks environment path(s): $(echo "$TRACKED_ENV" | tr '\\n' ' ')" || pass "tracked-env"
""",
    "root-workbook": """ROOT_WORKBOOK=$(printf '%s\\n' "$CHANGED_PATHS" | grep -Fx 'WORKBOOK.md'); rc=$?
classifier_rc "$rc" "root-workbook"
[ -n "$ROOT_WORKBOOK" ] && fail "root-workbook" \\
  "candidate touches shared root WORKBOOK.md; use var/swarm/<run>/<task>/WORKBOOK.md" || pass "root-workbook"
""",
}


def _gate_without_guard(tmp_path: Path, guard: str) -> Path:
    source = MERGE_GATE.read_text(encoding="utf-8")
    snippet = GUARD_SNIPPETS[guard]
    assert source.count(snippet) == 1, f"{guard} mutation did not bind exactly once"
    replacement = f'pass "{guard}" "guard removed by revert-check"'
    mutated = source.replace(snippet, replacement + "\n")
    path = tmp_path / f"merge-gate-without-{guard}.sh"
    path.write_text(mutated, encoding="utf-8")
    applied = [
        line for line in path.read_text(encoding="utf-8").splitlines() if line == replacement
    ]
    assert applied == [replacement], f"{guard} mutation was not written exactly once"
    print(f"MUTATION APPLIED [{guard}]: {applied[0]}")
    return path


def _assert_single_guard_revert(
    fixture: M8Repo,
    tmp_path: Path,
    *,
    case_name: str,
    guard: str,
) -> None:
    case = fixture.branches[case_name]
    protected = _run_gate(fixture, case)
    print(f"PROTECTED [{guard}] rc={protected.returncode}\n{_output(protected)}")
    assert protected.returncode != 0, _output(protected)
    assert case.refusal in _output(protected)
    assert case.reason in _output(protected)

    mutated_gate = _gate_without_guard(tmp_path, guard)
    reverted = _run_gate(fixture, case, gate_script=mutated_gate)
    print(f"REVERTED [{guard}] rc={reverted.returncode}\n{_output(reverted)}")
    assert reverted.returncode == 0, _output(reverted)
    assert "MERGE GATE: PASS" in _output(reverted)
    assert re.search(r"reachability\s+ok", _output(reverted)), _output(reverted)


def test_five_fixture_branches_refuse_four_and_accept_clean_control(m8_repo: M8Repo) -> None:
    for key in ("archi", "empty", "venv", "workbook"):
        case = m8_repo.branches[key]
        refused = _run_gate(m8_repo, case)
        assert refused.returncode != 0, _output(refused)
        assert case.refusal in _output(refused)
        assert case.reason in _output(refused)
        assert "MERGE GATE: PASS" not in _output(refused)

    control = _run_gate(m8_repo, m8_repo.branches["control"])
    print(f"CONTROL [clean] rc={control.returncode}\n{_output(control)}")
    assert control.returncode == 0, _output(control)
    assert "MERGE GATE: PASS" in _output(control)
    assert re.search(r"reachability\s+ok", _output(control)), _output(control)


def test_hermetic_venv_symlink_refuses_even_clean_control(m8_repo: M8Repo) -> None:
    """A checkout whose .venv-hermetic is a symlink certifies NO candidate (A4).

    A symlinked .venv-hermetic (e.g. redirected to .venv) means `make
    test-hermetic` would have installed the always-on testfarm plugin into the
    production venv — every pytest result from that checkout is suspect, so the
    gate refuses outright, even for an otherwise clean candidate.
    """
    link = m8_repo.path / ".venv-hermetic"
    link.symlink_to(m8_repo.path / ".venv")
    try:
        refused = _run_gate(m8_repo, m8_repo.branches["control"])
        assert refused.returncode != 0, _output(refused)
        assert "hermetic-venv-shape" in _output(refused)
        assert "symlink" in _output(refused)
        assert "MERGE GATE: PASS" not in _output(refused)
    finally:
        link.unlink()

    # A real directory (like absence) is the accepted shape: control passes again.
    (m8_repo.path / ".venv-hermetic").mkdir()
    accepted = _run_gate(m8_repo, m8_repo.branches["control"])
    assert accepted.returncode == 0, _output(accepted)
    assert re.search(r"hermetic-venv-shape\s+ok", _output(accepted)), _output(accepted)


def test_revert_oracle_path_guard_accepts_archi_fixture(m8_repo: M8Repo, tmp_path: Path) -> None:
    _assert_single_guard_revert(m8_repo, tmp_path, case_name="archi", guard="oracle-path")


def test_revert_no_change_guard_accepts_empty_fixture(m8_repo: M8Repo, tmp_path: Path) -> None:
    _assert_single_guard_revert(m8_repo, tmp_path, case_name="empty", guard="no-change")


def test_revert_tracked_env_guard_accepts_venv_fixture(m8_repo: M8Repo, tmp_path: Path) -> None:
    _assert_single_guard_revert(m8_repo, tmp_path, case_name="venv", guard="tracked-env")


def test_revert_root_workbook_guard_accepts_workbook_fixture(
    m8_repo: M8Repo, tmp_path: Path
) -> None:
    _assert_single_guard_revert(m8_repo, tmp_path, case_name="workbook", guard="root-workbook")


# --- counterfeit verdict: the EXIT CODE is the primary signal -----------------
#
# The gate used to grep the harness's stdout summary and throw `wait`'s status
# away, so the verdict came from text produced by the very tree under judgement.
# The revert-check below restores exactly that scoring block and shows a harness
# that FAILED being scored PASS — the defect, executed end to end.

_CLEAN_REPORT = "total=59  caught=59  survived=0  other=0"

_EXIT_CODE_SCORING = """      CF_RC=$(sed -n '1p' "$STEP_DIR/counterfeit.status" 2>/dev/null)
      CF_VERDICT=""
      if [ -z "$CF_RC" ]; then
        fail "counterfeit-gate" "harness produced NO exit status (worker died) — an instrument that did not run is NOT a pass"
      elif CF_VERDICT=$(judge_counterfeit "$CF_RC" "$STEP_DIR/counterfeit.out"); then
        pass "counterfeit-gate" "$CF_VERDICT"
        record_step_receipt "counterfeit-gate" "$CF_CMD" "$STEP_DIR/counterfeit.out" \\
          "$CF_VERDICT" "$CF_STARTED" "$CF_RC"
      else
        fail "counterfeit-gate" "${CF_VERDICT:-rc=$CF_RC and no judgeable verdict}"
      fi
"""

_SUMMARY_ONLY_SCORING = """      CF=$(cat "$STEP_DIR/counterfeit.out" 2>/dev/null)
      REPORT=$(printf '%s' "$CF" | grep -oE 'total=[0-9]+ +caught=[0-9]+ +survived=[0-9]+ +other=[0-9]+' | tail -1)
      if [ -z "$REPORT" ]; then
        fail "counterfeit-gate" "produced NO verdict (did not run)"
      elif printf '%s' "$REPORT" | grep -q "other=[1-9]"; then
        fail "counterfeit-gate" "$REPORT (entries errored — dead coverage)"
      elif printf '%s' "$REPORT" | grep -q "survived=0"; then
        pass "counterfeit-gate" "$REPORT"
      else
        fail "counterfeit-gate" "$REPORT"
      fi
"""


def _gate_scoring_by_summary_only(tmp_path: Path) -> Path:
    """merge-gate.sh with the pre-fix, text-only counterfeit scoring restored."""
    source = MERGE_GATE.read_text(encoding="utf-8")
    assert source.count(_EXIT_CODE_SCORING) == 1, (
        "counterfeit exit-code scoring mutation did not bind exactly once — the "
        "block moved; re-anchor this revert-check instead of deleting it"
    )
    path = tmp_path / "merge-gate-summary-only.sh"
    path.write_text(source.replace(_EXIT_CODE_SCORING, _SUMMARY_ONLY_SCORING), encoding="utf-8")
    return path


def test_counterfeit_harness_exit_code_is_the_verdict_not_its_printed_summary(
    m8_repo: M8Repo, tmp_path: Path
) -> None:
    """NEGATIVE CONTROL (defect 2): a harness that exits non-zero while printing
    a clean-looking summary line must REFUSE the merge.

    PROTECTED: today's gate reads the measured exit status and refuses on the
    disagreement. REVERTED: with the pre-fix summary-grep scoring restored, the
    same failing harness earns "MERGE GATE: PASS" — which is the hole.
    """
    case = m8_repo.branches["control"]
    failing_harness = {
        "MERGE_GATE_TEST_CF_RC": "1",
        "MERGE_GATE_TEST_CF_REPORT": _CLEAN_REPORT,
        # Isolate the LIVE path: no receipt may be reused or minted here.
        "MERGE_GATE_STEP_RECEIPTS": "0",
    }

    protected = _run_gate(m8_repo, case, env_extra=failing_harness)
    print(f"PROTECTED [counterfeit-exit-code] rc={protected.returncode}\n{_output(protected)}")
    assert protected.returncode != 0, (
        "A COUNTERFEIT HARNESS THAT EXITED 1 WAS SCORED A PASS on the strength of "
        f"its own clean-looking summary line:\n{_output(protected)}"
    )
    assert "MERGE GATE: PASS" not in _output(protected)
    assert re.search(r"counterfeit-gate\s+FAIL", _output(protected)), _output(protected)
    assert "DISAGREE" in _output(protected), _output(protected)

    reverted = _run_gate(
        m8_repo,
        case,
        gate_script=_gate_scoring_by_summary_only(tmp_path),
        env_extra=failing_harness,
    )
    print(f"REVERTED [counterfeit-exit-code] rc={reverted.returncode}\n{_output(reverted)}")
    assert reverted.returncode == 0, (
        "the revert-check did not reproduce the defect — the pre-fix scoring block "
        f"should have passed this failing harness:\n{_output(reverted)}"
    )
    assert "MERGE GATE: PASS" in _output(reverted)


def test_counterfeit_gate_refuses_when_the_harness_reports_errored_entries(
    m8_repo: M8Repo,
) -> None:
    """Behaviour that already worked must survive: other>0 is dead coverage.

    Here the harness AGREES with itself (exit 1 and other=1), so the refusal
    names the dead coverage rather than a disagreement.
    """
    refused = _run_gate(
        m8_repo,
        m8_repo.branches["control"],
        env_extra={
            "MERGE_GATE_TEST_CF_RC": "1",
            "MERGE_GATE_TEST_CF_REPORT": "total=59  caught=58  survived=0  other=1",
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )

    assert refused.returncode != 0, _output(refused)
    assert "dead coverage" in _output(refused), _output(refused)
    assert "MERGE GATE: PASS" not in _output(refused)


def test_counterfeit_gate_refuses_a_harness_that_printed_no_verdict_line(
    m8_repo: M8Repo,
) -> None:
    """An instrument that did not run is NOT a pass — and a format shift in the
    report line lands here too, so the gate fails closed on it."""
    refused = _run_gate(
        m8_repo,
        m8_repo.branches["control"],
        env_extra={
            "MERGE_GATE_TEST_CF_RC": "2",
            "MERGE_GATE_TEST_CF_REPORT": "COUNTERFEIT GATE REFUSED: duplicate id 'cf-a'",
            "MERGE_GATE_STEP_RECEIPTS": "0",
        },
    )

    assert refused.returncode != 0, _output(refused)
    assert "NO verdict line" in _output(refused), _output(refused)
    # The reason is never empty: the pre-fix grep matched neither the harness's
    # own refusal prefix nor a traceback.
    assert "COUNTERFEIT GATE REFUSED" in _output(refused), _output(refused)


def test_a_green_counterfeit_run_still_passes_and_mints_a_reusable_receipt(
    m8_repo: M8Repo,
) -> None:
    """The fix must not cost the gate its skip-on-reuse: a green corpus passes,
    mints a receipt bound to this candidate, and the next run reuses it."""
    green = {"MERGE_GATE_TEST_CF_RC": "0", "MERGE_GATE_TEST_CF_REPORT": _CLEAN_REPORT}

    first = _run_gate(m8_repo, m8_repo.branches["control"], env_extra=green)
    assert first.returncode == 0, _output(first)
    assert re.search(rf"counterfeit-gate\s+ok — {re.escape(_CLEAN_REPORT)}", _output(first)), (
        _output(first)
    )

    # The harness now FAILS, but the receipt from the green run is fresh and
    # correctly bound, so the step is skipped — and the reused verdict is the
    # green one, re-judged by the same rule on the way out of the store.
    second = _run_gate(
        m8_repo,
        m8_repo.branches["control"],
        env_extra={"MERGE_GATE_TEST_CF_RC": "1", "MERGE_GATE_TEST_CF_REPORT": _CLEAN_REPORT},
    )
    assert second.returncode == 0, _output(second)
    assert "reused" in _output(second), _output(second)
