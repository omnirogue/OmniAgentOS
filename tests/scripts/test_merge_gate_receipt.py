"""Acceptance tests for merge-gate signed candidate receipts."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omniagentos.scheduler import gate_evidence as gate_evidence_mod
from omniagentos.scheduler.gate_evidence import (
    SCHEMA,
    GateEvidence,
    GateEvidenceError,
    GateEvidenceRefusal,
    GateEvidenceStore,
    GateExecutionInfraError,
    binding_digest,
    verify_candidate_receipt,
    workspace_digest_for,
)
from tests.scripts.test_merge_gate_m8_refusals import REAL_PYTHON, fake_python_for, run_contained

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_GATE = REPO_ROOT / "scripts" / "merge-gate.sh"
MERGE_ROUTINE_ID = "merge-gate"
MERGE_GATE_TYPE = "merge_candidate"
# Independent wire-contract literal — must not be imported from production SCHEMA.
_SCHEMA_V3_WIRE = "omniagentos.gate-evidence.v3"


@dataclass(frozen=True)
class GateRepo:
    path: Path
    evidence_root: Path
    branch: str
    merge_base_sha: str
    candidate_sha: str


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
    """Run the real receipt verifier while making unrelated gate suites instant.

    Branch retarget for the TOCTOU pin test is done by merge-gate.sh itself when
    ``MERGE_GATE_TEST_RETARGET_SHA`` / ``MERGE_GATE_TEST_RETARGET_REF`` are set
    (after freezing ``$CANDIDATE_SHA`` and verifying the receipt). That is more
    reliable than intercepting the verifier process.
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


@pytest.fixture
def gate_repo(tmp_path: Path) -> GateRepo:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Merge Gate Test")
    _git(repo, "config", "user.email", "merge-gate@example.com")
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
    _git(repo, "commit", "-m", "candidate")
    candidate_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    _install_fake_python(repo)

    return GateRepo(
        path=repo,
        evidence_root=tmp_path / "gate-evidence",
        branch=branch,
        merge_base_sha=merge_base_sha,
        candidate_sha=candidate_sha,
    )


def test_merge_receipt_schema_wire_contract_is_v3() -> None:
    """Receipt schema identity is v3; expected side is a string literal.

    Failing-on-revert: ``SCHEMA = "…v2"`` in gate_evidence.py must fail here.
    Using the imported SCHEMA constant as the expected value would stay green.
    """
    assert gate_evidence_mod.SCHEMA == _SCHEMA_V3_WIRE
    assert SCHEMA == _SCHEMA_V3_WIRE


@pytest.mark.parametrize(
    ("option", "following_flag"),
    [("--candidate", "--emit-receipt"), ("--emit-receipt", "--candidate")],
)
def test_merge_gate_rejects_an_option_used_as_an_option_value(
    option: str, following_flag: str, tmp_path: Path
) -> None:
    """A flag must not consume the next flag as its value.

    This is a parser boundary: accepting ``--candidate --emit-receipt`` as a
    branch defers the error until after workspace resolution, producing a
    misleading refusal (and potentially minting a run receipt) instead of
    rejecting the malformed invocation immediately.
    """
    result = subprocess.run(
        [
            "bash",
            str(MERGE_GATE),
            option,
            following_flag,
            str(tmp_path / "value.json"),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "MERGE_GATE_PY": "/bin/false"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert f"missing-value — {option} needs" in result.stderr


@pytest.mark.parametrize(
    ("argument", "option"),
    [
        ("--candidate=--emit-receipt", "--candidate"),
        ("--emit-receipt=--candidate", "--emit-receipt"),
        ("--candidate=", "--candidate"),
        ("--emit-receipt=", "--emit-receipt"),
    ],
)
def test_merge_gate_rejects_an_empty_or_flag_like_equals_value(
    argument: str, option: str, tmp_path: Path
) -> None:
    result = subprocess.run(
        ["bash", str(MERGE_GATE), argument, str(tmp_path / "value.json")],
        cwd=REPO_ROOT,
        env={**os.environ, "MERGE_GATE_PY": "/bin/false"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert f"missing-value — {option} needs" in result.stderr


def _receipt(
    gate_repo: GateRepo,
    *,
    candidate_sha: str | None = None,
    merge_base_sha: str | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
    checks_collected: int = 1,
    checks_passed: int = 1,
) -> GateEvidence:
    candidate = candidate_sha or gate_repo.candidate_sha
    merge_base = merge_base_sha or gate_repo.merge_base_sha
    command = "anthropic-review candidate"
    targets = ("candidate",)
    workspace_digest = workspace_digest_for(gate_repo.path)
    finished = finished_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    started = started_at or (datetime.now(UTC) - timedelta(minutes=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return GateEvidence(
        schema=_SCHEMA_V3_WIRE,
        routine_id=MERGE_ROUTINE_ID,
        run_id=candidate,
        iteration=1,
        gate_type=MERGE_GATE_TYPE,
        command=command,
        targets=targets,
        workspace_digest=workspace_digest,
        binding_digest=binding_digest(
            routine_id=MERGE_ROUTINE_ID,
            run_id=candidate,
            iteration=1,
            gate_type=MERGE_GATE_TYPE,
            command=command,
            targets=targets,
            workspace_digest=workspace_digest,
            candidate_sha=candidate,
            merge_base_sha=merge_base,
        ),
        # Merge receipts are pytest evidence by contract — see
        # gate_evidence.MERGE_GATE_TOOL.
        tool="pytest",
        tool_version="8.3.2",
        exit_code=0,
        checks_collected=checks_collected,
        checks_passed=checks_passed,
        checks_skipped=0,
        checks_failed=0,
        started_at=started,
        finished_at=finished,
        nonce="6b5f01b9e87c9b4e67978f23b20a660d",
        workspace_sha=candidate,
        workspace_tree_clean=True,
        interpreter=str(REAL_PYTHON),
        interpreter_version="3.12",
        node_inventory_digest="0" * 64,
        deselected_count=0,
        candidate_sha=candidate,
        merge_base_sha=merge_base,
    )


def _receipt_path(gate_repo: GateRepo, candidate_sha: str | None = None) -> Path:
    candidate = candidate_sha or gate_repo.candidate_sha
    return gate_repo.evidence_root / "records" / MERGE_ROUTINE_ID / f"{candidate}.json"


def _write_payload(path: Path, evidence: GateEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.to_payload(), sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _run_gate(
    gate_repo: GateRepo,
    receipt: Path | None = None,
    *,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command = ["bash", str(MERGE_GATE), gate_repo.branch]
    if receipt is not None:
        command.append(str(receipt))
    env = {
        **os.environ,
        "REPO": str(gate_repo.path),
        # STATE THE INTERPRETER, never inherit it — see fake_python_for().
        "MERGE_GATE_PY": fake_python_for(gate_repo.path),
        "MERGE_GATE_EVIDENCE_ROOT": str(gate_repo.evidence_root),
    }
    if env_extra:
        env.update(env_extra)
    # CONTAINED: a gate spawns pytest which spawns more gates; on timeout
    # subprocess.run kills only the direct child and the rest orphan to PPID 1.
    return run_contained(
        command,
        cwd=gate_repo.path,
        env=env,
    )


def _output(completed: subprocess.CompletedProcess[str]) -> str:
    return completed.stdout + completed.stderr


def test_merge_gate_accepts_tip_receipt_then_rejects_it_after_one_more_commit(
    gate_repo: GateRepo,
) -> None:
    store = GateEvidenceStore(gate_repo.evidence_root)
    first = store.record(_receipt(gate_repo))
    first_path = _receipt_path(gate_repo)

    accepted = _run_gate(gate_repo)
    assert accepted.returncode == 0, _output(accepted)

    _git(gate_repo.path, "checkout", gate_repo.branch)
    (gate_repo.path / "later.txt").write_text("later\n", encoding="utf-8")
    _git(gate_repo.path, "add", "later.txt")
    _git(gate_repo.path, "commit", "-m", "later")
    new_tip = _git(gate_repo.path, "rev-parse", "HEAD")
    _git(gate_repo.path, "checkout", "main")
    advanced = replace(gate_repo, candidate_sha=new_tip)

    stale = _run_gate(advanced, first_path)
    assert stale.returncode != 0, _output(stale)
    assert "different candidate" in _output(stale).lower()

    store.record(_receipt(advanced))
    refreshed = _run_gate(advanced)
    assert refreshed.returncode == 0, _output(refreshed)
    assert first.candidate_sha != new_tip


def test_merge_gate_refuses_unsigned_hand_written_receipt(gate_repo: GateRepo) -> None:
    GateEvidenceStore(gate_repo.evidence_root)
    counterfeit = _receipt(gate_repo)
    assert counterfeit.signature == ""
    _write_payload(_receipt_path(gate_repo), counterfeit)

    refused = _run_gate(gate_repo)

    assert refused.returncode != 0, _output(refused)
    assert "signature" in _output(refused).lower()


def test_merge_gate_refuses_valid_signature_for_a_different_tree(gate_repo: GateRepo) -> None:
    store = GateEvidenceStore(gate_repo.evidence_root)
    foreign = store.sign(
        _receipt(
            gate_repo,
            candidate_sha=gate_repo.merge_base_sha,
            merge_base_sha=gate_repo.merge_base_sha,
        )
    )
    _write_payload(_receipt_path(gate_repo), foreign)

    refused = _run_gate(gate_repo)

    assert refused.returncode != 0, _output(refused)
    assert "different candidate" in _output(refused).lower()


def test_merge_gate_refuses_valid_signature_for_a_different_merge_base(
    gate_repo: GateRepo,
) -> None:
    store = GateEvidenceStore(gate_repo.evidence_root)
    foreign = store.sign(
        _receipt(
            gate_repo,
            merge_base_sha=gate_repo.candidate_sha,
        )
    )
    _write_payload(_receipt_path(gate_repo), foreign)

    refused = _run_gate(gate_repo)

    assert refused.returncode != 0, _output(refused)
    assert "different merge-base" in _output(refused).lower()


def test_merge_gate_refuses_absent_receipt(gate_repo: GateRepo) -> None:
    refused = _run_gate(gate_repo)
    out = _output(refused)

    assert refused.returncode == 3, out
    assert "no signed receipt" in out.lower()
    # The remedy must name the canonical wrapper (review r1: the raw
    # gate_evidence mint-candidate CLI defaults --command to a narrow
    # self-test, which must never be the advertised path to a receipt).
    assert "scripts/mint-merge-candidate.py" in out
    assert "--candidate-sha" in out
    assert "trial-merge" not in out

    run_receipts = list(
        (gate_repo.evidence_root / "records" / MERGE_ROUTINE_ID).glob(
            f"{gate_repo.candidate_sha}.run-*.json"
        )
    )
    assert len(run_receipts) == 1
    run_receipt = json.loads(run_receipts[0].read_text(encoding="utf-8"))
    assert run_receipt["exit_code"] == 3
    assert run_receipt["refusal_reason"].startswith("signed-receipt-missing:")


def test_verify_candidate_receipt_creates_no_trust_state_when_key_absent(
    gate_repo: GateRepo,
    tmp_path: Path,
) -> None:
    """Verification must use only an existing key; it must not mint trust state.

    Failing-on-revert: ``create_key=True`` in verify_candidate_receipt creates
    ``signing.key`` under the empty evidence root and this assertion fails.
    """
    signer = GateEvidenceStore(gate_repo.evidence_root)
    signed = signer.sign(_receipt(gate_repo))
    receipt = tmp_path / "standalone-receipt.json"
    _write_payload(receipt, signed)

    empty_root = tmp_path / "absent-trust"
    assert not empty_root.exists()

    with pytest.raises(GateEvidenceError) as raised:
        verify_candidate_receipt(
            receipt,
            evidence_root=empty_root,
            candidate_sha=gate_repo.candidate_sha,
            merge_base_sha=gate_repo.merge_base_sha,
        )

    # Binding assertion first: verification must not mint trust material under
    # the empty evidence root. create_key=True creates signing.key even when the
    # subsequent signature check fails against the newly minted key.
    assert not (empty_root / "signing.key").exists()
    assert not empty_root.exists() or not any(empty_root.iterdir())
    assert isinstance(raised.value, GateExecutionInfraError)
    assert "signing key" in str(raised.value).lower()


def test_merge_gate_refuses_moved_tip_and_keeps_post_checks_on_verified_sha(
    gate_repo: GateRepo,
) -> None:
    """Retarget after receipt auth must refuse; post-checks stay on pinned SHA.

    After ``$CANDIDATE_SHA`` is frozen and the receipt is verified, the gate
    (test-only) retargets ``$BRANCH`` to a multi-poison tip. Each post-auth
    consumer fails if it re-reads ``$BRANCH`` instead of ``$CANDIDATE_SHA``:

    - secrets: ``configs/accounts.yaml``
    - symlinks: a mode-120000 tree entry
    - migrations: in-place edit of an existing migration
    - M8 paths: ``ARCHI.md``, ``.venv/counterfeit.txt``, and root ``WORKBOOK.md``
    - trial merge: content that conflicts against current main

    Correct behavior: post-checks stay green on the verified tip, then
    ``candidate-tip-stable`` refuses because the branch name moved. Approving
    the unverified tip (or printing PASS for it) is the defect under test.

    Failing-on-revert (production only; this test file untouched):

    - drop tip recheck → gate PASS on poison tip
    - ``LEAKED=...HEAD...$BRANCH`` → secrets FAIL
    - ``SYMS=...$BRANCH`` → no-new-symlinks FAIL
    - ``MIG_EDIT=...HEAD...$BRANCH`` → migrations-append-only FAIL
    - trial ``merge ... $BRANCH`` → merge-clean FAIL (conflict)
    """
    path = gate_repo.path

    # Migration that exists on main so a poison tip can *modify* it (status M).
    _git(path, "checkout", "main")
    mig_dir = path / "omniagentos" / "db" / "migrations"
    mig_dir.mkdir(parents=True, exist_ok=True)
    mig_file = mig_dir / "001_init.sql"
    mig_file.write_text("-- append-only baseline\n", encoding="utf-8")
    _git(path, "add", "omniagentos/db/migrations/001_init.sql")
    _git(path, "commit", "-m", "add baseline migration")

    # Bring migration into the candidate so verified tip is still merge-clean.
    _git(path, "checkout", gate_repo.branch)
    _git(path, "merge", "main", "-m", "sync migration from main")
    verified_tip = _git(path, "rev-parse", "HEAD")

    # Build multi-poison tip from the verified candidate.
    secrets_path = Path("configs/accounts.yaml")
    (path / secrets_path).parent.mkdir(parents=True, exist_ok=True)
    (path / secrets_path).write_text("leaked: true\n", encoding="utf-8")
    _git(path, "add", str(secrets_path))

    (path / "link-target.txt").write_text("target\n", encoding="utf-8")
    (path / "poison-link").symlink_to("link-target.txt")
    _git(path, "add", "link-target.txt", "poison-link")

    mig_file.write_text("-- mutated in place (not append-only)\n", encoding="utf-8")
    _git(path, "add", "omniagentos/db/migrations/001_init.sql")

    (path / "ARCHI.md").write_text("poison oracle\n", encoding="utf-8")
    (path / "WORKBOOK.md").write_text("poison shared workbook\n", encoding="utf-8")
    (path / ".venv" / "counterfeit.txt").write_text("poison environment\n", encoding="utf-8")
    _git(path, "add", "ARCHI.md", "WORKBOOK.md")
    _git(path, "add", "-f", ".venv/counterfeit.txt")

    (path / "base.txt").write_text("poison-side\n", encoding="utf-8")
    _git(path, "add", "base.txt")
    _git(path, "commit", "-m", "poison tip")
    poison_tip = _git(path, "rev-parse", "HEAD")

    # Main-side edit of base.txt so merging the poison tip conflicts.
    _git(path, "checkout", "main")
    (path / "base.txt").write_text("main-side\n", encoding="utf-8")
    _git(path, "add", "base.txt")
    _git(path, "commit", "-m", "main advances base.txt")

    # Gate starts on the verified tip; fake python retargets mid-verification.
    _git(path, "update-ref", f"refs/heads/{gate_repo.branch}", verified_tip)
    assert poison_tip != verified_tip

    merge_base_sha = _git(path, "merge-base", "HEAD", verified_tip)
    store = GateEvidenceStore(gate_repo.evidence_root)
    store.record(
        _receipt(
            gate_repo,
            candidate_sha=verified_tip,
            merge_base_sha=merge_base_sha,
        )
    )

    refused = _run_gate(
        replace(gate_repo, candidate_sha=verified_tip, merge_base_sha=merge_base_sha),
        env_extra={
            "MERGE_GATE_TEST_RETARGET_REF": gate_repo.branch,
            "MERGE_GATE_TEST_RETARGET_SHA": poison_tip,
        },
    )
    out = _output(refused)

    # Pin assertions FIRST so an isolated $BRANCH revert fails on the consumer it
    # corrupts (not only on tip-stable). Each poison is independent:
    # secrets / symlink / migration-edit / merge-conflict on the unverified tip.
    assert re.search(r"secrets\s+ok", out), out
    assert re.search(r"no-new-symlinks\s+ok", out), out
    assert re.search(r"migrations-append-only\s+ok", out), out
    assert re.search(r"oracle-path\s+ok", out), out
    assert re.search(r"tracked-env\s+ok", out), out
    assert re.search(r"root-workbook\s+ok", out), out
    assert re.search(r"reachability\s+ok", out), out
    assert re.search(r"merge-clean\s+ok", out), out
    # Branch moved after auth — must refuse, never PASS the unverified tip.
    assert refused.returncode != 0, out
    assert "MERGE GATE: PASS" not in out
    assert re.search(r"candidate-tip-stable\s+FAIL", out), out
    assert "moved after verification" in out.lower()
    assert _git(path, "rev-parse", gate_repo.branch) == poison_tip


def test_merge_gate_refuses_legacy_markdown_verdict_without_receipt(
    gate_repo: GateRepo,
) -> None:
    """An approving hand-written markdown verdict is not a signed receipt.

    Failing-on-revert: restoring a ``var/swarm/verdicts/<branch>.md`` APPROVE
    fallback in merge-gate.sh makes this gate pass without a receipt.
    """
    verdict = gate_repo.path / "var" / "swarm" / "verdicts" / f"{gate_repo.branch}.md"
    verdict.parent.mkdir(parents=True, exist_ok=True)
    verdict.write_text(
        "VERDICT: APPROVE\n"
        "model: claude-opus-4-6 (anthropic)\n"
        "notes: hand-written, never produced by the verifier\n",
        encoding="utf-8",
    )

    refused = _run_gate(gate_repo)

    assert refused.returncode == 3, _output(refused)
    assert "no signed receipt" in _output(refused).lower()
    assert "MERGE GATE: PASS" not in _output(refused)


def test_verify_candidate_receipt_refuses_future_dated_hmac_receipt(
    gate_repo: GateRepo,
) -> None:
    """A validly HMAC-signed receipt dated in 2099 must not verify.

    Failing-on-revert: dropping the age/future branch in
    candidate_receipt_rejections lets this signed future receipt through.
    """
    store = GateEvidenceStore(gate_repo.evidence_root)
    future = store.sign(
        _receipt(
            gate_repo,
            started_at="2099-01-01T09:00:00Z",
            finished_at="2099-01-01T09:01:00Z",
        )
    )
    path = _receipt_path(gate_repo)
    _write_payload(path, future)

    with pytest.raises(GateEvidenceRefusal, match="future") as raised:
        verify_candidate_receipt(
            path,
            evidence_root=gate_repo.evidence_root,
            candidate_sha=gate_repo.candidate_sha,
            merge_base_sha=gate_repo.merge_base_sha,
        )

    assert "future" in str(raised.value).lower()
