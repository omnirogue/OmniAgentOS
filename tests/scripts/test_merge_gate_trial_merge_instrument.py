"""The trial merge must never blame the candidate for the instrument's failure.

``scripts/merge-gate.sh`` builds a scratch worktree and trial-merges the
authenticated candidate tip.  ``git merge`` has three outcomes, not two:

* ``0``      -- it judged the candidate and the merge is clean
* ``1..127`` -- it judged the candidate and found conflicts
* ``>=128``  -- it could not judge the candidate AT ALL: no committer identity,
  a corrupt or missing object, an unreadable ref, a locked index

The gate used to collapse all three into two with
``if ! git ... merge ... 2>/dev/null; then fail "merge-clean" "conflicts against main"``,
sending git's own words to ``/dev/null``.  An instrument failure was therefore
printed as a verdict about the candidate -- and because ``MERGE_OK`` stays 0,
the ladder, counterfeit corpus, dominance, doctrine, memlife and ruff were all
skipped, so nothing downstream contradicted the fabricated accusation.

That is not hypothetical.  PR #19 ("ci(security): enforce merge gate on pull
requests") added a CI job that configured no git identity.  GitHub-hosted
runners have no ``user.email``/``user.name`` and a hostname with no domain, so
git's ident auto-detection fails too; the trial merge exited 128 with
"Committer identity unknown" and job 92662324306 refused in 0.42s with
"conflicts against main" against a branch that merges perfectly cleanly.  The
701-line log contains no ladder, counterfeit or ruff line anywhere.

These tests pin both halves of the discrimination.  Neither passes against the
pre-fix script: the instrument test sees ``merge-clean FAIL -- conflicts
against main`` instead of a ``trial-merge-broken`` refusal, and the conflict
test sees a bare "conflicts against main" with no unmerged path named.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.scheduler.gate_evidence import (
    GateEvidence,
    GateEvidenceStore,
    binding_digest,
    workspace_digest_for,
)
from tests.scripts.test_merge_gate_m8_refusals import fake_python_for, run_contained

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_GATE = REPO_ROOT / "scripts" / "merge-gate.sh"
_VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
REAL_PYTHON = _VENV_PYTHON if _VENV_PYTHON.is_file() else Path(sys.executable)
MERGE_ROUTINE_ID = "merge-gate"
MERGE_GATE_TYPE = "merge_candidate"
_SCHEMA_V3_WIRE = "omniagentos.gate-evidence.v3"

# Defeats BOTH ways a host can supply a committer identity: the config files
# (global/system emptied to /dev/null, local unset below) and git's fallback
# auto-detection from username@hostname.  A GitHub runner fails on exactly this
# pair -- no configured ident, and a hostname with no domain so auto-detection
# cannot produce a fully-qualified address.  Pinning both makes the test
# deterministic on a developer laptop, where auto-detection WOULD otherwise
# succeed and hide the defect.
_NO_GIT_IDENTITY_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "user.useConfigOnly",
    "GIT_CONFIG_VALUE_0": "true",
}

# The mirror of the block above, for the tests whose PREMISE is that an
# identity exists.  Without this, those tests silently depend on the host's
# ambient git config: green on a developer laptop (~/.gitconfig), red on a
# GitHub-hosted runner (no config anywhere, and the `runner` account's empty
# GECOS name makes auto-detection fail with `empty ident name`) — the gate
# then correctly refuses `trial-merge-broken` and the merge-clean assertions
# never see a verdict.  Latent since these tests landed (2026-08-08); exposed
# whenever the impacted-test selector picks this file on CI.
_GIT_IDENTITY_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_COUNT": "2",
    "GIT_CONFIG_KEY_0": "user.name",
    "GIT_CONFIG_VALUE_0": "Trial Merge Test",
    "GIT_CONFIG_KEY_1": "user.email",
    "GIT_CONFIG_VALUE_1": "trial-merge-test@example.invalid",
}


@dataclass(frozen=True)
class GateRepo:
    path: Path
    evidence_root: Path
    branch: str
    merge_base_sha: str
    candidate_sha: str


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-c", "user.name=Trial Merge Test", "-c", "user.email=trial@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _local_identity(repo: Path) -> str:
    """Repo-local user.name/user.email, empty when neither is set."""
    completed = subprocess.run(
        ["git", "config", "--local", "--get-regexp", "^user\\.(name|email)$"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _install_fake_python(repo: Path) -> None:
    """Real receipt verification, instant everything else.

    Mirrors tests/scripts/test_merge_gate_receipt.py -- the suites after the
    trial merge are not what these tests are about, and running them for real
    would take ~20 minutes.
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
if [ "$1" = "-c" ]; then
  printf '%s/omniagentos/__init__.py' "$PWD"
  exit 0
fi
if [ "$1" = "-m" ] && [ "$2" = "pytest" ]; then
  printf '1 passed in 0.01s\\n'
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


def _build_repo(tmp_path: Path, *, conflicting: bool) -> GateRepo:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / ".gitignore").write_text(".venv\nvar\n", encoding="utf-8")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    reachability = repo / "scripts" / "reachability-gate.py"
    reachability.parent.mkdir(parents=True, exist_ok=True)
    reachability.write_bytes((REPO_ROOT / "scripts" / "reachability-gate.py").read_bytes())
    reachability.chmod(0o755)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    merge_base_sha = _git(repo, "rev-parse", "HEAD")

    branch = "candidate"
    _git(repo, "checkout", "-b", branch)
    (repo / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    _git(repo, "add", "candidate.txt")
    if conflicting:
        (repo / "base.txt").write_text("candidate-side\n", encoding="utf-8")
        _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "main")
    if conflicting:
        (repo / "base.txt").write_text("main-side\n", encoding="utf-8")
        _git(repo, "add", "base.txt")
        _git(repo, "commit", "-m", "main advances base.txt")

    _install_fake_python(repo)
    return GateRepo(
        path=repo,
        evidence_root=tmp_path / "gate-evidence",
        branch=branch,
        merge_base_sha=merge_base_sha,
        candidate_sha=candidate_sha,
    )


def _record_receipt(gate_repo: GateRepo) -> None:
    """Mint the signed candidate receipt so the gate reaches the trial merge.

    Without this the gate refuses at ``signed-receipt`` and never executes the
    block under test -- a skipped step reading as a pass is the exact
    favourable-absence trap these tests exist to close, so the reached-ness of
    the trial merge is asserted explicitly in every test below.
    """
    command = "anthropic-review candidate"
    targets = ("candidate",)
    workspace_digest = workspace_digest_for(gate_repo.path)
    now = datetime.now(UTC)
    GateEvidenceStore(gate_repo.evidence_root).record(
        GateEvidence(
            schema=_SCHEMA_V3_WIRE,
            routine_id=MERGE_ROUTINE_ID,
            run_id=gate_repo.candidate_sha,
            iteration=1,
            gate_type=MERGE_GATE_TYPE,
            command=command,
            targets=targets,
            workspace_digest=workspace_digest,
            binding_digest=binding_digest(
                routine_id=MERGE_ROUTINE_ID,
                run_id=gate_repo.candidate_sha,
                iteration=1,
                gate_type=MERGE_GATE_TYPE,
                command=command,
                targets=targets,
                workspace_digest=workspace_digest,
                candidate_sha=gate_repo.candidate_sha,
                merge_base_sha=gate_repo.merge_base_sha,
            ),
            tool="pytest",
            tool_version="8.3.2",
            exit_code=0,
            checks_collected=1,
            checks_passed=1,
            checks_skipped=0,
            checks_failed=0,
            started_at=(now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            finished_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            nonce="0123456789abcdef0123456789abcdef",
            workspace_sha=gate_repo.candidate_sha,
            workspace_tree_clean=True,
            interpreter=str(REAL_PYTHON),
            interpreter_version="3.12",
            node_inventory_digest="0" * 64,
            deselected_count=0,
            candidate_sha=gate_repo.candidate_sha,
            merge_base_sha=gate_repo.merge_base_sha,
        )
    )


def _run_gate(
    gate_repo: GateRepo, *, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "REPO": str(gate_repo.path),
        # STATE THE INTERPRETER, never inherit it — see fake_python_for().
        "MERGE_GATE_PY": fake_python_for(gate_repo.path),
        "MERGE_GATE_EVIDENCE_ROOT": str(gate_repo.evidence_root),
        **(env_extra or {}),
    }
    # CONTAINED: a gate spawns pytest which spawns more gates; on timeout
    # subprocess.run kills only the direct child and the rest orphan to PPID 1.
    return run_contained(
        ["bash", str(MERGE_GATE), gate_repo.branch],
        cwd=gate_repo.path,
        env=env,
    )


def _output(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stdout + completed.stderr


@pytest.fixture
def clean_repo(tmp_path: Path) -> GateRepo:
    repo = _build_repo(tmp_path, conflicting=False)
    _record_receipt(repo)
    return repo


@pytest.fixture
def conflicting_repo(tmp_path: Path) -> GateRepo:
    repo = _build_repo(tmp_path, conflicting=True)
    _record_receipt(repo)
    return repo


def test_a_runner_without_a_git_identity_is_not_reported_as_a_candidate_conflict(
    clean_repo: GateRepo,
) -> None:
    """rc>=128 from ``git merge`` is an instrument failure, never a verdict.

    The candidate here merges into main perfectly cleanly -- ``conflicts
    against main`` is provably false about it (asserted by the control test
    below, which runs the same candidate WITH an identity and gets a clean
    trial merge).  The only thing wrong is the environment.

    Failing-on-revert (production change only; this file untouched): restoring
      ``if ! git -C "$SCRATCH" merge ... 2>/dev/null; then
          fail "merge-clean" "conflicts against main"``
    prints ``merge-clean FAIL -- conflicts against main`` and never mentions
    ``trial-merge-broken``, so both assertions below fail.
    """
    # The fixture never writes a repo-local identity (its own git calls carry
    # `-c user.*` inline), so the env above leaves the gate's merge with no
    # identity source at all. Asserted, not assumed: a local user.email here
    # would silently satisfy the merge and make this test vacuous.
    assert not _local_identity(clean_repo.path), "fixture leaked a repo-local git identity"

    refused = _run_gate(clean_repo, env_extra=_NO_GIT_IDENTITY_ENV)
    out = _output(refused)

    # The step must have been REACHED. Without this a gate that refused earlier
    # (bad receipt, missing worktree) would satisfy every negative assertion
    # below while proving nothing -- a skipped check reading as a pass.
    assert "trial-merge-broken" in out, out

    # The accusation the pre-fix gate fabricated must be absent.
    assert not re.search(r"merge-clean\s+FAIL", out), out
    assert "conflicts against main" not in out, out

    # git's own words must survive to the operator instead of /dev/null.
    assert "identity" in out.lower(), out
    # Instrument failure exits 2 (refusal), not 1 (candidate refused).
    assert refused.returncode == 2, out


def test_the_same_candidate_trial_merges_cleanly_once_an_identity_exists(
    clean_repo: GateRepo,
) -> None:
    """Control for the test above: the candidate itself is merge-clean.

    This is what makes "conflicts against main" a FABRICATION rather than a
    mislabel, and it is also the assertion that PR #19's CI job needs in order
    to go green -- with a configured identity the gate proceeds past the trial
    merge and runs the suites it exists to run.
    """
    result = _run_gate(clean_repo, env_extra=_GIT_IDENTITY_ENV)
    out = _output(result)

    assert re.search(r"merge-clean\s+ok", out), out
    assert "trial-merge-broken" not in out, out
    assert result.returncode == 0, out


def test_a_real_conflict_still_fails_merge_clean_and_names_the_unmerged_path(
    conflicting_repo: GateRepo,
) -> None:
    """Discriminating rc>=128 must not stop the gate catching real conflicts.

    Failing-on-revert: the pre-fix block reported a bare ``conflicts against
    main`` with no path, so the ``unmerged paths: ... base.txt`` assertion
    fails against it.  A change that turned genuine conflicts into
    ``trial-merge-broken`` refusals would fail the first two assertions.
    """
    refused = _run_gate(conflicting_repo, env_extra=_GIT_IDENTITY_ENV)
    out = _output(refused)

    assert re.search(r"merge-clean\s+FAIL", out), out
    assert "trial-merge-broken" not in out, out
    assert "base.txt" in out, out
    assert re.search(r"unmerged paths:[^\n]*base\.txt", out), out
    assert refused.returncode != 0, out


def test_the_trial_merge_never_discards_git_stderr() -> None:
    """Static pin on the carrier itself.

    The defect was not the wrong branch taken -- it was that the evidence
    needed to notice was thrown away at the call site.  Any future edit that
    reintroduces ``2>/dev/null`` on the trial merge (or drops the >=128 arm)
    silences the diagnosis again for every failure mode that has not been
    written a behavioural test yet: corrupt object, unreadable ref, locked
    index, out of disk.
    """
    source = MERGE_GATE.read_text(encoding="utf-8")
    trial = source.split('step_begin "trial-merge"', 1)
    assert len(trial) == 2, "trial-merge step vanished from merge-gate.sh"
    block = trial[1].split('if [ "$MERGE_OK" -eq 1 ]', 1)[0]

    # Comment lines are not carriers -- the block deliberately QUOTES the old
    # broken call to explain it (same convention as
    # tests/scripts/test_verdict_artifact_integrity.py).
    code_lines = [line for line in block.splitlines() if not line.lstrip().startswith("#")]
    block = "\n".join(code_lines)
    merge_calls = [
        line
        for line in code_lines
        if re.search(r"\bgit\b[^#]*\bmerge(?![-\w])", line) and "--abort" not in line
    ]
    assert merge_calls, "no trial `git merge` found in the trial-merge block"
    for line in merge_calls:
        assert "2>/dev/null" not in line, f"trial merge discards git stderr: {line.strip()}"
    assert re.search(r"MERGE_RC[^\n]*-ge\s+128", block), (
        "the trial merge no longer distinguishes rc>=128 (git could not run) "
        "from a candidate-side conflict"
    )


def test_the_gate_run_receipt_records_the_instrument_failure(
    clean_repo: GateRepo,
) -> None:
    """A refusal nobody can audit later is the same defect one layer down.

    ``refuse`` mints a run receipt carrying the reason; pin that the reason
    recorded is the instrument failure, not a conflict.  Without this, the
    on-disk history of the run would still read as a candidate refusal even
    though the console now says otherwise.
    """
    assert not _local_identity(clean_repo.path), "fixture leaked a repo-local git identity"
    _run_gate(clean_repo, env_extra=_NO_GIT_IDENTITY_ENV)

    runs = sorted(
        (clean_repo.evidence_root / "records" / MERGE_ROUTINE_ID).glob(
            f"{clean_repo.candidate_sha}.run-*.json"
        )
    )
    assert runs, "no run receipt was minted for the refusal"
    payload = json.loads(runs[-1].read_text(encoding="utf-8"))
    blob = json.dumps(payload)
    assert "trial-merge-broken" in blob, blob
    assert "conflicts against main" not in blob, blob


# ---------------------------------------------------------------------------
# Incomplete propagation: the same defect had SIX carriers, not one, spread
# over four shell scripts and two Python drivers -- and merge-gate.sh alone
# carried TWO (the `trial-merge` step and the openapi-drift staging merge,
# three lines below a comment that had already fixed exactly this for
# `worktree add`). Fixing one leaves five places that tell an operator to
# rebase a branch git never examined. The enumeration is pinned so a seventh
# cannot be added silently.
# ---------------------------------------------------------------------------

SCRIPTS = REPO_ROOT / "scripts"

# Every script that runs `git merge` AND turns its exit status into a statement
# about the branch. Each must discriminate rc>=128.
_MERGE_ACCUSERS = (
    "merge-gate.sh",
    "merge-request.sh",
    "integrate.sh",
    # "merge-if-only-known-blocker.sh" DELETED 2026-08-13 by operator ruling: the
    # signed-receipt producer landed (1,300+ receipts on disk since 2026-07-30) and the
    # bypass this comment used to name had never once executed.
    "land-approved-lanes.py",
    "backlog-executor/executor.py",
)

# Shell writes `-ge 128`; Python writes `>= 128`.
_DISCRIMINATES = re.compile(r"(-ge\s+128|>=\s*128)")


def _code(path: Path) -> str:
    """Script text with comment lines removed (comments quote the old bug)."""
    return "\n".join(
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )


@pytest.mark.parametrize("name", _MERGE_ACCUSERS)
def test_every_merge_accuser_distinguishes_a_broken_git_from_a_conflict(name: str) -> None:
    """Failing-on-revert: drop the >=128 arm from any one of the six.

    Before this lane, all six collapsed ``git merge``'s exit status into a
    single conflict accusation, and four of them additionally discarded git's
    own words (``2>/dev/null`` in shell, an unused ``stderr``/``out`` capture
    in Python).
    """
    code = _code(SCRIPTS / name)
    assert _DISCRIMINATES.search(code), (
        f"{name} runs `git merge` and reports the result, but no longer "
        "distinguishes rc>=128 (git could not run) from a real conflict"
    )
    merge_lines = [
        line
        for line in code.splitlines()
        if re.search(r"\bgit\b[^\n]*\bmerge(?![-\w])", line) and "--abort" not in line
    ]
    assert merge_lines, f"{name} no longer runs git merge — update _MERGE_ACCUSERS"
    for line in merge_lines:
        assert not re.search(r"2>\s*/dev/null", line), (
            f"{name} discards git merge stderr, the evidence needed to tell an "
            f"instrument failure from a conflict: {line.strip()}"
        )


def test_no_unenumerated_script_turns_a_git_merge_exit_into_a_conflict_verdict() -> None:
    """The carrier list is closed.

    A new script that runs ``git merge`` and says "conflict" reintroduces the
    defect in a place nobody thought to look. It must either discriminate
    rc>=128 or be added here deliberately.
    """
    known = {str(SCRIPTS / name) for name in _MERGE_ACCUSERS}
    stragglers = []
    for pattern in ("*.sh", "*.py"):
        for path in sorted(SCRIPTS.rglob(pattern)):
            if str(path) in known:
                continue
            code = _code(path)
            runs_merge = any(
                re.search(r"\bgit\b[^\n]*\bmerge(?![-\w])", line)
                or re.search(r"""["']merge["']""", line)
                for line in code.splitlines()
                if "--abort" not in line and "abort" not in line
            )
            if runs_merge and "conflict" in code.lower() and not _DISCRIMINATES.search(code):
                stragglers.append(path.relative_to(REPO_ROOT))
    assert not stragglers, (
        "these scripts run `git merge` and report a conflict without ruling out "
        f"an instrument failure first: {stragglers}"
    )
